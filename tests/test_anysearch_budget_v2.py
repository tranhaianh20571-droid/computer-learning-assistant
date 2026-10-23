"""Search v2 boundary regressions with synthetic transport and no real sleeps."""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select

from backend.search.binding import bind_search
from backend.search.contracts import RawSearchEnvelope, SearchArgs, SearchProviderError
from backend.search.repository import SearchCallRow, SearchRepository, SearchTaskBudgetRow
from backend.slice1.capability_refresh import stamp_status
from backend.tasks.service import LearningTaskRow, utcnow

from test_anysearch_service import Adapter, env, service, setup


@pytest.fixture
def fast_sleep(monkeypatch):
    waits = []
    async def sleep(delay):
        waits.append(delay)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    return waits


def counts(env, task):
    with env.db.session() as sess:
        budget = sess.get(SearchTaskBudgetRow, task)
        call = sess.scalar(select(SearchCallRow).where(SearchCallRow.task_id == task))
        return (budget.logical_count, budget.wire_count, budget.retry_count,
            call.wire_count, call.retry_count)


@pytest.mark.parametrize("code", ["SEARCH_CONNECTION_ERROR", "SEARCH_TIMEOUT",
    "SEARCH_PROVIDER_UNAVAILABLE", "SEARCH_RATE_LIMITED"])
def test_sixth_request_succeeds_after_five_shared_retries(env, fast_sleep, code):
    task, _, _ = setup(env)
    adapter = Adapter(errors=[SearchProviderError(code, retryable=True) for _ in range(5)])
    result = service(env, task, adapter).attempt("Synthetic algebra")
    assert result.state == "succeeded"
    assert adapter.calls == 6 and adapter.closed == 1
    assert counts(env, task) == (1, 6, 5, 6, 5)
    assert fast_sleep and all(0 < wait <= .25 for wait in fast_sleep)


def test_sixth_failure_stops_without_seventh_wire_or_reset_on_recovery(env, fast_sleep):
    task, _, _ = setup(env)
    adapter = Adapter(errors=[SearchProviderError("SEARCH_PROVIDER_UNAVAILABLE", retryable=True)
        for _ in range(7)])
    runner = service(env, task, adapter)
    assert runner.attempt("Synthetic algebra").error_code == "SEARCH_PROVIDER_UNAVAILABLE"
    assert runner.continuation.mode == "offline"
    assert adapter.calls == 6 and counts(env, task) == (1, 6, 5, 6, 5)
    recovered = service(env, task, adapter)
    assert recovered.attempt("Synthetic algebra").error_code == "SEARCH_INTERRUPTED"
    assert adapter.calls == 6 and counts(env, task) == (1, 6, 5, 6, 5)


@pytest.mark.parametrize("code", ["SEARCH_CREDENTIAL_INVALID", "SEARCH_QUOTA_EXCEEDED",
    "SEARCH_OUTPUT_INVALID", "SEARCH_RESPONSE_TOO_LARGE", "SEARCH_REQUEST_REJECTED"])
def test_permanent_error_does_not_retry_even_if_provider_marks_retryable(env, fast_sleep, code):
    task, _, _ = setup(env)
    adapter = Adapter(errors=[SearchProviderError(code, retryable=True)])
    assert service(env, task, adapter).attempt("Synthetic algebra").error_code == code
    assert adapter.calls == adapter.closed == 1
    assert counts(env, task) == (1, 1, 0, 1, 0) and fast_sleep == []


def test_new_stage_window_is_120_seconds_and_not_clipped_to_initial_lease(env):
    task, _, _ = setup(env)
    runner = service(env, task, Adapter())
    runner.attempt("Synthetic algebra")
    with env.db.session() as sess:
        budget = sess.get(SearchTaskBudgetRow, task)
        stage = (budget.search_window_started_at, budget.search_deadline_at)
        assert 119.5 < (stage[1] - stage[0]).total_seconds() <= 120
        assert budget.search_deadline_at > sess.get(LearningTaskRow, task).lease_until
        assert budget.budget_policy_version == "personal-v2"
    service(env, task, Adapter()).attempt("Synthetic algebra", logical_slot=2)
    with env.db.session() as sess:
        budget = sess.get(SearchTaskBudgetRow, task)
        assert (budget.search_window_started_at, budget.search_deadline_at) == stage


def binding_and_call(env):
    task, _, view = setup(env)
    binding = bind_search(env.db, view, owner_user_id=env.owner, task_id=task,
        query="Synthetic algebra")
    repository = SearchRepository(env.db)
    query_id, _, refusal = repository.reserve(binding)
    assert refusal is None
    return task, binding, repository, query_id


