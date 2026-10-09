"""Real SDK observability contracts; synthetic data, no database or network."""
from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, MetricExporter, MetricExportResult
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import Tool
from pydantic_ai.usage import RequestUsage

from backend.logging_audit import otel_setup
from backend.logging_audit.exporter import HEALTH, SafeMetricExporter, sanitize_span
from backend.logging_audit.instrumentation import native_instrumentation, safe_attributes
from backend.logging_audit.otel_setup import ObservabilityRuntime, resolve_signal_endpoint
from backend.logging_audit.trace import (
    TraceContext, business_span, capture_task_traceparent, parse_traceparent,
    task_context, trace_context,
)

PRIVATE = "SYNTHETIC_PRIVATE_PAYLOAD_7c3a"
TASK = "task_12345678"
PARENT = "00-0123456789abcdef0123456789abcdef-123456789abcdef0-01"
LINK = "00-fedcba9876543210fedcba9876543210-fedcba9876543210-01"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Observability tests cannot use network or database")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    original_connect = socket.socket.connect
    original_socketpair = socket.socketpair
    local = threading.local()

    def socketpair(*args, **kwargs):
        local.building_self_pipe = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            local.building_self_pipe = False

    def connect(sock, address):
        # Windows asyncio builds its self-pipe using a loopback socketpair.
        if getattr(local, "building_self_pipe", False) and isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        return forbidden(sock, address)

    monkeypatch.setattr(socket, "socketpair", socketpair)
    monkeypatch.setattr(socket.socket, "connect", connect)
    from backend.logging_audit.db import Database
    monkeypatch.setattr(Database, "__init__", forbidden)


@pytest.fixture
def monitored():
    spans = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    runtime = ObservabilityRuntime(span_exporter=spans, metric_readers=[reader], environment="test")
    try:
        yield runtime, spans, reader
    finally:
        assert runtime.close(2000)
        assert runtime.close(2000)
        assert runtime.processor._closed.is_set()
        assert not runtime.processor._worker.is_alive()


def all_metrics(reader):
    data = reader.get_metrics_data()
    return {metric.name: metric for resource in data.resource_metrics
            for scope in resource.scope_metrics for metric in scope.metrics} if data else {}


def assert_private_absent(spans, reader):
    assert PRIVATE not in "".join(span.to_json() for span in spans)
    data = reader.get_metrics_data()
    assert PRIVATE not in repr(data)
    # Validate the actual OTLP protobuf representation, not only object views.
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics
    assert PRIVATE.encode() not in encode_spans(spans).SerializeToString()
    if data is not None:
        assert PRIVATE.encode() not in encode_metrics(data).SerializeToString()
    for span in spans:
        assert not span.events
        assert span.status.description is None
        assert not span.context.trace_state
        assert span.instrumentation_scope.name == "learning-assistant.native"
        for link in span.links:
            assert not link.attributes
            assert not link.context.trace_state
        assert not any(key in span.attributes for key in (
            "gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions",
            "tool_arguments", "tool_response", "gen_ai.tool.name", "model_request_parameters",
            "gen_ai.request.model", "gen_ai.agent.name", "url.full", "metadata",
        ))
    if data is not None:
        for resource in data.resource_metrics:
            assert set(resource.resource.attributes) <= {"service.name", "deployment.environment.name"}
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    assert metric.description == ""
                    for point in metric.data.data_points:
                        assert not point.exemplars
                        assert not any(key in point.attributes for key in (
                            "task.id", "request.id", "config_id", "gen_ai.request.model", "url.full",
                        ))


