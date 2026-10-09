"""T02：W3C traceparent 解析/生成与 trace/span 上下文。

HTTP 入口验证 W3C traceparent；无效或缺失时生成新的随机 trace ID。
内部异步任务保存上游 trace ID 并建立新 span。不把用户身份写入 trace 上下文。
"""

from __future__ import annotations

import re
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Optional

_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)

# 全零 trace/span 非法（W3C Trace Context）
_INVALID_TRACE = "0" * 32
_INVALID_SPAN = "0" * 16


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    sampled: bool = True

    def child(self) -> "TraceContext":
        return TraceContext(
            trace_id=self.trace_id,
            span_id=secrets.token_hex(8),
            parent_span_id=self.span_id,
            sampled=self.sampled,
        )

    def as_traceparent(self) -> str:
        flags = "01" if self.sampled else "00"
        return f"00-{self.trace_id}-{self.span_id}-{flags}"


_current_trace: ContextVar[Optional[TraceContext]] = ContextVar("current_trace", default=None)


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def parse_traceparent(header: Optional[str]) -> Optional[TraceContext]:
    """解析 W3C traceparent；无效或缺失返回 None（调用方生成新 trace）。"""
    if not header:
        return None
    m = _TRACEPARENT_RE.match(header.strip())
    if not m:
        return None
    version = m.group("version")
    if version == "ff":
        return None
    trace_id = m.group("trace_id")
    span_id = m.group("span_id")
    if trace_id == _INVALID_TRACE or span_id == _INVALID_SPAN:
        return None
    sampled = bool(int(m.group("flags"), 16) & 1)
    return TraceContext(trace_id=trace_id, span_id=span_id, sampled=sampled)


def ensure_trace(header: Optional[str] = None) -> TraceContext:
    """有有效 traceparent 则复用 trace 并新建 span；否则生成全新 trace。"""
    parent = parse_traceparent(header)
    if parent is not None:
        return parent.child()
    return TraceContext(trace_id=new_trace_id(), span_id=new_span_id())


def get_current_trace() -> Optional[TraceContext]:
    return _current_trace.get()


@contextmanager
def trace_context(ctx: TraceContext) -> Iterator[TraceContext]:
    token = _current_trace.set(ctx)
    otel_token = None
    try:
        from opentelemetry import context, trace
        # Prefer the active native span, especially across nested business scopes.
        if not trace.get_current_span().get_span_context().is_valid:
            parent = trace.SpanContext(
                trace_id=int(ctx.trace_id, 16), span_id=int(ctx.span_id, 16),
                is_remote=True, trace_flags=trace.TraceFlags(1 if ctx.sampled else 0),
            )
            otel_token = context.attach(trace.set_span_in_context(trace.NonRecordingSpan(parent)))
    except Exception:
        pass
    try:
        yield ctx
    finally:
        if otel_token is not None:
            from opentelemetry import context
            context.detach(otel_token)
        _current_trace.reset(token)


_task_attributes: ContextVar[dict] = ContextVar("observability_task", default={})


@contextmanager
def task_context(*, task_id=None, request_id=None, attempt=None) -> Iterator[None]:
    """Task metadata for business/native spans; never metric dimensions or baggage."""
    from .instrumentation import safe_attributes
    attrs = safe_attributes({"task.id": task_id, "request.id": request_id, "attempt": attempt})
    token = _task_attributes.set(attrs)
    try:
        yield
    finally:
        _task_attributes.reset(token)


def current_task_attributes() -> dict:
    return dict(_task_attributes.get())


def capture_task_traceparent() -> Optional[str]:
    """Capture only the active span IDs for a durable task, never baggage/tracestate."""
    from opentelemetry import trace
    sc = trace.get_current_span().get_span_context()
    if sc.is_valid:
        return TraceContext(f"{sc.trace_id:032x}", f"{sc.span_id:016x}", sampled=bool(sc.trace_flags.sampled)).as_traceparent()
    ctx = get_current_trace()
    return ctx.as_traceparent() if ctx else None


def _remote_span_context(header):
    from opentelemetry import trace
    parsed = parse_traceparent(header)
    if parsed is None:
        return None
    return trace.SpanContext(
        trace_id=int(parsed.trace_id, 16), span_id=int(parsed.span_id, 16),
        is_remote=True, trace_flags=trace.TraceFlags(1 if parsed.sampled else 0),
    )


@contextmanager
def business_span(stage: str, *, runtime=None, task_id=None, request_id=None, attempt=None, outcome=None, error_code=None, parent_traceparent=None, linked_traceparent=None) -> Iterator[object]:
    """Instrument application boundaries without recording payload or raw exceptions.

    Explicit durable parent wins; otherwise the active native span is used.
    trace_context supplies a remote parent when no native parent exists.
    Business exceptions propagate unchanged.
    """
    import time
    from opentelemetry import trace
    from .instrumentation import safe_attributes, STAGES
    from .otel_setup import get_runtime
    from .metrics import record_business_stage
    from .exporter import HEALTH
    runtime = runtime or get_runtime()
    safe_stage = stage if stage in STAGES else "task_attempt"
    attributes = current_task_attributes()
    attributes.update(safe_attributes({"stage": safe_stage, "task.id": task_id, "request.id": request_id, "attempt": attempt, "outcome": outcome, "error_code": error_code}))
    active_parent = trace.get_current_span().get_span_context()
    parent = _remote_span_context(parent_traceparent)
    parent_context = trace.set_span_in_context(trace.NonRecordingSpan(parent)) if parent else None
    if parent is None and active_parent.is_valid:
        parent_context = trace.set_span_in_context(trace.NonRecordingSpan(active_parent))
    linked = _remote_span_context(linked_traceparent)
    links = [trace.Link(linked)] if linked else []
    span = trace.NonRecordingSpan(parent or active_parent)
    if runtime:
        try:
            span = runtime.tracer.start_span("business." + safe_stage, attributes=attributes, context=parent_context, links=links)
        except Exception:
            HEALTH.add("spans_dropped")
    started = time.monotonic()
    actual_outcome = outcome or "succeeded"
    token = _current_trace.set(_current_trace.get())
    try:
        with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
            sc = span.get_span_context()
            if sc.is_valid:
                _current_trace.set(TraceContext(trace_id=f"{sc.trace_id:032x}", span_id=f"{sc.span_id:016x}", sampled=bool(sc.trace_flags.sampled)))
            try:
                yield span
            except BaseException:
                actual_outcome = "failed"
                try:
                    if span.is_recording():
                        span.set_status(trace.Status(trace.StatusCode.ERROR))
                except Exception:
                    HEALTH.add("measurement_failures")
                raise
    finally:
        _current_trace.reset(token)
        try:
            span.set_attributes(safe_attributes({"outcome": actual_outcome, "error_code": error_code}))
            span.end()
            record_business_stage(safe_stage, actual_outcome, (time.monotonic() - started) * 1000, error_code=error_code, runtime=runtime)
        except Exception:
            HEALTH.add("measurement_failures")


@contextmanager
def child_span() -> Iterator[TraceContext]:
    """在当前 trace 下创建子 span；无当前 trace 时创建全新 trace。"""
    parent = get_current_trace()
    ctx = parent.child() if parent is not None else TraceContext(
        trace_id=new_trace_id(), span_id=new_span_id()
    )
    with trace_context(ctx) as yielded:
        yield yielded
