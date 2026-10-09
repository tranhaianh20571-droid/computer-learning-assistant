"""Explicit lesson consent on an owned disposable PostgreSQL schema only."""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic_ai.models.test import TestModel
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from backend.logging_audit.db import Database, get_database_url
from backend.logging_audit.repositories import PgTaskEventStore
from backend.materials.models import MaterialRow
from backend.planning.models import (ConversationRow, CourseRow, CourseUnitRow, LessonRow,
    OutlineVersionRow, PlanningTaskStateRow, utcnow)
from backend.prompts.service import PromptService
from backend.slice1.disclosure_service import DisclosureService
from backend.slice1.errors import Slice1Error
from backend.slice1.models import DisclosureGrantRow, ServiceConfigRow
from backend.tasks.service import LearningTaskRow, TaskService
from backend.teaching.authorization import LessonAuthorizationRow
from backend.teaching.background import TeachingBackground
from backend.teaching.content_stream import read_snapshot
from backend.teaching.learning_models import LessonBlueprintRow, PreparedFirstSegmentRow
from backend.teaching.models import TeachingStepRow
from backend.teaching.service import TeachingService


@pytest.fixture
def env(monkeypatch):
    url = make_url(get_database_url())
    if not str(url.query.get("options", "")).startswith("-csearch_path=native_validation_"):
        pytest.skip("requires tests/run_native_regression.py disposable schema")
    db = Database()
    suffix = uuid.uuid4().hex[:10]
    owner, course, outline, unit, lesson = [name + suffix for name in ("owner_", "course_", "outline_", "unit_", "lesson_")]
    content, voice = "content_" + suffix, "voice_" + suffix
    with db.session() as sess:
        sess.add(CourseRow(course_id=course, owner_user_id=owner, title_ref="Synthetic course", current_outline_version_id=outline))
        sess.flush()
        sess.add(OutlineVersionRow(outline_version_id=outline, course_id=course))
        sess.flush()
        sess.add(CourseUnitRow(unit_id=unit, outline_version_id=outline, title_ref="Synthetic unit"))
        sess.flush()
        sess.add(LessonRow(lesson_id=lesson, outline_version_id=outline, unit_id=unit,
            title_ref="Synthetic lesson", goal_ref="Understand lookup"))
        sess.add(ServiceConfigRow(config_id=content, owner_user_id=owner, kind="content_model", protocol="openai",
            endpoint="https://model.example.test/v1", model_name="synthetic"))
        sess.add(ServiceConfigRow(config_id=voice, owner_user_id=owner, kind="tts", protocol="minimax",
            endpoint="https://voice.example.test", model_name="synthetic-voice"))
    calls = []
    def describe(current, kind):
        with db.session() as sess:
            row = sess.get(ServiceConfigRow, content if kind == "content_model" else voice)
            return {"config_id": row.config_id, "config_version": row.config_version,
                "protocol": row.protocol} if current == owner and row.is_active else None
    def model_factory(current, ident):
        calls.append(("content", ident))
        return TestModel(custom_output_args={"steps": [{"title": "Synthetic step",
            "text": "A unique key identifies a value. Verify the key before reading the value, then compare the returned value with the expected result. Repeat with a new key to check the rule.",
            "board": {"kind": "board", "title": "Lookup", "points": ["Key identifies value"], "nodes": [], "edges": [], "original_refs": []},
            "phase": "intuition", "covered_goal_ids": ["g1"], "lesson_finished": False}]})
    model_factory.config_for_owner = lambda current: describe(current, "content_model")
    def tts_factory(current, ident):
        calls.append(("tts", ident))
        return SimpleNamespace(synthesize=lambda text: {"audio_hex": b"synthetic audio".hex(),
            "subtitle": [{"begin_ms": 0, "end_ms": 1000, "text": text}]}, close=lambda: None)
    tts_factory.config_for_owner = lambda current: describe(current, "tts")
    tts_factory.uses_saved_config = True
    events = PgTaskEventStore(db)
    tasks = TaskService(db, events)
    blobs = {}
    storage = SimpleNamespace(put=lambda key, value: blobs.update({key: value}),
        get=lambda key: blobs[key], delete=lambda key: blobs.pop(key, None))
    service = TeachingService(db, tasks, events, PromptService(db), storage,
        model_factory, tts_factory, disclosures=DisclosureService(db))
    tts_factory.disclosures = service.disclosures
    service.configs = SimpleNamespace(latest_active_for_owner=describe)
    try:
        yield SimpleNamespace(service=service, db=db, owner=owner, course=course, lesson=lesson,
            content=content, voice=voice, calls=calls, blobs=blobs)
    finally:
        db.dispose()


