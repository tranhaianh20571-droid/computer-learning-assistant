"""S1 前置：FunctionModel 桥（PydanticAI ↔ 切片 1 内容模型适配器）。

边界（ADR-019/021 不变量）：凭据解密、HTTPS 强制、SSRF 校验、外发核验、
超时与响应大小限制全部留在切片 1 适配器层；本桥只做**翻译**：

- PydanticAI 消息（ModelRequest/ModelResponse parts）→ 适配器 messages
  （role/content dict 列表，与 model_runner 现有调用形状一致）；
- 适配器返回 {"content", "tool_arguments"} → ModelResponse(TextPart / ToolCallPart)；
- 适配器异常 → AdapterBridgeError（受控错误码 MODEL_TIMEOUT /
  MODEL_CAPABILITY_UNAVAILABLE，不透出原始供应商错误）。

当前完成度（如实标注）：消息翻译、内容/工具参数回填、受控错误映射、
能力状态透传。forced tool_choice / response_format 的请求装配在 S1 调用点
切换时补齐（届时从 AgentInfo.output_tools 取 schema）；本桥先服务桥单测、
TestModel/FunctionModel 测试基建与灰度验证。
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)


class AdapterBridgeError(RuntimeError):
    """适配器异常的受控包装：只带注册过的错误码。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def messages_to_adapter(messages: List[ModelMessage]) -> List[dict]:
    """PydanticAI 消息 → 适配器 messages（role/content dict）。

    SystemPromptPart → system；UserPromptPart → user；其余 part（工具返回等）
    以受控摘要并入最近 user 消息，不透传结构（适配器契约只认 role/content）。
    """
    out: List[dict] = []
    pending_extra: List[str] = []
    for msg in messages:
        kind = getattr(msg, "kind", "request")
        role = None
        if kind == "request":
            for part in msg.parts:
                part_kind = getattr(part, "part_kind", "")
                if part_kind == "system-prompt":
                    role, text = "system", str(getattr(part, "content", ""))
                    out.append({"role": "system", "content": text})
                elif part_kind == "user-prompt":
                    content = getattr(part, "content", "")
                    text = content if isinstance(content, str) else str(content)
                    out.append({"role": "user", "content": text})
                else:
                    # tool-return / retry-prompt 等非文本 part：摘要并入下一条 user
                    pending_extra.append(part_kind)
        elif kind == "response":
            # 模型历史回答：作为 assistant 消息回放（保持多轮上下文）
            texts = [
                getattr(p, "content", "")
                for p in msg.parts
                if getattr(p, "part_kind", "") == "text"
            ]
            if texts:
                out.append({"role": "assistant", "content": "\n".join(str(t) for t in texts)})
    if pending_extra and out:
        out[-1]["content"] = str(out[-1]["content"]) + (
            f"\n[bridge: {len(pending_extra)} non-text parts summarized]"
        )
    return out


def raw_to_model_response(raw: Any, *, output_tool_name: str = "final_result") -> ModelResponse:
    """适配器返回 → ModelResponse。优先 tool_arguments（tool 模式），其次 content。"""
    if not isinstance(raw, dict):
        return ModelResponse(parts=[TextPart(content=str(raw or ""))])
    tool_args = raw.get("tool_arguments")
    if tool_args is not None:
        args = tool_args if isinstance(tool_args, str) else _dumps(tool_args)
        return ModelResponse(
            parts=[ToolCallPart(tool_name=output_tool_name, args=args)]
        )
    content = raw.get("content")
    if isinstance(content, dict):
        content = _dumps(content)
    return ModelResponse(parts=[TextPart(content=str(content or ""))])


def _dumps(value: Any) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def _output_tool_name(info: Any) -> str:
    """从 AgentInfo 取输出工具名（默认 final_result）；跨版本不硬编码。"""
    tools = getattr(info, "output_tools", None) or []
    for tool in tools:
        name = getattr(tool, "name", None)
        if name:
            return str(name)
    return "final_result"


def make_bridge_function(adapter: Any) -> Callable:
    """生成 FunctionModel 的 function：闭包持有切片 1 适配器。"""

    def bridge_function(messages: List[ModelMessage], info: Any) -> ModelResponse:
        adapter_messages = messages_to_adapter(messages)
        try:
            raw = adapter.generate(adapter_messages)
        except Exception as exc:  # noqa: BLE001 — 供应商异常只映射受控错误码
            code = getattr(exc, "code", "MODEL")
            raise AdapterBridgeError(
                "MODEL_TIMEOUT" if code == "timeout" else "MODEL_CAPABILITY_UNAVAILABLE"
            ) from exc
        return raw_to_model_response(raw, output_tool_name=_output_tool_name(info))

    return bridge_function


def make_bridge_model(adapter: Any, *, model_name: Optional[str] = None):
    """适配器 → PydanticAI FunctionModel（延迟导入，保持包可独立测试）。"""
    from pydantic_ai.models.function import FunctionModel

    name = model_name or str(getattr(adapter, "model_name", "") or "slice1-adapter")
    return FunctionModel(function=make_bridge_function(adapter), model_name=name)


__all__ = [
    "AdapterBridgeError",
    "bridge_function",
    "make_bridge_function",
    "make_bridge_model",
    "messages_to_adapter",
    "raw_to_model_response",
]
