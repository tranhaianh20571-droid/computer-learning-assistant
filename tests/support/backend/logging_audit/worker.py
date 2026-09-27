"""最小 worker 壳：证明 API→worker 用同一 task_id/trace 推进阶段（AC-03）。

业务 worker（OCR/生成等）在后续切片；本模块只负责：
- 持有上游 trace_id
- 按 attempt 推进阶段事件
- 租约过期时只写 stale_discarded
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .logger import StructuredLogger
from .repositories import PgTaskEventStore
from .trace import TraceContext, child_span, trace_context


@dataclass
class WorkerResult:
    task_id: str
    attempt: int
    status: str
    error_code: Optional[str] = None
    duration_ms: int = 0


class StageWorker:
    def __init__(self, store: PgTaskEventStore, logger: StructuredLogger) -> None:
        self.store = store
        self.logger = logger

    def run_stage(
        self,
        task_id: str,
        *,
        stage: str,
        attempt: int,
        generation: int,
        owner_user_id: str,
        upstream_trace_id: Optional[str] = None,
        status: str = "succeeded",
        error_code: Optional[str] = None,
        duration_ms: int = 1,
    ) -> WorkerResult:
        """在上游 trace 下创建子 span 并写阶段事件。"""
        parent = TraceContext(
            trace_id=upstream_trace_id or "0" * 32,
            span_id="0" * 16,
        ) if upstream_trace_id else None

        if parent is not None:
            with trace_context(parent):
                with child_span() as span:
                    return self._do_run(
                        task_id,
                        stage=stage,
                        attempt=attempt,
                        generation=generation,
                        status=status,
                        error_code=error_code,
                        duration_ms=duration_ms,
                        trace_id=span.trace_id,
                    )
        with child_span() as span:
            return self._do_run(
                task_id,
                stage=stage,
                attempt=attempt,
                generation=generation,
                status=status,
                error_code=error_code,
                duration_ms=duration_ms,
                trace_id=span.trace_id,
            )

    def _do_run(
        self,
        task_id: str,
        *,
        stage: str,
        attempt: int,
        generation: int,
        status: str,
        error_code: Optional[str],
        duration_ms: int,
        trace_id: str,
    ) -> WorkerResult:
        event = self.store.record(
            task_id,
            stage=stage,
            status=status,
            error_code=error_code,
            duration_ms=duration_ms,
            attempt=attempt,
            generation=generation,
        )
        self.logger.log(
            "task.stage.succeeded" if status == "succeeded" else "task.stage.failed",
            level="INFO" if status == "succeeded" else "WARN",
            task_id=task_id,
            attempt=attempt,
            stage=stage,
            status=status,
            error_code=error_code,
            duration_ms=duration_ms,
            trace_id=trace_id,
        )
        return WorkerResult(
            task_id=task_id,
            attempt=attempt,
            status=event.status,
            error_code=event.error_code,
            duration_ms=duration_ms,
        )