def test_real_testmodel_native_spans_and_flags(monitored):
    runtime, exporter, reader = monitored
    capability = native_instrumentation(runtime)
    settings = capability.settings
    assert settings.include_content is False
    assert settings.include_binary_content is False
    assert settings.include_model_request_parameters is False
    assert settings.tracer is not None
    assert settings.meter._meter is runtime.raw_meter_provider.get_meter("pydantic-ai")
    before_trace = trace.get_tracer_provider()
    from opentelemetry import metrics
    before_meter = metrics.get_meter_provider()

    def private_tool(argument: str) -> str:
        return PRIVATE + argument

    agent = Agent(TestModel(custom_output_text=PRIVATE, model_name=PRIVATE),
                  name=PRIVATE, instructions=PRIVATE, metadata={"private": PRIVATE},
                  capabilities=[capability],
                  tools=[Tool(private_tool, name=PRIVATE, description=PRIVATE)])

    async def run():
        with task_context(task_id=TASK, request_id="req_12345678", attempt=2):
            with business_span("generate", runtime=runtime) as parent:
                result = await agent.run(PRIVATE, model_settings={"temperature": 0.4})
                assert result.output == PRIVATE
                return parent.get_span_context()
    parent = asyncio.run(run())
    assert runtime.force_flush(2000)
    spans = exporter.get_finished_spans()
    assert {span.name for span in spans} >= {"agent.run", "model.request", "tool.execute", "business.generate"}
    assert all(span.context.trace_id == parent.trace_id for span in spans)
    assert all(span.attributes.get("task.id") == TASK for span in spans)
    assert trace.get_tracer_provider() is before_trace
    assert metrics.get_meter_provider() is before_meter
    assert_private_absent(spans, reader)


def test_functionmodel_exact_usage_cache_fields_and_histograms(monitored):
    runtime, exporter, reader = monitored
    calls = []

    def model(messages, info):
        calls.append(messages)
        if len(calls) == 1:
            return ModelResponse([ToolCallPart(PRIVATE, {"argument": PRIVATE})],
                                 usage=RequestUsage(input_tokens=11, output_tokens=3,
                                                    cache_write_tokens=2, cache_read_tokens=1,
                                                    details={"reasoning_tokens": 2, PRIVATE: 23}))
        return ModelResponse([TextPart(PRIVATE)], usage=RequestUsage(input_tokens=7, output_tokens=5))

    def private_tool(argument: str) -> str:
        assert argument == PRIVATE
        return PRIVATE

    agent = Agent(FunctionModel(model, model_name=PRIVATE), name=PRIVATE, instructions=PRIVATE,
                  capabilities=[native_instrumentation(runtime)],
                  tools=[Tool(private_tool, name=PRIVATE, description=PRIVATE)])
    result = asyncio.run(agent.run(PRIVATE))
    assert result.usage.input_tokens == 18
    assert result.usage.output_tokens == 8
    assert runtime.force_flush(2000)
    spans = exporter.get_finished_spans()
    requests = [span for span in spans if span.name == "model.request"]
    assert sorted(span.attributes["gen_ai.usage.input_tokens"] for span in requests) == [7, 11]
    assert sorted(span.attributes["gen_ai.usage.output_tokens"] for span in requests) == [3, 5]
    run = next(span for span in spans if span.name == "agent.run")
    assert run.attributes["gen_ai.aggregated_usage.input_tokens"] == 18
    assert run.attributes["gen_ai.aggregated_usage.output_tokens"] == 8
    first = next(span for span in requests if span.attributes["gen_ai.usage.input_tokens"] == 11)
    assert first.attributes["gen_ai.usage.cache_creation.input_tokens"] == 2
    assert first.attributes["gen_ai.usage.cache_read.input_tokens"] == 1
    assert first.attributes["gen_ai.usage.details.reasoning_tokens"] == 2
    assert not any(PRIVATE in key for key in first.attributes)
    histogram = all_metrics(reader)["gen_ai.client.token.usage"]
    assert histogram.unit == "{token}"
    points = {point.attributes["gen_ai.token.type"]: point for point in histogram.data.data_points}
    assert (points["input"].count, points["input"].sum) == (2, 18)
    assert (points["output"].count, points["output"].sum) == (2, 8)
    assert sum(points["input"].bucket_counts) == 2
    assert_private_absent(spans, reader)


