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
        scope, domain_fence, category = _domain_binding(sess, task)

    def check_state():
        with db.session() as sess:
            current = tasks.validate_lease(lease, actor_id=actor_id, session=sess)
            actual, current_fence, current_category = _domain_binding(sess, current)
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

    def authorize(body, destination, actual_scope):
        from ..logging_audit.trace import business_span
        with business_span("auth", task_id=task_id, attempt=lease.attempt):
            check_state()
            if actual_scope != scope:
                raise error("TASK_CANCELLED", "outbound scope differs from task binding")
            if disclosures and getattr(source, "config_id", None):
                disclosures.verify_or_audit(task_id=task_id, config_id=source.config_id,
                    actor_id=actor_id, content_category=category, scope_snapshot=scope)

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


def _domain_binding(sess, task):
    from ..planning.models import (PlanningTaskStateRow, ConversationRow, CourseRow,
        LearningRouteRow, PlanningDraftRow, CourseUnitRow, LessonRow, LessonContentVersionRow)
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
        lesson_ids = sorted({item["lesson_id"] for item in context["lessons"]})
        for ident in lesson_ids:
            lesson = sess.get(LessonRow, ident)
            if not lesson or lesson.unit_id != row.unit_id or lesson.outline_version_id != row.outline_version_id:
                raise error("TASK_CANCELLED", "assessment lesson changed")
        scope = {"course_id": row.course_id, "unit_id": row.unit_id, "lesson_ids": lesson_ids}
        fence = (row.assessment_id, row.outline_version_id, row.context_json,
            row.questions_json if attempt else None, attempt.answers_json if attempt else None)
        return scope, fence, "generated_text"

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
        return result(conv.material_snapshot_json, (conv.conversation_id, conv.generation,
            conv.course_id, conv.outline_version_id, conv.lesson_id, conv.lesson_content_version_id,
            conv.session_state))
    if task.kind in ("chat_answer", "lesson_step_generate"):
        raise error("TASK_CANCELLED", "task conversation missing")
    return result("[]", ())


def _material_scope(sess, task_id):
    return _domain_binding(sess, sess.get(LearningTaskRow, task_id))[0]
