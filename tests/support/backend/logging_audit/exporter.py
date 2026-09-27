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
        except Exception as exc:  # noqa: BLE001 — 导出失败必须降级，不能抛出
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
