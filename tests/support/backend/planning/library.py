"""Private, owner-scoped library snapshots and deterministic planning identity."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import defaultdict

from sqlalchemy import select, text

from .errors import error
from .models import (
    CourseRow, CourseUnitRow, LearningRouteRow, LessonContentVersionRow,
    LessonPrerequisiteEdgeRow, LessonProgressRow, LessonRow, OutlineVersionRow,
    RouteCourseNodeRow, RouteDetachedCourseRow, RoutePrerequisiteEdgeRow,
    RouteStageRow, RouteVersionRow, load_json_field,
)


def normalize_text(value):
    cleaned = "".join(c for c in str(value or "") if c >= " " or c.isspace())
    return " ".join(unicodedata.normalize("NFC", cleaned).split())


def canonical_content(kind, content, title, scenario):
    """An explicit allowlist: layout and incidental model metadata never affect identity."""
    def edges(items):
        return sorted({(int(e["from_index"]), int(e["to_index"])) for e in items or []})

    def outline(value):
        return {"units": [{"title": normalize_text(u.get("title")), "lessons": [
            {"title": normalize_text(l.get("title")),
             "goal": normalize_text(l.get("goal") or l.get("title"))}
            for l in u.get("lessons", [])]} for u in value.get("units", [])],
            "prerequisites": edges(value.get("prerequisites"))}

    result = {"identity_version": 1, "kind": kind, "scenario": scenario,
              "title": normalize_text(content.get("title") or title),
              "goal": normalize_text(content.get("goal")), "scope": normalize_text(content.get("scope"))}
    if kind == "route":
        result["stages"] = [{"title": normalize_text(s.get("title")), "courses": [
            {"title": normalize_text(c.get("title")), "goal": normalize_text(c.get("goal")),
             "scope": normalize_text(c.get("scope")), **outline(c)} for c in s.get("courses", [])
        ]} for s in content.get("stages", [])]
        result["prerequisites"] = edges(content.get("prerequisites"))
    else:
        result.update(outline(content))
    # Basis is private business context, never loggable telemetry.
    result["basis"] = sorted(set(content.get("basis", [])))
    return result


def encoded_identity(content):
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(content):
    return "v1:" + hashlib.sha256(encoded_identity(content).encode("utf-8")).hexdigest()


def read_snapshot_session(sess):
    if sess.bind.dialect.name == "postgresql":
        sess.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))


class LibrarySnapshot:
    """Bounded bulk reads inside one DB snapshot; no per-card overview calls."""

    def __init__(self, sess, owner):
        self.owner = owner
        def rows(model, clause):
            return sess.execute(select(model).where(clause)).scalars().all()
        self.routes = {r.route_id: r for r in rows(LearningRouteRow,
            (LearningRouteRow.owner_user_id == owner) & LearningRouteRow.deleted_at.is_(None))}
        self.courses = {r.course_id: r for r in rows(CourseRow,
            (CourseRow.owner_user_id == owner) & CourseRow.deleted_at.is_(None))}
        self.versions = {v.route_version_id: v for v in rows(RouteVersionRow,
            RouteVersionRow.route_id.in_(self.routes))}
        self.outlines = {v.outline_version_id: v for v in rows(OutlineVersionRow,
            OutlineVersionRow.course_id.in_(self.courses))}
        self.stages = defaultdict(list)
        for row in rows(RouteStageRow, RouteStageRow.route_version_id.in_(self.versions)):
            self.stages[row.route_version_id].append(row)
        self.nodes = defaultdict(list)
        for row in rows(RouteCourseNodeRow, RouteCourseNodeRow.route_version_id.in_(self.versions)):
            if row.course_id in self.courses:
                self.nodes[row.route_version_id].append(row)
        self.detached = {(r.route_id, r.course_id) for r in rows(RouteDetachedCourseRow,
            RouteDetachedCourseRow.route_id.in_(self.routes))}
        from .cover_policy import grouped_owners
        self.cover_owners = grouped_owners(self.routes, self.versions, self.nodes, self.detached)
        self.units = defaultdict(list)
        for row in rows(CourseUnitRow, CourseUnitRow.outline_version_id.in_(self.outlines)):
            self.units[row.outline_version_id].append(row)
        self.lessons = defaultdict(list)
        for row in rows(LessonRow, LessonRow.outline_version_id.in_(self.outlines)):
            self.lessons[row.outline_version_id].append(row)
        lesson_ids = [l.lesson_id for ls in self.lessons.values() for l in ls]
        self.content_versions = {}
        for row in rows(LessonContentVersionRow, LessonContentVersionRow.lesson_id.in_(lesson_ids)):
            prior = self.content_versions.get(row.lesson_id)
            if prior is None or prior.content_version_no < row.content_version_no:
                self.content_versions[row.lesson_id] = row
        self.progress = {r.lesson_id: r for r in rows(LessonProgressRow,
            (LessonProgressRow.owner_user_id == owner) & LessonProgressRow.lesson_id.in_(lesson_ids))}
        self.lesson_edges = defaultdict(list)
        for row in rows(LessonPrerequisiteEdgeRow, LessonPrerequisiteEdgeRow.outline_version_id.in_(self.outlines)):
            self.lesson_edges[row.outline_version_id].append(row)
        self.route_edges = defaultdict(list)
        for row in rows(RoutePrerequisiteEdgeRow, RoutePrerequisiteEdgeRow.route_version_id.in_(self.versions)):
            self.route_edges[row.route_version_id].append(row)

    def outline_for_node(self, node):
        course = self.courses[node.course_id]
        exact = self.outlines.get(node.outline_version_id)
        if exact and exact.course_id == course.course_id:
            return exact, False
        sources = [v for v in self.outlines.values() if v.course_id == course.course_id
                   and v.source_route_version_id == node.route_version_id]
        if sources:
            return min(sources, key=lambda v: v.version_no), False
        current = self.outlines.get(course.current_outline_version_id)
        return current, bool(current)

    def ordered_lessons(self, outline_id):
        units = sorted(self.units[outline_id], key=lambda u: u.order_index)
        by_unit = defaultdict(list)
        for lesson in self.lessons[outline_id]:
            by_unit[lesson.unit_id].append(lesson)
        return units, by_unit

    def learning_state(self, lesson):
        row = self.progress.get(lesson.lesson_id)
        if row is None:
            return "not_started"
        content = self.content_versions.get(lesson.lesson_id)
        if row.learning_state == "user_completed" and (
            content is None or row.lesson_content_version_id != content.content_version_id
        ):
            return "in_progress"
        return row.learning_state

    def course(self, course_id, outline_id=None):
        course = self.courses.get(course_id)
        if course is None:
            raise error("course_not_found", status=404)
        version = self.outlines.get(outline_id or course.current_outline_version_id)
        if version and version.course_id != course_id:
            raise error("course_not_found", status=404)
        units, by_unit = self.ordered_lessons(version.outline_version_id if version else None)
        lessons = [l for u in units for l in sorted(by_unit[u.unit_id], key=lambda l: l.order_index)]
        states = {l.lesson_id: self.learning_state(l) for l in lessons}
        total = len(lessons)
        started = sum(states[l.lesson_id] != "not_started" for l in lessons)
        completed = sum(states[l.lesson_id] == "user_completed" for l in lessons)
        nxt = next((l for l in lessons if states[l.lesson_id] != "user_completed"), None)
        prereqs = {e.to_lesson_id for e in self.lesson_edges[version.outline_version_id if version else None]}
        from .cover_policy import projection
        return {**projection("course", course_id, self.cover_owners.get(course_id)),
            "kind": "course", "id": course_id, "course_id": course_id,
            "title": (version.title_snapshot if version else "") or course.title_ref,
            "scenario": course.scenario, "current_version_id": version.outline_version_id if version else None,
            "version_no": version.version_no if version else None,
            "outline_confirmed": bool(version), "teaching_unlocked": bool(version),
            "source_route_version_id": course.source_route_version_id,
            "source_route_node_id": course.source_route_node_id,
            "unit_count": len(units), "lesson_total": total, "lesson_started": started,
            "lesson_completed": completed, "completed": bool(version and total and completed == total),
            "state": "completed" if version and total and completed == total else
                "in_progress" if started else "not_started" if version else "preparing",
            "next_lesson_id": nxt.lesson_id if nxt else None,
            "next_lesson_title": (nxt.title_ref or nxt.goal_ref) if nxt else None,
            "progress": {"lesson_total": total, "lesson_started": started, "lesson_completed": completed},
            "units": [{"unit_id": u.unit_id, "order_index": u.order_index, "title": u.title_ref,
                "lesson_count": len(by_unit[u.unit_id]), "lessons": [{
                    "lesson_id": l.lesson_id, "order_index": l.order_index,
                    "title": l.title_ref or l.goal_ref, "goal": l.goal_ref,
                    "status_preparation": l.status_preparation, "learning_state": states[l.lesson_id],
                    "prereq_arranged": l.lesson_id in prereqs,
                    "prereq_hint": "prereq_arranged_not_mastered" if l.lesson_id in prereqs else None,
                    "startable": bool(version)} for l in sorted(by_unit[u.unit_id], key=lambda l: l.order_index)]}
                for u in units]}

    def route(self, route_id, version_id=None, include_history=True):
        route = self.routes.get(route_id)
        if route is None:
            raise error("route_not_found", status=404)
        version = self.versions.get(version_id or route.current_route_version_id)
        if version and version.route_id != route_id:
            raise error("route_not_found", status=404)
        if version_id and version is None:
            raise error("route_not_found", status=404)
        is_current = bool(version and version.route_version_id == route.current_route_version_id)
        stages = []
        for stage in sorted(self.stages[version.route_version_id if version else None], key=lambda s: s.order_index):
            courses = []
            for node in sorted(self.nodes[stage.route_version_id], key=lambda n: n.order_index):
                if node.stage_id != stage.stage_id or (is_current and (route_id, node.course_id) in self.detached):
                    continue
                outline, fallback = self.outline_for_node(node)
                item = self.course(node.course_id, outline.outline_version_id if outline else None)
                item.update(node_id=node.node_id, order_index=node.order_index,
                            goal_snapshot=node.goal_snapshot_ref, legacy_outline_fallback=fallback,
                            route_id=route_id, route_version_id=version.route_version_id)
                courses.append(item)
            stages.append({"stage_id": stage.stage_id, "order_index": stage.order_index,
                "title": stage.title_ref, "course_count": len(courses), "courses": courses})
        members = {c["course_id"]: c for s in stages for c in s["courses"]}
        history = []
        if include_history:
            for v in sorted(self.versions.values(), key=lambda v: v.version_no, reverse=True):
                if v.route_id != route_id or (version and v.route_version_id == version.route_version_id):
                    continue
                old = self.route(route_id, v.route_version_id, False)
                old_courses = [c for s in old["stages"] for c in s["courses"] if c["course_id"] not in members]
                if old_courses:
                    history.append({"version_id": v.route_version_id, "version_no": v.version_no,
                                    "title": old["title"], "courses": old_courses})
        done = sum(c["completed"] for c in members.values())
        from .cover_policy import projection
        return {**projection("route", route_id), "kind": "route", "id": route_id, "route_id": route_id,
            "title": (version.title_snapshot if version else "") or route.title_ref,
            "scenario": route.scenario, "current_version_id": version.route_version_id if version else None,
            "version_no": version.version_no if version else None, "has_version": bool(version),
            "is_current": is_current, "stages": stages, "history": history,
            "course_count": len(members), "course_completed": done,
            "completed": bool(members) and done == len(members),
            "state": "completed" if members and done == len(members) else
                "in_progress" if any(c["lesson_started"] for c in members.values()) else "not_started",
            "search_titles": [c["title"] for c in members.values()],
            "history_titles": [c["title"] for h in history for c in h["courses"]]}

    def contexts(self, course_id):
        result = []
        for v in self.versions.values():
            route = self.routes[v.route_id]
            if any(n.course_id == course_id for n in self.nodes[v.route_version_id]):
                current = v.route_version_id == route.current_route_version_id
                if current and (v.route_id, course_id) in self.detached:
                    continue
                result.append({"route_id": route.route_id, "route_version_id": v.route_version_id,
                    "title": v.title_snapshot or route.title_ref, "version_no": v.version_no,
                    "is_current": current})
        return sorted(result, key=lambda c: (not c["is_current"], -c["version_no"]))

    def library(self):
        grouped = set(self.cover_owners)
        items = []
        for route in self.routes.values():
            if route.current_route_version_id not in self.versions:
                continue
            item = self.route(route.route_id)
            if item["course_count"] or item["history"]:
                items.append(item)
        for c in self.courses.values():
            if c.course_id not in grouped:
                items.append(self.course(c.course_id))
        return sorted(items, key=lambda i: i["title"])

    def outline_content(self, outline_id):
        version = self.outlines.get(outline_id)
        units, by_unit = self.ordered_lessons(outline_id)
        lessons = [l for u in units for l in sorted(by_unit[u.unit_id], key=lambda l: l.order_index)]
        indices = {l.lesson_id: i for i, l in enumerate(lessons)}
        content = {"units": [{"title": u.title_ref, "lessons": [
            {"title": l.title_ref or l.goal_ref, "goal": l.goal_ref}
            for l in sorted(by_unit[u.unit_id], key=lambda l: l.order_index)]} for u in units],
            "prerequisites": [{"from_index": indices[e.from_lesson_id], "to_index": indices[e.to_lesson_id]}
                for e in self.lesson_edges[outline_id] if e.from_lesson_id in indices and e.to_lesson_id in indices]}
        if version:
            snapshot = load_json_field(version.content_snapshot_json, {})
            content.update({k: snapshot[k] for k in ("goal", "scope", "basis") if k in snapshot})
        return content

    def identity(self, kind, object_id):
        if kind != "route":
            c = self.courses[object_id]
            v = self.outlines.get(c.current_outline_version_id)
            if not v:
                return None
            return canonical_content(kind, self.outline_content(v.outline_version_id), v.title_snapshot or c.title_ref, c.scenario)
        route = self.routes[object_id]
        version = self.versions.get(route.current_route_version_id)
        if not version:
            return None
        stages, indices = [], {}
        for stage in sorted(self.stages[version.route_version_id], key=lambda s: s.order_index):
            courses = []
            for node in sorted(self.nodes[version.route_version_id], key=lambda n: n.order_index):
                if node.stage_id != stage.stage_id or (object_id, node.course_id) in self.detached:
                    continue
                indices[node.node_id] = len(indices)
                c = self.courses[node.course_id]
                outline, _ = self.outline_for_node(node)
                courses.append({"title": c.title_ref, "goal": node.goal_snapshot_ref,
                    "scope": node.scope_snapshot_ref, **self.outline_content(outline.outline_version_id if outline else None)})
            stages.append({"title": stage.title_ref, "courses": courses})
        content = {"stages": stages, "prerequisites": [
            {"from_index": indices[e.from_node_id], "to_index": indices[e.to_node_id]}
            for e in self.route_edges[version.route_version_id] if e.from_node_id in indices and e.to_node_id in indices]}
        snapshot = load_json_field(version.content_snapshot_json, {})
        content.update({k: snapshot[k] for k in ("goal", "scope", "basis") if k in snapshot})
        return canonical_content(kind, content, version.title_snapshot or route.title_ref, route.scenario)

    def matches(self, kind, identity, exclude_id=None):
        results = []
        candidates = self.routes if kind == "route" else self.courses
        for object_id in candidates:
            if object_id == exclude_id:
                continue
            prior = self.identity(kind, object_id)
            if prior is not None and fingerprint(prior) == fingerprint(identity) and encoded_identity(prior) == encoded_identity(identity):
                item = self.route(object_id, include_history=False) if kind == "route" else self.course(object_id)
                results.append({k: item[k] for k in ("kind", "id", "title", "state", "completed")}
                    | {"course_count": item.get("course_count", 1)})
        return results
