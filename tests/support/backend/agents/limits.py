"""循环上限 → 受控错误码映射（AGENT_LOOP_LIMIT，方案 S1a）。

PydanticAI 的 UsageLimits 超限抛 UsageLimitExceeded；这里统一映射为项目
注册错误码，服务层按既有 PlanningError 语义处理（可重试、用户可见）。
"""

from __future__ import annotations

from typing import Optional

from pydantic_ai.exceptions import UsageLimitExceeded

from ..planning.errors import PlanningError, error

DEFAULT_AGENT_REQUEST_LIMIT = 8
MODEL_FAILURE_CODES = ("MODEL_TIMEOUT", "MODEL_CAPABILITY_UNAVAILABLE", "MODEL_CONNECTION_ERROR",
    "MODEL_RATE_LIMITED", "MODEL_CREDENTIAL_INVALID", "MODEL_PROVIDER_ERROR", "MODEL_RESPONSE_TOO_LARGE")


def agent_usage_limits(max_requests: Optional[int] = None):
    """受控循环上限：默认 8 次模型请求（含框架内部重试）。"""
    from pydantic_ai import UsageLimits

    return UsageLimits(request_limit=max_requests or DEFAULT_AGENT_REQUEST_LIMIT)


def _registered_error(code):
    from ..logging_audit.contracts import normalize_error_code
    normalized = normalize_error_code(code)
    if normalized == "INTERNAL_ERROR" and code != normalized:
        from ..logging_audit.metrics import record_task_diagnostic
        record_task_diagnostic(reason="unknown_error_code", error_code=normalized)
    return normalized


def to_controlled_error(exc: BaseException) -> PlanningError:
    """框架/桥异常 → 受控错误码（不透出原始信息）。"""
    from pydantic_ai.exceptions import UnexpectedModelBehavior
    from ..slice1.errors import Slice1Error
    from ..logging_audit.audit import AuditWriteError
    from ..logging_audit.task_events import AccessDeniedError
    from ..tasks.service import LeaseError
    cause = exc
    for _ in range(6):
        if isinstance(cause, (AuditWriteError, LeaseError, AccessDeniedError)):
            raise cause
        if isinstance(cause, Slice1Error):
            return error(_registered_error(cause.code), "agent authorization rejected", status=cause.status)
        if isinstance(cause, PlanningError):
            normalized = _registered_error(cause.code)
            return cause if normalized == cause.code else error(normalized, "agent request rejected", status=cause.status)
        cause = getattr(cause, "__cause__", None)
        if cause is None:
            break
    if isinstance(exc, PlanningError):
        return exc
    if isinstance(exc, UsageLimitExceeded):
        return error("AGENT_LOOP_LIMIT", "agent loop limit exceeded")
    if isinstance(exc, UnexpectedModelBehavior) and getattr(exc, "__cause__", None) is None:
        return error("OUTPUT_INVALID", "agent output validation exhausted")
    code = getattr(exc, "code", None)
    if code in MODEL_FAILURE_CODES:
        return error(code, "agent model call failed")
    # SDKs can wrap body-read failures after the transport has returned headers.
    import httpx2
    from openai import APITimeoutError as OpenAITimeout, APIConnectionError as OpenAIConnection
    from anthropic import APITimeoutError as AnthropicTimeout, APIConnectionError as AnthropicConnection
    cause = exc
    seen = 0
    while cause is not None and seen < 6:
        c = getattr(cause, "code", None)
        if c in MODEL_FAILURE_CODES:
            return error(c, "agent model call failed")
        if isinstance(cause, (httpx2.TimeoutException, OpenAITimeout, AnthropicTimeout)):
            return error("MODEL_TIMEOUT", "agent model call timed out")
        if isinstance(cause, (httpx2.RequestError, OpenAIConnection, AnthropicConnection)):
            return error("MODEL_CONNECTION_ERROR", "agent model connection failed")
        cause = getattr(cause, "__cause__", None)
        seen += 1
    if isinstance(exc, UnexpectedModelBehavior):
        return error("OUTPUT_INVALID", "agent output validation exhausted")
    return error(_registered_error(getattr(exc, "code", None)), "agent run failed")


def allows_content_fallback(exc: BaseException) -> bool:
    """Only controlled content/provider failures permit a local content fallback."""
    allowed = {"OUTPUT_INVALID", "INVALID_OUTLINE_STRUCTURE", *MODEL_FAILURE_CODES}
    allowed.discard("MODEL_CREDENTIAL_INVALID")
    return isinstance(exc, PlanningError) and exc.code in allowed


def failure_reason(exc: BaseException, limit: int = 200) -> str:
    """受控失败诊断：只取异常类型与 pydantic 错误的 type/loc，绝不取输入内容。

    ``ValidationError.errors()`` 的 ``input``/``msg`` 可能带模型原始输出，属于
    内容追踪范畴，不得进入运行日志；这里仅保留结构信息（哪个字段、哪类约束），
    用于离线判断失败是字段级（SDK 反序列化阶段）还是结构级（业务校验器）。
    """
    parts: list[str] = []
    cause = exc
    for _ in range(6):
        if cause is None:
            break
        name = type(cause).__name__
        from .validation import OutputConstraintRetry, constraint_reason
        if isinstance(cause, OutputConstraintRetry):
            name += "[" + constraint_reason(cause) + "]"
        if name == "ValidationError":
            errors = getattr(cause, "errors", None)
            if callable(errors):
                try:
                    items = errors()
                except Exception:
                    items = []
                detail = ", ".join(
                    "{}@{}".format(item.get("type", "?"), ".".join(str(p) for p in item.get("loc", ())))
                    for item in items[:3]
                )
                if detail:
                    name = "{}[{}]".format(name, detail)
        if name not in parts:
            parts.append(name)
        cause = getattr(cause, "__cause__", None)
    return " <- ".join(parts)[:limit]


__all__ = ["agent_usage_limits", "to_controlled_error", "DEFAULT_AGENT_REQUEST_LIMIT", "allows_content_fallback", "failure_reason"]
