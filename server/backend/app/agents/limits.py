"""循环上限 → 受控错误码映射（AGENT_LOOP_LIMIT，方案 S1a）。

PydanticAI 的 UsageLimits 超限抛 UsageLimitExceeded；这里统一映射为项目
注册错误码，服务层按既有 PlanningError 语义处理（可重试、用户可见）。
"""

from __future__ import annotations

from typing import Optional

from pydantic_ai.exceptions import UsageLimitExceeded

from ..planning.errors import PlanningError, error

DEFAULT_AGENT_REQUEST_LIMIT = 8


def agent_usage_limits(max_requests: Optional[int] = None):
    """受控循环上限：默认 8 次模型请求（含框架内部重试）。"""
    from pydantic_ai import UsageLimits

    return UsageLimits(request_limit=max_requests or DEFAULT_AGENT_REQUEST_LIMIT)


def to_controlled_error(exc: BaseException) -> PlanningError:
    """框架/桥异常 → 受控错误码（不透出原始信息）。"""
    if isinstance(exc, UsageLimitExceeded):
        return error("AGENT_LOOP_LIMIT", "agent loop limit exceeded")
    code = getattr(exc, "code", None)
    if code in ("MODEL_TIMEOUT", "MODEL_CAPABILITY_UNAVAILABLE"):
        return error(code, "agent model call failed")
    # 链上找桥错误码（PydanticAI 会包一层 UnexpectedModelBehavior）
    cause = getattr(exc, "__cause__", None)
    seen = 0
    while cause is not None and seen < 5:
        c = getattr(cause, "code", None)
        if c in ("MODEL_TIMEOUT", "MODEL_CAPABILITY_UNAVAILABLE"):
            return error(c, "agent model call failed")
        cause = getattr(cause, "__cause__", None)
        seen += 1
    return error("MODEL_CAPABILITY_UNAVAILABLE", "agent run failed")


__all__ = ["agent_usage_limits", "to_controlled_error", "DEFAULT_AGENT_REQUEST_LIMIT"]
