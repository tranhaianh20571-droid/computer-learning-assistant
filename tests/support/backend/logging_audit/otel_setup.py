"""Operational OTel runtime and explicitly separate legacy content hook.

OTLP/HTTP traces and metrics go to deployment-injected Logfire endpoints. No
cloud configuration, content migration, or platform deletion is performed here.
"""
from __future__ import annotations

import base64
import os
import threading
import time
from typing import Optional

_tracer = None  # Legacy controlled content hook only; NEVER the native runtime tracer.
_configured_endpoint: Optional[str] = None
_runtime = None
_runtime_lock = threading.Lock()
DEFAULT_LANGFUSE_OTLP_PATH = "/api/public/otel/v1/traces"


def _basic_auth_header(public_key: str, secret_key: str) -> str:
    raw = f"{public_key}:{secret_key}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def resolve_otlp_endpoint(endpoint: str) -> str:
    """Legacy content endpoint resolver, retained for existing callers."""
    if endpoint.rstrip("/").endswith("/traces"):
        return endpoint
    return endpoint.rstrip("/") + DEFAULT_LANGFUSE_OTLP_PATH


def resolve_signal_endpoint(endpoint: str, signal: str) -> str:
    from urllib.parse import urlsplit
    parts = urlsplit(endpoint)
    if parts.scheme not in {"https", "http"} or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("OBSERVABILITY_ENDPOINT_INVALID")
    if signal not in {"traces", "metrics"}:
        raise ValueError("OBSERVABILITY_ENDPOINT_INVALID")
    base = endpoint.rstrip("/")
    if base.endswith("/v1/traces") or base.endswith("/v1/metrics"):
        base = base.rsplit("/", 1)[0]
    elif not base.endswith("/v1"):
        base += "/v1"
    return base + "/" + signal


class ObservabilityRuntime:
    """One application-owned trace provider and meter provider, no global override."""
    def __init__(self, *, span_exporter=None, metric_exporter=None, metric_readers=(), service_name="learning-assistant", environment="development", max_queue=256, batch_size=32, export_timeout=1.0, metric_interval_ms=10000):
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.metrics import MeterProvider, AlwaysOffExemplarFilter
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from .instrumentation import SafeMeterProvider
        from .exporter import SafeSpanExporter, SafeMetricExporter, SafeSpanProcessor
        # Resource.create() adds host/process/env-detected fields; use an explicit resource.
        self.resource = Resource({"service.name": "learning-assistant", "deployment.environment.name": environment if environment in {"development", "test", "staging", "production"} else "other"})
        self.tracer_provider = TracerProvider(resource=self.resource, shutdown_on_exit=False)
        self.processor = None
        if span_exporter is not None:
            self.processor = SafeSpanProcessor(SafeSpanExporter(span_exporter), max_queue=max_queue, batch_size=batch_size, export_timeout=export_timeout)
            self.tracer_provider.add_span_processor(self.processor)
        readers = list(metric_readers)
        if metric_exporter is not None:
            readers.append(PeriodicExportingMetricReader(SafeMetricExporter(metric_exporter), export_interval_millis=metric_interval_ms, export_timeout_millis=export_timeout * 1000))
        self.raw_meter_provider = MeterProvider(resource=self.resource, metric_readers=readers, exemplar_filter=AlwaysOffExemplarFilter(), shutdown_on_exit=False)
        self.meter_provider = SafeMeterProvider(self.raw_meter_provider)
        self.tracer = self.tracer_provider.get_tracer("learning-assistant.business")
        self._closed = False
        self._shutdown_result = False
        self._close_lock = threading.Lock()
        self._lifecycle_locks = (threading.Lock(), threading.Lock())

    def force_flush(self, timeout_millis=2000) -> bool:
        deadline = time.monotonic() + max(0, timeout_millis) / 1000
        ok = True
        for provider, lock in zip((self.tracer_provider, self.raw_meter_provider), self._lifecycle_locks):
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            ok = _bounded_call(lambda p=provider, ms=remaining: p.force_flush(timeout_millis=ms), remaining, lock=lock) and ok
        return ok

    def shutdown(self, timeout_millis=2000) -> bool:
        with self._close_lock:
            if self._closed:
                return self._shutdown_result
            self._closed = True
        deadline = time.monotonic() + max(0, timeout_millis) / 1000
        ok = True
        for provider, lock in zip((self.tracer_provider, self.raw_meter_provider), self._lifecycle_locks):
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if provider is self.tracer_provider:
                call = provider.shutdown
            else:
                call = lambda p=provider, ms=remaining: p.shutdown(timeout_millis=ms)
            ok = _bounded_call(call, remaining, lock=lock) and ok
        if self.processor is not None:
            ok = self.processor._closed.is_set() and ok
        with self._close_lock:
            self._shutdown_result = ok
        return ok

    def close(self, timeout_millis=2000) -> bool:
        return self.shutdown(timeout_millis)