def test_stream_events_first_chunk_native_histogram(monitored):
    runtime, exporter, reader = monitored

    async def streaming(messages, info):
        await asyncio.sleep(0.025)
        yield PRIVATE[:12]
        yield PRIVATE[12:]

    agent = Agent(FunctionModel(stream_function=streaming, model_name=PRIVATE),
                  capabilities=[native_instrumentation(runtime)])

    async def run():
        async with agent.run_stream_events(PRIVATE) as stream:
            events = [event async for event in stream]
        result_event = next(event for event in events if isinstance(event, AgentRunResultEvent))
        assert result_event.result.output == PRIVATE
        return result_event.result
    result = asyncio.run(run())
    assert runtime.force_flush(2000)
    spans = exporter.get_finished_spans()
    request = next(span for span in spans if span.name == "model.request")
    assert request.attributes["gen_ai.usage.input_tokens"] == result.usage.input_tokens
    histogram = all_metrics(reader)["gen_ai.client.operation.time_to_first_chunk"]
    assert histogram.unit == "s"
    point, = histogram.data.data_points
    assert point.count == 1
    assert 0.015 <= point.sum < 2
    assert sum(point.bucket_counts) == 1
    assert_private_absent(spans, reader)


@pytest.mark.parametrize("source", ["model", "tool", "business"])
def test_raw_exceptions_never_exported(monitored, source):
    runtime, exporter, reader = monitored

    def model(messages, info):
        if source == "model":
            raise RuntimeError(PRIVATE)
        return ModelResponse([ToolCallPart(PRIVATE, {"argument": PRIVATE})])

    def private_tool(argument: str) -> str:
        raise RuntimeError(PRIVATE)

    agent = Agent(FunctionModel(model), capabilities=[native_instrumentation(runtime)],
                  tools=[Tool(private_tool, name=PRIVATE, description=PRIVATE)])
    with pytest.raises(RuntimeError, match=PRIVATE):
        if source == "business":
            with business_span("generate", runtime=runtime):
                raise RuntimeError(PRIVATE)
        else:
            asyncio.run(agent.run(PRIVATE))
    assert runtime.force_flush(2000)
    spans = exporter.get_finished_spans()
    assert spans
    assert any(span.status.status_code == trace.StatusCode.ERROR for span in spans)
    assert_private_absent(spans, reader)


def test_preaggregation_merges_private_labels_and_rejects_unsafe_measurements(monitored):
    runtime, exporter, reader = monitored
    meter = runtime.meter_provider.get_meter(PRIVATE, PRIVATE, PRIVATE, {PRIVATE: PRIVATE})
    histogram = meter.create_histogram("model.call.duration", PRIVATE, PRIVATE)
    for amount, suffix in [(1, "a"), (2, "b")]:
        histogram.record(amount, {"stage": "generate", "task.id": TASK + suffix,
                                  "gen_ai.request.model": PRIVATE + suffix, "url.full": PRIVATE + suffix})
    for amount in (float("nan"), float("inf"), -1, True, PRIVATE):
        histogram.record(amount)
    meter.create_histogram(PRIVATE).record(3)
    metrics = all_metrics(reader)
    assert set(metrics) == {"model.call.duration"}
    histogram = metrics["model.call.duration"]
    point, = histogram.data.data_points
    assert point.attributes == {"stage": "generate"}
    assert (point.count, point.sum) == (2, 3)
    assert histogram.unit == "ms"
    assert_private_absent([], reader)


