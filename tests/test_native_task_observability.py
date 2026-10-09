"""Synthetic SQLite lifecycle spans and ASGI ingress; no external transport."""
from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from backend.agents.task_binding import AgentBudgetRow
from backend.logging_audit import otel_setup
from backend.logging_audit.db import Database
from backend.logging_audit.models import TaskEventRow, TaskStateRow
from backend.logging_audit.otel_setup import ObservabilityRuntime
from backend.logging_audit.repositories import PgTaskEventStore
from backend.logging_audit.trace import TraceContext, business_span, capture_task_traceparent, trace_context
from backend.tasks.service import LearningTaskRow, TaskAttemptRow, TaskService

PRIVATE = "SYNTHETIC_PRIVATE_TASK_BODY_53c2"
HEADER = "00-0123456789abcdef0123456789abcdef-123456789abcdef0-03"


@pytest.fixture
def env(monkeypatch):
    db = Database("sqlite+pysqlite:///:memory:")
    for row in (LearningTaskRow, AgentBudgetRow, TaskAttemptRow, TaskStateRow, TaskEventRow):
        row.__table__.create(db.engine)
    tasks = TaskService(db, PgTaskEventStore(db))
    exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    runtime = ObservabilityRuntime(span_exporter=exporter, metric_readers=[reader], environment="test")
    monkeypatch.setattr(otel_setup, "get_runtime", lambda: runtime)
    try:
        yield db, tasks, runtime, exporter, reader
    finally:
        assert runtime.close(2000)
        db.dispose()


def create(tasks):
    return tasks.create_task(owner_user_id=PRIVATE, kind="generic", subject_id=PRIVATE,
                             idempotency_key=PRIVATE)["task_id"]


def spans(env):
    _, _, runtime, exporter, reader = env
    assert runtime.force_flush(2000)
    result = exporter.get_finished_spans()
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    assert PRIVATE.encode() not in encode_spans(result).SerializeToString()
    for span in result:
        assert not span.events and not span.context.trace_state
        assert span.status.description is None
    data = reader.get_metrics_data()
    if data:
        assert PRIVATE not in repr(data)
        for resource in data.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    for point in metric.data.data_points:
                        assert "task.id" not in point.attributes
    return result


def test_queue_captures_recorded_root_and_dedup_preserves_binding(env):
    db, tasks, _, _, _ = env
    with business_span("receive") as root:
        ident = create(tasks)
    first = next(span for span in spans(env) if span.name == "business.queue")
    assert first.parent.span_id == root.get_span_context().span_id
    with db.session() as session:
        binding = session.get(AgentBudgetRow, ident).traceparent
    assert int(binding.split("-")[2], 16) == first.context.span_id
    with business_span("receive"):
        assert create(tasks) == ident
    with db.session() as session:
        assert session.get(AgentBudgetRow, ident).traceparent == binding
    assert len(tasks.events.get_events(ident)) == 1


def test_result_save_transaction_fence_publish_hierarchy(env):
    db, tasks, _, _, _ = env
    lease = tasks.claim(create(tasks), lease_owner="synthetic_worker")
    with business_span("task_attempt") as root:
        def publish(session):
            session.get(LearningTaskRow, lease.task_id).model_config_version = PRIVATE
        assert tasks.submit_result(lease, stage="generate", status="succeeded", publish=publish)["accepted"]
    observed = {span.name: span for span in spans(env)}
    save = observed["business.save"]
    transaction = observed["business.transaction"]
    assert save.parent.span_id == root.get_span_context().span_id
    assert transaction.parent.span_id == save.context.span_id
    for name in ("business.commit_fence", "business.publish"):
        assert observed[name].parent.span_id == transaction.context.span_id
        assert observed[name].attributes["task.id"] == lease.task_id
        assert observed[name].attributes["attempt"] == lease.attempt
    with db.session() as session:
        assert session.get(LearningTaskRow, lease.task_id).model_config_version == PRIVATE


def test_publish_error_keeps_exception_and_rolls_back_with_safe_error_spans(env):
    db, tasks, _, _, _ = env
    lease = tasks.claim(create(tasks), lease_owner="synthetic_worker")
    original = RuntimeError(PRIVATE)
    def publish(session):
        session.get(LearningTaskRow, lease.task_id).model_config_version = PRIVATE
        raise original
    with pytest.raises(RuntimeError) as caught:
        tasks.submit_result(lease, stage="generate", status="succeeded", publish=publish)
    assert caught.value is original
    observed = {span.name: span for span in spans(env)}
    for name in ("business.save", "business.transaction", "business.publish"):
        assert observed[name].status.status_code is trace.StatusCode.ERROR
        assert observed[name].attributes["outcome"] == "failed"
    with db.session() as session:
        row = session.get(LearningTaskRow, lease.task_id)
        assert row.status == "leased" and row.model_config_version is None
    assert len(tasks.events.get_events(lease.task_id)) == 1