def test_concurrent_retry_reservations_never_exceed_shared_five(env):
    task, binding, repository, query_id = binding_and_call(env)
    def consume(_):
        try:
            return repository.consume_retry(binding, query_id)
        except SearchProviderError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(consume, range(12)))
    assert sorted(item for item in outcomes if isinstance(item, int)) == [1, 2, 3, 4, 5]
    assert outcomes.count("SEARCH_BUDGET_EXCEEDED") == 7
    assert counts(env, task) == (1, 0, 5, 0, 5)
    recovered = SearchRepository(env.db)
    with pytest.raises(SearchProviderError, match="SEARCH_BUDGET_EXCEEDED"):
        recovered.consume_retry(binding, query_id)


def test_different_logical_calls_share_retry_and_wire_limits(env):
    task, binding, repository, first = binding_and_call(env)
    second, deadline, refusal = repository.reserve(binding, logical_slot=2)
    assert refusal is None
    for _ in range(5):
        repository.consume_retry(binding, first)
    with pytest.raises(SearchProviderError, match="SEARCH_BUDGET_EXCEEDED"):
        repository.consume_retry(binding, second)
    for index in range(6):
        repository.consume_wire(binding, first if index % 2 else second)
    with pytest.raises(SearchProviderError, match="SEARCH_BUDGET_EXCEEDED"):
        repository.consume_wire(binding, first)
    with env.db.session() as sess:
        budget = sess.get(SearchTaskBudgetRow, task)
        calls = list(sess.scalars(select(SearchCallRow).where(SearchCallRow.task_id == task)))
        assert (budget.logical_count, budget.wire_count, budget.retry_count) == (2, 6, 5)
        assert sum(call.wire_count for call in calls) == 6
        assert sum(call.retry_count for call in calls) == 5
        assert budget.search_deadline_at == deadline


def test_expired_shared_deadline_refuses_retry_without_consuming_count(env):
    task, binding, repository, query_id = binding_and_call(env)
    with env.db.session() as sess:
        sess.get(SearchTaskBudgetRow, task).search_deadline_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(SearchProviderError, match="SEARCH_TIMEOUT"):
        repository.consume_retry(binding, query_id)
    assert counts(env, task) == (1, 0, 0, 0, 0)


def test_each_request_is_capped_at_twenty_seconds(env, fast_sleep, monkeypatch):
    task, _, _ = setup(env)
    durations = []
    original = asyncio.timeout
    def timeout(delay):
        durations.append(delay)
        return original(delay)
    monkeypatch.setattr(asyncio, "timeout", timeout)
    adapter = Adapter(errors=[TimeoutError()])
    assert service(env, task, adapter).attempt("Synthetic algebra").state == "succeeded"
    assert durations == [20, 20] and counts(env, task) == (1, 2, 1, 2, 1)


def test_short_remaining_stage_window_caps_single_request(env, fast_sleep, monkeypatch):
    task, binding, repository, _ = binding_and_call(env)
    with env.db.session() as sess:
        sess.get(SearchTaskBudgetRow, task).search_deadline_at = utcnow() + timedelta(seconds=4)
    durations = []
    original = asyncio.timeout
    def timeout(delay):
        durations.append(delay)
        return original(delay)
    monkeypatch.setattr(asyncio, "timeout", timeout)
    assert service(env, task, Adapter()).attempt("Synthetic algebra", logical_slot=2).state == "succeeded"
    assert len(durations) == 1 and 0 < durations[0] <= 4


def test_retry_after_larger_than_remaining_window_stops_without_waiting(env, fast_sleep):
    task, course, _ = setup(env)
    adapter = Adapter(errors=[SearchProviderError("SEARCH_RATE_LIMITED", retryable=True,
        retry_after=utcnow() + timedelta(seconds=121))])
    result = service(env, task, adapter).attempt("Synthetic algebra")
    assert result.error_code == "SEARCH_TIMEOUT"
    assert adapter.calls == 1 and fast_sleep == []
    following = next_task_for_course(env, course)
    assert service(env, following, adapter).attempt("Synthetic algebra").error_code == "SEARCH_RATE_LIMITED"
    assert adapter.calls == 1


