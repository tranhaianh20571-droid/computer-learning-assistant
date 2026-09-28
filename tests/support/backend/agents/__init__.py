"""Agent 层（PydanticAI v2，ADR-021/022 配套）。

S1 前置落地：FunctionModel 桥 + 受控错误映射。五+1 个 Agent 定义与
受控工具（search_materials/validate_board/get_outline_context）按
`文档/架构/v2/pydanticai-v2-refactor-execution-plan.md` S1–S3 逐步落位。
"""

from .bridge import (
    AdapterBridgeError,
    make_bridge_function,
    make_bridge_model,
    messages_to_adapter,
    raw_to_model_response,
)

__all__ = [
    "AdapterBridgeError",
    "make_bridge_function",
    "make_bridge_model",
    "messages_to_adapter",
    "raw_to_model_response",
]