def approve(env, *, purpose="start", conversation_id=None, tts=False, prewarm=False):
    service = env.service
    preflight = service.preflight_lesson(env.course, env.lesson, actor_id=env.owner,
        purpose=purpose, conversation_id=conversation_id)
    return service.confirm_lesson(env.course, env.lesson, actor_id=env.owner,
        purpose=purpose, conversation_id=conversation_id, fingerprint=preflight["fingerprint"],
        content_confirmed=True, tts_confirmed=tts, prewarm_opt_in=prewarm)


def consent(env):
    with env.db.session() as sess:
        return sess.scalars(select(LessonAuthorizationRow).where(
            LessonAuthorizationRow.owner_user_id == env.owner).order_by(LessonAuthorizationRow.created_at.desc())).first()


def test_start_without_consent_creates_no_task_or_conversation(env):
    with pytest.raises(Slice1Error) as caught:
        env.service.start_lesson(env.course, env.lesson, actor_id=env.owner)
    assert caught.value.code == "disclosure_required"
    with env.db.session() as sess:
        assert not sess.scalar(select(ConversationRow.conversation_id).where(ConversationRow.owner_user_id == env.owner))
        assert not sess.scalar(select(LearningTaskRow.task_id).where(LearningTaskRow.owner_user_id == env.owner))
    assert env.calls == []


@pytest.mark.parametrize("change", ["config", "context", "template"])
def test_preflight_change_requires_review_again(env, monkeypatch, change):
    view = env.service.preflight_lesson(env.course, env.lesson, actor_id=env.owner)
    if change == "template":
        old = env.service.prompts.load_local_template
        monkeypatch.setattr(env.service.prompts, "load_local_template", lambda name: old(name) | {"sha256": "a" * 64})
    else:
        with env.db.session() as sess:
            if change == "config": sess.get(ServiceConfigRow, env.content).config_version += 1
            else: sess.get(LessonRow, env.lesson).goal_ref = "Changed reviewed context"
    with pytest.raises(Slice1Error) as caught:
        env.service.confirm_lesson(env.course, env.lesson, actor_id=env.owner,
            fingerprint=view["fingerprint"], content_confirmed=True)
    assert caught.value.code == "disclosure_required"
    assert env.calls == []


def test_content_confirmation_does_not_approve_tts_or_automatic_prewarm(env):
    start = approve(env)
    root = consent(env)
    assert root.content_root_grant_id and root.tts_root_grant_id is None
    assert env.service.preparation.prepare_course(env.course, env.owner)["status"] == "authorization_required"
    assert env.service.run_step_generation(start["task_id"])["status"] == "succeeded"
    with pytest.raises(Slice1Error) as caught:
        env.service.ensure_audio(start["conversation_id"], actor_id=env.owner, step_no=1)
    assert caught.value.code == "disclosure_required"
    assert not any(kind == "tts" for kind, _ in env.calls)
    with env.db.session() as sess:
        following = sess.scalars(select(LearningTaskRow).where(LearningTaskRow.owner_user_id == env.owner,
            LearningTaskRow.subject_id == start["conversation_id"] + ":2:1")).one()
        child = sess.scalar(select(DisclosureGrantRow).where(DisclosureGrantRow.task_id == following.task_id))
        assert child.parent_grant_id == root.content_root_grant_id
        assert json.loads(child.scope_snapshot) == {"material_ids": []}


@pytest.mark.parametrize("mutation,code", [("revoke", "disclosure_revoked"), ("expire", "disclosure_expired"),
    ("version", "disclosure_required"), ("endpoint", "disclosure_revoked")])
