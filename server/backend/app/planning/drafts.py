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
from datetime import timezone
from typing import Callable, Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session

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
    LessonContentVersionRow,
    LessonProgressRow,
    OutlineVersionRow,
    PlanningCandidateRow,
    PlanningDraftRow,
    PlanningConfirmationRow,
    PlanningTaskStateRow,
    RouteCourseNodeRow,
    RoutePrerequisiteEdgeRow,
    RouteStageRow,
    RouteVersionRow,
    load_json_field,
    utcnow,
)
from .outputs import OutlineCandidate, RouteCandidate, validate_content_json
from .library import LibrarySnapshot, canonical_content, encoded_identity, fingerprint
from .errors import PlanningDuplicate
from .locking import planning_target


class DraftService:
    def __init__(self, db: Database, audit: Optional[PgAuditLog] = None) -> None:
        self.db = db
        self.audit = audit

    def _freeze_sources(self, sess, draft, lesson_pairs=()):
        from ..materials.study_sources import freeze_sources
        state = sess.scalar(select(PlanningTaskStateRow).where(
            PlanningTaskStateRow.owner_user_id == draft.owner_user_id,
            PlanningTaskStateRow.draft_id == draft.draft_id).order_by(PlanningTaskStateRow.updated_at.desc()))
        ids = load_json_field(state.material_ids_json, []) if state else []
        if not ids:
            return "{}"
        return json.dumps(freeze_sources(sess, draft.owner_user_id, ids, lesson_pairs), ensure_ascii=False)

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
            _, locked = planning_target(sess, row.draft_kind, row.target_id, actor_id)
            return self._view(row) | {"locked": locked}

    def _view(self, row: PlanningDraftRow) -> dict:
        return {
            "draft_id": row.draft_id,
            "draft_kind": row.draft_kind,
            "target_id": row.target_id,
            "base_version_id": row.base_version_id,
            "revision": row.revision,
            "content": load_json_field(row.content_json, {}),
            "coverage": load_json_field(row.coverage_json, {}) if row.coverage_json else {},
            "review": load_json_field(row.review_json, {}) if row.review_json else {},
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
            planning_target(sess, draft_kind, target_id, owner_user_id, mutable=True)
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
            row = sess.get(PlanningDraftRow, draft_id, with_for_update=True)
            if row is None or row.owner_user_id != actor_id:
                raise error("draft_not_found", status=404)
            planning_target(sess, row.draft_kind, row.target_id, actor_id, mutable=True)
            if row.revision != revision:
                raise error("DRAFT_REVISION_MISMATCH", f"expected revision {row.revision}", status=409)
            # 内容形态校验（长度/层级/索引合法）；DAG 留给确认事务
            validate_content_json(row.draft_kind, content)
            row.content_json = json.dumps(content, ensure_ascii=False)
            # 人工修改后旧体检结论不再代表当前草稿。
            row.review_json = None
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
        lease=None,
        publication_guard: Optional[Callable[[Session], None]] = None,
        coverage: Optional[dict] = None,
        review: Optional[dict] = None,
    ) -> dict:
        """AI 候选落库；人工编辑（更高 revision）后旧候选不得替换新草稿。"""
        with self.db.session() as sess:
            target_kind = "route" if candidate_kind != "outline_generate" else "course_outline"
            planning_target(sess, target_kind, target_id, owner_user_id, mutable=True)
            if lease is not None or publication_guard is not None:
                from ..tasks.service import LearningTaskRow
                task = sess.get(LearningTaskRow, source_task_id, with_for_update=True)
                if not task or task.owner_user_id != owner_user_id or task.cancel_requested:
                    raise error("STALE_RESULT_DISCARDED", status=409)
                lease_until = task.lease_until
                if lease_until is not None and lease_until.tzinfo is None:
                    lease_until = lease_until.replace(tzinfo=timezone.utc)
                if lease is not None and (task.generation != lease.generation
                    or task.lease_token != lease.lease_token or not lease_until or lease_until <= utcnow()):
                    raise error("STALE_RESULT_DISCARDED", status=409)
            # Check against this transaction after locking the task and before any candidate/draft write.
            if publication_guard is not None:
                publication_guard(sess)
            draft = (
                sess.execute(
                    select(PlanningDraftRow).where(
                        PlanningDraftRow.owner_user_id == owner_user_id,
                        PlanningDraftRow.draft_kind
                        == ("route" if candidate_kind != "outline_generate" else "course_outline"),
                        PlanningDraftRow.target_id == target_id,
                    ).with_for_update()
                )
                .scalars()
                .first()
            )
            if draft is not None and base_draft_revision < draft.revision:
                candidate = PlanningCandidateRow(
                    candidate_id=f"cand_{uuid.uuid4().hex[:12]}", draft_id=draft.draft_id,
                    target_id=target_id, candidate_kind=candidate_kind,
                    base_draft_revision=base_draft_revision, generation_revision=1,
                    content_json=json.dumps(content, ensure_ascii=False), source_task_id=source_task_id,
                )
                sess.add(candidate)
                sess.commit()
                return {"applied": False, "reason": "draft_advanced", "draft_revision": draft.revision,
                        "candidate_id": candidate.candidate_id}
            if draft is None:
                draft = PlanningDraftRow(
                    draft_id=f"draft_{uuid.uuid4().hex[:12]}",
                    owner_user_id=owner_user_id,
                    draft_kind="route" if candidate_kind != "outline_generate" else "course_outline",
                    target_id=target_id,
                    base_version_id=None,
                    revision=1,
                    content_json=json.dumps(content, ensure_ascii=False),
                    coverage_json=json.dumps(coverage or {}, ensure_ascii=False) if coverage is not None else None,
                    review_json=json.dumps(review or {}, ensure_ascii=False) if review is not None else None,
                    saved_state="saved",
                )
                sess.add(draft)
                sess.flush()
            else:
                draft.content_json = json.dumps(content, ensure_ascii=False)
                if coverage is not None:
                    draft.coverage_json = json.dumps(coverage, ensure_ascii=False)
                if review is not None:
                    draft.review_json = json.dumps(review, ensure_ascii=False)
                draft.revision += 1
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
    def edit_confirmed(self, target_kind, target_id, *, actor_id):
        if target_kind not in ("route", "course"):
            raise error("invalid_field")
        with self.db.session() as sess:
            _, locked = planning_target(sess, target_kind, target_id, actor_id)
        if locked:
            raise error("PLANNING_LOCKED", "课程已确认，不能再调整规划。", status=409)
        raise error("state_invalid", "尚无已确认课程", status=409)

    def _identity(self, sess, draft, target):
        content = load_json_field(draft.content_json, {}).copy()
        # Only server-owned task state supplies material scope, never client-supplied basis.
        state = sess.execute(select(PlanningTaskStateRow).where(
            PlanningTaskStateRow.owner_user_id == draft.owner_user_id,
            PlanningTaskStateRow.draft_id == draft.draft_id
        ).order_by(PlanningTaskStateRow.updated_at.desc())).scalars().first()
        content["basis"] = load_json_field(state.material_ids_json, []) if state else []
        return canonical_content(draft.draft_kind, content, target.title_ref, target.scenario)

    def duplicate_check(self, draft_id, *, actor_id, revision):
        with self.db.session() as sess:
            draft = sess.get(PlanningDraftRow, draft_id)
            if draft is None or draft.owner_user_id != actor_id:
                raise error("draft_not_found", status=404)
            if draft.revision != revision:
                raise error("DRAFT_REVISION_MISMATCH", status=409)
            library = LibrarySnapshot(sess, actor_id)
            target = (library.routes if draft.draft_kind == "route" else library.courses).get(draft.target_id)
            if target is None:
                raise error("draft_not_found", status=404)
            content = load_json_field(draft.content_json, {})
            validate_content_json(draft.draft_kind, content)
            identity = self._identity(sess, draft, target)
            current = library.identity(draft.draft_kind, draft.target_id)
            return {"revision": revision, "fingerprint": fingerprint(identity),
                "same_target": current is not None and encoded_identity(current) == encoded_identity(identity),
                "matches": library.matches(draft.draft_kind, identity, draft.target_id)}

    def confirm_draft(
        self,
        draft_id: str,
        *,
        actor_id: str,
        revision: int,
        idempotency_key: str,
        allow_duplicate: bool = False,
        duplicate_fingerprint: str | None = None,
        acknowledge_issues: bool = False,
    ) -> dict:
        """确认草稿 → 不可变版本。单事务校验 + 建版本 + 指针 + 审计。"""
        with self.db.session() as sess:
            # Owner-scoped transaction lock: different draft/target rows cannot race dedupe.
            if sess.bind.dialect.name == "postgresql":
                sess.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:owner, 0))"),
                             {"owner": "planning-confirm:" + actor_id})
            draft = sess.get(PlanningDraftRow, draft_id, with_for_update=True)
            if draft is None or draft.owner_user_id != actor_id:
                raise error("draft_not_found", status=404)

            target_model = LearningRouteRow if draft.draft_kind == "route" else CourseRow
            target = sess.get(target_model, draft.target_id, with_for_update=True)
            if target is None or target.owner_user_id != actor_id or target.deleted_at is not None:
                raise error("draft_not_found", status=404)
            receipt = sess.execute(select(PlanningConfirmationRow).where(
                PlanningConfirmationRow.draft_id == draft_id,
                PlanningConfirmationRow.revision == revision,
                PlanningConfirmationRow.owner_user_id == actor_id)).scalar_one_or_none()
            if receipt:
                return load_json_field(receipt.result_json, {}) | {"deduped": True}

            # Legacy idempotency lookup must be scoped to this target, never another owner.
            version_model = RouteVersionRow if draft.draft_kind == "route" else OutlineVersionRow
            existing = (
                sess.execute(
                    select(version_model).where(
                        (RouteVersionRow.source_task_id == f"confirm:{idempotency_key}")
                        if draft.draft_kind == "route"
                        else (OutlineVersionRow.source_task_id == f"confirm:{idempotency_key}")
                    ).where(
                        RouteVersionRow.route_id == draft.target_id if draft.draft_kind == "route"
                        else OutlineVersionRow.course_id == draft.target_id
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

            review = load_json_field(draft.review_json, {}) if draft.review_json else {}
            if review.get("acknowledgement_required") and not acknowledge_issues:
                raise error(
                    "PLANNING_REVIEW_REQUIRED",
                    "规划体检存在关键缺口，需要用户确认后才能使用。",
                    status=409,
                )

            content = load_json_field(draft.content_json, {})
            validate_content_json(draft.draft_kind, content)
            if draft.draft_kind == "route" and content.get("structure_version") == 2:
                from .retry_policy import missing_courses
                if missing_courses(content):
                    raise error("INVALID_OUTLINE_STRUCTURE", status=400)
            identity = self._identity(sess, draft, target)
            identity_fingerprint = fingerprint(identity)
            if allow_duplicate and duplicate_fingerprint != identity_fingerprint:
                raise error("DUPLICATE_ACK_STALE", "草稿已变化，请重新确认", status=409)
            library = LibrarySnapshot(sess, actor_id)
            current = library.identity(draft.draft_kind, draft.target_id)
            if current is not None and encoded_identity(current) == encoded_identity(identity):
                vid = target.current_route_version_id if draft.draft_kind == "route" else target.current_outline_version_id
                version = sess.get(version_model, vid)
                result = {"version_id": vid, "version_no": version.version_no, "deduped": True}
            else:
                if target.current_route_version_id if draft.draft_kind == "route" else target.current_outline_version_id:
                    raise error("PLANNING_LOCKED", "课程已确认，不能再调整规划。", status=409)
                matches = library.matches(draft.draft_kind, identity, draft.target_id)
                if matches and not allow_duplicate:
                    raise PlanningDuplicate({"revision": revision, "fingerprint": identity_fingerprint,
                                             "matches": matches})
                result = (self._confirm_route(sess, draft, actor_id, idempotency_key, content)
                          if draft.draft_kind == "route" else
                          self._confirm_outline(sess, draft, actor_id, idempotency_key, content))
                version = sess.get(version_model, result["version_id"])
                version.title_snapshot = identity["title"]
                version.content_snapshot_json = encoded_identity(identity)
                version.content_fingerprint = identity_fingerprint
                target.title_ref = identity["title"]
            sess.add(PlanningConfirmationRow(confirmation_id=f"pc_{uuid.uuid4().hex[:12]}",
                owner_user_id=actor_id, draft_id=draft_id, revision=revision,
                idempotency_key=idempotency_key, result_json=json.dumps(result)))
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
        state = sess.execute(select(PlanningTaskStateRow).where(
            PlanningTaskStateRow.owner_user_id == actor_id,
            PlanningTaskStateRow.draft_id == draft.draft_id,
        ).order_by(PlanningTaskStateRow.updated_at.desc())).scalars().first()
        version = RouteVersionRow(
            route_version_id=f"rv_{uuid.uuid4().hex[:12]}",
            route_id=route.route_id,
            version_no=max_no + 1,
            source_task_id=f"confirm:{idem}",
            prompt_binding_id=None,
            source_snapshot_json=self._freeze_sources(sess, draft),
            scope_snapshot_json=state.scope_contract_json if state else None,
            coverage_snapshot_json=draft.coverage_json,
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
                raw_course = content["stages"][s_index]["courses"][c_index]
                source_id = raw_course.get("_source_course_id")
                old_node = sess.execute(select(RouteCourseNodeRow).where(
                    RouteCourseNodeRow.route_version_id == draft.base_version_id,
                    RouteCourseNodeRow.course_id == source_id)).scalars().first() if source_id and draft.base_version_id else None
                source_course = sess.get(CourseRow, source_id) if old_node else None
                if source_course and source_course.owner_user_id == actor_id and not source_course.deleted_at:
                    course_id = source_id
                    course_rows[course_order] = course_id
                    source_course.title_ref = course.title
                else:
                    course_id = f"crs_{uuid.uuid4().hex[:12]}"
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
                    scope_snapshot_ref=str(content["stages"][s_index]["courses"][c_index].get("scope", "")),
                    prereq_requirements_ref="",
                )
                sess.add(node_row)
                candidate_node_by_index[course_order] = node_row.node_id
                if course.units:
                    # 四层草稿：确认时逐课落不可变大纲版本（导图与讲次同版）
                    node_row.outline_version_id = self._materialize_course_outline(
                        sess, course_id=course_id, units=course.units, prerequisites=course.prerequisites,
                        source_route_version_id=version.route_version_id,
                        source_task_id=f"confirm:{idem}", raw_units=raw_course.get("units", []),
                        course_goal=course.goal, course_scope=str(raw_course.get("scope", "")),
                        source_outline_id=old_node.outline_version_id if old_node and old_node.goal_snapshot_ref == course.goal
                        and old_node.scope_snapshot_ref == str(raw_course.get("scope", "")) else None,
                    )
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

    def _materialize_course_outline(
        self, sess, *, course_id: str, units, prerequisites=(),
        source_route_version_id: str, source_task_id: str, raw_units=(), source_outline_id=None,
        course_goal="", course_scope="",
    ) -> str:
        """课程内嵌单元/讲次 → 该课程的不可变大纲版本（四层图确认后保持到讲次粒度）。

        与 _confirm_outline 同一套行结构；来源固定为本次路线确认。
        """
        course = sess.get(CourseRow, course_id)
        if course is None:
            raise error("course_not_found", status=404)
        max_no = (
            sess.execute(
                select(OutlineVersionRow.version_no)
                .where(OutlineVersionRow.course_id == course_id)
                .order_by(OutlineVersionRow.version_no.desc())
                .limit(1)
            ).scalar_one_or_none()
            or 0
        )
        version = OutlineVersionRow(
            outline_version_id=f"ov_{uuid.uuid4().hex[:12]}",
            course_id=course_id,
            title_snapshot=course.title_ref,
            version_no=max_no + 1,
            source_task_id=source_task_id,
            prompt_binding_id=None,
            source_route_version_id=source_route_version_id,
            source_route_node_id=None,
            goal_snapshot_ref=course_goal,
            scope_snapshot_ref=course_scope,
        )
        sess.add(version)
        sess.flush()
        lesson_ids = []
        for u_index, unit in enumerate(units):
            unit_row = CourseUnitRow(
                unit_id=f"unit_{uuid.uuid4().hex[:12]}",
                outline_version_id=version.outline_version_id,
                order_index=u_index,
                title_ref=unit.title,
            )
            sess.add(unit_row)
            sess.flush()
            for l_index, lesson in enumerate(unit.lessons):
                lesson_id = f"les_{uuid.uuid4().hex[:12]}"
                lesson_ids.append(lesson_id)
                sess.add(
                    LessonRow(
                        lesson_id=lesson_id,
                        outline_version_id=version.outline_version_id,
                        unit_id=unit_row.unit_id,
                        order_index=l_index,
                        title_ref=lesson.title,
                        goal_ref=lesson.goal or lesson.title,
                        status_preparation="not_generated",
                    )
                )
        sess.flush()
        for edge in prerequisites:
            sess.add(LessonPrerequisiteEdgeRow(
                edge_id=f"lpe_{uuid.uuid4().hex[:12]}", outline_version_id=version.outline_version_id,
                from_lesson_id=lesson_ids[edge.from_index], to_lesson_id=lesson_ids[edge.to_index],
            ))
        self._carry_metadata_progress(sess, course_id, source_outline_id, version.outline_version_id,
                                      raw_units, prerequisites)
        from ..materials.study_sources import freeze_sources
        route_version = sess.get(RouteVersionRow, source_route_version_id)
        source = load_json_field(route_version.source_snapshot_json, {}) if route_version else {}
        if source:
            pairs = zip(lesson_ids, (l.model_dump() for u in units for l in u.lessons))
            version.source_snapshot_json = json.dumps(freeze_sources(sess, course.owner_user_id,
                source["material_ids"], pairs), ensure_ascii=False)
        course.current_outline_version_id = version.outline_version_id
        from ..teaching.course_preparation import register_course
        register_course(sess, course)
        return version.outline_version_id

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
            goal_snapshot_ref=str(content.get("goal", "")),
            scope_snapshot_ref=str(content.get("scope", "")),
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
                    title_ref=lesson.title,
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

        old_outline = sess.get(OutlineVersionRow, draft.base_version_id) if draft.base_version_id else None
        same_scope = old_outline and old_outline.goal_snapshot_ref == str(content.get("goal", "")) and old_outline.scope_snapshot_ref == str(content.get("scope", ""))
        self._carry_metadata_progress(sess, course.course_id, draft.base_version_id if same_scope else None,
                                      version.outline_version_id, content.get("units", []), candidate.prerequisites)
        version.source_snapshot_json = self._freeze_sources(sess, draft, zip(
            lesson_ids_by_index.values(), (l.model_dump() for u in candidate.units for l in u.lessons)))
        course.current_outline_version_id = version.outline_version_id
        from ..teaching.course_preparation import register_course
        register_course(sess, course)
        self._audit(
            "config.changed", actor_id=actor_id, object_id=version.outline_version_id,
            result="outline_confirmed",
        )
        return {"version_id": version.outline_version_id, "version_no": version.version_no, "deduped": False}

    def _carry_metadata_progress(self, sess, course_id, old_version_id, new_version_id, raw_units, prerequisites):
        """Carry only exact goal/scope/prerequisite matches; old versions stay immutable."""
        if not old_version_id:
            return
        old_version = sess.get(OutlineVersionRow, old_version_id)
        course = sess.get(CourseRow, course_id)
        if not old_version or old_version.course_id != course_id:
            return
        raw = [l for u in raw_units for l in u.get("lessons", [])]
        new_units = list(sess.scalars(select(CourseUnitRow).where(CourseUnitRow.outline_version_id == new_version_id).order_by(CourseUnitRow.order_index)))
        new_lessons = [l for u in new_units for l in sess.scalars(select(LessonRow).where(LessonRow.unit_id == u.unit_id).order_by(LessonRow.order_index))]
        old_lessons = {l.lesson_id:l for l in sess.scalars(select(LessonRow).where(LessonRow.outline_version_id == old_version_id))}
        sources = [l.get("_source_lesson_id") for l in raw]
        old_incoming = {}
        for e in sess.scalars(select(LessonPrerequisiteEdgeRow).where(LessonPrerequisiteEdgeRow.outline_version_id == old_version_id)):
            old_incoming.setdefault(e.to_lesson_id,set()).add(e.from_lesson_id)
        new_incoming = {}
        for e in prerequisites:
            if e.from_index < len(sources) and e.to_index < len(sources):
                new_incoming.setdefault(e.to_index,set()).add(sources[e.from_index])
        used = set()
        for i,(metadata,new) in enumerate(zip(raw,new_lessons)):
            source = metadata.get("_source_lesson_id"); old = old_lessons.get(source)
            if (not old or source in used or old.goal_ref != new.goal_ref
                or old_incoming.get(source,set()) != new_incoming.get(i,set())):
                continue
            used.add(source)
            previous = sess.execute(select(LessonContentVersionRow).where(LessonContentVersionRow.lesson_id == source)
                .order_by(LessonContentVersionRow.content_version_no.desc())).scalars().first()
            if not previous or previous.goal_ref != new.goal_ref:
                continue
            fresh = LessonContentVersionRow(content_version_id=f"lcv_{uuid.uuid4().hex[:12]}", lesson_id=new.lesson_id,
                content_version_no=1, goal_ref=previous.goal_ref, scope_ref=previous.scope_ref)
            sess.add(fresh); sess.flush()
            progress = sess.execute(select(LessonProgressRow).where(LessonProgressRow.owner_user_id == course.owner_user_id,
                LessonProgressRow.lesson_id == source, LessonProgressRow.lesson_content_version_id == previous.content_version_id)).scalars().first()
            if progress:
                sess.add(LessonProgressRow(progress_id=f"prog_{uuid.uuid4().hex[:12]}",owner_user_id=course.owner_user_id,
                    lesson_id=new.lesson_id,lesson_content_version_id=fresh.content_version_id,learning_state=progress.learning_state))

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
