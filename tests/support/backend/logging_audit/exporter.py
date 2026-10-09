"""T02：观测导出器降级（OTel/Langfuse 不可用时业务继续）。

导出使用有界异步队列和超时/熔断；队列满、导出超时或不可用时
只丢弃观测事件并递增本地指标，不阻塞或失败业务任务。
安全审计的写入失败策略与此分离（见 audit.py）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from .contracts import AlertLevel
from .logger import StructuredLogger


@dataclass
class ExporterStats:
    exported: int = 0
    dropped: int = 0
    failures: int = 0
    circuit_open: bool = False

    def snapshot(self) -> dict:
        return {
            "exported": self.exported,
            "dropped": self.dropped,
            "failures": self.failures,
            "circuit_open": self.circuit_open,
        }


class ObservabilityExporter:
    """有界队列 + 熔断的观测导出器。核心任务永不因导出失败而失败。"""

    def __init__(
        self,
        name: str = "otel",
        max_queue: int = 100,
        logger: Optional[StructuredLogger] = None,
        sink: Optional[Callable[[dict], None]] = None,
        failure_threshold: int = 3,
    ) -> None:
        self.name = name
        self.max_queue = max_queue
        self.logger = logger
        self.sink = sink or (lambda item: None)
        self.failure_threshold = failure_threshold
        self.stats = ExporterStats()
        self._queue: List[dict] = []
        self._lock = threading.Lock()
        self._consecutive_failures = 0

    def enqueue(self, item: dict) -> bool:
        """尝试导出观测事件；失败或队列满时丢弃并计数，返回是否入队。"""
        with self._lock:
            if self.stats.circuit_open:
                self.stats.dropped += 1
                self._emit_degraded("circuit_open")
                return False
            if len(self._queue) >= self.max_queue:
                self.stats.dropped += 1
                self._emit_degraded("queue_full")
                return False
            self._queue.append(item)

        try:
            self.sink(item)
            with self._lock:
                self.stats.exported += 1
                self._consecutive_failures = 0
            return True
        except Exception:  # noqa: BLE001 — 导出失败必须降级，不能抛出
            with self._lock:
                self.stats.failures += 1
                self.stats.dropped += 1
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.failure_threshold:
                    self.stats.circuit_open = True
            self._emit_degraded("export_failed")
            return False

    def _emit_degraded(self, reason: str) -> None:
        if self.logger is None:
            return
        try:
            self.logger.log(
                "observability.export.failed",
                level="WARN",
                exporter=self.name,
                error_code="OBSERVABILITY_EXPORT_FAILED",
                reason=reason,
                queue_length=len(self._queue),
                dropped_count=self.stats.dropped,
                degraded=True,
            )
        except Exception:  # noqa: BLE001 — 日志自身失败也不能影响业务
            pass

    def reset_circuit(self) -> None:
        with self._lock:
            self.stats.circuit_open = False
            self._consecutive_failures = 0

    def queue_length(self) -> int:
        with self._lock:
            return len(self._queue)

    def drain(self) -> list[dict]:
        with self._lock:
            items = list(self._queue)
            self._queue.clear()
            return items


class ObservabilityHealth:
    """Independent local counters remain readable when OTLP is down/disabled."""
    def __init__(self):
        self._lock = threading.Lock()
        self._counts = {key: 0 for key in (
            "spans_exported", "spans_dropped", "span_export_failures", "metrics_exported",
            "metrics_dropped", "metric_export_failures", "queue_dropped", "measurement_failures",
            "configuration_failures", "lifecycle_failures", "lifecycle_timeouts", "content_hook_failures",
        )}

    def add(self, key, amount=1):
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + amount

    def snapshot(self):
        with self._lock:
            return dict(self._counts)


HEALTH = ObservabilityHealth()


def safe_span_context(context):
    from opentelemetry.trace import SpanContext, TraceFlags
    if context is None or not context.is_valid:
        return None
    return SpanContext(
        trace_id=context.trace_id, span_id=context.span_id, is_remote=context.is_remote,
        trace_flags=TraceFlags(1 if context.trace_flags.sampled else 0),
    )


def sanitize_span(span):
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from opentelemetry.trace import Link, Status, StatusCode
    from .instrumentation import safe_attributes, safe_span_name
    # Drop events, exception text, link attributes, tracestate and status description.
    # Resource and instrumentation scope can themselves contain user/host metadata.
    attributes = safe_attributes(span.attributes)
    links = tuple(Link(context) for link in span.links if (context := safe_span_context(link.context)) is not None)
    resource_attributes = {"service.name": "learning-assistant"}
    env = span.resource.attributes.get("deployment.environment.name")
    if env in {"development", "test", "staging", "production", "other"}:
        resource_attributes["deployment.environment.name"] = env
    return ReadableSpan(
        name=safe_span_name(attributes), context=safe_span_context(span.context), parent=safe_span_context(span.parent),
        kind=span.kind, start_time=span.start_time, end_time=span.end_time,
        status=Status(span.status.status_code if span.status else StatusCode.UNSET),
        attributes=attributes, events=(), links=links, resource=Resource(resource_attributes),
        instrumentation_scope=InstrumentationScope("learning-assistant.native"),
    )


def sanitize_metrics(data):
    from dataclasses import replace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from .instrumentation import METRIC_NAMES, METRIC_UNITS, safe_attributes
    resources = []
    for resource in data.resource_metrics:
        scopes = []
        for scope in resource.scope_metrics:
            metrics = []
            for metric in scope.metrics:
                if metric.name not in METRIC_NAMES:
                    HEALTH.add("metrics_dropped")
                    continue
                points = [replace(point, attributes=safe_attributes(point.attributes, metrics=True), exemplars=[]) for point in metric.data.data_points]
                metrics.append(replace(metric, description="", unit=METRIC_UNITS[metric.name], data=replace(metric.data, data_points=points)))
            if metrics:
                scopes.append(replace(scope, scope=InstrumentationScope("learning-assistant.native"), schema_url="", metrics=metrics))
        if scopes:
            env = resource.resource.attributes.get("deployment.environment.name", "other")
            safe_resource = Resource({"service.name": "learning-assistant", "deployment.environment.name": env if env in {"development", "test", "staging", "production"} else "other"})
            resources.append(replace(resource, resource=safe_resource, schema_url="", scope_metrics=scopes))
    return replace(data, resource_metrics=resources)


# These are SDK exporter/processor implementations, not a second instrumentation layer.
from opentelemetry.sdk.trace import SpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult  # noqa: E402
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult  # noqa: E402


class SafeSpanExporter(SpanExporter):
    def __init__(self, delegate):
        self.delegate = delegate
        self._export_lock = threading.Lock()

    def export(self, spans):
        if not self._export_lock.acquire(blocking=False):
            HEALTH.add("spans_dropped", len(spans))
            return SpanExportResult.FAILURE
        try:
            safe = tuple(sanitize_span(span) for span in spans)
            result = self.delegate.export(safe)
            if result == SpanExportResult.SUCCESS:
                HEALTH.add("spans_exported", len(safe))
            else:
                HEALTH.add("span_export_failures")
                HEALTH.add("spans_dropped", len(safe))
            return result
        except Exception:
            HEALTH.add("span_export_failures")
            HEALTH.add("spans_dropped", len(spans))
            return SpanExportResult.FAILURE
        finally:
            self._export_lock.release()

    def force_flush(self, timeout_millis=30000):
        try:
            return self.delegate.force_flush(timeout_millis) is not False
        except Exception:
            HEALTH.add("lifecycle_failures")
            return False

    def shutdown(self):
        try:
            self.delegate.shutdown()
        except Exception:
            HEALTH.add("lifecycle_failures")


class SafeMetricExporter(MetricExporter):
    def __init__(self, delegate):
        super().__init__(preferred_temporality=getattr(delegate, "_preferred_temporality", None), preferred_aggregation=getattr(delegate, "_preferred_aggregation", None))
        self.delegate = delegate
        self._export_lock = threading.Lock()

    def export(self, metrics_data, timeout_millis=10000, **kwargs):
        if not self._export_lock.acquire(blocking=False):
            HEALTH.add("metrics_dropped")
            return MetricExportResult.FAILURE
        try:
            safe = sanitize_metrics(metrics_data)
            result = self.delegate.export(safe, timeout_millis=timeout_millis, **kwargs)
            if result == MetricExportResult.SUCCESS:
                HEALTH.add("metrics_exported")
            else:
                HEALTH.add("metric_export_failures")
                HEALTH.add("metrics_dropped")
            return result
        except Exception:
            HEALTH.add("metric_export_failures")
            HEALTH.add("metrics_dropped")
            return MetricExportResult.FAILURE
        finally:
            self._export_lock.release()

    def force_flush(self, timeout_millis=10000):
        try:
            return self.delegate.force_flush(timeout_millis) is not False
        except Exception:
            HEALTH.add("lifecycle_failures")
            return False

    def shutdown(self, timeout_millis=30000, **kwargs):
        try:
            self.delegate.shutdown(timeout_millis=timeout_millis, **kwargs)
        except Exception:
            HEALTH.add("lifecycle_failures")


class SafeSpanProcessor(SpanProcessor):
    """Sanitize on_end before a bounded FIFO; one worker never blocks the task.

    A stuck transport holds at most one worker/batch; new items then hit the
    bounded queue and get counted. flush/shutdown have explicit deadlines.
    """
    def __init__(self, exporter, max_queue=256, batch_size=32, export_timeout=1.0):
        import queue
        self.exporter = exporter
        self._queue = queue.Queue(maxsize=max(1, max_queue))
        self._batch_size = max(1, batch_size)
        self._timeout = max(0.001, export_timeout)
        self._closed = threading.Event()
        self._stopping = threading.Event()
        self._worker = threading.Thread(target=self._run, name="safe-otel-export", daemon=True)
        self._worker.start()

    def on_start(self, span, parent_context=None):
        try:
            from .trace import current_task_attributes
            span.set_attributes(current_task_attributes())
        except Exception:
            HEALTH.add("measurement_failures")

    def on_end(self, span):
        import queue
        if not span.context or not span.context.trace_flags.sampled:
            return
        try:
            if self._stopping.is_set():
                HEALTH.add("spans_dropped")
                return
            # Sanitize before queueing so content does not remain in export buffers.
            self._queue.put_nowait(sanitize_span(span))
        except queue.Full:
            HEALTH.add("queue_dropped")
            HEALTH.add("spans_dropped")
        except Exception:
            HEALTH.add("spans_dropped")

    def _run(self):
        import queue
        try:
            while not self._stopping.is_set() or not self._queue.empty():
                try:
                    batch = [self._queue.get(timeout=0.05)]
                except queue.Empty:
                    continue
                while len(batch) < self._batch_size:
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                try:
                    self.exporter.export(batch)
                finally:
                    for _ in batch:
                        self._queue.task_done()
        finally:
            self.exporter.shutdown()
            self._closed.set()

    def queue_length(self):
        return self._queue.qsize()

    def force_flush(self, timeout_millis=30000):
        import time
        deadline = time.monotonic() + max(0, timeout_millis) / 1000
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    HEALTH.add("lifecycle_timeouts")
                    return False
                self._queue.all_tasks_done.wait(remaining)
        return True

    def shutdown(self):
        self._stopping.set()
        if not self._closed.wait(self._timeout):
            HEALTH.add("lifecycle_timeouts")


class _DropTransportLogs:
    def filter(self, record):
        # Upstream OTLP logs raw response body/URL/exception on failures. The
        # health snapshot replaces those unsafe logs with controlled counters.
        return False


def suppress_transport_logs():
    import logging
    for name in (
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        "opentelemetry.exporter.otlp.proto.http.metric_exporter",
        "opentelemetry.exporter.http.transport.requests",
        "opentelemetry.exporter.http.transport.httpx",
    ):
        logger = logging.getLogger(name)
        if not any(isinstance(item, _DropTransportLogs) for item in logger.filters):
            logger.addFilter(_DropTransportLogs())