def test_worker_rechecks_approval_before_factory_and_has_recovery_action(env, mutation, code):
    start = approve(env)
    root = consent(env)
    with env.db.session() as sess:
        grant = sess.get(DisclosureGrantRow, root.content_root_grant_id)
        if mutation == "revoke": grant.revoked_at = utcnow()
        elif mutation == "expire": grant.expires_at = utcnow() - timedelta(seconds=1)
        elif mutation == "version": sess.get(ServiceConfigRow, env.content).config_version += 1
        else: sess.get(ServiceConfigRow, env.content).endpoint = "https://changed.example.test/v1"
    result = env.service.run_step_generation(start["task_id"])
    assert result == {"status": "failed", "error_code": code.upper()}
    assert env.calls == []
    snapshot = read_snapshot(env.service, start["task_id"], env.owner)
    assert snapshot["event"] == "error" and snapshot["required_action"] == "authorize"
    view = env.service.get_workspace(start["conversation_id"], actor_id=env.owner)
    assert view["authorization_required"] and view["required_action"] == "authorize"
    recovery = approve(env, purpose="recover", conversation_id=start["conversation_id"])
    assert recovery["task_id"] == start["task_id"] and recovery["status"] == "queued"
    assert env.service.run_step_generation(start["task_id"])["status"] == "succeeded"


def test_tts_confirmation_is_separate_and_root_revocation_fences_audio_factory(env):
    start = approve(env, tts=True)
    assert env.service.run_step_generation(start["task_id"])["status"] == "succeeded"
    task = env.service.ensure_audio(start["conversation_id"], actor_id=env.owner, step_no=1)["task_id"]
    root = consent(env)
    env.service.disclosures.revoke(root.tts_root_grant_id, actor_id=env.owner)
    result = env.service.run_tts(task)
    assert result == {"status": "failed", "error_code": "DISCLOSURE_REVOKED"}
    assert not any(kind == "tts" for kind, _ in env.calls)
    recovery = approve(env, purpose="recover", conversation_id=start["conversation_id"], tts=True)
    assert recovery["status"] == "queued"
    assert env.service.run_tts(task)["status"] == "succeeded"


def test_prewarm_requires_explicit_opt_in_and_exact_lesson_scope(env):
    view = env.service.preflight_lesson(env.course, env.lesson, actor_id=env.owner, purpose="prewarm")
    with pytest.raises(Slice1Error) as caught:
        env.service.confirm_lesson(env.course, env.lesson, actor_id=env.owner, purpose="prewarm",
            fingerprint=view["fingerprint"], content_confirmed=True)
    assert caught.value.code == "disclosure_required"
    task = approve(env, purpose="prewarm", prewarm=True)["task_id"]
    root = consent(env)
    assert root.prewarm_opt_in and root.purpose == "prewarm"
    with env.db.session() as sess:
        grant = sess.get(DisclosureGrantRow, root.content_root_grant_id)
        assert json.loads(grant.scope_snapshot)["lesson_id"] == env.lesson
        assert not sess.scalar(select(ConversationRow.conversation_id).where(ConversationRow.owner_user_id == env.owner))
    assert env.service.preparation.run(task)["status"] == "succeeded"
    assert not any(kind == "tts" for kind, _ in env.calls)


