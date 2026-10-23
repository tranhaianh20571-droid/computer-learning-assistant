"""Server-owned attempt binding and durable task-wide AI request accounting."""
from __future__ import annotations

import json
from datetime import timezone
from sqlalchemy import ForeignKey, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from ..logging_audit.models import Base
from ..logging_audit.repositories import PgTaskEventStore
from ..planning.errors import error
from ..tasks.service import LearningTaskRow, Lease, TaskService
from .contracts import AgentDeps, RunBudget


class AgentBudgetRow(Base):
    __tablename__ = "agent_task_budgets"
    task_id: Mapped[str] = mapped_column(String(64), ForeignKey("learning_tasks.task_id", ondelete="CASCADE"), primary_key=True)
    logical_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    wire_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=128)
    tool_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=256)
    traceparent: Mapped[str | None] = mapped_column(String(55))


def bind_task_deps(db, source, *, actor_id, task_id, disclosures=None):
    tasks = TaskService(db, PgTaskEventStore(db))
    source_cache = {}
    with db.session() as sess:
        task = sess.get(LearningTaskRow, task_id)
        if task is None or task.owner_user_id != actor_id:
            raise error("task_not_found", status=404)
        lease = Lease(task.task_id, task.attempt_id, task.lease_token, task.lease_owner,
            task.generation, task.attempt, task.lease_until, task.deadline_at, actor_id)
        deadline = task.deadline_at
        if deadline and deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        # Serialize budget initialization through the task row, including concurrent roles.
        tasks.validate_lease(lease, actor_id=actor_id, session=sess)
        row = sess.get(AgentBudgetRow, task_id)
        if row is None:
            from ..logging_audit.trace import capture_task_traceparent
            row = AgentBudgetRow(task_id=task_id, traceparent=capture_task_traceparent())
            sess.add(row)
            sess.flush()
        request_limit, tool_limit = row.request_limit, row.tool_limit
        parent_traceparent = row.traceparent
        scope, domain_fence, category = _domain_binding(sess, task, source_cache=source_cache)

    def check_state():
        with db.session() as sess:
            from ..auth.models import UserRow
            user = sess.get(UserRow, actor_id)
            if user is not None and user.status != "approved":
                raise error("TASK_CANCELLED", "account access revoked")
            current = tasks.validate_lease(lease, actor_id=actor_id, session=sess)
            actual, current_fence, current_category = _domain_binding(sess, current, source_cache=source_cache)
            if actual != scope or current_fence != domain_fence or current_category != category:
                raise error("TASK_CANCELLED", "bound task domain changed")
            if getattr(source, "config_id", None):
                from ..slice1.models import ServiceConfigRow
                config = sess.get(ServiceConfigRow, source.config_id)
                if (not config or not config.is_active or config.config_version != source.config_version
                    or config.endpoint.rstrip("/") != source.endpoint.rstrip("/")
                    or config.owner_scope == "user" and config.owner_user_id != actor_id):
                    raise error("MODEL_CREDENTIAL_INVALID", "bound configuration changed")
            material_ids = scope.get("material_ids", [])
            if material_ids:
                from ..materials.models import MaterialRow
                existing = set(sess.scalars(select(MaterialRow.material_id).where(
                    MaterialRow.material_id.in_(material_ids), MaterialRow.owner_user_id == actor_id,
                    MaterialRow.deleted_at.is_(None))))
                if existing != set(material_ids):
                    raise error("TASK_CANCELLED", "selected material no longer authorized")

    def debit(kind):
        names = {"logical": "logical_requests", "wire": "wire_requests", "tool": "tool_calls"}
        with db.session() as sess:
            tasks.validate_lease(lease, actor_id=actor_id, session=sess)
            row = sess.get(AgentBudgetRow, task_id, with_for_update=True)
            name = names[kind]
            limit = row.tool_limit if kind == "tool" else row.request_limit
            if getattr(row, name) >= limit:
                raise error("AGENT_LOOP_LIMIT")
            setattr(row, name, getattr(row, name) + 1)
            if task.kind == "character_build_stage":
                from ..characters.distillation_models import CharacterBuildStageRow, CharacterBuildRow
                stage=sess.get(CharacterBuildStageRow,task_id)
                build=sess.get(CharacterBuildRow,stage.build_id)
                root=sess.get(AgentBudgetRow,build.task_id,with_for_update=True)
                root_limit=root.tool_limit if kind=="tool" else root.request_limit
                if getattr(root,name)>=root_limit:
                    raise error("AGENT_LOOP_LIMIT")
                setattr(root,name,getattr(root,name)+1)

    def authorize(body, destination, actual_scope):
        from ..logging_audit.trace import business_span
        with business_span("auth", task_id=task_id, attempt=lease.attempt):
            check_state()
            if actual_scope != scope:
                raise error("TASK_CANCELLED", "outbound scope differs from task binding")
            if disclosures and getattr(source, "config_id", None):
                disclosures.verify_or_audit(task_id=task_id, config_id=source.config_id,
                    actor_id=actor_id, content_category=category, scope_snapshot=scope,
                    expected_grant_id=getattr(source, "disclosure_grant_id", None))

    return AgentDeps(actor_id=actor_id, task_id=task_id, attempt=lease.attempt,
        attempt_id=lease.attempt_id, generation=lease.generation,
        config_id=getattr(source,"config_id",None), config_version=getattr(source,"config_version",None),
        disclosure_scope=scope, budget=RunBudget(request_limit=request_limit,wire_limit=request_limit,
            tool_limit=tool_limit,deadline=deadline,debit=debit),
        check_state=check_state, authorize_payload=authorize, omit_content_trace=True,
        parent_traceparent=parent_traceparent)