def test_cancel_during_retry_after_wait_prevents_second_wire(env, monkeypatch):
    task, _, _ = setup(env)
    waits = []
    async def cancel(delay):
        waits.append(delay)
        with env.db.session() as sess:
            sess.get(LearningTaskRow, task).cancel_requested = True
    monkeypatch.setattr(asyncio, "sleep", cancel)
    adapter = Adapter(errors=[SearchProviderError("SEARCH_RATE_LIMITED", retryable=True,
        retry_after=utcnow() + timedelta(seconds=10))])
    with pytest.raises(Exception) as rejected:
        service(env, task, adapter).attempt("Synthetic algebra")
    assert getattr(rejected.value, "code", None) in ("TASK_CANCELLED", "TASK_CANCEL_REQUESTED")
    assert adapter.calls == adapter.closed == 1 and len(waits) == 1 and waits[0] <= .25
    assert counts(env, task) == (1, 1, 1, 1, 1)


def test_failed_probe_does_not_clear_same_version_credential_block(env):
    task, course, view = setup(env)
    adapter = Adapter(errors=[SearchProviderError("SEARCH_CREDENTIAL_INVALID")])
    assert service(env, task, adapter).attempt("Synthetic algebra").error_code == "SEARCH_CREDENTIAL_INVALID"
    env.configs.set_capability_status(view["config_id"],
        stamp_status({"text": {"state": "unknown", "detail": "SEARCH_TIMEOUT"}}, 1),
        expected_version=1, actor_id=env.owner)
    next_task = env.tasks.create_task(owner_user_id=env.owner, kind="outline_generate", subject_id=course,
        idempotency_key="synthetic-next-" + task)["task_id"]
    from backend.planning.models import PlanningTaskStateRow
    with env.db.session() as sess:
        sess.add(PlanningTaskStateRow(task_id=next_task, owner_user_id=env.owner, target_id=course,
            target_kind="course", material_ids_json="[]"))
    env.tasks.claim(next_task, lease_owner="synthetic-worker")
    assert service(env, next_task, adapter).attempt("Synthetic algebra").error_code == "SEARCH_CREDENTIAL_INVALID"
    assert adapter.calls == 1


def test_default_ten_results_reach_model_tool_without_another_wire(env):
    task, _, _ = setup(env)
    class TenResults(Adapter):
        async def search(self, request, **kwargs):
            await super().search(request, **kwargs)
            assert request.max_results == 10
            return RawSearchEnvelope.model_validate_json(json.dumps({"code": 0,
                "request_id": "11111111-1111-4111-8111-111111111111", "data": {
                    "results": [{"title": "Synthetic source", "url": f"https://example.test/source/{index}",
                        "snippet": "Synthetic evidence"} for index in range(10)],
                    "metadata": {"total_results": 10, "search_time_ms": 1}}}))
    adapter = TenResults()
    runner = service(env, task, adapter)
    first = runner.attempt("Synthetic algebra")
    assert len(first.results) == 10
    from backend.search.agent_capability import bind_agent_search
    execute = bind_agent_search(runner, "Synthetic algebra")
    cached, continuation = asyncio.run(execute(SearchArgs(query="Synthetic algebra")))
    assert len(cached.results) == 10 and cached.query_id == first.query_id
    assert adapter.calls == 1 and continuation.mode == "with_evidence"


def next_task_for_course(env, course):
    from backend.planning.models import PlanningTaskStateRow
    import uuid
    task = env.tasks.create_task(owner_user_id=env.owner, kind="outline_generate", subject_id=course,
        idempotency_key=uuid.uuid4().hex)["task_id"]
    with env.db.session() as sess:
        sess.add(PlanningTaskStateRow(task_id=task, owner_user_id=env.owner, target_id=course,
            target_kind="course", material_ids_json="[]"))
    env.tasks.claim(task, lease_owner="synthetic-worker")
    return task


def config_text(env, view):
    from backend.slice1.models import ServiceConfigRow
    with env.db.session() as sess:
        return sess.get(ServiceConfigRow, view["config_id"]).status_dict()["text"]