def test_task_and_grant_confirmation_rollback_together(env, monkeypatch):
    original = env.service.authorization.record
    def broken(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic post-grant failure")
    monkeypatch.setattr(env.service.authorization, "record", broken)
    with pytest.raises(RuntimeError, match="synthetic post-grant failure"):
        approve(env)
    with env.db.session() as sess:
        assert not sess.scalar(select(LearningTaskRow.task_id).where(LearningTaskRow.owner_user_id == env.owner))
        assert not sess.scalar(select(DisclosureGrantRow.grant_id).where(DisclosureGrantRow.owner_user_id == env.owner))
        assert not sess.scalar(select(LessonAuthorizationRow.authorization_id).where(LessonAuthorizationRow.owner_user_id == env.owner))
        assert not sess.scalar(select(ConversationRow.conversation_id).where(ConversationRow.owner_user_id == env.owner))


def test_next_refinement_and_followup_require_current_root_consent(env):
    start = approve(env)
    assert env.service.run_step_generation(start["task_id"])["status"] == "succeeded"
    root = consent(env)
    env.service.disclosures.revoke(root.content_root_grant_id, actor_id=env.owner)
    with pytest.raises(Slice1Error):
        env.service.followup(start["conversation_id"], actor_id=env.owner, content="Synthetic question", idempotency_key="question")
    assert env.service.preparation.queue_refinement(start["conversation_id"], env.owner) == {
        "status": "authorization_required", "error_code": "disclosure_revoked"}
    with env.db.session() as sess:
        next_task = sess.scalar(select(LearningTaskRow).where(LearningTaskRow.subject_id == start["conversation_id"] + ":2:1"))
    before = len(env.calls)
    assert env.service.run_step_generation(next_task.task_id)["error_code"] == "DISCLOSURE_REVOKED"
    assert len(env.calls) == before


def test_expired_queue_is_terminal_without_running_factory_and_recoverable(env):
    start = approve(env)
    with env.db.session() as sess:
        sess.get(LearningTaskRow, start["task_id"]).deadline_at = utcnow() - timedelta(seconds=1)
    snapshot = read_snapshot(env.service, start["task_id"], env.owner)
    assert snapshot["status"] == "expired" and snapshot["required_action"] == "retry"
    assert env.calls == []
    assert approve(env, purpose="recover", conversation_id=start["conversation_id"])["status"] == "queued"


@pytest.mark.parametrize("mutation", ["context", "blueprint", "material_delete", "material_hash"])
def test_material_approval_is_exact_and_context_mutation_fails_before_model(env, mutation):
    material = "material_" + uuid.uuid4().hex[:10]
    state = "state_" + uuid.uuid4().hex[:10]
    with env.db.session() as sess:
        sess.add(MaterialRow(material_id=material, owner_user_id=env.owner, filename_hash="a"*64,
            source_bytes_sha256="b"*64, kind="txt", size_bytes=1))
        sess.add(PlanningTaskStateRow(task_id=state, owner_user_id=env.owner, target_kind="course",
            target_id=env.course, material_ids_json=json.dumps([material])))
    start = approve(env)
    root = consent(env)
    with env.db.session() as sess:
        grant = sess.get(DisclosureGrantRow, root.content_root_grant_id)
        assert grant.content_category == "material" and json.loads(grant.scope_snapshot)["material_ids"] == [material]
        if mutation == "context": sess.get(PlanningTaskStateRow, state).intake_json = json.dumps({"answers": [{"answer": "Changed profile"}]})
        elif mutation == "blueprint":
            blueprint = sess.get(LessonBlueprintRow, start["conversation_id"])
            blueprint.context_json = json.dumps(json.loads(blueprint.context_json) | {"lesson_goal": "Unreviewed goal"})
        elif mutation == "material_delete": sess.get(MaterialRow, material).deleted_at = utcnow()
        else: sess.get(MaterialRow, material).source_bytes_sha256 = "c"*64
    assert env.service.run_step_generation(start["task_id"])["status"] == "failed"
    assert env.calls == []


def test_revocation_during_sdk_retry_stops_next_wire_and_publication(env):
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel
    start = approve(env)
    root = consent(env)
    wires = []
    def function(messages, info):
        wires.append(1)
        env.service.disclosures.revoke(root.content_root_grant_id, actor_id=env.owner)
        return ModelResponse(parts=[TextPart("synthetic invalid output")])
    original = env.service.model_adapter_factory
    def factory(owner, task_id):
        env.calls.append(("content", task_id))
        return FunctionModel(function)
    factory.config_for_owner = original.config_for_owner
    env.service.model_adapter_factory = factory
    assert env.service.run_step_generation(start["task_id"])["error_code"] == "DISCLOSURE_REVOKED"
    assert len(wires) == 1
    with env.db.session() as sess:
        assert not sess.scalar(select(TeachingStepRow.step_id).where(TeachingStepRow.conversation_id == start["conversation_id"]))


def test_failed_prewarm_can_be_reviewed_and_retried_with_new_root(env):
    first = approve(env, purpose="prewarm", prewarm=True)
    root = consent(env)
    env.service.disclosures.revoke(root.content_root_grant_id, actor_id=env.owner)
    assert env.service.preparation.run(first["task_id"])["status"] == "failed"
    retry = approve(env, purpose="prewarm", prewarm=True)
    assert retry["status"] == "queued" and retry["task_id"] != first["task_id"]
    assert consent(env).content_root_grant_id != root.content_root_grant_id
    assert env.service.preparation.run(retry["task_id"])["status"] == "succeeded"


def test_background_marks_revoked_queued_task_failed_without_attempt_or_factory(env):
    start = approve(env)
    env.service.disclosures.revoke(consent(env).content_root_grant_id, actor_id=env.owner)
    worker = TeachingBackground(env.service)
    try:worker.tick()
    finally:worker.close()
    with env.db.session() as sess:
        task = sess.get(LearningTaskRow, start["task_id"])
        assert task.status == "failed" and task.attempt == 0 and task.lease_token is None
    snapshot = read_snapshot(env.service, start["task_id"], env.owner)
    assert snapshot["required_action"] == "authorize" and snapshot["error_code"] == "DISCLOSURE_REVOKED"
    assert env.calls == []


@pytest.mark.parametrize("replace_child", [False, True])
def test_unrelated_newer_grant_cannot_replace_lesson_approval(env, replace_child):
    start = approve(env)
    root = consent(env)
    task_id = start["task_id"]
    if replace_child:
        assert env.service.run_step_generation(task_id)["status"] == "succeeded"
        with env.db.session() as sess:
            task_id = sess.scalar(select(LearningTaskRow.task_id).where(
                LearningTaskRow.subject_id == start["conversation_id"] + ":2:1"))
    env.service.disclosures.create(actor_id=env.owner, task_id=task_id, config_id=env.content,
        content_category="query", scope_snapshot={"material_ids": []})
    if not replace_child: env.service.disclosures.revoke(root.content_root_grant_id, actor_id=env.owner)
    before = len(env.calls)
    result = env.service.run_step_generation(task_id)
    assert result["status"] == "failed" and result["error_code"] in ("DISCLOSURE_REVOKED", "DISCLOSURE_REQUIRED")
    assert len(env.calls) == before


def test_content_only_start_reuses_warm_text_but_not_audio_without_voice_confirmation(env):
    warm = approve(env, purpose="prewarm", prewarm=True, tts=True)
    assert env.service.preparation.run(warm["task_id"])["status"] == "succeeded"
    with env.db.session() as sess:
        cache = sess.scalar(select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.task_id == warm["task_id"]))
        assert cache.audio_object_key and cache.audio_bytes
    content_calls = sum(kind == "content" for kind, _ in env.calls)
    start = approve(env, tts=False)
    assert start["cache_hit"]
    with env.db.session() as sess:
        step = sess.scalar(select(TeachingStepRow).where(TeachingStepRow.conversation_id == start["conversation_id"],
            TeachingStepRow.step_no == 1))
        assert step and step.audio_id is None
    assert sum(kind == "content" for kind, _ in env.calls) == content_calls
    with pytest.raises(Slice1Error):
        env.service.ensure_audio(start["conversation_id"], actor_id=env.owner, step_no=1)


