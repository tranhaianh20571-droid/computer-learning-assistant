"""Synthetic SQLite convergence: no vendor requests or production database access."""
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from backend.agents.limits import allows_content_fallback, to_controlled_error
from backend.agents.task_binding import AgentBudgetRow
from backend.logging_audit.contracts import ERROR_CODES, normalize_error_code, validate_error_code
from backend.logging_audit.db import Database
from backend.logging_audit.metrics import task_diagnostics_snapshot
from backend.logging_audit.models import TaskEventRow, TaskStateRow
from backend.logging_audit.repositories import PgTaskEventStore
from backend.logging_audit.task_events import AccessDeniedError
from backend.planning.errors import error as planning_error
from backend.slice1.errors import ERROR_CODES as SLICE_CODES, Slice1Error
from backend.tasks.service import LearningTaskRow, LeaseError, TaskAttemptRow, TaskService, utcnow


@pytest.fixture
def env():
    db = Database('sqlite+pysqlite:///:memory:')
    for model in (LearningTaskRow, AgentBudgetRow, TaskAttemptRow, TaskStateRow, TaskEventRow):
        model.__table__.create(db.engine)
    events = PgTaskEventStore(db)
    try:
        yield db, TaskService(db, events), events
    finally:
        db.dispose()


def new_task(tasks, key='synthetic', **kwargs):
    return tasks.create_task(owner_user_id='owner', kind=kwargs.pop('kind', 'lesson_step_generate'),
        subject_id='conv_synthetic:0:1', idempotency_key=key, stage='generate', **kwargs)['task_id']


def change(db, task_id, **values):
    with db.session() as sess:
        row = sess.get(LearningTaskRow, task_id)
        for name, value in values.items():
            setattr(row, name, value)


@pytest.mark.parametrize('code', sorted(SLICE_CODES))
@pytest.mark.parametrize('wrapped', [False, True])
def test_slice_codes_are_registered_and_never_fallback(code, wrapped):
    exc = Slice1Error(code, 'synthetic unsafe details', status=SLICE_CODES[code])
    if wrapped:
        outer = RuntimeError('synthetic SDK wrapper')
        outer.__cause__ = exc
        exc = outer
    result = to_controlled_error(exc)
    assert result.code == code.upper() and result.status == SLICE_CODES[code]
    assert validate_error_code(result.code)
    assert not allows_content_fallback(result)
    assert 'unsafe' not in str(result)


@pytest.mark.parametrize('code', [None, 'unknown_synthetic', 'bad\nsynthetic', {'arbitrary': 'value'}])
def test_unknown_codes_fail_closed_without_raw_diagnostics(code):
    assert normalize_error_code(code) == 'INTERNAL_ERROR'
    exc = Slice1Error('unknown_synthetic', 'synthetic secret marker') if code is None else RuntimeError('synthetic secret marker')
    if code is not None:
        exc.code = code
    result = to_controlled_error(exc)
    assert result.code == 'INTERNAL_ERROR' and not allows_content_fallback(result)
    diagnostic = str(task_diagnostics_snapshot())
    assert 'unknown_synthetic' not in diagnostic and 'secret marker' not in diagnostic


@pytest.mark.parametrize('code', ['OUTPUT_INVALID', 'MODEL_TIMEOUT', 'MODEL_CREDENTIAL_INVALID', 'AGENT_LOOP_LIMIT'])
def test_registered_planning_errors_preserve_identity(code):
    exc = planning_error(code)
    assert to_controlled_error(exc) is exc


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("kind,code", [("timeout", "MODEL_TIMEOUT"), ("connection", "MODEL_CONNECTION_ERROR")])
def test_sdk_wrapped_transport_failure_preserves_retry_eligibility(provider, kind, code):
    import importlib
    import httpx2
    from pydantic_ai.exceptions import ModelAPIError
    from backend.planning.retry_policy import AUTO_RETRY_ERRORS
    sdk = importlib.import_module(provider)
    request = httpx2.Request("POST", "https://synthetic.invalid/v1/messages")
    failure = sdk.APITimeoutError(request=request) if kind == "timeout" else sdk.APIConnectionError(request=request)
    wrapped = ModelAPIError("synthetic-model", "synthetic-private-error")
    wrapped.__cause__ = failure
    result = to_controlled_error(wrapped)
    assert result.code == code and result.code in AUTO_RETRY_ERRORS
    assert "synthetic-private" not in str(result)


