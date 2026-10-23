"""Offline domain scopes, authorization and durable budgets for native requests."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.agents.task_binding import AgentBudgetRow, bind_task_deps
from backend.auth.models import UserRow
from backend.logging_audit.db import Database
from backend.logging_audit.models import TaskEventRow, TaskStateRow
from backend.logging_audit.repositories import PgTaskEventStore
from backend.materials.models import MaterialRow
from backend.planning.errors import PlanningError
from backend.planning.feature_models import CoverAssetRow, PlanningDialogueRow
from backend.planning.models import (ConversationRow, CourseRow, CourseUnitRow, LearningRouteRow,
    LessonRow, LessonContentVersionRow, PlanningDraftRow, PlanningTaskStateRow)
from backend.slice1.disclosure_service import DisclosureService
from backend.slice1.errors import Slice1Error
from backend.slice1.models import DisclosureGrantRow, ServiceConfigRow
from backend.tasks.service import LearningTaskRow, TaskAttemptRow, TaskService, utcnow
from backend.teaching.learning_models import (AssessmentAttemptRow, PreparedFirstSegmentRow,
    UnitAssessmentRow, LessonBlueprintRow)


@contextmanager
def fails(code):
    with pytest.raises(PlanningError) as caught:
        yield
    assert caught.value.code == code


@pytest.fixture
def env():
    db = Database("sqlite+pysqlite:///:memory:")
    for model in (UserRow, LearningTaskRow, AgentBudgetRow, TaskAttemptRow, TaskStateRow, TaskEventRow,
        ConversationRow, CourseRow, CourseUnitRow, LearningRouteRow, LessonRow,
        PlanningDraftRow, PlanningTaskStateRow, CoverAssetRow, PlanningDialogueRow,
        PreparedFirstSegmentRow, UnitAssessmentRow, AssessmentAttemptRow, MaterialRow,
        LessonContentVersionRow, LessonBlueprintRow, ServiceConfigRow, DisclosureGrantRow):
        model.__table__.create(db.engine)
    tasks = TaskService(db, PgTaskEventStore(db))
    with db.session() as sess:
        sess.add(CourseRow(course_id="course_1", owner_user_id="owner", current_outline_version_id="outline_1"))
        sess.add(CourseUnitRow(unit_id="unit_1", outline_version_id="outline_1", title_ref="synthetic unit"))
        sess.add(LessonRow(lesson_id="lesson_1", unit_id="unit_1", outline_version_id="outline_1"))
        sess.add(MaterialRow(material_id="material_1", owner_user_id="owner", filename_hash="a"*64,
            source_bytes_sha256="b"*64, kind="txt", size_bytes=1))
        sess.add(ServiceConfigRow(config_id="config_1", config_version=1, owner_user_id="owner",
            owner_scope="user", kind="content_model", protocol="openai", endpoint="https://example.invalid/v1",
            model_name="synthetic"))
    source = SimpleNamespace(config_id="config_1", config_version=1, endpoint="https://example.invalid/v1")
    try:
        yield db, tasks, source, DisclosureService(db)
    finally:
        db.dispose()


def task(env, kind, subject="course_1"):
    _, tasks, _, _ = env
    ident = tasks.create_task(owner_user_id="owner", kind=kind, subject_id=subject,
        idempotency_key=kind)["task_id"]
    tasks.claim(ident, lease_owner="synthetic_worker")
    return ident


def bind(env, ident):
    db, _, source, disclosures = env
    return bind_task_deps(db, source, actor_id="owner", task_id=ident, disclosures=disclosures)


def mutate(env, model, ident, **values):
    with env[0].session() as sess:
        row = sess.get(model, ident)
        for name, value in values.items():
            setattr(row, name, value)


def prewarm(env):
    ident = task(env, "lesson_first_prepare", "first:synthetic")
    with env[0].session() as sess:
        sess.add(PreparedFirstSegmentRow(cache_id="prepared_1", owner_user_id="owner", course_id="course_1",
            lesson_id="lesson_1", task_id=ident, text_key="synthetic", plan_json="{}",
            context_json=json.dumps({"outline_version_id": "outline_1", "material_ids": ["material_1"]}),
            expires_at=utcnow()+timedelta(days=1)))
    return ident


def grant(env, ident, category, scope):
    return env[3].create(actor_id="owner", task_id=ident, config_id="config_1",
        content_category=category, scope_snapshot=scope)


def authorize(deps):
    deps.authorize_payload(b"synthetic request", "https://example.invalid/v1/chat/completions",
        deps.disclosure_scope)


def test_prewarm_actual_material_scope_requires_material_grant(env):
    ident = prewarm(env)
    deps = bind(env, ident)
    assert deps.disclosure_scope == {"material_ids": ["material_1"]}
    grant(env, ident, "query", {"material_ids": []})
    with pytest.raises(Slice1Error, match="disclosure_required"):
        authorize(deps)
    grant(env, ident, "material", deps.disclosure_scope)
    authorize(deps)
    mutate(env, MaterialRow, "material_1", deleted_at=utcnow())
    with fails("TASK_CANCELLED"):
        authorize(deps)


@pytest.mark.parametrize("model,ident,values", [
    (CourseRow, "course_1", {"owner_user_id": "other"}),
    (CourseRow, "course_1", {"deleted_at": utcnow()}),
    (CourseRow, "course_1", {"current_outline_version_id": "outline_2"}),
    (PreparedFirstSegmentRow, "prepared_1", {"task_id": "replacement"}),
    (PreparedFirstSegmentRow, "prepared_1", {"owner_user_id": "other"}),
])
def test_prewarm_request_rechecks_domain(env, model, ident, values):
    deps = bind(env, prewarm(env))
    mutate(env, model, ident, **values)
    with fails("TASK_CANCELLED"):
        deps.check()


def test_altered_payload_scope_cannot_reuse_grant(env):
    ident = prewarm(env)
    deps = bind(env, ident)
    grant(env, ident, "material", deps.disclosure_scope)
    with fails("TASK_CANCELLED"):
        deps.authorize_payload(b"synthetic", "https://example.invalid/v1", {"material_ids": []})
    record = grant(env, ident, "material", deps.disclosure_scope)
    env[3].revoke(record["grant_id"], actor_id="owner")
    with pytest.raises(Slice1Error, match="disclosure_revoked"):
        authorize(deps)


def dialogue(env):
    ident = task(env, "planning_dialogue")
    with env[0].session() as sess:
        sess.add(PlanningDraftRow(draft_id="draft_1", owner_user_id="owner", target_id="course_1",
            draft_kind="course_outline", revision=1))
        sess.add(PlanningDialogueRow(task_id=ident, draft_id="draft_1", owner_user_id="owner",
            base_revision=1, user_content="synthetic", base_json="{}"))
        sess.add(PlanningTaskStateRow(task_id="source_1", owner_user_id="owner", draft_id="draft_1",
            material_ids_json='["material_1"]'))
    return ident


def test_dialogue_uses_draft_source_material_scope_and_revision_fence(env):
    deps = bind(env, dialogue(env))
    assert deps.disclosure_scope == {"material_ids": ["material_1"]}
    deps.check()
    mutate(env, PlanningDraftRow, "draft_1", revision=2)
    with fails("TASK_CANCELLED"):
        deps.check()


def test_cover_request_rechecks_target_ownership(env):
    ident = task(env, "course_cover")
    with env[0].session() as sess:
        sess.add(CoverAssetRow(asset_id="cover_1", task_id=ident, owner_user_id="owner",
            target_kind="course", target_id="course_1"))
    deps = bind(env, ident)
    deps.check()
    mutate(env, CourseRow, "course_1", deleted_at=utcnow())
    with fails("TASK_CANCELLED"):
        deps.check()


def assessment(env, grade=False):
    ident = task(env, "assessment_grade" if grade else "unit_assessment_generate")
    with env[0].session() as sess:
        sess.add(UnitAssessmentRow(assessment_id="assessment_1", owner_user_id="owner", course_id="course_1",
            unit_id="unit_1", outline_version_id="outline_1", task_id="source_assessment" if grade else ident,
            context_json=json.dumps({"lessons": [{"lesson_id": "lesson_1", "taught": [{"text": "synthetic"}]}]})))
        if grade:
            sess.add(AssessmentAttemptRow(attempt_id="answer_1", assessment_id="assessment_1", owner_user_id="owner",
                task_id=ident, idempotency_key="synthetic", answers_json='{"question_1":"synthetic"}'))
    return ident


@pytest.mark.parametrize("grade", [False, True])
def test_assessment_generated_text_requires_exact_owned_unit_scope(env, grade):
    ident = assessment(env, grade)
    deps = bind(env, ident)
    scope = {"course_id": "course_1", "unit_id": "unit_1", "lesson_ids": ["lesson_1"]}
    assert deps.disclosure_scope == scope
    grant(env, ident, "query", scope)
    with pytest.raises(Slice1Error, match="disclosure_required"):
        authorize(deps)
    grant(env, ident, "generated_text", {**scope, "lesson_ids": []})
    with pytest.raises(Slice1Error, match="disclosure_required"):
        authorize(deps)
    grant(env, ident, "generated_text", scope)
    authorize(deps)
    mutate(env, CourseRow, "course_1", owner_user_id="other")
    with fails("TASK_CANCELLED"):
        deps.check()


@pytest.mark.parametrize("field,value", [("generation", 2), ("owner_user_id", "other"),
    ("deleted_at", utcnow()), ("session_state", "archived")])
def test_conversation_fence_checked_each_request(env, field, value):
    ident = task(env, "chat_answer", "conversation_1")
    with env[0].session() as sess:
        sess.add(ConversationRow(conversation_id="conversation_1", owner_user_id="owner", generation=1))
    deps = bind(env, ident)
    deps.check()
    mutate(env, ConversationRow, "conversation_1", **{field: value})
    with fails("TASK_CANCELLED"):
        deps.check()


def test_consumed_prewarm_rechecks_conversation(env):
    ident = prewarm(env)
    deps = bind(env, ident)
    with env[0].session() as sess:
        sess.add(ConversationRow(conversation_id="consumed_1", owner_user_id="owner", course_id="course_1",
            lesson_id="lesson_1", kind="teaching", generation=1))
        sess.get(PreparedFirstSegmentRow, "prepared_1").consumed_conversation_id = "consumed_1"
    deps.check()
    mutate(env, ConversationRow, "consumed_1", deleted_at=utcnow())
    with fails("TASK_CANCELLED"):
        deps.check()


def test_prewarm_cannot_expand_bound_materials(env):
    deps = bind(env, prewarm(env))
    mutate(env, PreparedFirstSegmentRow, "prepared_1", context_json=json.dumps({
        "outline_version_id": "outline_1", "material_ids": ["material_1", "material_2"]}))
    with fails("TASK_CANCELLED"):
        deps.check()


@pytest.mark.parametrize("values", [{"is_active": False}, {"config_version": 2},
    {"owner_user_id": "other"}, {"endpoint": "https://other.invalid/v1"}])
def test_configuration_changes_rejected_each_request(env, values):
    deps = bind(env, prewarm(env))
    mutate(env, ServiceConfigRow, "config_1", **values)
    with fails("MODEL_CREDENTIAL_INVALID"):
        deps.check()


def test_budget_survives_new_binding_and_retry(env):
    ident = prewarm(env)
    mutate(env, AgentBudgetRow, ident, request_limit=2)
    first = bind(env, ident)
    first.budget.consume("logical")
    second = bind(env, ident)
    second.budget.consume("logical")
    third = bind(env, ident)
    with pytest.raises(PlanningError, match="AGENT_LOOP_LIMIT"):
        third.budget.consume("logical")
    with env[0].session() as sess:
        assert sess.get(AgentBudgetRow, ident).logical_requests == 2


@pytest.mark.parametrize("marker,state,allowed", [
    ("followup:1", "paused", True), ("followup:1", "active", True),
    ("followup:1", "awaiting_resume", False), ("followup:2", "paused", False),
    ("1", "paused", False), ("followup:1:extra", "paused", False),
])
def test_followup_paused_session_is_exactly_scoped(env, marker, state, allowed):
    ident = task(env, "lesson_step_generate", "conversation_1:" + marker)
    with env[0].session() as sess:
        sess.add(ConversationRow(conversation_id="conversation_1", owner_user_id="owner",
            kind="teaching", generation=1, session_state=state))
    if not allowed:
        with fails("TASK_CANCELLED"):
            bind(env, ident)
        return
    deps = bind(env, ident)
    deps.check()
    mutate(env, ConversationRow, "conversation_1", session_state="active" if state == "paused" else "paused")
    with fails("TASK_CANCELLED"):
        deps.check()


def refinement(env):
    ident = task(env, "lesson_plan_refine", "conversation_1:" + "a"*64)
    context = {"lesson_id": "lesson_1", "outline_version_id": "outline_1", "material_ids": ["material_1"]}
    with env[0].session() as sess:
        sess.add(ConversationRow(conversation_id="conversation_1", owner_user_id="owner", kind="teaching",
            generation=1, course_id="course_1", lesson_id="lesson_1", outline_version_id="outline_1",
            lesson_content_version_id="content_1", material_snapshot_json="[]"))
        sess.add(LessonContentVersionRow(content_version_id="content_1", lesson_id="lesson_1"))
        sess.add(LessonBlueprintRow(conversation_id="conversation_1", owner_user_id="owner", lesson_id="lesson_1",
            outline_version_id="outline_1", content_version_id="content_1", plan_json="{}",
            context_json=json.dumps(context), source_task_id="frozen_first_text"))
    return ident


def test_refinement_uses_frozen_blueprint_material_scope_without_cache(env):
    ident = refinement(env)
    deps = bind(env, ident)
    assert deps.disclosure_scope == {"material_ids": ["material_1"]}
    grant(env, ident, "query", {"material_ids": []})
    with pytest.raises(Slice1Error, match="disclosure_required"):
        authorize(deps)
    grant(env, ident, "material", deps.disclosure_scope)
    authorize(deps)
    mutate(env, MaterialRow, "material_1", deleted_at=utcnow())
    with fails("TASK_CANCELLED"):
        authorize(deps)


@pytest.mark.parametrize("model,ident,values", [
    (ConversationRow, "conversation_1", {"deleted_at": utcnow()}),
    (ConversationRow, "conversation_1", {"generation": 2}),
    (ConversationRow, "conversation_1", {"session_state": "paused"}),
    (ConversationRow, "conversation_1", {"lesson_content_version_id": "other"}),
    (CourseRow, "course_1", {"current_outline_version_id": "outline_2"}),
    (CourseRow, "course_1", {"owner_user_id": "other"}),
    (LessonRow, "lesson_1", {"outline_version_id": "outline_2"}),
    (LessonContentVersionRow, "content_1", {"lesson_id": "other"}),
    (LessonBlueprintRow, "conversation_1", {"owner_user_id": "other"}),
    (LessonBlueprintRow, "conversation_1", {"source_task_id": "replacement"}),
    (LessonBlueprintRow, "conversation_1", {"context_json": "{}"}),
    (LessonBlueprintRow, "conversation_1", {"context_json": json.dumps({"lesson_id": "lesson_1",
        "outline_version_id": "outline_1", "material_ids": ["material_1"], "learner_profile": "changed"})}),
])
def test_refinement_checks_domain_every_model_request(env, model, ident, values):
    deps = bind(env, refinement(env))
    mutate(env, model, ident, **values)
    with fails("TASK_CANCELLED"):
        deps.check()


@pytest.mark.parametrize("subject", ["conversation_1", "conversation_1:short", "conversation_1:" + "g"*64])
def test_refinement_requires_plan_key_subject(env, subject):
    ident = refinement(env)
    mutate(env, LearningTaskRow, ident, subject_id=subject)
    with fails("TASK_CANCELLED"):
        bind(env, ident)


def test_refinement_requires_frozen_blueprint(env):
    ident = refinement(env)
    with env[0].session() as sess:
        sess.delete(sess.get(LessonBlueprintRow, "conversation_1"))
    with fails("TASK_CANCELLED"):
        bind(env, ident)


def test_actual_authorization_uses_safe_business_span(env, monkeypatch):
    entered = []
    @contextmanager
    def span(stage, **attrs):
        entered.append((stage, attrs))
        yield
    monkeypatch.setattr("backend.logging_audit.trace.business_span", span)
    ident = prewarm(env)
    deps = bind(env, ident)
    grant(env, ident, "material", deps.disclosure_scope)
    authorize(deps)
    assert entered == [("auth", {"task_id": ident, "attempt": 1})]


def test_background_dispatches_refinement_once_to_separate_pool(env):
    from concurrent.futures import Future
    from backend.teaching.background import TeachingBackground
    ident = refinement(env)
    mutate(env, LearningTaskRow, ident, status="queued")
    second = env[1].create_task(owner_user_id="owner", kind="lesson_plan_refine",
        subject_id="conversation_1:" + "b"*64, idempotency_key="second_refinement")["task_id"]
    checked, executed = [], []
    def eligible(sess, item):
        checked.append(item.task_id)
        return True
    preparation = SimpleNamespace(refinement_eligible=eligible,
        run_refinement=lambda item: executed.append(item) or {"status": "succeeded"},
        reconcile=lambda **kwargs: None)
    service = SimpleNamespace(db=env[0], tasks=env[1], preparation=preparation, observed_sessions={},
        tts_adapter_factory=None, disclosures=None, configs=None)
    from backend.teaching.authorization import TeachingAuthorization
    service.authorization = TeachingAuthorization(service)
    background = TeachingBackground(service)
    class ImmediatePool:
        def submit(self, fn, *args):
            result = Future()
            result.set_result(fn(*args))
            return result
        def shutdown(self, **kwargs):
            pass
    background.pools["refine"].shutdown(wait=False)
    background.pools["refine"] = ImmediatePool()
    try:
        background.tick()
        assert executed == [ident]
        assert checked == [ident]
        assert background.pending[ident][0] == "refine"
        assert second not in background.pending
    finally:
        background.close()


def test_background_expires_refinement_without_running_it(env):
    from backend.teaching.background import TeachingBackground
    ident = refinement(env)
    mutate(env, LearningTaskRow, ident, status="queued", deadline_at=utcnow()-timedelta(seconds=1))
    preparation = SimpleNamespace(refinement_eligible=lambda *args: pytest.fail("expired task checked"),
        reconcile=lambda **kwargs: None)
    service = SimpleNamespace(db=env[0], tasks=env[1], preparation=preparation, observed_sessions={},
        tts_adapter_factory=None, disclosures=None, configs=None)
    from backend.teaching.authorization import TeachingAuthorization
    service.authorization = TeachingAuthorization(service)
    background = TeachingBackground(service)
    try:
        background.tick()
        with env[0].session() as sess:
            assert sess.get(LearningTaskRow, ident).status == "expired"
        assert not background.pending
    finally:
        background.close()