def test_tts_retry_rechecks_selected_audio_root_without_discarding_approved_text(env):
    warm = approve(env, purpose="prewarm", prewarm=True, tts=True)
    root = consent(env)
    calls = []

    def synthesize(text):
        calls.append(text)
        env.service.disclosures.revoke(root.tts_root_grant_id, actor_id=env.owner)
        raise Slice1Error("timeout")

    def factory(owner, task_id):
        return SimpleNamespace(synthesize=synthesize, close=lambda: None)

    previous = env.service.tts_adapter_factory
    factory.config_for_owner = previous.config_for_owner
    factory.uses_saved_config = True
    factory.disclosures = env.service.disclosures
    env.service.tts_adapter_factory = factory
    assert env.service.preparation.run(warm["task_id"])["status"] == "succeeded"
    assert len(calls) == 1
    with env.db.session() as sess:
        cache = sess.scalar(select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.task_id == warm["task_id"]))
        assert cache.candidate_json != "{}"
        assert cache.status == "text_ready" and not cache.audio_object_key and not cache.audio_bytes


def test_idempotent_audio_lookup_preserves_revoked_child_until_explicit_reconfirmation(env):
    start = approve(env, tts=True)
    assert env.service.run_step_generation(start["task_id"])["status"] == "succeeded"
    created = env.service.ensure_audio(start["conversation_id"], actor_id=env.owner, step_no=1)
    with env.db.session() as sess:
        child = sess.scalar(select(DisclosureGrantRow).where(DisclosureGrantRow.task_id == created["task_id"],
            DisclosureGrantRow.content_category == "audio_text"))
        child_id, parent_id = child.grant_id, child.parent_grant_id
        step = sess.scalar(select(TeachingStepRow).where(TeachingStepRow.conversation_id == start["conversation_id"],
            TeachingStepRow.step_no == 1))
        original_text, original_version = step.text, step.text_version_no
    env.service.disclosures.revoke(child_id, actor_id=env.owner)
    calls_before = len(env.calls)
    with pytest.raises(Slice1Error) as caught:
        env.service.ensure_audio(start["conversation_id"], actor_id=env.owner, step_no=1)
    assert caught.value.code == "disclosure_revoked"
    assert len(env.calls) == calls_before
    with env.db.session() as sess:
        assert sess.scalar(select(func.count()).select_from(DisclosureGrantRow).where(
            DisclosureGrantRow.task_id == created["task_id"], DisclosureGrantRow.content_category == "audio_text")) == 1
        assert sess.get(DisclosureGrantRow, parent_id).revoked_at is None
        step = sess.scalar(select(TeachingStepRow).where(TeachingStepRow.conversation_id == start["conversation_id"],
            TeachingStepRow.step_no == 1))
        assert step.audio_id is None and step.text == original_text and step.text_version_no == original_version
    approve(env, purpose="recover", conversation_id=start["conversation_id"], tts=True)
    recovered = env.service.ensure_audio(start["conversation_id"], actor_id=env.owner, step_no=1)
    assert recovered["task_id"] == created["task_id"]
    with env.db.session() as sess:
        assert sess.scalar(select(func.count()).select_from(DisclosureGrantRow).where(
            DisclosureGrantRow.task_id == created["task_id"], DisclosureGrantRow.content_category == "audio_text")) == 2


