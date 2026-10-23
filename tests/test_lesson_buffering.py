"""Continuous lesson buffering against real isolated SQL and synthetic providers."""
from datetime import timedelta
from time import monotonic

import pytest
from sqlalchemy import select

from backend.logging_audit.models import TaskStateRow
from backend.characters.models import ClassroomPlaybackRow
from backend.planning.models import ConversationRow, utcnow
from backend.prompts.service import PromptBindingRow
from backend.tasks.service import LearningTaskRow
from backend.slice1.models import ServiceConfigRow
from backend.teaching.background import TeachingBackground
from backend.teaching.models import TeachingStepRow
from backend.teaching.recovery import recover_observed
from test_native_teaching_authorization import isolated_scheduler, approve  # noqa: F401
from test_teaching_assessments import LearningAdapter


@pytest.fixture
def buffered(isolated_scheduler, monkeypatch):
    env = isolated_scheduler
    original = env.service.model_adapter_factory
    def factory(owner, task):
        return LearningAdapter()
    factory.config_for_owner = original.config_for_owner
    env.service.model_adapter_factory = factory
    monkeypatch.setattr(env.service.preparation, "refinement_eligible", lambda *args: False)
    monkeypatch.setattr(env.service.preparation, "reconcile", lambda **kwargs: [])
    monkeypatch.setattr(env.service.feynman, "kick", lambda task: None)
    start = approve(env, tts=True)
    env.conv = start["conversation_id"]
    env.service.observed_sessions[env.conv] = monotonic()
    env.background = TeachingBackground(env.service)
    try:
        yield env, start
    finally:
        env.background.close()


def task_for(env, number, kind="lesson_step_generate"):
    with env.db.session() as sess:
        return sess.scalar(select(LearningTaskRow).where(
            LearningTaskRow.subject_id == f"{env.conv}:{number}:1",
            LearningTaskRow.kind == kind,
            LearningTaskRow.cancel_requested.is_(False))
            .order_by(LearningTaskRow.created_at.desc()))


def generate_until(env, start, last):
    for number in range(1, last + 1):
        task = start["task_id"] if number == 1 else task_for(env, number).task_id
        assert env.service.run_step_generation(task)["status"] == "succeeded"


def checkpoint(env, number=1, *, paused=False, seq=1):
    env.service.save_checkpoint(env.conv, actor_id=env.owner, client_seq=seq,
        step_no=number, position_ms=0,
        board_state={"presentation": {"paused": paused, "text_complete": True}})


def test_real_dispatch_buffers_text_board_and_audio_without_playback_advance(buffered):
    env, start = buffered
    assert env.service.run_step_generation(start["task_id"])["status"] == "succeeded"
    for _ in range(16):
        env.background.tick()
        for _, future, _ in list(env.background.pending.values()):
            assert future.result(timeout=20)["status"] == "succeeded"
    with env.db.session() as sess:
        steps = list(sess.scalars(select(TeachingStepRow).where(
            TeachingStepRow.conversation_id == env.conv).order_by(TeachingStepRow.step_no)))
        assert [step.step_no for step in steps] == list(range(1, 7))
        assert all(step.board_scene_id and step.audio_id for step in steps)
        assert not task_for(env, 7)
    view = env.service.get_workspace(env.conv, actor_id=env.owner)
    assert view["last_checkpoint"] is None
    assert all(step["audio_ready"] for step in view["steps"])


def test_feynman_boundary_stops_then_skip_resumes_to_lesson_end(buffered):
    env, start = buffered
    generate_until(env, start, 6)
    assert task_for(env, 7) is None
    checkpoint(env, 6)
    point = env.service.feynman.view(env.conv, env.owner)["checkpoints"][0]
    opened = env.service.feynman.start(env.conv, env.owner, point["checkpoint_id"])
    with env.db.session() as sess:
        conv = sess.get(ConversationRow, env.conv)
        assert not env.service._buffer_step_allowed(sess, conv, 6)
    env.service.feynman.action(env.conv, env.owner, opened["revision"], "skip")
    env.background.tick()
    assert task_for(env, 7) is not None
    assert env.service.run_step_generation(task_for(env, 7).task_id)["status"] == "succeeded"
    assert task_for(env, 8) is None