def test_create_caller_transaction_dedup_and_rollback(env):
    db, tasks, _ = env
    with pytest.raises(RuntimeError, match='synthetic_abort'):
        with db.session() as sess:
            task = new_task(tasks, session=sess)
            assert new_task(tasks, session=sess) == task
            assert sess.get(AgentBudgetRow, task) is not None
            assert sess.get(TaskStateRow, task) is not None
            raise RuntimeError('synthetic_abort')
    with db.session() as sess:
        for model in (LearningTaskRow, AgentBudgetRow, TaskStateRow, TaskEventRow):
            assert sess.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize('status', ['queued', 'leased', 'running'])
def test_sweep_atomic_inclusive_and_idempotent(env, monkeypatch, status):
    import backend.tasks.service as module
    db, tasks, events = env
    task = new_task(tasks)
    lease = tasks.claim(task, lease_owner='worker') if status != 'queued' else None
    now = utcnow()
    change(db, task, deadline_at=now, status=status)
    monkeypatch.setattr(module, 'utcnow', lambda: now)
    result = tasks.sweep_expired_tasks(now=now)
    assert result[0]['status'] == 'expired'
    current = tasks.get_task(task, actor_id='owner')
    assert current['error_code'] == 'TASK_EXPIRED' and current['retryable']
    assert not {'lease_token', 'lease_owner', 'lease_until'} & current.keys()
    with db.session() as sess:
        row = sess.get(LearningTaskRow, task)
        assert row.lease_token is None and row.lease_owner is None and row.lease_until is None
        if lease:
            attempt = sess.get(TaskAttemptRow, lease.attempt_id)
            assert attempt.result_status == 'expired' and attempt.finished_at is not None
    terminal = events.get_events(task)[-1]
    assert terminal.error_code == 'TASK_EXPIRED' and terminal.status == 'failed'
    assert terminal.revision == current['revision']
    assert tasks.sweep_expired_tasks(now=now) == []
    if lease:
        assert not tasks.submit_result(lease, stage='generate', status='succeeded')['accepted']


def test_sweep_filters_limit_cancellation_and_completed_immunity(env):
    db, tasks, events = env
    first = new_task(tasks, 'first', kind='generic')
    second = new_task(tasks, 'second')
    third = new_task(tasks, 'third')
    finished = new_task(tasks, 'finished')
    tasks.submit_result(tasks.claim(finished, lease_owner='worker'), stage='generate', status='succeeded')
    now = utcnow()
    for task in (first, second, third, finished):
        change(db, task, deadline_at=now)
    change(db, second, cancel_requested=True)
    assert tasks.sweep_expired_tasks(now=now, kinds=[]) == []
    assert tasks.sweep_expired_tasks(now=now, task_ids=[]) == []
    result = tasks.sweep_expired_tasks(now=now, kinds=['lesson_step_generate'], task_ids=[second])
    assert result[0]['status'] == 'cancelled'
    assert tasks.get_task(second)['error_code'] == 'TASK_CANCELLED'
    assert len(tasks.sweep_expired_tasks(now=now, limit=1)) == 1
    assert tasks.get_task(finished)['status'] == 'succeeded'
    assert events.get_events(finished)[-1].status == 'succeeded'


def test_sweep_event_failure_rolls_back_task_attempt_and_event(env, monkeypatch):
    db, tasks, events = env
    task = new_task(tasks)
    lease = tasks.claim(task, lease_owner='worker')
    now = utcnow()
    change(db, task, deadline_at=now)
    original = events.record
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('synthetic_event_write_failure')
    monkeypatch.setattr(events, 'record', fail)
    with pytest.raises(RuntimeError):
        tasks.sweep_expired_tasks(now=now)
    assert tasks.get_task(task)['status'] == 'leased'
    with db.session() as sess:
        assert sess.get(TaskAttemptRow, lease.attempt_id).finished_at is None
    assert len(events.get_events(task)) == 1