def _bounded_call(call, timeout_millis, *, lock=None) -> bool:
    from .exporter import HEALTH
    if lock is not None and not lock.acquire(blocking=False):
        HEALTH.add("lifecycle_timeouts")
        return False
    done = threading.Event()
    result = [False]
    def run():
        try:
            result[0] = call() is not False
        except Exception:
            HEALTH.add("lifecycle_failures")
        finally:
            if lock is not None:
                lock.release()
            done.set()
    threading.Thread(target=run, daemon=True, name="observability-lifecycle").start()
    if not done.wait(max(0, timeout_millis) / 1000):
        HEALTH.add("lifecycle_timeouts")
        return False
    return result[0]


def configure_otel(*, enabled: bool, endpoint: Optional[str], public_key: Optional[str] = None, secret_key: Optional[str] = None, service_name: str = "learning-assistant", token: Optional[str] = None, environment: str = "development", traces_endpoint: Optional[str] = None, metrics_endpoint: Optional[str] = None) -> bool:
    """Initialize the operational pipeline once; credentials only from deployment.

    Legacy Langfuse credential arguments remain accepted, but cannot implicitly
    send the content channel to operational Logfire monitoring.
    """
    global _runtime, _configured_endpoint
    from .exporter import HEALTH, suppress_transport_logs
    if not enabled or not (endpoint or (traces_endpoint and metrics_endpoint)):
        return False
    with _runtime_lock:
        if _runtime is not None:
            return True
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            suppress_transport_logs()
            trace_url = resolve_signal_endpoint(traces_endpoint or endpoint, "traces")
            metric_url = resolve_signal_endpoint(metrics_endpoint or endpoint, "metrics")
            headers = {"authorization": token} if token else {}
            runtime = ObservabilityRuntime(
                span_exporter=OTLPSpanExporter(endpoint=trace_url, headers=headers, timeout=1),
                metric_exporter=OTLPMetricExporter(endpoint=metric_url, headers=headers, timeout=1),
                service_name=service_name, environment=environment,
            )
            _runtime = runtime
            _configured_endpoint = endpoint
            return True
        except Exception:
            HEALTH.add("configuration_failures")
            return False


def get_runtime():
    return _runtime


def get_tracer():
    return _runtime.tracer if _runtime else None


def otel_configured() -> bool:
    return _runtime is not None


def record_model_span(name: str, *, task_id: Optional[str] = None, mode: Optional[str] = None, prompt: Optional[str] = None, answer: Optional[str] = None, user_question: Optional[str] = None) -> None:
    """Legacy explicitly injected content tracer, never the operational provider.

    The existing controlled content_stream still receives its own events. This
    compatibility hook does not configure a second provider or migrate content.
    """
    if _tracer is None:
        return
    try:
        with _tracer.start_as_current_span(name, record_exception=False, set_status_on_exception=False) as span:
            for key, value in (("task.id", task_id), ("gen_ai.operation.mode", mode), ("gen_ai.prompt", prompt), ("gen_ai.completion", answer), ("user.question", user_question)):
                if value:
                    span.set_attribute(key, value[:16384])
    except Exception:
        from .exporter import HEALTH
        HEALTH.add("content_hook_failures")


def shutdown_otel(timeout_millis=2000) -> bool:
    global _runtime
    with _runtime_lock:
        runtime, _runtime = _runtime, None
    return runtime.shutdown(timeout_millis) if runtime else True


def reset_otel() -> None:
    global _tracer, _configured_endpoint
    shutdown_otel()
    # An injected legacy content tracer is owned by its injector, not this runtime.
    _tracer = None
    _configured_endpoint = None


def otel_from_env(service_name: str = "learning-assistant") -> bool:
    """Explicit deployment opt-in avoids reusing old content endpoints/credentials."""
    return configure_otel(
        enabled=os.environ.get("OTEL_ENABLED", "").lower() in ("1", "true", "yes"),
        endpoint=os.environ.get("LOGFIRE_OTLP_ENDPOINT") or None,
        traces_endpoint=os.environ.get("LOGFIRE_OTLP_TRACES_ENDPOINT") or None,
        metrics_endpoint=os.environ.get("LOGFIRE_OTLP_METRICS_ENDPOINT") or None,
        token=os.environ.get("LOGFIRE_TOKEN") or None,
        environment=os.environ.get("ENVIRONMENT", "development"),
        service_name=service_name,
    )


def observability_health_snapshot() -> dict:
    from .exporter import HEALTH
    snapshot = HEALTH.snapshot()
    snapshot["configured"] = otel_configured()
    runtime = get_runtime()
    snapshot["queue_length"] = runtime.processor.queue_length() if runtime and runtime.processor else 0
    return snapshot
