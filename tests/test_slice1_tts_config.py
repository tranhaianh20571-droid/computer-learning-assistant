"""Saved TTS uses real task leases and exact step/prewarm disclosure scope."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
import json
import uuid

import httpx
import pytest

from backend.logging_audit.db import Database
from backend.logging_audit.models import install_audit_triggers
from backend.logging_audit.repositories import PgAuditLog, PgTaskEventStore
from backend.planning.models import ConversationRow, CourseRow
from backend.slice1.config_service import ConfigService
from backend.slice1.disclosure_service import DisclosureService
from backend.slice1.errors import Slice1Error
from backend.tasks.service import LearningTaskRow, LeaseError, TaskService, utcnow
from backend.teaching.learning_models import PreparedFirstSegmentRow
from backend.teaching.models import TeachingStepRow
from backend.teaching.outputs import StepCandidate
from backend.teaching.tts_config import configured_tts_factory

TEXT = "Synthetic teaching text bound to one immutable task and one approved scope. " * 2


@pytest.fixture(scope="module")
def database():
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    yield db
    db.dispose()


@pytest.fixture()
def bound(database, monkeypatch):
    audit = PgAuditLog(database)
    configs = ConfigService(database, encryption_key="synthetic-tts-config-key")
    disclosures = DisclosureService(database, audit)
    tasks = TaskService(database, PgTaskEventStore(database))
    built, sent = [], []

    def no_network(*args, **kwargs):
        raise AssertionError("synthetic TTS tests must not send network requests")

    monkeypatch.setattr(httpx.Client, "send", no_network)

    def build(protocol, **options):
        built.append((protocol, options))
        assert protocol == "minimax"
        assert options["voice_id"] == "synthetic-voice"
        assert options["credentials"] == {"api_key": "synthetic-key", "voice_id": "synthetic-voice"}
        def synthesize(text, **kwargs):
            sent.append((text, kwargs))
            return {"audio_hex": b"synthetic-audio".hex()}
        return SimpleNamespace(synthesize=synthesize, close=options["client"].close)

    monkeypatch.setattr("backend.teaching.tts_config.build_adapter", build)

    def make(kind="tts", *, grant=True, scope_override=None, claim=True):
        suffix = uuid.uuid4().hex[:12]
        owner = f"user_{suffix}"
        view = configs.create(
            actor_id=owner, is_admin=False, kind="tts", protocol="minimax",
            endpoint="https://voice.example.test", model_name="speech-2.8-hd",
            credentials={"api_key": "synthetic-key", "voice_id": "synthetic-voice"},
        )
        course, lesson, outline = f"course_{suffix}", f"lesson_{suffix}", f"outline_{suffix}"
        with database.session() as sess:
            sess.add(CourseRow(course_id=course, owner_user_id=owner, current_outline_version_id=outline))
        scope = {"course_id": course, "lesson_id": lesson, "outline_version_id": outline}
        if kind == "tts":
            conv = f"conv_{suffix}"
            with database.session() as sess:
                sess.add(ConversationRow(conversation_id=conv, owner_user_id=owner, kind="teaching", generation=1,
                    course_id=course, lesson_id=lesson, outline_version_id=outline))
                sess.flush()
                sess.add(TeachingStepRow(
                    step_id=f"step_{suffix}", conversation_id=conv, generation=1,
                    step_no=2, text_version_no=3, text=TEXT, status="ready",
                ))
            subject = f"{conv}:2:1"
        else:
            subject = course
        task = tasks.create_task(
            owner_user_id=owner, kind=kind, subject_id=subject,
            idempotency_key=f"synthetic-tts-{suffix}", stage="tts",
            model_config_version=f"{view['config_id']}:{view['config_version']}",
        )["task_id"]
        if kind == "lesson_first_prepare":
            candidate = StepCandidate(text=TEXT, title="Synthetic lesson", board={"title": "Synthetic board"})
            with database.session() as sess:
                sess.add(PreparedFirstSegmentRow(
                    cache_id=f"cache_{suffix}", owner_user_id=owner, course_id=course,
                    lesson_id=lesson, task_id=task, text_key=f"text_{suffix}",
                    context_json=json.dumps({"outline_version_id": outline}), plan_json="{}", candidate_json=candidate.model_dump_json(),
                    expires_at=utcnow() + timedelta(hours=1),
                ))
        lease = tasks.claim(task, lease_owner=f"synthetic-worker-{suffix}") if claim else None
        approved = disclosures.create(
            actor_id=owner, task_id=task, config_id=view["config_id"], content_category="audio_text",
            scope_snapshot=scope if scope_override is None else scope_override,
        ) if grant else None
        return SimpleNamespace(
            owner=owner, view=view, task=task, scope=scope, lease=lease, grant=approved,
            factory=configured_tts_factory(configs, disclosures), configs=configs,
            disclosures=disclosures, tasks=tasks, db=database, built=built, sent=sent,
        )

    yield make
    for _, options in built:
        options["client"].close()


@pytest.mark.parametrize("kind", ["tts", "lesson_first_prepare"])
def test_real_step_or_prewarm_scope_allows_exact_text_only(bound, kind):
    state = bound(kind)
    adapter = state.factory(state.owner, state.task)
    assert adapter.task_id == state.task
    assert adapter.config_id == state.view["config_id"] and adapter.config_version == 1
    assert adapter.synthesize(TEXT, synthetic_option=True)["audio_hex"] == b"synthetic-audio".hex()
    assert state.sent == [(TEXT, {"synthetic_option": True})]
    assert state.disclosures.verify(
        task_id=state.task, config_id=state.view["config_id"], content_category="audio_text",
        actor_id=state.owner, scope_snapshot=state.scope,
    )["scope_snapshot"] == state.scope
    with pytest.raises(Slice1Error) as exc:
        adapter.synthesize(TEXT + " unapproved text")
    assert exc.value.code == "disclosure_required"
    assert len(state.sent) == 1


@pytest.mark.parametrize("kind", ["tts", "lesson_first_prepare"])
def test_missing_or_uncovered_scope_prevents_adapter_creation(bound, kind):
    state = bound(kind, scope_override={"unrelated": "synthetic"})
    with pytest.raises(Slice1Error) as exc:
        state.factory(state.owner, state.task)
    assert exc.value.code == "disclosure_required"
    assert not state.built and not state.sent


def test_no_grant_prevents_adapter_creation(bound):
    state = bound(grant=False)
    with pytest.raises(Slice1Error) as exc:
        state.factory(state.owner, state.task)
    assert exc.value.code == "disclosure_required"
    assert not state.built


@pytest.mark.parametrize("kind", ["tts", "lesson_first_prepare"])
def test_revocation_after_factory_is_rechecked_before_sending(bound, kind):
    state = bound(kind)
    adapter = state.factory(state.owner, state.task)
    state.disclosures.revoke(state.grant["grant_id"], actor_id=state.owner)
    with pytest.raises(Slice1Error) as exc:
        adapter.synthesize(TEXT)
    assert exc.value.code == "disclosure_revoked"
    assert not state.sent


def test_config_change_after_factory_is_rechecked_before_sending(bound):
    state = bound()
    adapter = state.factory(state.owner, state.task)
    state.configs.update(state.view["config_id"], actor_id=state.owner, is_admin=False, model_name="synthetic-new-model")
    with pytest.raises(Slice1Error) as exc:
        adapter.synthesize(TEXT)
    assert exc.value.code == "config_not_found"
    assert not state.sent


@pytest.mark.parametrize("change,code", [("lease", "LEASE_LOST"), ("cancel", "TASK_CANCELLED"), ("deadline", "TASK_EXPIRED")])
def test_current_task_fences_are_rechecked_before_sending(bound, change, code):
    state = bound()
    adapter = state.factory(state.owner, state.task)
    with state.db.session() as sess:
        row = sess.get(LearningTaskRow, state.task)
        if change == "lease":
            row.lease_token = "synthetic-replaced-token"
        elif change == "cancel":
            row.cancel_requested = True
        else:
            row.deadline_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(LeaseError) as exc:
        adapter.synthesize(TEXT)
    assert exc.value.code == code
    assert not state.sent


def test_unclaimed_task_is_not_authorized_by_grant_alone(bound):
    state = bound(claim=False)
    with pytest.raises(LeaseError) as exc:
        state.factory(state.owner, state.task)
    assert exc.value.code == "LEASE_LOST"
    assert not state.built


def test_task_config_version_fence_prevents_adapter_creation(bound):
    state = bound()
    with state.db.session() as sess:
        sess.get(LearningTaskRow, state.task).model_config_version = f"{state.view['config_id']}:2"
    with pytest.raises(Slice1Error) as exc:
        state.factory(state.owner, state.task)
    assert exc.value.code == "config_not_found"
    assert not state.built


def test_foreign_task_cannot_use_known_owner_config(bound):
    owner, stranger = bound(), bound()
    with pytest.raises(Slice1Error) as exc:
        owner.factory(owner.owner, stranger.task)
    assert exc.value.code == "config_not_found"
    assert not owner.built and not owner.sent


@pytest.mark.parametrize("kind", ["tts", "lesson_first_prepare"])
def test_task_authorization_hook_runs_at_creation_and_before_each_send(bound, kind):
    state = bound(kind)
    checked = []
    def authorize_task(task_id, *, audio=False):
        assert task_id == state.task and audio is True
        state.tasks.validate_lease(state.lease, actor_id=state.owner)
        state.disclosures.verify_or_audit(task_id=task_id, config_id=state.view["config_id"],
            actor_id=state.owner, content_category="audio_text", scope_snapshot=state.scope)
        checked.append((task_id, audio))
    state.factory.authorize_task = authorize_task
    adapter = state.factory(state.owner, state.task)
    assert checked == [(state.task, True)]
    adapter.synthesize(TEXT)
    assert checked == [(state.task, True)] * 2
    assert len(state.sent) == 1


@pytest.mark.parametrize("phase", ["creation", "send"])
@pytest.mark.parametrize("code", ["disclosure_required", "disclosure_revoked", "disclosure_expired"])
def test_task_authorization_hook_failure_prevents_external_audio(bound, phase, code):
    from backend.slice1.errors import error
    state = bound()
    adapter = state.factory(state.owner, state.task) if phase == "send" else None
    def deny(task_id, *, audio=False):
        assert task_id == state.task and audio is True
        raise error(code)
    state.factory.authorize_task = deny
    with pytest.raises(Slice1Error) as caught:
        if adapter:
            adapter.synthesize(TEXT)
        else:
            state.factory(state.owner, state.task)
    assert caught.value.code == code
    assert not state.sent
    assert len(state.built) == (1 if adapter else 0)


@pytest.mark.parametrize("change", ["text", "text_version"])
def test_current_text_and_version_are_rechecked_before_audio_send(bound, change):
    state = bound()
    adapter = state.factory(state.owner, state.task)
    with state.db.session() as sess:
        task = sess.get(LearningTaskRow, state.task)
        conversation_id = task.subject_id.split(":")[0]
        from sqlalchemy import select
        step = sess.scalar(select(TeachingStepRow).where(TeachingStepRow.conversation_id == conversation_id))
        if change == "text":
            step.text = TEXT + " changed after authorization"
        else:
            step.text_version_no += 1
    with pytest.raises(Slice1Error) as caught:
        adapter.synthesize(TEXT)
    assert caught.value.code == "disclosure_required"
    assert not state.sent