@pytest.mark.parametrize("empty", [False, True])
def test_real_success_or_empty_refreshes_status_but_unwired_block_does_not(env, empty):
    from datetime import datetime
    task, course, view = setup(env)
    class Response(Adapter):
        async def search(self, request, **kwargs):
            raw = await super().search(request, **kwargs)
            if empty:
                raw = RawSearchEnvelope.model_validate_json(json.dumps({"code": 0,
                    "request_id": "11111111-1111-4111-8111-111111111111", "data": {
                        "results": [], "metadata": {"total_results": 0, "search_time_ms": 1}}}))
            return raw
    before = config_text(env, view)["checked_at"]
    assert service(env, task, Response()).attempt("Synthetic algebra").state == ("empty" if empty else "succeeded")
    available = config_text(env, view)
    assert available["state"] == "available" and available["config_version"] == 1
    checked = datetime.fromisoformat(available["checked_at"])
    expires = datetime.fromisoformat(available["expires_at"])
    assert checked > datetime.fromisoformat(before) and expires > utcnow()
    assert (expires - checked).total_seconds() == 6 * 60 * 60
    failed_task = next_task_for_course(env, course)
    adapter = Adapter(errors=[SearchProviderError("SEARCH_CREDENTIAL_INVALID")])
    assert service(env, failed_task, adapter).attempt("Synthetic algebra").error_code == "SEARCH_CREDENTIAL_INVALID"
    blocked = config_text(env, view)
    assert blocked["state"] == "unavailable" and blocked["detail"] == "SEARCH_CREDENTIAL_INVALID"
    skipped_task = next_task_for_course(env, course)
    assert service(env, skipped_task, adapter).attempt("Synthetic algebra").error_code == "SEARCH_CREDENTIAL_INVALID"
    assert adapter.calls == 1 and config_text(env, view) == blocked


def test_older_success_cannot_clear_newer_same_version_credential_failure(env):
    task, course, view = setup(env)
    failure_task = next_task_for_course(env, course)
    newer_failure = Adapter(errors=[SearchProviderError("SEARCH_CREDENTIAL_INVALID")])
    status = []
    class OlderSuccess(Adapter):
        async def search(self, request, **kwargs):
            raw = await super().search(request, **kwargs)
            assert (await service(env, failure_task, newer_failure).execute("Synthetic algebra")).error_code == "SEARCH_CREDENTIAL_INVALID"
            status.append(config_text(env, view))
            return raw
    assert service(env, task, OlderSuccess()).attempt("Synthetic algebra").state == "succeeded"
    assert len(status) == 1 and status[0]["state"] == "unavailable"
    assert config_text(env, view) == status[0]


def test_cancel_in_flight_cancels_transport_and_cannot_publish_evidence(env):
    task, _, _ = setup(env)
    cancelled = []
    class Pending(Adapter):
        async def search(self, request, **kwargs):
            await super().search(request, **kwargs)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
    adapter = Pending()
    runner = service(env, task, adapter)
    async def scenario():
        nonlocal entered
        entered = asyncio.Event()
        execution = asyncio.create_task(runner.execute("Synthetic algebra"))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            with env.db.session() as sess:
                sess.get(LearningTaskRow, task).cancel_requested = True
            with pytest.raises(Exception) as rejected:
                await asyncio.wait_for(execution, timeout=2)
            assert getattr(rejected.value, "code", None) in ("TASK_CANCELLED", "TASK_CANCEL_REQUESTED")
        finally:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
    entered = None
    asyncio.run(scenario())
    assert cancelled == [True] and adapter.calls == adapter.closed == 1
    assert runner.outcome is None
    with env.db.session() as sess:
        call = sess.scalar(select(SearchCallRow).where(SearchCallRow.task_id == task))
        assert call.state == "sending" and call.result_count == 0


def test_runtime_log_keeps_search_trace_counts_and_fixed_reason_without_content(env, fast_sleep, tmp_path, monkeypatch):
    from backend.logging_audit import runtime_sink
    monkeypatch.setenv("RUNTIME_LOG_DIR", str(tmp_path))
    runtime_sink._sink.cache_clear()
    try:
        task, _, view = setup(env)
        adapter = Adapter(errors=[SearchProviderError("SEARCH_PROVIDER_UNAVAILABLE", retryable=True)])
        assert service(env, task, adapter).attempt("Synthetic algebra").state == "succeeded"
        lines = [line for path in tmp_path.glob("runtime-*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()]
        rows = [json.loads(line) for line in lines]
        search = [row for row in rows if row["event"].startswith("search.") and row.get("task_id") == task]
        assert [row["event"] for row in search] == ["search.request.failed", "search.request.completed", "search.execution.completed"]
        assert [(row["requests"], row["retry_count"]) for row in search] == [(1, 0), (2, 1), (2, 1)]
        assert search[-1]["reason"] == "success"
        assert all(row["trace_id"] and row["attempt"] == 1 and row["config_id"] == view["config_id"] for row in search)
        assert len({row["trace_id"] for row in search}) == 1
        text = "\n".join(lines)
        assert not any(content in text for content in ("Synthetic algebra", "Synthetic source", "Synthetic evidence text", "synthetic-service-key", "https://example.test/algebra"))
        assert all(not {"query", "title", "url", "excerpt", "prompt", "response", "api_key"} & row.keys() for row in search)
    finally:
        runtime_sink._sink.cache_clear()