def test_workspace_has_durable_lease_deadline_and_recovery_metadata(env):
    start = approve(env, tts=True)
    assert env.service.run_step_generation(start["task_id"])["status"] == "succeeded"
    audio = env.service.ensure_audio(start["conversation_id"], actor_id=env.owner, step_no=1)
    lease = env.service.tasks.claim(audio["task_id"], lease_owner="synthetic-recovery", lease_seconds=60)
    workspace = env.service.get_workspace(start["conversation_id"], actor_id=env.owner)
    queued = next(task for task in workspace["queued_tasks"] if task["task_id"] == audio["task_id"])
    assert queued["status"] == "leased" and queued["attempt"] == 1
    assert queued["deadline_at"] and queued["lease_until"]
    assert queued["generation"] == lease.generation and queued["revision"] >= 1
    assert not queued["retryable"] and not queued["recovery"]["available"]
    env.service.tasks.recover_worker_failure(lease, stage="tts", error_code="MODEL_TIMEOUT")
    workspace = env.service.get_workspace(start["conversation_id"], actor_id=env.owner)
    failed = next(task for task in workspace["queued_tasks"] if task["task_id"] == audio["task_id"])
    assert failed["status"] == "failed" and failed["error_code"] == "MODEL_TIMEOUT"
    assert failed["lease_until"] is None and failed["deadline_at"] == queued["deadline_at"]
    assert failed["retryable"] and failed["required_action"] == "retry"
    assert failed["recovery"] == {"available": True, "requires_confirmation": True, "action": "retry"}
    repeated = env.service.ensure_audio(start["conversation_id"], actor_id=env.owner, step_no=1)
    assert repeated == {"status": "failed", "task_id": audio["task_id"]}