@pytest.mark.parametrize('reason', ['deadline', 'lease', 'failure'])
def test_owner_retry_preserves_budget_subject_and_fences_old_worker(env, reason):
    db, tasks, events = env
    task = new_task(tasks)
    lease = tasks.claim(task, lease_owner='old_worker')
    with db.session() as sess:
        budget = sess.get(AgentBudgetRow, task)
        budget.logical_requests, budget.wire_requests, budget.tool_calls = 9, 7, 4
    if reason == 'deadline':
        change(db, task, deadline_at=utcnow() - timedelta(seconds=1))
    elif reason == 'lease':
        change(db, task, lease_until=utcnow() - timedelta(seconds=1))
    else:
        tasks.submit_result(lease, stage='generate', status='failed', error_code='DISCLOSURE_REQUIRED')
    result = tasks.requeue_for_retry(task, actor_id='owner')
    assert result['requeued'] and result['generation'] == 2
    current = tasks.get_task(task)
    assert current['subject_id'] == 'conv_synthetic:0:1' and current['error_code'] is None
    with db.session() as sess:
        budget = sess.get(AgentBudgetRow, task)
        assert (budget.logical_requests, budget.wire_requests, budget.tool_calls) == (9, 7, 4)
        assert tasks._as_utc(sess.get(LearningTaskRow, task).deadline_at) > utcnow()
    assert not tasks.submit_result(lease, stage='generate', status='succeeded')['accepted']
    fresh = tasks.claim(task, lease_owner='fresh_worker')
    assert fresh.generation == 2 and fresh.attempt == 2 and fresh.attempt_id != lease.attempt_id
    assert events.get_state(task).generation == 2


def test_retry_does_not_replace_live_worker_or_allow_wrong_owner(env):
    _, tasks, _ = env
    task = new_task(tasks)
    lease = tasks.claim(task, lease_owner='worker')
    assert not tasks.requeue_for_retry(task, actor_id='owner')['requeued']
    assert tasks.validate_lease(lease)
    with pytest.raises(AccessDeniedError):
        tasks.requeue_for_retry(task, actor_id='intruder')


@pytest.mark.parametrize('change_kind', ['expiry', 'cancel', 'failure'])
def test_recovery_can_terminalize_own_expired_lease_without_publish(env, change_kind):
    db, tasks, events = env
    task = new_task(tasks)
    lease = tasks.claim(task, lease_owner='worker')
    change(db, task, lease_until=utcnow() - timedelta(seconds=1))
    if change_kind == 'expiry':
        change(db, task, deadline_at=utcnow() - timedelta(seconds=1))
    elif change_kind == 'cancel':
        change(db, task, cancel_requested=True)
    result = tasks.recover_worker_failure(lease, error_code='disclosure_required')
    assert result['accepted'] and result['recovery_attempts'] == 1
    assert result['status'] == {'expiry':'expired', 'cancel':'cancelled', 'failure':'failed'}[change_kind]
    assert tasks.get_task(task)['error_code'] == {'expiry':'TASK_EXPIRED', 'cancel':'TASK_CANCELLED', 'failure':'DISCLOSURE_REQUIRED'}[change_kind]
    assert len(events.get_events(task)) == 2
    assert not tasks.recover_worker_failure(lease)['accepted']


@pytest.mark.parametrize('field,value', [('generation', 9), ('attempt_id', 'forged'),
    ('lease_token', 'forged'), ('lease_owner', 'forged'), ('owner_user_id', 'intruder')])
def test_recovery_never_mutates_a_newer_or_forged_attempt(env, field, value):
    _, tasks, events = env
    lease = tasks.claim(new_task(tasks), lease_owner='worker')
    assert not tasks.recover_worker_failure(replace(lease, **{field:value}))['accepted']
    assert tasks.get_task(lease.task_id)['status'] == 'leased'
    assert len(events.get_events(lease.task_id)) == 1