def test_pause_preserves_cache_and_resume_continues_from_its_end(buffered):
    env, start = buffered
    generate_until(env, start, 3)
    checkpoint(env, paused=True)
    fourth = task_for(env, 4)
    with env.db.session() as sess:
        assert not env.background.eligible(sess, sess.get(LearningTaskRow, fourth.task_id))
    checkpoint(env, paused=False, seq=2)
    with env.db.session() as sess:
        assert env.background.eligible(sess, sess.get(LearningTaskRow, fourth.task_id))
        assert len(list(sess.scalars(select(LearningTaskRow).where(
            LearningTaskRow.subject_id == f"{env.conv}:4:1")))) == 1
    assert env.service.run_step_generation(fourth.task_id)["status"] == "succeeded"
    assert task_for(env, 5) is not None


def test_followup_resume_replaces_cancelled_audio_and_reuses_ready_content(buffered):
    env, start = buffered
    generate_until(env, start, 3)
    original = env.service.ensure_audio(env.conv, actor_id=env.owner, step_no=3)["task_id"]
    env.service.followup(env.conv, actor_id=env.owner,
        content="Explain the lookup condition.", idempotency_key="buffer-audio-followup")
    assert env.service.tasks.get_task(original)["status"] == "cancelled"
    with env.db.session() as sess:
        sess.get(ConversationRow, env.conv).session_state = "awaiting_resume"
    env.service.resume_lesson(env.conv, actor_id=env.owner)
    renewed = env.service.ensure_audio(env.conv, actor_id=env.owner, step_no=3)["task_id"]
    assert renewed != original
    assert env.service.run_tts(renewed)["audio_status"] == "ready"
    assert env.service.run_step_generation(task_for(env, 4).task_id)["status"] == "succeeded"
    with env.db.session() as sess:
        assert len(list(sess.scalars(select(TeachingStepRow).where(
            TeachingStepRow.conversation_id == env.conv)))) == 4


def test_current_audio_is_dispatched_before_distant_text(buffered, monkeypatch):
    env, start = buffered
    generate_until(env, start, 3)
    audio = env.service.ensure_audio(env.conv, actor_id=env.owner, step_no=1)["task_id"]
    dispatched = []
    execute = env.background._execute
    def record(task, group):
        dispatched.append(task)
        return execute(task, group)
    monkeypatch.setattr(env.background, "_execute", record)
    env.background.tick()
    for _, future, _ in list(env.background.pending.values()):
        future.result(timeout=20)
    assert dispatched[0] == audio


def test_mentor_buffering_is_idempotent_and_restarts_after_followup(buffered):
    env, start = buffered
    with env.db.session() as sess:
        sess.add(ClassroomPlaybackRow(conversation_id=env.conv, version=1,
            effective_step_no=1, profile_json="{}"))
    generate_until(env, start, 3)
    fourth = task_for(env, 4)
    for _ in range(3):
        with env.db.session() as sess:
            env.service._ensure_prefetch(sess, env.conv, env.owner, 2, 1)
    with env.db.session() as sess:
        bindings = list(sess.scalars(select(PromptBindingRow).where(
            PromptBindingRow.task_id == fourth.task_id)))
        assert len({binding.prompt_name for binding in bindings}) == len(bindings)
    env.service.followup(env.conv, actor_id=env.owner,
        content="Explain lookup.", idempotency_key="buffer-mentor-followup")
    env.service.resume_lesson(env.conv, actor_id=env.owner)
    renewed = task_for(env, 4)
    assert renewed.task_id != fourth.task_id
    assert env.service.run_step_generation(renewed.task_id)["status"] == "succeeded"