def test_durable_parent_link_and_tracestate_payload_removal(monitored):
    runtime, exporter, reader = monitored
    with business_span("task_attempt", runtime=runtime, parent_traceparent=PARENT,
                       linked_traceparent=LINK, task_id=TASK) as outer:
        captured = capture_task_traceparent()
        assert parse_traceparent(captured).span_id == f"{outer.get_span_context().span_id:016x}"
        with business_span("generate", runtime=runtime) as inner:
            assert inner.get_span_context().trace_id == int(PARENT.split("-")[1], 16)
    assert capture_task_traceparent() is None
    assert runtime.force_flush(2000)
    spans = exporter.get_finished_spans()
    root = next(span for span in spans if span.name == "business.task_attempt")
    child = next(span for span in spans if span.name == "business.generate")
    assert root.parent.span_id == int(PARENT.split("-")[2], 16)
    assert child.parent.span_id == root.context.span_id
    assert root.links[0].context.trace_id == int(LINK.split("-")[1], 16)
    raw = runtime.tracer.start_span(PRIVATE, attributes={"url.full": PRIVATE, "metadata": PRIVATE},
                                    links=[trace.Link(trace.SpanContext(1, 2, True, trace.TraceFlags(1),
                                                                       trace.TraceState([("private", PRIVATE)])),
                                                      {"private": PRIVATE})])
    raw.add_event(PRIVATE, {"exception.message": PRIVATE})
    raw.set_status(trace.Status(trace.StatusCode.ERROR, PRIVATE))
    raw.end()
    assert runtime.force_flush(2000)
    safe = sanitize_span(exporter.get_finished_spans()[-1])
    assert safe.links[0].context.span_id == 2
    assert_private_absent(exporter.get_finished_spans(), reader)
    with trace_context(TraceContext("1" * 32, "2" * 16)):
        assert capture_task_traceparent() == "00-" + "1" * 32 + "-" + "2" * 16 + "-01"
    assert parse_traceparent(PARENT[:-2] + "03").sampled is True
    assert parse_traceparent(PARENT[:-2] + "02").sampled is False


def test_bounded_nonblocking_export_queue_and_close():
    entered, release = threading.Event(), threading.Event()

    class BlockingExporter(SpanExporter):
        def export(self, spans):
            entered.set()
            release.wait(3)
            assert all(PRIVATE not in span.to_json() for span in spans)
            return SpanExportResult.SUCCESS
        def shutdown(self):
            pass

    runtime = ObservabilityRuntime(span_exporter=BlockingExporter(), max_queue=3, batch_size=1,
                                   export_timeout=0.03)
    before = HEALTH.snapshot()
    try:
        runtime.tracer.start_span(PRIVATE).end()
        assert entered.wait(1)
        started = time.monotonic()
        for _ in range(60):
            span = runtime.tracer.start_span(PRIVATE, attributes={"metadata": PRIVATE})
            span.end()
        assert time.monotonic() - started < 0.5
        assert runtime.processor.queue_length() <= 3
        assert HEALTH.snapshot()["queue_dropped"] >= before["queue_dropped"] + 57
        with runtime.processor._queue.mutex:
            assert all(PRIVATE not in span.to_json() for span in runtime.processor._queue.queue)
        started = time.monotonic()
        assert not runtime.force_flush(30)
        assert time.monotonic() - started < 0.3
        assert not runtime.close(80)
        assert not runtime.close(80)
    finally:
        release.set()
        assert runtime.processor._closed.wait(2)
        runtime.processor._worker.join(1)
        assert not runtime.processor._worker.is_alive()


def test_safe_metric_exporter_scrubs_metadata_and_degrades(monitored):
    runtime, _, reader = monitored
    meter = runtime.meter_provider.get_meter(PRIVATE)
    meter.create_histogram("model.call.duration").record(8, {"stage": "generate", "metadata": PRIVATE})
    data = reader.get_metrics_data()
    captured = []

    class CaptureMetricExporter(MetricExporter):
        def export(self, data, timeout_millis=10000, **kwargs):
            captured.append(data)
            return MetricExportResult.SUCCESS
        def force_flush(self, timeout_millis=10000):
            raise RuntimeError(PRIVATE)
        def shutdown(self, timeout_millis=30000, **kwargs):
            raise RuntimeError(PRIVATE)

    safe = SafeMetricExporter(CaptureMetricExporter())
    assert safe.export(data) == MetricExportResult.SUCCESS
    assert PRIVATE not in repr(captured)
    assert safe.force_flush(20) is False
    safe.shutdown(20)
    safe.delegate.export = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(PRIVATE))
    assert safe.export(data) == MetricExportResult.FAILURE


