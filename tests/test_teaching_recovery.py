"""Durable observed-session recovery; synthetic tasks and no supplier requests."""
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from time import monotonic
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from test_teaching_assessments import env, database, lesson
from backend.agents.task_binding import AgentBudgetRow
from backend.logging_audit.models import TaskStateRow
from backend.planning.models import ConversationRow, utcnow
from backend.tasks.service import LearningTaskRow
from backend.teaching.background import TeachingBackground
from backend.teaching.recovery import recover_observed
from backend.teaching.service import TeachingService


def failed(env, code="MODEL_CONNECTION_ERROR"):
    _, state, owner, _ = env
    _, _, started = lesson(env)
    task_id = started["task_id"]
    lease = state.tasks.claim(task_id, lease_owner="synthetic-recovery")
    state.tasks.submit_result(lease, stage="generate", status="failed", error_code=code)
    with state.db.session() as session:
        task = session.get(LearningTaskRow, task_id)
        task.updated_at = utcnow() - timedelta(seconds=120)
        task.deadline_at = utcnow() - timedelta(seconds=60)
        budget = session.get(AgentBudgetRow, task_id)
        budget.logical_requests = budget.wire_requests = 2
    state.teaching.observed_sessions[started["conversation_id"]] = monotonic()
    return started, TeachingBackground(state.teaching)


@pytest.mark.parametrize("code", ["MODEL_CONNECTION_ERROR", "OUTPUT_INVALID"])
def test_failed_task_recovers_after_restart_without_resetting_budget(env, code):
    started, background = failed(env, code=code)
    state = env[1]
    try:
        restarted = TeachingBackground(state.teaching)
        try:
            recover_observed(restarted)
        finally:
            restarted.close()
        with state.db.session() as session:
            task = session.get(LearningTaskRow, started["task_id"])
            assert task.status == "queued" and task.generation == 2
            assert task.deadline_at > utcnow()
            budget = session.get(AgentBudgetRow, task.task_id)
            assert (budget.logical_requests, budget.wire_requests) == (2, 2)
        recover_observed(background)
        with state.db.session() as session:
            task = session.get(LearningTaskRow, started["task_id"])
            assert task.generation == 2
        result = state.teaching.run_step_generation(started["task_id"])
        assert result["status"] == "succeeded"
    finally:
        background.close()


def test_invalid_output_stops_automatic_recovery_after_three_generations(env):
    started, background = failed(env, code="OUTPUT_INVALID")
    try:
        with env[1].db.session() as session:
            session.get(LearningTaskRow, started["task_id"]).generation = 3
        recover_observed(background)
        assert env[1].tasks.get_task(started["task_id"])["status"] == "failed"
        with env[1].db.session() as session:
            budget = session.get(AgentBudgetRow, started["task_id"])
            assert budget.logical_requests == budget.wire_requests == 2
    finally:
        background.close()

@pytest.mark.parametrize("guard", ["unobserved", "paused", "cancelled", "limit", "backoff", "credential"])
def test_recovery_respects_activity_authority_and_durable_limits(env, guard):
    started, background = failed(env)
    state = env[1]
    try:
        if guard == "unobserved":
            state.teaching.observed_sessions.clear()
        with state.db.session() as session:
            task = session.get(LearningTaskRow, started["task_id"])
            if guard == "paused":
                session.get(ConversationRow, started["conversation_id"]).session_state = "paused"
            if guard == "cancelled":
                task.cancel_requested = True
            if guard == "limit":
                task.generation = 6
            if guard == "backoff":
                task.updated_at = utcnow()
            if guard == "credential":
                session.get(TaskStateRow, task.task_id).error_code = "MODEL_CREDENTIAL_INVALID"
        recover_observed(background)
        assert state.tasks.get_task(started["task_id"])["status"] == "failed"
    finally:
        background.close()


def test_deadlock_is_a_controlled_transient_storage_error():
    exc = OperationalError("synthetic", {}, SimpleNamespace(sqlstate="40P01"))
    assert TeachingService._controlled_failure(exc) == "TASK_STORAGE_BUSY"


def test_concurrent_recovery_advances_generation_only_once(env):
    started, background = failed(env)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: recover_observed(background), range(2)))
        assert env[1].tasks.get_task(started["task_id"])["generation"] == 2
    finally:
        background.close()


def test_unobserved_old_failures_do_not_starve_active_classroom(env):
    started, background = failed(env)
    state, owner = env[1], env[2]
    try:
        with state.db.session() as session:
            for number in range(33):
                created = state.tasks.create_task(owner_user_id=owner, kind="lesson_step_generate",
                    subject_id=f"synthetic-inactive-{number}:1:1", idempotency_key=f"inactive-{number}",
                    stage="generate", lane="teaching_prefetch", session=session)
                task = session.get(LearningTaskRow, created["task_id"])
                task.status = "failed"
                task.updated_at = utcnow() - timedelta(seconds=300)
                session.get(TaskStateRow, task.task_id).error_code = "MODEL_CONNECTION_ERROR"
        recover_observed(background)
        assert state.tasks.get_task(started["task_id"])["status"] == "queued"
    finally:
        background.close()


@pytest.mark.parametrize("revoked", [False, True])
def test_audio_failure_recovers_with_existing_authority(env, monkeypatch, revoked):
    from test_lecture_audio import prepared
    started, _, _, disclosures, calls = prepared(env, monkeypatch)
    client, state, owner, headers = env
    conv = started["conversation_id"]
    task_id = state.teaching.ensure_audio(conv, actor_id=owner, step_no=1)["task_id"]
    lease = state.tasks.claim(task_id, lease_owner="synthetic-tts-recovery")
    state.tasks.submit_result(lease, stage="tts", status="failed", error_code="TTS_TIMEOUT")
    with state.db.session() as session:
        session.get(LearningTaskRow, task_id).updated_at = utcnow() - timedelta(seconds=120)
    if revoked:
        grant = disclosures.list_for_task(task_id, actor_id=owner)[0]
        disclosures.revoke(grant["grant_id"], actor_id=owner)
    state.teaching.observed_sessions[conv] = monotonic()
    background = TeachingBackground(state.teaching)
    try:
        recover_observed(background)
        if revoked:
            assert state.tasks.get_task(task_id)["status"] == "failed"
            assert calls == []
            return
        assert state.tasks.get_task(task_id)["status"] == "queued"
        assert state.teaching.run_tts(task_id)["audio_status"] == "ready"
        assert len(calls) == 1
        assert client.get(f"/api/teaching/{conv}", headers=headers).json()["steps"][0]["audio_ready"]
    finally:
        background.close()