def test_stale_fence_does_not_publish(env):
    _, tasks, _, _, _ = env
    lease = tasks.claim(create(tasks), lease_owner="synthetic_worker")
    calls = []
    result = tasks.submit_result(replace(lease, attempt_id="synthetic_forged"), stage="generate",
                                 status="succeeded", publish=calls.append)
    assert result["accepted"] is False and calls == []
    observed = spans(env)
    assert "business.publish" not in {span.name for span in observed}
    fence = next(span for span in observed if span.name == "business.commit_fence")
    assert fence.status.status_code is trace.StatusCode.ERROR
    assert tasks.get_task(lease.task_id)["status"] == "leased"


def test_tracer_start_failure_cannot_fail_commit_or_correlation(env, monkeypatch):
    db, tasks, runtime, _, _ = env
    def broken(*args, **kwargs):
        raise RuntimeError(PRIVATE)
    monkeypatch.setattr(runtime.tracer, "start_span", broken)
    incoming = TraceContext("1" * 32, "2" * 16)
    with trace_context(incoming):
        ident = create(tasks)
        assert capture_task_traceparent() == incoming.as_traceparent()
    with db.session() as session:
        assert session.get(AgentBudgetRow, ident).traceparent == incoming.as_traceparent()
    lease = tasks.claim(ident, lease_owner="synthetic_worker")
    assert tasks.submit_result(lease, stage="generate", status="succeeded")["accepted"]
    assert not spans(env)


def test_no_runtime_preserves_incoming_parent(monkeypatch):
    monkeypatch.setattr(otel_setup, "get_runtime", lambda: None)
    incoming = TraceContext("3" * 32, "4" * 16)
    with trace_context(incoming), business_span("queue"):
        assert capture_task_traceparent() == incoming.as_traceparent()
    assert not trace.get_current_span().get_span_context().is_valid


@pytest.mark.parametrize("header", [HEADER, "malformed-" + PRIVATE])
def test_asgi_ingress_validates_only_traceparent_ignores_private_headers(env, monkeypatch, header):
    import backend.logging_audit.db as db_module
    monkeypatch.setattr(db_module, "get_db", lambda: env[0])
    from backend.logging_audit.app import AppState, create_app
    app = create_app(AppState(env[0]))
    @app.get("/synthetic-private")
    async def endpoint():
        with business_span("auth"):
            return {"traceparent": capture_task_traceparent()}
    with TestClient(app) as client:
        response = client.get("/synthetic-private?payload=" + PRIVATE,
                              headers={"traceparent": header, "tracestate": "private=" + PRIVATE,
                                       "baggage": "secret=" + PRIVATE, "authorization": PRIVATE})
    assert response.status_code == 200
    observed = {span.name: span for span in spans(env)}
    receive = observed["business.receive"]
    assert observed["business.auth"].parent.span_id == receive.context.span_id
    assert response.headers["x-trace-id"] == f"{receive.context.trace_id:032x}"
    if header == HEADER:
        assert receive.context.trace_id == int(HEADER.split("-")[1], 16)
        assert receive.context.trace_flags.sampled
    else:
        assert receive.context.trace_id != int(HEADER.split("-")[1], 16)
    assert capture_task_traceparent() is None


def test_standalone_ingress_uses_same_owned_runtime_without_network(env):
    from backend.logging_audit.server import ApiHandler
    handler = object.__new__(ApiHandler)
    handler.headers = {"traceparent": HEADER, "tracestate": PRIVATE, "baggage": PRIVATE}
    captured = []
    handler._with_trace(lambda: captured.append(capture_task_traceparent()))
    receive = next(span for span in spans(env) if span.name == "business.receive")
    assert captured == [f"00-{receive.context.trace_id:032x}-{receive.context.span_id:016x}-01"]
    assert handler._current_trace_id == f"{receive.context.trace_id:032x}"


def test_telemetry_status_failure_does_not_replace_business_exception(env, monkeypatch):
    original = RuntimeError(PRIVATE)
    def broken(*args, **kwargs):
        raise ValueError("synthetic telemetry failure")
    with pytest.raises(RuntimeError) as caught:
        with business_span("domain_validate") as span:
            monkeypatch.setattr(span, "set_status", broken)
            raise original
    assert caught.value is original
    observed = spans(env)
    assert observed[-1].attributes["outcome"] == "failed"


def test_blocked_export_does_not_block_task_commit(env, monkeypatch):
    _, tasks, runtime, exporter, _ = env
    entered = threading.Event()
    release = threading.Event()
    original = exporter.export
    def blocked(batch):
        entered.set()
        assert release.wait(2)
        return original(batch)
    monkeypatch.setattr(exporter, "export", blocked)
    try:
        ident = create(tasks)
        assert entered.wait(1)
        started = time.monotonic()
        lease = tasks.claim(ident, lease_owner="synthetic_worker")
        assert tasks.submit_result(lease, stage="generate", status="succeeded")["accepted"]
        assert time.monotonic() - started < 0.5
        assert not runtime.force_flush(20)
    finally:
        release.set()
    assert {span.name for span in spans(env)} >= {"business.queue", "business.commit_fence", "business.save"}
