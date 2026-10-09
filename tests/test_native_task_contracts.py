"""Offline SQLite contracts for current leases, atomic events and disclosure scopes."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.orm import Session

from backend.agents.task_binding import AgentBudgetRow
from backend.logging_audit.audit import AuditWriteError
from backend.logging_audit.db import Database
from backend.logging_audit.models import TaskEventRow, TaskStateRow
from backend.logging_audit.repositories import PgAuditLog, PgTaskEventStore
from backend.logging_audit.task_events import AccessDeniedError
from backend.slice1.disclosure_service import DisclosureService
from backend.slice1.errors import Slice1Error
from backend.slice1.models import DisclosureGrantRow, ServiceConfigRow
from backend.tasks.service import LearningTaskRow, LeaseError, TaskAttemptRow, TaskService, utcnow


@pytest.fixture
def services():
    db = Database("sqlite+pysqlite:///:memory:")
    for model in (LearningTaskRow, AgentBudgetRow, TaskAttemptRow, TaskStateRow, TaskEventRow,
                  ServiceConfigRow, DisclosureGrantRow):
        model.__table__.create(db.engine)
    events = PgTaskEventStore(db)
    tasks = TaskService(db, events)
    try:
        yield db, tasks, events
    finally:
        db.dispose()


def create_task(tasks):
    return tasks.create_task(owner_user_id="user_owner", kind="generic", subject_id="subject_1",
                             idempotency_key="synthetic_task")["task_id"]


def claim(tasks):
    return tasks.claim(create_task(tasks), lease_owner="worker_1")


def mutate(db, model, ident, **values):
    with db.session() as sess:
        row = sess.get(model, ident)
        for key, value in values.items():
            setattr(row, key, value)


@pytest.mark.parametrize("field,value", [
    ("attempt_id", "other_attempt"), ("attempt", 9), ("generation", 2),
    ("lease_owner", "other_worker"), ("lease_token", "other_token"),
    ("owner_user_id", "other_owner"), ("status", "succeeded"),
    ("lease_until", None), ("lease_until", utcnow() - timedelta(seconds=1)),
])
def test_validate_reads_each_current_fence(services, field, value):
    db, tasks, _ = services
    lease = claim(tasks)
    mutate(db, LearningTaskRow, lease.task_id, **{field: value})
    with pytest.raises(LeaseError, match="changed|active") as exc:
        tasks.validate_lease(lease)
    assert exc.value.code == "LEASE_LOST"


@pytest.mark.parametrize("field,value,code", [
    ("cancel_requested", True, "TASK_CANCELLED"),
    ("status", "cancelled", "TASK_CANCELLED"),
    ("deadline_at", utcnow() - timedelta(seconds=1), "TASK_EXPIRED"),
])
def test_cancel_deadline_changes_override_startup_snapshot(services, field, value, code):
    db, tasks, _ = services
    lease = claim(tasks)
    mutate(db, LearningTaskRow, lease.task_id, **{field: value})
    with pytest.raises(LeaseError) as exc:
        tasks.validate_lease(lease)
    assert exc.value.code == code


@pytest.mark.parametrize("field,value", [
    ("attempt_id", ""), ("lease_owner", ""), ("lease_token", ""),
    ("attempt_id", "forged"), ("generation", 8), ("attempt", 8),
])
def test_forged_lease_rejected(services, field, value):
    _, tasks, _ = services
    lease = claim(tasks)
    with pytest.raises(LeaseError) as exc:
        tasks.validate_lease(replace(lease, **{field: value}))
    assert exc.value.code == "LEASE_LOST"


def test_owner_actor_and_legacy_lease_contract(services):
    _, tasks, _ = services
    lease = claim(tasks)
    assert lease.owner_user_id == "user_owner"
    assert tasks.validate_lease(lease, actor_id="user_owner").task_id == lease.task_id
    assert tasks.validate_lease(replace(lease, owner_user_id=None), actor_id="user_owner")
    with pytest.raises(AccessDeniedError):
        tasks.validate_lease(lease, actor_id="intruder")
    with pytest.raises(LeaseError) as exc:
        tasks.validate_lease(replace(lease, task_id="missing"))
    assert exc.value.code == "TASK_NOT_FOUND"


def test_claim_next_binds_owner(services):
    _, tasks, _ = services
    create_task(tasks)
    assert tasks.claim_next("interactive", lease_owner="worker_1").owner_user_id == "user_owner"


def test_validate_locks_refreshes_identity_map_without_autoflush(services):
    db, tasks, _ = services
    lease = claim(tasks)
    with db.session() as sess:
        cached = sess.get(LearningTaskRow, lease.task_id)
        sess.execute(update(LearningTaskRow).where(LearningTaskRow.task_id == lease.task_id)
                     .values(cancel_requested=True).execution_options(synchronize_session=False))
        assert cached.cancel_requested is False
        cached.cancel_requested = False
        captured = []
        def observe(state):
            if state.is_select:
                captured.append(state.statement)
        event.listen(sess, "do_orm_execute", observe)
        try:
            with pytest.raises(LeaseError) as exc:
                tasks.validate_lease(lease, session=sess)
        finally:
            event.remove(sess, "do_orm_execute", observe)
        assert exc.value.code == "TASK_CANCELLED"
        assert cached.cancel_requested is True
        assert captured[-1]._for_update_arg is not None
        assert captured[-1].get_execution_options()["populate_existing"] is True


def test_current_expiry_not_local_snapshot_and_heartbeat_deadline(services):
    db, tasks, _ = services
    lease = claim(tasks)
    expired_snapshot = replace(lease, lease_until=utcnow() - timedelta(seconds=1),
                               deadline_at=utcnow() - timedelta(seconds=1))
    assert tasks.validate_lease(expired_snapshot)
    deadline = utcnow() + timedelta(seconds=15)
    mutate(db, LearningTaskRow, lease.task_id, deadline_at=deadline)
    renewed = tasks.heartbeat(expired_snapshot, lease_seconds=60)
    assert renewed == deadline
    with db.session() as sess:
        row = sess.get(LearningTaskRow, lease.task_id)
        attempt = sess.get(TaskAttemptRow, lease.attempt_id)
        assert row.status == "running"
        assert row.lease_until == attempt.lease_until
    mutate(db, LearningTaskRow, lease.task_id, lease_until=utcnow() - timedelta(seconds=1))
    with pytest.raises(LeaseError):
        tasks.heartbeat(lease)


def test_task_trace_binding_is_durable_and_idempotent(services):
    from backend.logging_audit.trace import TraceContext, trace_context
    db, tasks, events = services
    ctx = TraceContext("1" * 32, "2" * 16)
    with trace_context(ctx):
        task_id = create_task(tasks)
    with trace_context(TraceContext("3" * 32, "4" * 16)):
        assert create_task(tasks) == task_id
    with db.session() as sess:
        budget = sess.get(AgentBudgetRow, task_id)
        assert budget.traceparent == ctx.as_traceparent()
        assert budget.logical_requests == budget.wire_requests == budget.tool_calls == 0
        assert budget.request_limit == 128 and budget.tool_limit == 256
        assert sess.scalar(select(func.count()).select_from(AgentBudgetRow)) == 1
    assert len(events.get_events(task_id)) == 1


def test_task_creation_and_events_rollback_together(services, monkeypatch):
    db, tasks, events = services
    original = events.create_task
    def fail_after_write(*args, **kwargs):
        assert isinstance(kwargs["session"], Session)
        original(*args, **kwargs)
        raise RuntimeError("synthetic_event_failure")
    monkeypatch.setattr(events, "create_task", fail_after_write)
    with pytest.raises(RuntimeError, match="synthetic_event_failure"):
        create_task(tasks)
    with db.session() as sess:
        for model in (LearningTaskRow, AgentBudgetRow, TaskStateRow, TaskEventRow):
            assert sess.scalar(select(func.count()).select_from(model)) == 0


def test_submit_atomically_publishes_task_attempt_state_and_event(services):
    db, tasks, events = services
    lease = claim(tasks)
    def publish(sess):
        sess.get(LearningTaskRow, lease.task_id).model_config_version = "published_1"
    result = tasks.submit_result(lease, stage="generate", status="succeeded", publish=publish)
    assert result["accepted"] is True
    with db.session() as sess:
        row = sess.get(LearningTaskRow, lease.task_id)
        attempt = sess.get(TaskAttemptRow, lease.attempt_id)
        assert row.status == "succeeded" and row.stage == "generate" and row.revision == 2
        assert row.model_config_version == "published_1" and row.lease_token is None
        assert attempt.result_status == "succeeded" and attempt.finished_at is not None
    state = events.get_state(lease.task_id)
    recorded = events.get_events(lease.task_id)
    assert [item.sequence for item in recorded] == [1, 2]
    assert recorded[-1].revision == state.revision == result["revision"]
    assert recorded[-1].stage == state.stage == "generate"
    assert recorded[-1].status == state.status == "succeeded"


@pytest.mark.parametrize("failure", ["event", "publish"])
def test_submit_failure_rolls_back_every_write(services, monkeypatch, failure):
    db, tasks, events = services
    lease = claim(tasks)
    original = events.record
    def fail_event(*args, **kwargs):
        assert isinstance(kwargs["session"], Session)
        original(*args, **kwargs)
        raise RuntimeError("synthetic_event_failure")
    if failure == "event":
        monkeypatch.setattr(events, "record", fail_event)
    def publish(sess):
        sess.get(LearningTaskRow, lease.task_id).model_config_version = "published_1"
        if failure == "publish":
            raise RuntimeError("synthetic_publish_failure")
    with pytest.raises(RuntimeError, match="synthetic_.*_failure"):
        tasks.submit_result(lease, stage="generate", status="succeeded", publish=publish)
    with db.session() as sess:
        row = sess.get(LearningTaskRow, lease.task_id)
        attempt = sess.get(TaskAttemptRow, lease.attempt_id)
        assert row.status == "leased" and row.revision == 1
        assert row.lease_token == lease.lease_token and row.model_config_version is None
        assert attempt.result_status is None and attempt.finished_at is None
    assert events.get_state(lease.task_id).revision == 1
    assert len(events.get_events(lease.task_id)) == 1


@pytest.mark.parametrize("reason", ["attempt", "cancel", "deadline", "diagnostic", "duplicate"])
def test_stale_submission_never_publishes_or_overwrites_state(services, reason):
    db, tasks, events = services
    lease = claim(tasks)
    if reason == "attempt":
        lease = replace(lease, attempt_id="forged_attempt")
    elif reason == "cancel":
        mutate(db, LearningTaskRow, lease.task_id, cancel_requested=True)
    elif reason == "deadline":
        mutate(db, LearningTaskRow, lease.task_id, deadline_at=utcnow() - timedelta(seconds=1))
    elif reason == "duplicate":
        tasks.submit_result(lease, stage="generate", status="succeeded")
    before = tasks.get_task(lease.task_id)
    event_state = events.get_state(lease.task_id)
    calls = []
    result = tasks.submit_result(lease, stage="generate", status="failed", publish=calls.append,
                                 diagnostics_only=reason == "diagnostic")
    assert result["accepted"] is False and calls == []
    assert tasks.get_task(lease.task_id) == before
    assert events.get_state(lease.task_id) == event_state
    assert events.get_events(lease.task_id)[-1].status == "stale_discarded"


@pytest.mark.parametrize("field,code", [("lease_until", "LEASE_LOST"), ("deadline_at", "TASK_EXPIRED")])
def test_exact_current_expiry_is_not_live(services, monkeypatch, field, code):
    import backend.tasks.service as task_module
    db, tasks, _ = services
    lease = claim(tasks)
    now = utcnow()
    mutate(db, LearningTaskRow, lease.task_id, **{field: now})
    monkeypatch.setattr(task_module, "utcnow", lambda: now)
    with pytest.raises(LeaseError) as exc:
        tasks.validate_lease(lease)
    assert exc.value.code == code


def test_stale_diagnostic_write_failure_is_not_swallowed(services, monkeypatch):
    _, tasks, events = services
    lease = claim(tasks)
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic_event_failure")
    monkeypatch.setattr(events, "record", fail)
    with pytest.raises(RuntimeError, match="synthetic_event_failure"):
        tasks.submit_result(replace(lease, attempt_id="forged"), stage="generate", status="failed")


def test_event_store_caller_session_retains_transaction_ownership(services):
    db, _, events = services
    with pytest.raises(RuntimeError):
        with db.session() as sess:
            events.create_task("event_only", "user_owner", "subject_1", "upload", session=sess)
            events.record("event_only", stage="generate", status="succeeded", session=sess)
            raise RuntimeError("synthetic_abort")
    with db.session() as sess:
        assert sess.get(TaskStateRow, "event_only") is None
        assert sess.scalar(select(func.count()).select_from(TaskEventRow)) == 0


@pytest.fixture
def disclosure(services):
    db, tasks, _ = services
    task_id = create_task(tasks)
    with db.session() as sess:
        sess.add(ServiceConfigRow(config_id="config_1", owner_user_id="user_owner",
                                  owner_scope="user", kind="content_model", protocol="openai",
                                  endpoint="https://provider.example.test/v1"))
    service = DisclosureService(db)
    scope = {"material_ids": ["material_1", "material_2"], "purpose": "planning",
             "options": {"page_ids": [1, 2], "enabled": True}}
    grant = service.create(actor_id="user_owner", task_id=task_id, config_id="config_1",
                           content_category="material", scope_snapshot=scope)
    return db, service, task_id, grant, scope


def verify(disclosure, **overrides):
    _, service, task_id, _, scope = disclosure
    kwargs = dict(task_id=task_id, config_id="config_1", content_category="material",
                  actor_id="user_owner", scope_snapshot=scope)
    kwargs.update(overrides)
    return service.verify(**kwargs)


@pytest.mark.parametrize("actual", [
    {"material_ids": ["material_1"]},
    {"material_ids": [], "purpose": "planning", "options": {"page_ids": [1], "enabled": True}},
])
def test_disclosure_actual_scope_may_only_narrow(disclosure, actual):
    assert verify(disclosure, scope_snapshot=actual)["grant_id"] == disclosure[3]["grant_id"]


@pytest.mark.parametrize("actual", [
    None, [], {"material_ids": ["material_3"]}, {"purpose": "other"},
    {"unapproved_key": []}, {"options": {"page_ids": [True]}},
    {"options": {"enabled": 1}}, {"material_ids": "material_1"},
    {"options": {"page_ids": [3]}}, {"material_ids": [float("nan")]},
])
def test_disclosure_missing_expanded_or_wrong_type_scope_fails_closed(disclosure, actual):
    with pytest.raises(Slice1Error) as exc:
        verify(disclosure, scope_snapshot=actual)
    assert exc.value.code == "disclosure_required"


@pytest.mark.parametrize("snapshot", ["not-json", "null", "[]", '{"x":NaN}', ""])
def test_malformed_approved_scope_fails_closed(disclosure, snapshot):
    db, _, _, grant, _ = disclosure
    mutate(db, DisclosureGrantRow, grant["grant_id"], scope_snapshot=snapshot)
    with pytest.raises(Slice1Error) as exc:
        verify(disclosure)
    assert exc.value.code == "disclosure_required"


@pytest.mark.parametrize("model,values,code", [
    (DisclosureGrantRow, {"owner_user_id": "intruder"}, "disclosure_required"),
    (DisclosureGrantRow, {"revoked_at": utcnow()}, "disclosure_revoked"),
    (DisclosureGrantRow, {"expires_at": None}, "disclosure_expired"),
    (DisclosureGrantRow, {"expires_at": utcnow() - timedelta(seconds=1)}, "disclosure_expired"),
    (ServiceConfigRow, {"config_version": 2}, "disclosure_revoked"),
    (ServiceConfigRow, {"endpoint": "https://other.example.test/v1"}, "disclosure_revoked"),
    (ServiceConfigRow, {"kind": "tts"}, "disclosure_revoked"),
    (ServiceConfigRow, {"is_active": False}, "config_not_found"),
    (ServiceConfigRow, {"owner_user_id": "intruder"}, "access_denied"),
    (ServiceConfigRow, {"owner_scope": "unknown"}, "access_denied"),
])
def test_disclosure_checks_current_grant_and_config_even_without_actor(disclosure, model, values, code):
    db, _, _, grant, _ = disclosure
    mutate(db, model, grant["grant_id"] if model is DisclosureGrantRow else "config_1", **values)
    with pytest.raises(Slice1Error) as exc:
        verify(disclosure, actor_id=None)
    assert exc.value.code == code


def test_disclosure_requires_existing_owned_task_for_creation_and_verification(disclosure):
    _, service, task_id, _, _ = disclosure
    for ident, actor, code in (("missing_task", "user_owner", "disclosure_required"),
                               (task_id, "intruder", "access_denied")):
        with pytest.raises(Slice1Error) as exc:
            service.create(actor_id=actor, task_id=ident, config_id="config_1", content_category="query")
        assert exc.value.code == code
        with pytest.raises(Slice1Error) as exc:
            verify(disclosure, task_id=ident, actor_id=actor)
        assert exc.value.code == code


def test_shared_admin_configuration_remains_task_owner_bound(disclosure):
    db, _, _, _, _ = disclosure
    mutate(db, ServiceConfigRow, "config_1", owner_scope="admin", owner_user_id=None)
    assert verify(disclosure, actor_id=None)
    with pytest.raises(Slice1Error) as exc:
        verify(disclosure, actor_id="intruder")
    assert exc.value.code == "access_denied"


def test_disclosure_audit_failure_is_visible_and_grant_creation_rolls_back(disclosure):
    db, _, task_id, _, _ = disclosure
    audit = PgAuditLog(db, fail_write=True)
    service = DisclosureService(db, audit)
    with pytest.raises(AuditWriteError):
        service.create(actor_id="user_owner", task_id=task_id, config_id="config_1", content_category="query")
    with db.session() as sess:
        assert sess.scalar(select(func.count()).select_from(DisclosureGrantRow)) == 1
    with pytest.raises(AuditWriteError):
        service.verify_or_audit(task_id=task_id, config_id="config_1", content_category="material")
    assert audit.write_failures == 2


def test_audit_query_cannot_hide_failed_query_audit(services, monkeypatch):
    db, _, _ = services
    audit = PgAuditLog(db, fail_write=True)
    class EmptyRows:
        def scalars(self):
            return self
        def all(self):
            return []
    monkeypatch.setattr(Session, "execute", lambda *args, **kwargs: EmptyRows())
    with pytest.raises(AuditWriteError):
        audit.query(actor_id="admin_1", role="admin")
    assert audit.write_failures == 1