def test_repeated_blocked_flush_has_bounded_helpers():
    entered, release = threading.Event(), threading.Event()
    runtime = ObservabilityRuntime()
    calls = []

    def blocked(timeout_millis):
        calls.append(timeout_millis)
        entered.set()
        release.wait(2)
        return True

    runtime.tracer_provider.force_flush = blocked
    try:
        assert not runtime.force_flush(20)
        assert entered.wait(1)
        for _ in range(12):
            assert not runtime.force_flush(20)
        assert len(calls) == 1
    finally:
        release.set()
        assert runtime._lifecycle_locks[0].acquire(timeout=1)
        runtime._lifecycle_locks[0].release()
        assert runtime.close(1000)


def test_no_runtime_never_uses_global_instrumentation(monkeypatch):
    monkeypatch.setattr(otel_setup, "get_runtime", lambda: None)
    settings = native_instrumentation().settings
    assert settings.tracer.start_span(PRIVATE).is_recording() is False
    assert settings.include_content is False
    assert settings.include_binary_content is False
    assert settings.include_model_request_parameters is False
    assert safe_attributes({"attempt": True, "duration_ms": -1, "metadata": PRIVATE}) == {}


def test_otlp_configuration_credentials_only_reach_transport(monkeypatch):
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as trace_exporter
    import opentelemetry.exporter.otlp.proto.http.metric_exporter as metric_exporter
    calls = []
    runtimes = []
    monkeypatch.setattr(otel_setup, "_runtime", None)
    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", lambda **kw: calls.append(("trace", kw)) or object())
    monkeypatch.setattr(metric_exporter, "OTLPMetricExporter", lambda **kw: calls.append(("metric", kw)) or object())
    monkeypatch.setattr(otel_setup, "ObservabilityRuntime", lambda **kw: runtimes.append(kw) or object())
    assert otel_setup.configure_otel(enabled=True, endpoint="https://synthetic.invalid", token=PRIVATE)
    assert {kw["endpoint"] for _, kw in calls} == {
        "https://synthetic.invalid/v1/traces", "https://synthetic.invalid/v1/metrics",
    }
    assert all(kw["headers"] == {"authorization": PRIVATE} and kw["timeout"] == 1 for _, kw in calls)
    assert PRIVATE not in repr(runtimes)
    assert otel_setup.configure_otel(enabled=True, endpoint="https://synthetic.invalid", token=PRIVATE)
    assert len(runtimes) == 1


def test_transport_errors_are_not_logged(caplog):
    import logging
    from backend.logging_audit.exporter import suppress_transport_logs
    suppress_transport_logs()
    for name in ("opentelemetry.exporter.otlp.proto.http.trace_exporter",
                 "opentelemetry.exporter.otlp.proto.http.metric_exporter"):
        logging.getLogger(name).error(PRIVATE)
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("endpoint", ["https://synthetic.invalid", "https://synthetic.invalid/v1",
                                     "https://synthetic.invalid/v1/traces"])
def test_signal_endpoint_resolution(endpoint):
    assert resolve_signal_endpoint(endpoint, "traces") == "https://synthetic.invalid/v1/traces"
    assert resolve_signal_endpoint(endpoint, "metrics") == "https://synthetic.invalid/v1/metrics"


def test_deployment_env_explicit_logfire_channel(monkeypatch):
    captured = []
    monkeypatch.setattr(otel_setup, "configure_otel", lambda **kwargs: captured.append(kwargs) or True)
    monkeypatch.setenv("OTEL_ENABLED", "true")
    monkeypatch.setenv("LOGFIRE_TOKEN", "synthetic-test-credential")
    monkeypatch.setenv("LOGFIRE_OTLP_ENDPOINT", "https://synthetic.invalid")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", PRIVATE)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://content.invalid")
    assert otel_setup.otel_from_env()
    assert captured[0]["endpoint"] == "https://synthetic.invalid"
    assert captured[0]["token"] == "synthetic-test-credential"
    assert PRIVATE not in repr(captured)
