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
    sampled = m.group("flags")[-1] == "1"
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
    try:
        yield ctx
    finally:
        _current_trace.reset(token)


@contextmanager
def child_span() -> Iterator[TraceContext]:
    """在当前 trace 下创建子 span；无当前 trace 时创建全新 trace。"""
    parent = get_current_trace()
    ctx = parent.child() if parent is not None else TraceContext(
        trace_id=new_trace_id(), span_id=new_span_id()
    )
    with trace_context(ctx) as yielded:
        yield yielded
