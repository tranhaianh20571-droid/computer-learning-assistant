"""Agent 层（PydanticAI v2，ADR-023 / 方案 v1.1 S1a）。

S1a 完整落地：FunctionModel 桥（装配/流式/内容捕获）+ runner（门控降级
tool→prompted）+ limits（AGENT_LOOP_LIMIT）。五+1 个 Agent 定义与受控
工具按 S1b–S3 随调用点切换落位。
"""

from .bridge import (
    AdapterBridgeError,
    make_bridge_function,
    make_bridge_model,
    make_fallback_model,
    make_stream_function,
    messages_to_adapter,
    raw_to_model_response,
)
from .limits import agent_usage_limits, to_controlled_error
from .runner import run_chat_agent, run_structured_agent

__all__ = [
    "AdapterBridgeError",
    "make_bridge_function",
    "make_bridge_model",
    "make_fallback_model",
    "make_stream_function",
    "messages_to_adapter",
    "raw_to_model_response",
    "agent_usage_limits",
    "to_controlled_error",
    "run_structured_agent",
    "run_chat_agent",
]
