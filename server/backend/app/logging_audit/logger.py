"""T02：统一结构化运行日志。

一行一个 UTF-8 JSON；必填 schema_version/timestamp/level/event/service/environment。
有处理链时必须带 trace_id/span_id；任务相关带 request_id/task_id/attempt。
异常堆栈只保留脱敏类型与内部错误码。
"""

from __future__ import annotations

import io
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, TextIO

from .contracts import (
    REQUIRED_COMMON_FIELDS,
    SCHEMA_VERSION,
    ContractError,
    EventSpec,
    LogLevel,
    validate_error_code,
    validate_event_name,
    validate_level,
    validate_stage,
    validate_status,
)
from .sanitize import dumps_line, map_vendor_error, sanitize_record
from .trace import get_current_trace


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


class StructuredLogger:
    """服务端结构化日志器：校验事件注册表、注入公共字段、过滤敏感字段。"""

    def __init__(
        self,
        service: str,
        environment: str = "development",
        stream: Optional[TextIO] = None,
        strict: bool = True,
    ) -> None:
        self.service = service
        self.environment = environment
        self.stream = stream if stream is not None else sys.stdout
        self.strict = strict
        self._clock = utc_now_iso

    def set_clock(self, fn) -> None:
        """测试可注入固定时钟。"""
        self._clock = fn

    def log(
        self,
        event: str,
        level: str = "INFO",
        *,
        task_id: Optional[str] = None,
        attempt: Optional[int] = None,
        request_id: Optional[str] = None,
        stage: Optional[str] = None,
        status: Optional[str] = None,
        error_code: Optional[str] = None,
        duration_ms: Optional[int] = None,
        **fields: Any,
    ) -> dict:
        spec = validate_event_name(event)
        validate_level(level)
        if stage is not None:
            validate_stage(stage)
        if status is not None:
            validate_status(status)
        if error_code is not None:
            validate_error_code(error_code)

        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": self._clock(),
            "level": level,
            "event": event,
            "service": self.service,
            "environment": self.environment,
        }
        trace = get_current_trace()
        if trace is not None:
            record["trace_id"] = trace.trace_id
            record["span_id"] = trace.span_id

        if task_id is not None:
            record["task_id"] = task_id
        if attempt is not None:
            record["attempt"] = attempt
        if request_id is not None:
            record["request_id"] = request_id
        if stage is not None:
            record["stage"] = stage
        if status is not None:
            record["status"] = status
        if error_code is not None:
            record["error_code"] = error_code
        if duration_ms is not None:
            record["duration_ms"] = duration_ms

        for key, value in fields.items():
            if value is None:
                continue
            record[key] = value

        self._validate_required(spec, record)
        sanitized = sanitize_record(record)
        line = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        self.stream.write(line + "\n")
        if hasattr(self.stream, "flush"):
            self.stream.flush()
        return sanitized

    def _validate_required(self, spec: EventSpec, record: Mapping[str, Any]) -> None:
        missing = sorted(spec.required_fields - set(record.keys()))
        if missing:
            if self.strict:
                raise ContractError("MISSING_FIELD", f"事件 {spec.name} 缺少必填字段: {missing}")
            return
        for name in REQUIRED_COMMON_FIELDS:
            if name not in record:
                raise ContractError("MISSING_FIELD", f"缺少公共必填字段: {name}")

    def exception_to_error_code(self, exc: BaseException) -> str:
        """异常只映射为受控错误码，不输出原始堆栈。"""
        if isinstance(exc, ContractError):
            return "INTERNAL_ERROR"
        return map_vendor_error(str(exc))

    def log_exception(
        self,
        event: str,
        exc: BaseException,
        *,
        level: str = "ERROR",
        **fields: Any,
    ) -> dict:
        error_code = fields.pop("error_code", None) or self.exception_to_error_code(exc)
        reason = fields.pop("reason", None)
        if reason is None:
            reason = type(exc).__name__
        return self.log(
            event,
            level=level,
            error_code=error_code,
            reason=reason,
            **fields,
        )


class MemoryLogStream(io.StringIO):
    """测试用内存日志流。"""

    def lines(self) -> list[str]:
        return [ln for ln in self.getvalue().splitlines() if ln.strip()]

    def records(self) -> list[dict]:
        return [json.loads(ln) for ln in self.lines()]