@pytest.mark.parametrize("mutation", ["revoke", "voice_version", "pause", "mentor"])
def test_deep_audio_late_result_cannot_publish_after_context_change(buffered, mutation):
    env, start = buffered
    generate_until(env, start, 3)
    original = env.service.tts_adapter_factory
    task_id = env.service.ensure_audio(env.conv, actor_id=env.owner, step_no=3)["task_id"]
    def factory(owner, task):
        adapter = original(owner, task)
        synthesize = adapter.synthesize
        def changed(text):
            if mutation == "revoke":
                grant = env.service.disclosures.list_for_task(task, actor_id=owner)[0]
                env.service.disclosures.revoke(grant["grant_id"], actor_id=owner)
            elif mutation == "voice_version":
                with env.db.session() as sess:
                    sess.get(ServiceConfigRow, env.voice).config_version += 1
            elif mutation == "pause":
                env.service.followup(env.conv, actor_id=owner,
                    content="Explain lookup.", idempotency_key="audio-inflight-followup")
            else:
                with env.db.session() as sess:
                    step = sess.scalar(select(TeachingStepRow).where(
                        TeachingStepRow.conversation_id == env.conv, TeachingStepRow.step_no == 3))
                    step.text_version_no += 1
            return synthesize(text)
        adapter.synthesize = changed
        return adapter
    factory.config_for_owner = original.config_for_owner
    env.service.tts_adapter_factory = factory
    result = env.service.run_tts(task_id)
    assert result["status"] in ("failed", "stale_discarded")
    with env.db.session() as sess:
        assert sess.scalar(select(TeachingStepRow).where(
            TeachingStepRow.conversation_id == env.conv, TeachingStepRow.step_no == 3)).audio_id is None
    assert env.blobs == {}


@pytest.mark.parametrize("kind", ["lesson_step_generate", "tts"])
def test_deep_transient_failure_recovers_with_existing_budget(buffered, kind):
    env, start = buffered
    generate_until(env, start, 3)
    task_id = (task_for(env, 4).task_id if kind == "lesson_step_generate" else
        env.service.ensure_audio(env.conv, actor_id=env.owner, step_no=3)["task_id"])
    lease = env.service.tasks.claim(task_id, lease_owner="synthetic-buffer-recovery")
    env.service.tasks.submit_result(lease, stage="generate" if kind == "lesson_step_generate" else "tts",
        status="failed", error_code="MODEL_CONNECTION_ERROR" if kind == "lesson_step_generate" else "TTS_TIMEOUT")
    with env.db.session() as sess:
        sess.get(LearningTaskRow, task_id).updated_at = utcnow() - timedelta(seconds=120)
    recover_observed(env.background)
    with env.db.session() as sess:
        task = sess.get(LearningTaskRow, task_id)
        assert task.status == "queued" and task.generation == 2
        assert sess.get(TaskStateRow, task_id).error_code is None


@pytest.mark.parametrize("guard", ["unobserved", "followup", "deleted", "revoked"])
def test_deep_tasks_respect_activity_lifecycle_and_authorization(buffered, guard):
    env, start = buffered
    generate_until(env, start, 3)
    fourth = task_for(env, 4)
    if guard == "unobserved":
        env.service.observed_sessions.clear()
    elif guard == "followup":
        env.service.followup(env.conv, actor_id=env.owner,
            content="Explain the lookup condition.", idempotency_key="buffer-followup")
    elif guard == "deleted":
        with env.db.session() as sess:
            sess.get(ConversationRow, env.conv).deleted_at = utcnow()
    else:
        grant = env.service.disclosures.list_for_task(fourth.task_id, actor_id=env.owner)[0]
        env.service.disclosures.revoke(grant["grant_id"], actor_id=env.owner)
    with env.db.session() as sess:
        assert not env.background.eligible(sess, sess.get(LearningTaskRow, fourth.task_id))
        assert len(list(sess.scalars(select(TeachingStepRow).where(
            TeachingStepRow.conversation_id == env.conv)))) == 3