@pytest.mark.parametrize('permanent', [False, True])
def test_recovery_write_retries_are_bounded_and_atomic(env, monkeypatch, permanent):
    db, tasks, events = env
    lease = tasks.claim(new_task(tasks), lease_owner='worker')
    original, calls = events.record, []
    def fail(*args, **kwargs):
        calls.append(1)
        original(*args, **kwargs)
        if permanent or len(calls) == 1:
            raise RuntimeError('synthetic second failure')
    monkeypatch.setattr(events, 'record', fail)
    if permanent:
        with pytest.raises(LeaseError) as caught:
            tasks.recover_worker_failure(lease)
        assert caught.value.code == 'WORKER_RECOVERY_FAILED'
        assert tasks.get_task(lease.task_id)['status'] == 'leased'
        assert len(events.get_events(lease.task_id)) == 1
        with db.session() as sess:
            assert sess.get(TaskAttemptRow, lease.attempt_id).finished_at is None
    else:
        assert tasks.recover_worker_failure(lease)['recovery_attempts'] == 2
        assert tasks.get_task(lease.task_id)['status'] == 'failed'
        assert len(events.get_events(lease.task_id)) == 2
    assert len(calls) == 2


def test_fail_queued_never_claims_or_changes_budget(env):
    db, tasks, events = env
    task = new_task(tasks)
    before = tasks.get_task(task)
    with db.session() as sess:
        sess.get(AgentBudgetRow, task).logical_requests = 4
    result = tasks.fail_queued(task, expected_generation=before['generation'],
        expected_revision=before['revision'], error_code='disclosure_revoked')
    assert result['accepted'] and result['error_code'] == 'DISCLOSURE_REVOKED'
    assert tasks.get_task(task)['status'] == 'failed'
    with db.session() as sess:
        assert sess.get(LearningTaskRow, task).attempt == 0
        assert sess.get(AgentBudgetRow, task).logical_requests == 4
        assert sess.scalar(select(func.count()).select_from(TaskAttemptRow)) == 0
    assert len(events.get_events(task)) == 2
    assert not tasks.fail_queued(task, expected_generation=1, expected_revision=1,
        error_code='DISCLOSURE_REQUIRED')['accepted']


@pytest.mark.parametrize('mutation', ['generation', 'revision', 'claim'])
def test_fail_queued_discards_stale_scan(env, mutation):
    db, tasks, events = env
    task = new_task(tasks)
    if mutation == 'claim':
        lease = tasks.claim(task, lease_owner='current_worker')
    else:
        change(db, task, **{mutation:2})
    assert not tasks.fail_queued(task, expected_generation=1, expected_revision=1,
        error_code='DISCLOSURE_REQUIRED')['accepted']
    assert len(events.get_events(task)) == 1
    if mutation == 'claim':
        assert tasks.validate_lease(lease)


@pytest.mark.parametrize('cancelled', [False, True])
def test_fail_queued_deadline_and_cancel_take_priority(env, cancelled):
    db, tasks, _ = env
    task = new_task(tasks)
    change(db, task, deadline_at=utcnow() - timedelta(seconds=1), cancel_requested=cancelled)
    result = tasks.fail_queued(task, expected_generation=1, expected_revision=1,
        error_code='DISCLOSURE_REQUIRED')
    assert result['status'] == ('cancelled' if cancelled else 'expired')
    assert result['error_code'] == ('TASK_CANCELLED' if cancelled else 'TASK_EXPIRED')


def test_fail_queued_event_failure_rolls_back(env, monkeypatch):
    _, tasks, events = env
    task = new_task(tasks)
    original = events.record
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('synthetic event storage unavailable')
    monkeypatch.setattr(events, 'record', fail)
    with pytest.raises(RuntimeError):
        tasks.fail_queued(task, expected_generation=1, expected_revision=1,
            error_code='DISCLOSURE_REQUIRED')
    assert tasks.get_task(task)['status'] == 'queued'
    assert len(events.get_events(task)) == 1


def test_unknown_failed_submission_converges_but_nonfailure_contract_is_strict(env):
    from backend.logging_audit.contracts import ContractError
    _, tasks, events = env
    lease = tasks.claim(new_task(tasks), lease_owner='worker')
    with pytest.raises(ContractError):
        tasks.submit_result(lease, stage='generate', status='partial', error_code='unknown_synthetic')
    result = tasks.submit_result(lease, stage='generate', status='failed', error_code='unknown_synthetic')
    assert result['accepted'] and tasks.get_task(lease.task_id)['error_code'] == 'INTERNAL_ERROR'
    assert events.get_events(lease.task_id)[-1].error_code == 'INTERNAL_ERROR'
