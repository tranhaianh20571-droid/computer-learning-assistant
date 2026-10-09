"""Owner-scoped, version-consistent course and route queries (ADR-026)."""

from __future__ import annotations

from .library import LibrarySnapshot, normalize_text, read_snapshot_session
from .errors import error
from .models import LearningNavigationStateRow, utcnow

PREREQ_NOT_MASTERY_HINT = "prereq_arranged_not_mastered"


class OverviewService:
    def __init__(self, db):
        self.db = db

    def route_overview(self, route_id, *, actor_id, version_id=None):
        with self.db.session() as sess:
            read_snapshot_session(sess)
            return LibrarySnapshot(sess, actor_id).route(route_id, version_id)

    def course_overview(self, course_id, *, actor_id, route_id=None, route_version_id=None):
        with self.db.session() as sess:
            read_snapshot_session(sess)
            library = LibrarySnapshot(sess, actor_id)
            contexts = library.contexts(course_id)
            context = None
            outline_id = None
            if route_id or route_version_id:
                context = next((c for c in contexts if c["route_id"] == route_id and
                    (not route_version_id or c["route_version_id"] == route_version_id)), None)
                if context is None:
                    raise error("course_not_found", status=404)
                node = next(n for n in library.nodes[context["route_version_id"]] if n.course_id == course_id)
                outline, _ = library.outline_for_node(node)
                outline_id = outline.outline_version_id if outline else None
            result = library.course(course_id, outline_id)
            result.update(context=context, contexts=contexts)
            if outline_id and outline_id != library.courses[course_id].current_outline_version_id:
                result["teaching_unlocked"] = False
                result["historical_outline"] = True
            return result

    def list_library(self, *, actor_id):
        with self.db.session() as sess:
            read_snapshot_session(sess)
            return LibrarySnapshot(sess, actor_id).library()

    def list_course_summaries(self, *, actor_id):
        with self.db.session() as sess:
            read_snapshot_session(sess)
            library = LibrarySnapshot(sess, actor_id)
            return [library.course(cid) for cid in library.courses]

    def course_progress(self, sess, course_id):
        from .models import CourseRow
        course = sess.get(CourseRow, course_id)
        if course is None or course.deleted_at is not None:
            return {"lesson_total": 0, "lesson_started": 0, "lesson_completed": 0, "outline_confirmed": False}
        summary = LibrarySnapshot(sess, course.owner_user_id).course(course_id)
        return summary["progress"] | {"outline_confirmed": summary["outline_confirmed"]}

    def input_matches(self, value, *, actor_id):
        with self.db.session() as sess:
            read_snapshot_session(sess)
            library = LibrarySnapshot(sess, actor_id)
            routes = [{"kind": "route", "id": r.route_id, "title": r.title_ref}
                    for r in library.routes.values()
                    if r.current_route_version_id and normalize_text(r.input_ref or r.title_ref) == normalize_text(value)]
            courses = [{"kind": "course", "id": c.course_id, "title": c.title_ref}
                       for c in library.courses.values() if c.current_outline_version_id
                       and not library.contexts(c.course_id) and normalize_text(c.title_ref) == normalize_text(value)]
            return routes + courses

    def update_navigation(self, *, actor_id, route_id=None, course_id=None, unit_id=None, lesson_id=None):
        with self.db.session() as sess:
            if sess.bind.dialect.name == "postgresql":
                from sqlalchemy import text
                sess.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:owner, 0))"),
                             {"owner": "learning-navigation:" + actor_id})
            library = LibrarySnapshot(sess, actor_id)
            if route_id and route_id not in library.routes:
                raise error("route_not_found", status=404)
            if course_id and course_id not in library.courses:
                raise error("course_not_found", status=404)
            row = sess.get(LearningNavigationStateRow, actor_id)
            if row is None:
                row = LearningNavigationStateRow(owner_user_id=actor_id)
                sess.add(row)
            if route_id is not None:
                row.last_route_id = route_id
            if course_id is not None:
                row.last_course_id = course_id
            if unit_id is not None:
                row.last_unit_id = unit_id
            if lesson_id is not None:
                row.last_lesson_id = lesson_id
            row.updated_at = utcnow()
            sess.flush()
            return self._navigation(row)

    @staticmethod
    def _navigation(row):
        return {"last_route_id": row.last_route_id if row else None,
                "last_course_id": row.last_course_id if row else None,
                "last_unit_id": row.last_unit_id if row else None,
                "last_lesson_id": row.last_lesson_id if row else None}

    def get_navigation(self, *, actor_id):
        with self.db.session() as sess:
            return self._navigation(sess.get(LearningNavigationStateRow, actor_id))


__all__ = ["OverviewService", "PREREQ_NOT_MASTERY_HINT"]