def _owned(row, actor_id):
    if row is None or row.owner_user_id != actor_id or getattr(row, "deleted_at", None):
        raise error("TASK_CANCELLED", "task domain no longer authorized")
    return row


def _materials(value):
    ids = json.loads(value or "[]")
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        raise error("TASK_CANCELLED", "invalid task material scope")
    return sorted(set(ids))


def _domain_binding(sess, task, *, source_cache=None):
    if task.kind == "material_parse":
        from ..materials.models import MaterialRow, DocumentAttachmentRow
        material = _owned(sess.get(MaterialRow, task.subject_id), task.owner_user_id)
        attachment = _owned(sess.get(DocumentAttachmentRow, task.subject_id), task.owner_user_id)
        if attachment.task_id != task.task_id:
            raise error("TASK_CANCELLED", "material task changed")
        binding = json.loads(attachment.result_json).get("extraction_binding", {})
        return ({"material_ids": [material.material_id], "sha256": material.source_bytes_sha256},
                (material.source_bytes_sha256, json.dumps(binding, sort_keys=True)), "material")
    if task.kind in ("review_generate", "review_grade"):
        from ..review.service import domain_binding
        return domain_binding(sess, task)
    if task.kind == "source_import":
        from ..materials.source_models import ImportedSourceRow
        from ..materials.models import MaterialRow
        row = _owned(sess.get(ImportedSourceRow, task.subject_id), task.owner_user_id)
        material = _owned(sess.get(MaterialRow, task.subject_id), task.owner_user_id)
        if row.task_id != task.task_id:
            raise error("TASK_CANCELLED", "source task changed")
        return {"material_ids": [material.material_id]}, (row.source_version, row.binding_json), "material"
    if task.kind in ("character_build","character_build_stage"):
        from ..characters.distillation import build_domain
        return build_domain(sess,task)
    from ..planning.models import (PlanningTaskStateRow, ConversationRow, CourseRow,
        LearningRouteRow, PlanningDraftRow, CourseUnitRow, LessonRow, LessonContentVersionRow,
        OutlineVersionRow)
    from ..planning.feature_models import PlanningDialogueRow, CoverAssetRow
    from ..teaching.learning_models import (PreparedFirstSegmentRow, UnitAssessmentRow,
        AssessmentAttemptRow, LessonBlueprintRow)
    actor = task.owner_user_id

    def parent(kind, ident):
        return _owned(sess.get(CourseRow if kind in ("course", "course_outline")
            else LearningRouteRow, ident), actor)

    def result(materials, fence):
        scope = {"material_ids": _materials(materials)}
        return scope, fence, "material" if scope["material_ids"] else "query"

    if task.kind == "lesson_plan_refine":
        conversation_id, separator, plan_key = task.subject_id.partition(":")
        if not separator or len(plan_key) != 64 or any(char not in "0123456789abcdef" for char in plan_key):
            raise error("TASK_CANCELLED", "invalid refinement subject")
        conv = _owned(sess.get(ConversationRow, conversation_id), actor)
        row = _owned(sess.get(LessonBlueprintRow, conversation_id), actor)
        course = parent("course", conv.course_id)
        lesson = sess.get(LessonRow, conv.lesson_id)
        content = sess.get(LessonContentVersionRow, conv.lesson_content_version_id)
        context = json.loads(row.context_json)
        from ..materials.study_sources import verify_sources
        verify_sources(sess, actor, context.get("source_snapshot", {}))
        if (conv.kind != "teaching" or conv.session_state != "active"
            or course.current_outline_version_id != conv.outline_version_id
            or row.lesson_id != conv.lesson_id or row.outline_version_id != conv.outline_version_id
            or row.content_version_id != conv.lesson_content_version_id
            or not lesson or lesson.outline_version_id != conv.outline_version_id
            or not content or content.lesson_id != conv.lesson_id
            or context.get("lesson_id") != conv.lesson_id
            or context.get("outline_version_id") != conv.outline_version_id):
            raise error("TASK_CANCELLED", "refinement lesson binding changed")
        return result(json.dumps(context.get("material_ids", [])),
            (plan_key, row.context_json, row.source_task_id, row.lesson_id, row.outline_version_id,
                row.content_version_id, conv.course_id, conv.generation, conv.session_state,
                course.current_outline_version_id, content.content_version_id))

    if task.kind == "lesson_first_prepare":
        row = _owned(sess.scalars(select(PreparedFirstSegmentRow).where(
            PreparedFirstSegmentRow.task_id == task.task_id)).first(), actor)
        course = parent("course", row.course_id)
        context = json.loads(row.context_json)
        from ..materials.study_sources import verify_sources
        verify_sources(sess, actor, context.get("source_snapshot", {}))
        if context.get("outline_version_id") != course.current_outline_version_id:
            raise error("TASK_CANCELLED", "prepared outline changed")
        lesson = sess.get(LessonRow, row.lesson_id)
        if not lesson or lesson.outline_version_id != course.current_outline_version_id:
            raise error("TASK_CANCELLED", "prepared lesson changed")
        if row.consumed_conversation_id:
            consumed = _owned(sess.get(ConversationRow, row.consumed_conversation_id), actor)
            if (consumed.session_state != "active" or consumed.course_id != row.course_id
                or consumed.lesson_id != row.lesson_id):
                raise error("TASK_CANCELLED", "prepared conversation changed")
        return result(json.dumps(context.get("material_ids", [])),
            (row.cache_id, row.course_id, row.lesson_id, row.text_key, row.context_json,
                course.current_outline_version_id))

    if task.kind == "unit_scope_generate":
        from ..teaching.unit_scope import UnitScopeRow
        row = _owned(sess.scalar(select(UnitScopeRow).where(
            UnitScopeRow.task_id == task.task_id)), actor)
        course = parent("course", row.course_id)
        unit = sess.get(CourseUnitRow, row.unit_id)
        if (not unit or unit.outline_version_id != row.outline_version_id
                or course.current_outline_version_id != row.outline_version_id):
            raise error("TASK_CANCELLED", "scope unit changed")
        context = json.loads(row.context_json)
        from ..materials.study_sources import verify_sources
        snapshot = context.get("source_snapshot", {})
        if source_cache is None:
            verify_sources(sess, actor, snapshot)
        else:
            from ..materials.source_models import source_records
            material_ids = snapshot.get("material_ids", [])
            records = source_records(sess, actor, material_ids) if material_ids else []
            signature = (actor, row.context_json, tuple(sorted(
                (material.material_id, material.owner_user_id, material.status,
                 material.source_bytes_sha256, source.owner_user_id, source.result_json)
                for material, source in records)))
            if source_cache.get("scope_sources") != signature:
                verify_sources(sess, actor, snapshot)
                source_cache["scope_sources"] = signature
        from ..planning.models import OutlineVersionRow
        outline = sess.get(OutlineVersionRow, row.outline_version_id)
        if (course.title_ref != context["course_title"] or unit.title_ref != context["unit_title"]
                or json.loads(outline.source_snapshot_json or "{}") != context["source_snapshot"]):
            raise error("TASK_CANCELLED", "scope inputs changed")
        lesson_ids = sorted(item["lesson_id"] for item in context["lessons"])
        actual_lessons = {lesson.lesson_id: lesson for lesson in sess.scalars(
            select(LessonRow).where(LessonRow.unit_id == row.unit_id))}
        if sorted(actual_lessons) != lesson_ids:
            raise error("TASK_CANCELLED", "scope lesson membership changed")
        for ident in lesson_ids:
            lesson = actual_lessons[ident]
            frozen = next(item for item in context["lessons"] if item["lesson_id"] == ident)
            if not lesson or lesson.unit_id != row.unit_id or lesson.outline_version_id != row.outline_version_id:
                raise error("TASK_CANCELLED", "scope lesson changed")
            if lesson.goal_ref != frozen["goal"] or lesson.title_ref != frozen["title"]:
                raise error("TASK_CANCELLED", "scope lesson goals changed")
        scope = {"course_id": row.course_id, "unit_id": row.unit_id, "lesson_ids": lesson_ids}
        materials = context.get("source_snapshot", {}).get("material_ids", [])
        if materials:
            scope["material_ids"] = sorted(set(materials))
        return scope, (row.scope_id, row.input_hash, row.context_json), "material" if materials else "generated_text"

    if task.kind in ("unit_assessment_generate", "assessment_grade"):
        row = sess.scalars(select(UnitAssessmentRow).where(
            UnitAssessmentRow.task_id == task.task_id)).first()
        attempt = None
        if row is None:
            attempt = _owned(sess.scalars(select(AssessmentAttemptRow).where(
                AssessmentAttemptRow.task_id == task.task_id)).first(), actor)
            row = sess.get(UnitAssessmentRow, attempt.assessment_id)
        row = _owned(row, actor)
        course = parent("course", row.course_id)
        unit = sess.get(CourseUnitRow, row.unit_id)
        if (not unit or unit.outline_version_id != row.outline_version_id
            or course.current_outline_version_id != row.outline_version_id):
            raise error("TASK_CANCELLED", "assessment unit changed")
        context = json.loads(row.context_json)
        from ..materials.study_sources import verify_sources
        snapshot = context.get("source_snapshot", {})
        verify_sources(sess, actor, snapshot)
        lesson_ids = sorted({item["lesson_id"] for item in context["lessons"]})
        for ident in lesson_ids:
            lesson = sess.get(LessonRow, ident)
            if not lesson or lesson.unit_id != row.unit_id or lesson.outline_version_id != row.outline_version_id:
                raise error("TASK_CANCELLED", "assessment lesson changed")
        scope = {"course_id": row.course_id, "unit_id": row.unit_id, "lesson_ids": lesson_ids}
        scope_fence = ()
        if context.get("scope_hash"):
            from ..teaching.unit_scope import UnitScopeRow
            scope_row = sess.scalar(select(UnitScopeRow).join(
                LearningTaskRow, UnitScopeRow.task_id == LearningTaskRow.task_id).where(
                    UnitScopeRow.owner_user_id == actor, UnitScopeRow.unit_id == row.unit_id,
                    UnitScopeRow.outline_version_id == row.outline_version_id,
                    UnitScopeRow.scope_hash == context["scope_hash"],
                    LearningTaskRow.status == "succeeded"))
            if not scope_row:
                raise error("TASK_CANCELLED", "assessment scope changed")
            scope_context = json.loads(scope_row.context_json)
            unit_lessons = {lesson.lesson_id: lesson for lesson in sess.scalars(
                select(LessonRow).where(LessonRow.unit_id == row.unit_id))}
            frozen_lessons = {item["lesson_id"]: item for item in scope_context.get("lessons", [])}
            outline = sess.get(OutlineVersionRow, row.outline_version_id)
            if (set(unit_lessons) != set(frozen_lessons)
                    or any(item.goal_ref != frozen_lessons[ident]["goal"]
                        or item.title_ref != frozen_lessons[ident]["title"]
                        for ident, item in unit_lessons.items())
                    or course.title_ref != scope_context.get("course_title")
                    or unit.title_ref != scope_context.get("unit_title")
                    or not outline
                    or json.loads(outline.source_snapshot_json or "{}") != scope_context.get("source_snapshot")):
                raise error("TASK_CANCELLED", "assessment scope inputs changed")
            scope_fence = (scope_row.scope_id, scope_row.input_hash, scope_row.scope_hash,
                scope_row.context_json)
        if snapshot:
            scope["material_ids"] = sorted(set(snapshot.get("material_ids", [])))
        fence = (row.assessment_id, row.outline_version_id, row.context_json,
            row.questions_json if attempt else None, attempt.answers_json if attempt else None,
            *scope_fence)
        return scope, fence, "material" if snapshot.get("material_ids") else "generated_text"

    if task.kind == "planning_dialogue":
        turn = _owned(sess.get(PlanningDialogueRow, task.task_id), actor)
        draft = _owned(sess.get(PlanningDraftRow, turn.draft_id), actor)
        parent(draft.draft_kind, draft.target_id)
        if draft.revision != turn.base_revision:
            raise error("TASK_CANCELLED", "dialogue draft changed")
        state = sess.scalars(select(PlanningTaskStateRow).where(
            PlanningTaskStateRow.draft_id == draft.draft_id,
            PlanningTaskStateRow.owner_user_id == actor).order_by(
                PlanningTaskStateRow.updated_at.desc())).first()
        return result(state.material_ids_json if state else "[]",
            (turn.draft_id, turn.base_revision, draft.target_id, draft.draft_kind))

    if task.kind == "course_cover":
        asset = _owned(sess.scalars(select(CoverAssetRow).where(
            CoverAssetRow.task_id == task.task_id)).first(), actor)
        target = parent(asset.target_kind, asset.target_id)
        return result("[]", (asset.asset_id, asset.target_kind, asset.target_id, target.title_ref))

    state = sess.get(PlanningTaskStateRow, task.task_id)
    if state:
        _owned(state, actor)
        if state.abandoned_at:
            raise error("TASK_CANCELLED", "planning was abandoned")
        if state.target_id:
            parent(state.target_kind, state.target_id)
        return result(state.material_ids_json, (state.target_id, state.target_kind, state.draft_id))

    conv = sess.get(ConversationRow, task.subject_id.split(":")[0])
    if conv:
        _owned(conv, actor)
        parts = task.subject_id.split(":")
        followup = (task.kind == "lesson_step_generate" and conv.kind == "teaching"
            and len(parts) == 3 and parts[1] == "followup" and parts[2] == str(conv.generation))
        allowed_states = ("active", "paused") if followup else ("active",)
        if conv.session_state not in allowed_states:
            raise error("TASK_CANCELLED", "conversation inactive")
        if conv.course_id:
            parent("course", conv.course_id)
        blueprint = sess.get(LessonBlueprintRow, conv.conversation_id)
        scope_fence = ()
        if blueprint:
            from ..materials.study_sources import verify_sources
            context = json.loads(blueprint.context_json)
            verify_sources(sess, actor, context.get("source_snapshot", {}))
            scope_hash = context.get("unit_scope_hash")
            if scope_hash:
                from ..teaching.unit_scope import UnitScopeRow
                from ..planning.models import CourseUnitRow, OutlineVersionRow
                unit = sess.get(CourseUnitRow, sess.get(LessonRow, conv.lesson_id).unit_id)
                course = sess.get(CourseRow, conv.course_id)
                scope_row = sess.scalar(select(UnitScopeRow).join(
                    LearningTaskRow, UnitScopeRow.task_id == LearningTaskRow.task_id).where(
                        UnitScopeRow.owner_user_id == actor, UnitScopeRow.unit_id == unit.unit_id,
                        UnitScopeRow.outline_version_id == conv.outline_version_id,
                        UnitScopeRow.scope_hash == scope_hash, LearningTaskRow.status == "succeeded"))
                frozen = json.loads(scope_row.context_json) if scope_row else {}
                outline = sess.get(OutlineVersionRow, conv.outline_version_id)
                current_lessons = {item.lesson_id: item for item in sess.scalars(
                    select(LessonRow).where(LessonRow.unit_id == unit.unit_id))}
                frozen_lessons = {item["lesson_id"]: item for item in frozen.get("lessons", [])}
                if (not scope_row or not unit or not course or not outline
                        or course.current_outline_version_id != conv.outline_version_id
                        or course.title_ref != frozen.get("course_title")
                        or unit.title_ref != frozen.get("unit_title")
                        or json.loads(outline.source_snapshot_json or "{}") != frozen.get("source_snapshot")
                        or set(current_lessons) != set(frozen_lessons)
                        or any(item.goal_ref != frozen_lessons[ident]["goal"]
                            or item.title_ref != frozen_lessons[ident]["title"]
                            for ident, item in current_lessons.items())):
                    raise error("TASK_CANCELLED", "frozen lesson scope changed")
                scope_fence = (scope_row.scope_id, scope_row.scope_hash, scope_row.input_hash,
                    scope_row.context_json)
        return result(conv.material_snapshot_json, (conv.conversation_id, conv.generation,
            conv.course_id, conv.outline_version_id, conv.lesson_id, conv.lesson_content_version_id,
            conv.session_state, *scope_fence))
    if task.kind in ("chat_answer", "lesson_step_generate"):
        raise error("TASK_CANCELLED", "task conversation missing")
    return result("[]", ())


def _material_scope(sess, task_id):
    return _domain_binding(sess, sess.get(LearningTaskRow, task_id))[0]
