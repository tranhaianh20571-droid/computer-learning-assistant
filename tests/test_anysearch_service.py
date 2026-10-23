"""Personal search orchestration against synthetic rows and transport, never a provider."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.auth.models import UserRow
from backend.logging_audit.db import Database
from backend.planning.errors import PlanningError
from backend.planning.models import CourseRow, PlanningTaskStateRow
from backend.search.contracts import RawSearchEnvelope, SearchProviderError
from backend.search.repository import SearchCallRow, SearchTaskBudgetRow
from backend.search.service import SearchService
from backend.slice1.capability_refresh import stamp_status
from backend.slice1.config_service import ConfigService
from backend.slice1.models import ServiceConfigRow
from backend.tasks.service import TaskService, utcnow
from backend.logging_audit.repositories import PgTaskEventStore


@pytest.fixture
def env():
    db = Database()
    db.create_all()
    owner, other = ["search_" + uuid.uuid4().hex for _ in range(2)]
    with db.session() as sess:
        for actor in (owner, other):
            sess.add(UserRow(user_id=actor, email=actor + "@example.test", password_hash="synthetic",
                status="approved", is_admin=False))
    configs = ConfigService(db, encryption_key="synthetic-service-encryption")
    tasks = TaskService(db, PgTaskEventStore(db))
    yield SimpleNamespace(db=db, owner=owner, other=other, configs=configs, tasks=tasks)
    from backend.tasks.service import LearningTaskRow
    with db.session() as sess:
        for task in sess.scalars(select(LearningTaskRow).where(
                LearningTaskRow.owner_user_id.in_((owner, other)),
                LearningTaskRow.status.in_(("queued", "leased", "running")))):
            task.status = "cancelled"
            task.cancel_requested = True
            task.lease_owner = task.lease_token = task.lease_until = None
    db.dispose()


def setup(env, *, configured=True, topic="Synthetic algebra"):
    view = None
    if configured:
        view = env.configs.create(actor_id=env.owner, is_admin=False, kind="search", protocol="anysearch",
            endpoint="https://api.anysearch.com", model_name="", credentials={"api_key":"synthetic-service-key"})
        env.configs.set_capability_status(view["config_id"], stamp_status({"text":{"state":"available","detail":""}}, 1),
            expected_version=1, actor_id=env.owner)
    course_id = "course_" + uuid.uuid4().hex
    with env.db.session() as sess:
        sess.add(CourseRow(course_id=course_id, owner_user_id=env.owner, title_ref=topic))
    task_id = env.tasks.create_task(owner_user_id=env.owner, kind="outline_generate", subject_id=course_id,
        idempotency_key=uuid.uuid4().hex)["task_id"]
    with env.db.session() as sess:
        sess.add(PlanningTaskStateRow(task_id=task_id, owner_user_id=env.owner, target_id=course_id,
            target_kind="course", material_ids_json="[]"))
    env.tasks.claim(task_id, lease_owner="synthetic-worker")
    return task_id, course_id, view


class Adapter:
    def __init__(self, *, errors=(), before_result=None):
        self.errors = list(errors)
        self.calls = 0
        self.closed = 0
        self.before_result = before_result

    async def search(self, request, *, check_outbound, before_wire, remaining_seconds):
        assert remaining_seconds() > 0
        body = request.model_dump_json(exclude_none=True).encode()
        check_outbound(body, "https://api.anysearch.com/v1/search")
        before_wire()
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        if self.before_result:
            self.before_result()
        return RawSearchEnvelope.model_validate_json(json.dumps({"code":0,"request_id":str(uuid.uuid4()),
            "data":{"results":[{"title":"Synthetic source", "url":"https://example.test/algebra",
                "snippet":"Synthetic evidence text"}],"metadata":{"total_results":1,"search_time_ms":1}}}))

    async def aclose(self):
        self.closed += 1


def service(env, task, adapter):
    return SearchService(env.db, env.configs, owner_user_id=env.owner, task_id=task,
        adapter_factory=lambda *_: adapter)


def test_success_stores_only_metadata_and_shared_budget(env):
    task, _, _ = setup(env)
    adapter = Adapter()
    runner = service(env, task, adapter)
    result = runner.attempt("Synthetic algebra")
    assert result.state == "succeeded" and runner.continuation.mode == "with_evidence"
    assert adapter.calls == adapter.closed == 1
    with env.db.session() as sess:
        budget = sess.get(SearchTaskBudgetRow, task)
        call = sess.scalars(select(SearchCallRow).where(SearchCallRow.task_id == task)).one()
        assert (budget.logical_count, budget.wire_count) == (1, 1)
        assert call.result_count == 1 and call.state == "succeeded"
        assert not {"query", "title", "url", "excerpt", "body", "api_key"}.intersection(call.__table__.columns.keys())


def test_retired_guest_owner_cannot_claim_search_task(env):
    from backend.tasks.service import LearningTaskRow, LeaseError
    env.owner = "guest_" + uuid.uuid4().hex
    with pytest.raises(LeaseError) as rejected:
        setup(env)
    assert rejected.value.code == "TASK_CANCELLED"
    with env.db.session() as sess:
        task = sess.scalar(select(LearningTaskRow).where(LearningTaskRow.owner_user_id == env.owner))
        assert task.status == "cancelled"
    assert env.configs.active_search_for_owner(env.other) is None


def test_same_slot_never_replays_after_memory_loss(env):
    task, _, _ = setup(env)
    adapter = Adapter()
    service(env, task, adapter).attempt("Synthetic algebra")
    recovered = service(env, task, adapter)
    result = recovered.attempt("Synthetic algebra")
    assert result.error_code == "SEARCH_INTERRUPTED" and recovered.continuation.mode == "offline"
    assert adapter.calls == 1
    with env.db.session() as sess:
        assert sess.get(SearchTaskBudgetRow, task).logical_count == 1


@pytest.mark.parametrize("code", ["SEARCH_CREDENTIAL_INVALID", "SEARCH_QUOTA_EXCEEDED",
    "SEARCH_OUTPUT_INVALID", "SEARCH_RESPONSE_TOO_LARGE", "SEARCH_REQUEST_REJECTED"])
def test_permanent_provider_failure_is_not_retried(env, code):
    task, _, _ = setup(env)
    adapter = Adapter(errors=[SearchProviderError(code)])
    runner = service(env, task, adapter)
    assert runner.attempt("Synthetic algebra").error_code == code
    assert runner.continuation.mode == "offline" and adapter.calls == adapter.closed == 1


def test_only_confirmed_unsent_connect_failure_retries(env):
    task, _, _ = setup(env)
    adapter = Adapter(errors=[SearchProviderError("SEARCH_CONNECTION_ERROR", unsent=True)])
    runner = service(env, task, adapter)
    assert runner.attempt("Synthetic algebra").state == "succeeded"
    assert adapter.calls == 2 and adapter.closed == 1
    with env.db.session() as sess:
        assert sess.get(SearchTaskBudgetRow, task).wire_count == 2


@pytest.mark.parametrize("mutation", ["rotate", "disable", "cancel", "topic", "delete"])
def test_control_change_before_response_cannot_become_offline_success(env, mutation):
    task, course, view = setup(env)
    def change():
        if mutation == "rotate":
            env.configs.update(view["config_id"], actor_id=env.owner, is_admin=False,
                credentials={"api_key":"synthetic-new-service-key"})
        elif mutation == "disable":
            env.configs.deactivate(view["config_id"], actor_id=env.owner, is_admin=False)
        elif mutation == "cancel":
            from backend.tasks.service import LearningTaskRow
            with env.db.session() as sess:
                sess.get(LearningTaskRow, task).cancel_requested = True
        else:
            with env.db.session() as sess:
                target = sess.get(CourseRow, course)
                if mutation == "topic": target.title_ref = "A different topic"
                else: target.deleted_at = utcnow()
    adapter = Adapter(before_result=change)
    with pytest.raises(Exception) as failure:
        service(env, task, adapter).attempt("Synthetic algebra")
    assert getattr(failure.value, "code", None) in ("TASK_CANCELLED", "TASK_CANCEL_REQUESTED")
    assert adapter.calls == adapter.closed == 1


@pytest.mark.parametrize("status", [
    {},
    {"text": {"state": "unknown", "config_version": 1}},
    {"text": {"state": "available", "config_version": 0}},
    {"text": {"state": "available", "config_version": 1,
        "expires_at": "2020-01-01T00:00:00Z"}},
])
def test_unprobed_or_stale_probe_can_execute_actual_search(env, status):
    task, _, view = setup(env)
    with env.db.session() as sess:
        sess.get(ServiceConfigRow, view["config_id"]).capability_status = json.dumps(status)
    adapter = Adapter()
    result = service(env, task, adapter).attempt("Synthetic algebra")
    assert result.state == "succeeded" and adapter.calls == adapter.closed == 1


def test_disabled_and_unconfigured_search_do_not_touch_other_user_key(env, monkeypatch):
    task, _, _ = setup(env, configured=False)
    env.configs.create(actor_id=env.other, is_admin=False, kind="search", protocol="anysearch",
        endpoint="https://api.anysearch.com", model_name="", credentials={"api_key":"synthetic-other-user-key"})
    monkeypatch.setattr(env.configs, "resolve_search_credentials", lambda *_a, **_k: pytest.fail("foreign key decrypted"))
    adapter = Adapter()
    assert service(env, task, adapter).attempt("Synthetic algebra").reason == "no_config"
    monkeypatch.setattr(env.configs, "active_search_for_owner", lambda *_: pytest.fail("disabled config read"))
    assert service(env, task, adapter).attempt("Synthetic algebra", disabled=True).reason == "disabled"
    assert adapter.calls == 0


def test_persistent_deadline_exhaustion_does_not_restart_window(env):
    task, _, _ = setup(env)
    adapter = Adapter()
    runner = service(env, task, adapter)
    runner.attempt("Synthetic algebra")
    with env.db.session() as sess:
        deadline = sess.get(SearchTaskBudgetRow, task)
        started = deadline.search_window_started_at
        deadline.search_deadline_at = utcnow()-timedelta(seconds=1)
    result = runner.attempt("Synthetic algebra", logical_slot=2)
    assert result.error_code == "SEARCH_TIMEOUT" and adapter.calls == 1
    with env.db.session() as sess:
        assert sess.get(SearchTaskBudgetRow, task).search_window_started_at == started


def test_same_version_credential_failure_blocks_future_wire_until_new_probe(env):
    task, course, view = setup(env)
    adapter = Adapter(errors=[SearchProviderError("SEARCH_CREDENTIAL_INVALID")])
    assert service(env, task, adapter).attempt("Synthetic algebra").error_code == "SEARCH_CREDENTIAL_INVALID"
    def next_task():
        ident = env.tasks.create_task(owner_user_id=env.owner, kind="outline_generate", subject_id=course,
            idempotency_key=uuid.uuid4().hex)["task_id"]
        with env.db.session() as sess:
            sess.add(PlanningTaskStateRow(task_id=ident, owner_user_id=env.owner, target_id=course,
                target_kind="course", material_ids_json="[]"))
        env.tasks.claim(ident, lease_owner="synthetic-worker")
        return ident
    blocked = service(env, next_task(), adapter).attempt("Synthetic algebra")
    assert blocked.error_code == "SEARCH_CREDENTIAL_INVALID" and adapter.calls == 1
    env.configs.set_capability_status(view["config_id"], stamp_status({"text":{"state":"available","detail":""}}, 1),
        expected_version=1, actor_id=env.owner)
    assert service(env, next_task(), adapter).attempt("Synthetic algebra").state == "succeeded"
    assert adapter.calls == 2


def test_unusable_secret_is_controlled_offline_not_raw_exception(env, monkeypatch):
    task, _, _ = setup(env)
    from backend.slice1.errors import Slice1Error
    def broken(*args, **kwargs):
        raise Slice1Error("credential_invalid", "synthetic-decryption-detail")
    monkeypatch.setattr(env.configs, "resolve_search_credentials", broken)
    adapter = Adapter()
    runner = service(env, task, adapter)
    assert runner.attempt("Synthetic algebra").error_code == "SEARCH_CREDENTIAL_INVALID"
    assert runner.continuation.mode == "offline" and adapter.calls == 0


def test_optional_tool_reuses_memory_but_never_rebinds_rotated_key(env):
    task, _, view = setup(env)
    adapter = Adapter()
    runner = service(env, task, adapter)
    result = runner.attempt("Synthetic algebra")
    from backend.search.agent_capability import bind_agent_search
    from backend.search.contracts import SearchArgs
    execute = bind_agent_search(runner, "Synthetic algebra")
    cached, continuation = asyncio.run(execute(SearchArgs(query="Synthetic algebra", max_results=1)))
    assert cached.query_id == result.query_id and len(cached.results) == 1
    assert adapter.calls == 1 and continuation.mode == "with_evidence"
    env.configs.update(view["config_id"], actor_id=env.owner, is_admin=False,
        credentials={"api_key":"synthetic-rotated-key"})
    with pytest.raises(PlanningError):
        runner.attempt("Synthetic algebra", logical_slot=2)
    assert adapter.calls == 1


def test_audit_failure_rolls_back_wire_reservation_and_never_sends(env, monkeypatch):
    task, _, _ = setup(env)
    from backend.logging_audit.audit import AuditWriteError
    from backend.logging_audit.repositories import PgAuditLog
    original = PgAuditLog.append
    def reject(self, event, **kwargs):
        if event == "search.execution":
            raise AuditWriteError("synthetic-search-audit-failure")
        return original(self, event, **kwargs)
    monkeypatch.setattr(PgAuditLog, "append", reject)
    adapter = Adapter()
    with pytest.raises(AuditWriteError):
        service(env, task, adapter).attempt("Synthetic algebra")
    assert adapter.calls == 0 and adapter.closed == 1
    with env.db.session() as sess:
        assert sess.get(SearchTaskBudgetRow, task).wire_count == 0


def test_failed_completion_audit_cannot_publish_reusable_evidence(env, monkeypatch):
    task, _, _ = setup(env)
    from backend.logging_audit.audit import AuditWriteError
    from backend.logging_audit.repositories import PgAuditLog
    original = PgAuditLog.append
    def reject(self, event, **kwargs):
        if event == "search.execution" and kwargs.get("result") == "succeeded":
            raise AuditWriteError("synthetic-search-completion-audit-failure")
        return original(self, event, **kwargs)
    monkeypatch.setattr(PgAuditLog, "append", reject)
    runner = service(env, task, Adapter())
    with pytest.raises(AuditWriteError):
        runner.attempt("Synthetic algebra")
    assert runner.outcome is None and runner.continuation is None
    with env.db.session() as sess:
        call = sess.scalar(select(SearchCallRow).where(SearchCallRow.task_id == task))
        assert call.state == "sending" and call.result_count == 0


def test_search_audit_uses_system_identity_not_learner_confirmation(env):
    task, _, _ = setup(env)
    service(env, task, Adapter()).attempt("Synthetic algebra")
    from backend.logging_audit.models import AuditLogRow
    with env.db.session() as sess:
        records = list(sess.scalars(select(AuditLogRow).where(
            AuditLogRow.object_id == task, AuditLogRow.event == "search.execution")))
        assert [record.result for record in records] == ["sending", "succeeded"]
        assert all(record.actor_id == "system:search" and record.target_actor_id == env.owner for record in records)
        assert not any(record.event == "external.disclosure.confirmed" for record in records)
