"""ADR-022：模型调用内容捕获（`model.call.content` 事件 + OTel 内容 span）。

- 总开关 `CONTENT_TRACE_ENABLED`（环境变量，默认关）：关闭时 emit 完全 no-op，
  保留一键关停能力。
- 内容三项（提示词 / 模型回答 / 用户问题）只走内容通道：
  1) `model.call.content` 事件 —— 经 StructuredLogger 路由到内容输出口
     （content_stream，独立留存）；字段级 sanitize 保证控制字符清理与
     16 KiB 截断，凭据类字段名仍被 FORBIDDEN 名单拒绝。
  2) OTel span（GenAI 语义约定属性）—— 未装配 tracer 时静默跳过。
- 聚合指标 `model.call.completed/failed` 保持零内容；两条链路互不污染。
- 发射失败（logger 异常、契约错误）只递增 emit_errors，绝不影响模型调用。
"""

from __future__ import annotations

import json
import os
from typing import Any, List, Optional

from .logger import StructuredLogger
from .otel_setup import record_model_span

_DEFAULT_ENVIRONMENT = "development"


def content_trace_enabled() -> bool:
    return os.environ.get("CONTENT_TRACE_ENABLED", "").lower() in ("1", "true", "yes")


class ContentTrace:
    """内容通道发射器：logger 可注入（测试/宿主），默认 stdout。"""

    def __init__(self, environment: Optional[str] = None) -> None:
        self._environment = environment or os.environ.get("ENVIRONMENT", _DEFAULT_ENVIRONMENT)
        self._logger: Optional[StructuredLogger] = None
        self._default_logger: Optional[StructuredLogger] = None
        self.emit_errors = 0

    def attach_logger(self, logger: Optional[StructuredLogger]) -> None:
        self._logger = logger

    def _get_logger(self) -> StructuredLogger:
        if self._logger is not None:
            return self._logger
        if self._default_logger is None:
            self._default_logger = StructuredLogger(service="api", environment=self._environment)
        return self._default_logger

    def emit(
        self,
        stage: str,
        status: str,
        *,
        mode: Optional[str] = None,
        task_id: Optional[str] = None,
        attempt: Optional[int] = None,
        config_id: Optional[str] = None,
        user_question: Optional[str] = None,
        prompt_messages: Optional[List[dict]] = None,
        answer: Optional[Any] = None,
        reason: Optional[str] = None,
    ) -> None:
        """捕获一次模型调用的内容。开关关闭时直接返回。"""
        if not content_trace_enabled():
            return
        model_prompt = None
        if prompt_messages:
            try:
                model_prompt = json.dumps(prompt_messages, ensure_ascii=False)
            except (TypeError, ValueError):
                model_prompt = "[unserializable messages]"
        model_answer = None
        if answer is not None:
            model_answer = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        try:
            self._get_logger().log(
                "model.call.content",
                level="INFO",
                stage=stage,
                status=status,
                task_id=task_id,
                attempt=attempt,
                mode=mode,
                config_id=config_id,
                model_prompt=model_prompt,
                model_answer=model_answer,
                user_question=user_question,
                reason=reason,
            )
        except Exception:  # noqa: BLE001 — 内容发射失败绝不影响业务调用
            self.emit_errors += 1
            return
        # OTel span：与内容事件同源；未装配 tracer 时为 no-op
        record_model_span(
            "model.call",
            task_id=task_id,
            mode=mode,
            prompt=model_prompt,
            answer=model_answer,
            user_question=user_question,
        )


_instance: Optional[ContentTrace] = None


def get_content_trace() -> ContentTrace:
    global _instance
    if _instance is None:
        _instance = ContentTrace()
    return _instance


def reset_content_trace() -> None:
    global _instance
    _instance = None


__all__ = [
    "ContentTrace", "get_content_trace", "reset_content_trace", "content_trace_enabled",
]
