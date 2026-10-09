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
    # 链上找桥错误码（PydanticAI 会包一层 UnexpectedModelBehavior）
    cause = getattr(exc, "__cause__", None)
    seen = 0
    while cause is not None and seen < 5:
        c = getattr(cause, "code", None)
        if c in MODEL_FAILURE_CODES:
            return error(c, "agent model call failed")
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


__all__ = ["agent_usage_limits", "to_controlled_error", "DEFAULT_AGENT_REQUEST_LIMIT", "allows_content_fallback"]
