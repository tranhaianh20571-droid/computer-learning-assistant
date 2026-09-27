"""草稿编辑与确认应用（T05）。

契约（交接文档 §3.1 / 架构 §3.1）：
- 编辑已确认内容先建草稿，不原地更新已确认版本；
- 草稿乐观锁：revision 匹配才更新（compare-and-swap），冲突给 4xx 不重写；
- 确认事务（单事务）：所有权、草稿 revision、字段、层级完整性、先修 DAG
  全部校验 → 创建不可变版本 + 更新当前指针 + 审计；任一失败整体不生效；
- 确认接口「对象 + 草稿 revision + 幂等键」防重：重复幂等键返回既有版本号；
- 由路线展开大纲：继承来源路线版本、课程节点、目标/先修快照与生成依据；
- 同一课程已有大纲时，重选路线节点不进生成流程（由服务层判断）。
"""

from __future__ import annotations

import json
import uuid
from typing import Callable, Optional

from sqlalchemy import select

from ..logging_audit.db import Database
from ..logging_audit.repositories import PgAuditLog
from .errors import error
from .models import (
    CourseRow,
    CourseUnitRow,
    DRAFT_SAVED_STATES,
    LearningRouteRow,
    LessonPrerequisiteEdgeRow,
    LessonRow,
    OutlineVersionRow,
    PlanningCandidateRow,
    PlanningDraftRow,
    RouteCourseNodeRow,
    RoutePrerequisiteEdgeRow,
    RouteStageRow,
    RouteVersionRow,
    load_json_field,
    utcnow,
)
from .outputs import OutlineCandidate, RouteCandidate, validate_content_json


