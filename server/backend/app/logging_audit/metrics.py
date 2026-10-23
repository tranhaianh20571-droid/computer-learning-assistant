"""模型/生成调用聚合指标（ADR-020 配套：观测收尾 A1）。

进程内低基数计数器（stage × mode × outcome × error_code），并发出
model.call.completed / model.call.failed 结构化事件（runtime+metric）。
事件只含注册表允许的受控字段，不含提示词、回答正文、用户问题或任何内容。
未注入日志器时使用默认 stdout 日志器（service=api）；测试可 attach_logger
注入内存流。指标发射失败只递增 emit_errors，绝不影响业务调用。
"""

from __future__ import annotations

import os
import threading
from typing import Any, Dict, List, Optional, Tuple

from .logger import StructuredLogger

_Key = Tuple[str, Optional[str], str, Optional[str]]


def _default_environment() -> str:
    return os.environ.get("ENVIRONMENT", "development")


class ModelCallMetrics:
    """线程安全聚合计数器 + 结构化事件发射。"""

    def __init__(self, environment: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[_Key, int] = {}
        self._duration_ms: Dict[_Key, int] = {}
        self._logger: Optional[StructuredLogger] = None
        self._default_logger: Optional[StructuredLogger] = None
        self._environment = environment or _default_environment()
        self.emit_errors = 0

    def attach_logger(self, logger: Optional[StructuredLogger]) -> None:
        """注入测试/宿主日志器；传 None 恢复默认 stdout 日志器。"""
        self._logger = logger

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._duration_ms.clear()
            self.emit_errors = 0

    def record(
        self,
        *,
        stage: str,
        outcome: str,
        duration_ms: int,
        mode: Optional[str] = None,
        error_code: Optional[str] = None,
        attempts: Optional[int] = None,
        config_id: Optional[str] = None,
        capability: Optional[str] = None,
        task_id: Optional[str] = None,
        reason: Optional[str] = None,
        config_version: Optional[int | str] = None,
        requests: Optional[int] = None,
    ) -> None:
        from .instrumentation import safe_attributes
        dimensions = safe_attributes({"stage": stage, "mode": mode, "outcome": outcome, "error_code": error_code}, metrics=True)
        stage = dimensions.get("stage", "other")
        mode = dimensions.get("mode")
        error_code = dimensions.get("error_code")
        outcome = "succeeded" if outcome == "succeeded" else "failed"
        key: _Key = (stage, mode, outcome, error_code)
        positive = max(0, int(duration_ms))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1
            self._duration_ms[key] = self._duration_ms.get(key, 0) + positive
        record_operational_measurement("model.call", positive, {"stage": stage, "mode": mode, "outcome": outcome, "error_code": error_code})
        self._emit(
            stage=stage, outcome=outcome, duration_ms=positive, mode=mode,
            error_code=error_code, attempts=attempts, config_id=config_id,
            capability=capability, task_id=task_id, reason=reason,
            config_version=config_version, requests=requests,
        )

    def snapshot(self) -> dict:
        with self._lock:
            calls: List[dict] = [
                {
                    "stage": key[0],
                    "mode": key[1],
                    "outcome": key[2],
                    "error_code": key[3],
                    "count": count,
                    "duration_ms": self._duration_ms.get(key, 0),
                }
                for key, count in sorted(
                    self._counters.items(),
                    key=lambda item: tuple(part or "" for part in item[0]),
                )
            ]
            emit_errors = self.emit_errors
        return {"calls": calls, "emit_errors": emit_errors}

    # 内部 ------------------------------------------------------------------

    def _resolve_logger(self) -> StructuredLogger:
        if self._logger is not None:
            return self._logger
        if self._default_logger is None:
            self._default_logger = StructuredLogger("api", self._environment)
        return self._default_logger

    def _emit(self, **kw: Any) -> None:
        succeeded = kw["outcome"] == "succeeded"
        fields: Dict[str, Any] = {
            "stage": kw["stage"],
            "status": "succeeded" if succeeded else "failed",
            "duration_ms": kw["duration_ms"],
        }
        for name in ("mode", "config_id", "capability", "task_id", "reason"):
            value = kw.get(name)
            if value:
                fields[name] = value
        for name in ("config_version", "requests"):
            if kw.get(name) is not None:
                fields[name] = kw[name]
        if kw.get("attempts") is not None:
            fields["attempt"] = kw["attempts"]
        level = "INFO"
        if not succeeded:
            fields["error_code"] = kw.get("error_code") or "MODEL_CAPABILITY_UNAVAILABLE"
            level = "WARN"
        try:
            self._resolve_logger().log(
                "model.call.completed" if succeeded else "model.call.failed",
                level=level,
                **fields,
            )
        except Exception:  # noqa: BLE001 - 指标发射绝不影响业务
            with self._lock:
                self.emit_errors += 1


def record_operational_measurement(prefix, duration_ms, attributes, *, runtime=None):
    from .otel_setup import get_runtime
    from .exporter import HEALTH
    runtime = runtime or get_runtime()
    if runtime is None:
        return
    try:
        meter = runtime.meter_provider.get_meter("learning-assistant.business")
        meter.create_counter(prefix + (".calls" if prefix == "business.stage" else ".count")).add(1, attributes)
        meter.create_histogram(prefix + ".duration", unit="ms").record(duration_ms, attributes)
    except Exception:
        HEALTH.add("measurement_failures")


def record_business_stage(stage, outcome, duration_ms, *, error_code=None, runtime=None):
    record_operational_measurement("business.stage", duration_ms, {"stage": stage, "outcome": outcome, "error_code": error_code}, runtime=runtime)


_TASK_DIAGNOSTICS_LOCK = threading.Lock()
_TASK_DIAGNOSTICS: Dict[Tuple[str, str], int] = {}


def record_task_diagnostic(*, reason: str, error_code: str, task_id=None, attempt=None) -> None:
    """Runtime diagnostics are best-effort, independent of terminal transactions."""
    from .contracts import normalize_error_code
    reason = reason if reason in ("unknown_error_code", "worker_recovery_retry", "worker_recovery_failed") else "other"
    error_code = normalize_error_code(error_code)
    with _TASK_DIAGNOSTICS_LOCK:
        key = (reason, error_code)
        _TASK_DIAGNOSTICS[key] = _TASK_DIAGNOSTICS.get(key, 0) + 1
    try:
        StructuredLogger("worker", _default_environment()).log("task.convergence.diagnostic",
            level="ERROR" if reason == "worker_recovery_failed" else "WARN",
            reason=reason, error_code=error_code, task_id=task_id, attempt=attempt)
    except Exception:
        with _TASK_DIAGNOSTICS_LOCK:
            key = ("emit_failed", "INTERNAL_ERROR")
            _TASK_DIAGNOSTICS[key] = _TASK_DIAGNOSTICS.get(key, 0) + 1


def task_diagnostics_snapshot() -> dict:
    with _TASK_DIAGNOSTICS_LOCK:
        return {"diagnostics": [{"reason": reason, "error_code": code, "count": count}
            for (reason, code), count in sorted(_TASK_DIAGNOSTICS.items())]}


_MODEL_METRICS = ModelCallMetrics()


def get_model_call_metrics() -> ModelCallMetrics:
    """进程级单例；模型与 TTS 调用出口统一向它记账。"""
    return _MODEL_METRICS


def reset_model_call_metrics() -> None:
    _MODEL_METRICS.reset()


def model_call_metrics_snapshot() -> dict:
    return _MODEL_METRICS.snapshot()


__all__ = [
    "ModelCallMetrics",
    "get_model_call_metrics",
    "model_call_metrics_snapshot",
    "reset_model_call_metrics",
]