class DraftService:
    def __init__(self, db: Database, audit: Optional[PgAuditLog] = None) -> None:
        self.db = db
        self.audit = audit

    # ---- 审计 ----
    def _audit(self, event: str, *, actor_id: str, object_id: str, result: str, reason: str = "") -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(
                event, actor_id=actor_id, object_type="planning_draft",
                object_id=object_id, result=result, reason=reason or None,
            )
        except Exception:  # noqa: BLE001 - 审计失败不吞业务结果，但必须可见
            raise

    # ---- 查询 ----
    def get_draft(self, draft_id: str, *, actor_id: str) -> dict:
        with self.db.session() as sess:
            row = sess.get(PlanningDraftRow, draft_id)
            if row is None or row.owner_user_id != actor_id:
                raise error("draft_not_found", status=404)
            return self._view(row)

    def _view(self, row: PlanningDraftRow) -> dict:
        return {
            "draft_id": row.draft_id,
            "draft_kind": row.draft_kind,
            "target_id": row.target_id,
            "base_version_id": row.base_version_id,
            "revision": row.revision,
            "content": load_json_field(row.content_json, {}),
            "saved_state": row.saved_state,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def find_or_create_draft(
        self,
        *,
        owner_user_id: str,
        draft_kind: str,
        target_id: str,
        base_version_id: Optional[str],
        initial_content: Optional[dict] = None,
    ) -> dict:
        """同一目标复用未确认草稿；范围变化时由调用方建新草稿承接。"""
        if draft_kind not in ("route", "course_outline"):
            raise error("invalid_field", f"unknown draft_kind {draft_kind}")
        with self.db.session() as sess:
            row = (
                sess.execute(
                    select(PlanningDraftRow).where(
                        PlanningDraftRow.owner_user_id == owner_user_id,
                        PlanningDraftRow.draft_kind == draft_kind,
                        PlanningDraftRow.target_id == target_id,
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                row = PlanningDraftRow(
                    draft_id=f"draft_{uuid.uuid4().hex[:12]}",
                    owner_user_id=owner_user_id,
                    draft_kind=draft_kind,
                    target_id=target_id,
                    base_version_id=base_version_id,
                    revision=1,
                    content_json=json.dumps(initial_content or {}, ensure_ascii=False),
                    saved_state="saved",
                )
                sess.add(row)
                sess.flush()
                view = self._view(row)
                sess.commit()
                return view
            return self._view(row)

    # ---- 乐观锁保存 ----
    def save_draft(
        self,
        draft_id: str,
        *,
        actor_id: str,
        revision: int,
        content: dict,
        saved_state: str = "saved",
    ) -> dict:
        """compare-and-swap：revision 匹配才写入；冲突抛 DRAFT_REVISION_MISMATCH。"""
        if saved_state not in DRAFT_SAVED_STATES:
            raise error("invalid_field", f"unknown saved_state {saved_state}")
        with self.db.session() as sess:
            row = sess.get(PlanningDraftRow, draft_id)
            if row is None or row.owner_user_id != actor_id:
                raise error("draft_not_found", status=404)
            if row.revision != revision:
                raise error("DRAFT_REVISION_MISMATCH", f"expected revision {row.revision}", status=409)
            # 内容形态校验（长度/层级/索引合法）；DAG 留给确认事务
            validate_content_json(row.draft_kind, content)
            row.content_json = json.dumps(content, ensure_ascii=False)
            row.revision += 1
            row.saved_state = saved_state
            row.updated_at = utcnow()
            sess.flush()
            view = self._view(row)
            sess.commit()
            return view

    def mark_save_failed(self, draft_id: str, *, actor_id: str) -> None:
        with self.db.session() as sess:
            row = sess.get(PlanningDraftRow, draft_id)
            if row is None or row.owner_user_id != actor_id:
                raise error("draft_not_found", status=404)
            row.saved_state = "failed"
            sess.commit()

    # ---- 候选 ----
    def apply_candidate(
        self,
        *,
        owner_user_id: str,
        target_id: str,
        candidate_kind: str,
        content: dict,
        source_task_id: str,
        base_draft_revision: int = 0,
    ) -> dict:
        """AI 候选落库；人工编辑（更高 revision）后旧候选不得替换新草稿。"""
        with self.db.session() as sess:
            draft = (
                sess.execute(
                    select(PlanningDraftRow).where(
                        PlanningDraftRow.owner_user_id == owner_user_id,
                        PlanningDraftRow.draft_kind
                        == ("route" if candidate_kind != "outline_generate" else "course_outline"),
                        PlanningDraftRow.target_id == target_id,
                    )
                )
                .scalars()
                .first()
            )
            if draft is not None and base_draft_revision < draft.revision:
                # 草稿已被人工推进：候选只记录，不覆盖草稿
                return {"applied": False, "reason": "draft_advanced", "draft_revision": draft.revision}
            if draft is None:
                draft = PlanningDraftRow(
                    draft_id=f"draft_{uuid.uuid4().hex[:12]}",
                    owner_user_id=owner_user_id,
                    draft_kind="route" if candidate_kind != "outline_generate" else "course_outline",
                    target_id=target_id,
                    base_version_id=None,
                    revision=1,
                    content_json=json.dumps(content, ensure_ascii=False),
                    saved_state="saved",
                )
                sess.add(draft)
                sess.flush()
            else:
                draft.content_json = json.dumps(content, ensure_ascii=False)
                draft.saved_state = "saved"
                draft.updated_at = utcnow()
            candidate = PlanningCandidateRow(
                candidate_id=f"cand_{uuid.uuid4().hex[:12]}",
                draft_id=draft.draft_id,
                target_id=target_id,
                candidate_kind=candidate_kind,
                base_draft_revision=draft.revision,
                generation_revision=1,
                content_json=json.dumps(content, ensure_ascii=False),
                source_task_id=source_task_id,
            )
            sess.add(candidate)
            sess.flush()
            result = {"applied": True, "draft_id": draft.draft_id, "draft_revision": draft.revision,
                      "candidate_id": candidate.candidate_id}
            sess.commit()
            return result

    # ---- 确认事务 ----
    def confirm_draft(
        self,
        draft_id: str,
        *,
        actor_id: str,
        revision: int,
        idempotency_key: str,
    ) -> dict:
        """确认草稿 → 不可变版本。单事务校验 + 建版本 + 指针 + 审计。"""
        with self.db.session() as sess:
            draft = sess.get(PlanningDraftRow, draft_id)
            if draft is None or draft.owner_user_id != actor_id:
                raise error("draft_not_found", status=404)

            # 幂等：同幂等键已确认过 → 返回既有版本，不产生重复有效版本
            existing = (
                sess.execute(
                    select(RouteVersionRow if draft.draft_kind == "route" else OutlineVersionRow).where(
                        (RouteVersionRow.source_task_id == f"confirm:{idempotency_key}")
                        if draft.draft_kind == "route"
                        else (OutlineVersionRow.source_task_id == f"confirm:{idempotency_key}")
                    )
                )
                .scalars()
                .first()
            )
            if existing is not None and draft.draft_kind == "route":
                return {"version_id": existing.route_version_id, "version_no": existing.version_no,
                        "deduped": True}
            if existing is not None:
                return {"version_id": existing.outline_version_id, "version_no": existing.version_no,
                        "deduped": True}

            if draft.revision != revision:
                raise error("DRAFT_REVISION_MISMATCH", f"expected revision {draft.revision}", status=409)

            content = load_json_field(draft.content_json, {})
            validate_content_json(draft.draft_kind, content)

            if draft.draft_kind == "route":
                result = self._confirm_route(sess, draft, actor_id, idempotency_key, content)
            else:
                result = self._confirm_outline(sess, draft, actor_id, idempotency_key, content)
            sess.commit()
            return result

    def _confirm_route(self, sess, draft: PlanningDraftRow, actor_id: str, idem: str, content: dict) -> dict:
        route = sess.get(LearningRouteRow, draft.target_id)
        if route is None or route.owner_user_id != actor_id:
            raise error("route_not_found", status=404)
        candidate = RouteCandidate.model_validate(content)

        max_no = (
            sess.execute(
                select(RouteVersionRow.version_no)
                .where(RouteVersionRow.route_id == route.route_id)
                .order_by(RouteVersionRow.version_no.desc())
                .limit(1)
            ).scalar_one_or_none()
            or 0
        )
        version = RouteVersionRow(
            route_version_id=f"rv_{uuid.uuid4().hex[:12]}",
            route_id=route.route_id,
            version_no=max_no + 1,
            source_task_id=f"confirm:{idem}",
            prompt_binding_id=None,
        )
        sess.add(version)
        sess.flush()

        candidate_node_by_index: dict[int, str] = {}
        course_rows: dict[int, str] = {}
        stage_index = 0
        course_order = 0
        for s_index, stage in enumerate(candidate.stages):
            stage_row = RouteStageRow(
                stage_id=f"rst_{uuid.uuid4().hex[:12]}",
                route_version_id=version.route_version_id,
                order_index=s_index,
                title_ref=stage.title,
            )
            sess.add(stage_row)
            sess.flush()
            for c_index, course in enumerate(stage.courses):
                course_id = course_rows.get(course_order) or f"crs_{uuid.uuid4().hex[:12]}"
                if course_order not in course_rows:
                    sess.add(
                        CourseRow(
                            course_id=course_id,
                            owner_user_id=actor_id,
                            scenario=route.scenario,
                            title_ref=course.title,
                            source_route_version_id=version.route_version_id,
                            source_route_node_id=None,
                        )
                    )
                    course_rows[course_order] = course_id
                node_row = RouteCourseNodeRow(
                    node_id=f"rcn_{uuid.uuid4().hex[:12]}",
                    route_version_id=version.route_version_id,
                    stage_id=stage_row.stage_id,
                    course_id=course_id,
                    order_index=c_index,
                    goal_snapshot_ref=course.goal,
                    scope_snapshot_ref="",
                    prereq_requirements_ref="",
                )
                sess.add(node_row)
                candidate_node_by_index[course_order] = node_row.node_id
                course_order += 1
            stage_index += 1

        for edge in candidate.prerequisites:
            flat_from = edge.from_index
            flat_to = edge.to_index
            if flat_from not in candidate_node_by_index or flat_to not in candidate_node_by_index:
                raise error("INVALID_PREREQUISITE", "prerequisite references unknown course")
            sess.add(
                RoutePrerequisiteEdgeRow(
                    edge_id=f"rpe_{uuid.uuid4().hex[:12]}",
                    route_version_id=version.route_version_id,
                    from_node_id=candidate_node_by_index[flat_from],
                    to_node_id=candidate_node_by_index[flat_to],
                )
            )

        route.current_route_version_id = version.route_version_id
        self._audit(
            "config.changed", actor_id=actor_id, object_id=version.route_version_id,
            result="route_confirmed",
        )
        return {"version_id": version.route_version_id, "version_no": version.version_no, "deduped": False}

    def _confirm_outline(self, sess, draft: PlanningDraftRow, actor_id: str, idem: str, content: dict) -> dict:
        course = sess.get(CourseRow, draft.target_id)
        if course is None or course.owner_user_id != actor_id:
            raise error("course_not_found", status=404)
        candidate = OutlineCandidate.model_validate(content)

        max_no = (
            sess.execute(
                select(OutlineVersionRow.version_no)
                .where(OutlineVersionRow.course_id == course.course_id)
                .order_by(OutlineVersionRow.version_no.desc())
                .limit(1)
            ).scalar_one_or_none()
            or 0
        )
        version = OutlineVersionRow(
            outline_version_id=f"ov_{uuid.uuid4().hex[:12]}",
            course_id=course.course_id,
            version_no=max_no + 1,
            source_task_id=f"confirm:{idem}",
            prompt_binding_id=None,
            source_route_version_id=course.source_route_version_id,
            source_route_node_id=course.source_route_node_id,
            goal_snapshot_ref="",
            scope_snapshot_ref="",
        )
        sess.add(version)
        sess.flush()

        lesson_ids_by_index: dict[int, str] = {}
        lesson_order = 0
        for u_index, unit in enumerate(candidate.units):
            unit_row = CourseUnitRow(
                unit_id=f"unit_{uuid.uuid4().hex[:12]}",
                outline_version_id=version.outline_version_id,
                order_index=u_index,
                title_ref=unit.title,
            )
            sess.add(unit_row)
            sess.flush()
            for l_index, lesson in enumerate(unit.lessons):
                lesson_row = LessonRow(
                    lesson_id=f"les_{uuid.uuid4().hex[:12]}",
                    outline_version_id=version.outline_version_id,
                    unit_id=unit_row.unit_id,
                    order_index=l_index,
                    goal_ref=lesson.goal,
                    status_preparation="not_generated",
                )
                sess.add(lesson_row)
                lesson_ids_by_index[lesson_order] = lesson_row.lesson_id
                lesson_order += 1

        for edge in candidate.prerequisites:
            if edge.from_index not in lesson_ids_by_index or edge.to_index not in lesson_ids_by_index:
                raise error("INVALID_PREREQUISITE", "prerequisite references unknown lesson")
            sess.add(
                LessonPrerequisiteEdgeRow(
                    edge_id=f"lpe_{uuid.uuid4().hex[:12]}",
                    outline_version_id=version.outline_version_id,
                    from_lesson_id=lesson_ids_by_index[edge.from_index],
                    to_lesson_id=lesson_ids_by_index[edge.to_index],
                )
            )

        course.current_outline_version_id = version.outline_version_id
        self._audit(
            "config.changed", actor_id=actor_id, object_id=version.outline_version_id,
            result="outline_confirmed",
        )
        return {"version_id": version.outline_version_id, "version_no": version.version_no, "deduped": False}

    # ---- 教学门槛（本切片只校验） ----
    def ensure_outline_confirmed(self, course_id: str, *, actor_id: str) -> None:
        with self.db.session() as sess:
            course = sess.get(CourseRow, course_id)
            if course is None or course.owner_user_id != actor_id:
                raise error("course_not_found", status=404)
            if not course.current_outline_version_id:
                raise error("LESSON_UNLOCK_REQUIRED", "outline not confirmed", status=409)

    # ---- 由路线展开大纲 ----
    def course_has_outline(self, course_id: str) -> bool:
        with self.db.session() as sess:
            course = sess.get(CourseRow, course_id)
            return bool(course and course.current_outline_version_id)


__all__ = ["DraftService"]
