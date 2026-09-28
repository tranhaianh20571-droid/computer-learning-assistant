"""S1a：FunctionModel 桥（PydanticAI ↔ 切片 1 内容模型适配器）——完整版。

边界（ADR-019/023 不变量）：凭据解密、HTTPS 强制、SSRF 校验、外发核验、
超时与响应大小限制全部留在切片 1 适配器层；本桥只做翻译与装配：

- 消息翻译：PydanticAI parts → 适配器 role/content messages；
- 请求装配：AgentInfo.output_tools（ToolOutput 模式）→ 适配器
  `tools`/`tool_choice` 参数（forced，与 model_runner 的 tool 档同形）；
- 回填：`tool_arguments` → ToolCallPart（工具名动态取自 info）；content → TextPart；
- 流式：`make_stream_function` 对接适配器 `generate_stream_text`（异步增量）；
- 内容捕获（ADR-022）：成功/失败出口 emit `model.call.content`（开关受
  CONTENT_TRACE_ENABLED 控制）——调用点迁移后 model_runner 出口被绕过，
  这里是内容通道的接续点（方案 v1.1 风险 R1）；
- 供应商异常 → AdapterBridgeError（MODEL_TIMEOUT / MODEL_CAPABILITY_UNAVAILABLE）。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Callable, List, Optional

from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)

from ..logging_audit.content_trace import get_content_trace


class AdapterBridgeError(RuntimeError):
    """适配器异常的受控包装：只带注册过的错误码。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _controlled_code(exc: BaseException) -> str:
    code = getattr(exc, "code", "MODEL_TIMEOUT")
    return "MODEL_TIMEOUT" if code == "timeout" else "MODEL_CAPABILITY_UNAVAILABLE"


def messages_to_adapter(messages: List[ModelMessage]) -> List[dict]:
    """PydanticAI 消息 → 适配器 messages（role/content dict）。

    SystemPromptPart → system；UserPromptPart → user；模型历史回答 → assistant；
    其余 part（工具返回/重试提示等）以受控摘要并入，不透传结构。
    PromptedOutput 的 schema 注入已由框架写入 user 消息正文，无需桥处理。
    """
    out: List[dict] = []
    pending_extra: List[str] = []
    for msg in messages:
        kind = getattr(msg, "kind", "request")
        if kind == "request":
            for part in msg.parts:
                part_kind = getattr(part, "part_kind", "")
                if part_kind == "system-prompt":
                    out.append({"role": "system", "content": str(getattr(part, "content", ""))})
                elif part_kind == "user-prompt":
                    content = getattr(part, "content", "")
                    text = content if isinstance(content, str) else str(content)
                    out.append({"role": "user", "content": text})
                else:
                    pending_extra.append(part_kind)
        elif kind == "response":
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


def _output_tools_payload(info: Any) -> Optional[dict]:
    """AgentInfo.output_tools → 适配器 tool 档请求参数（forced tool_choice）。"""
    tools = getattr(info, "output_tools", None) or []
    defs = [
        {
            "type": "function",
            "function": {
                "name": str(t.name),
                "description": "submit structured result",
                "parameters": getattr(t, "parameters_json_schema", None) or {},
            },
        }
        for t in tools
        if getattr(t, "name", None)
    ]
    if not defs:
        return None
    first = defs[0]["function"]["name"]
    return {
        "tools": defs,
        "tool_choice": {"type": "function", "function": {"name": first}},
    }


def _output_tool_name(info: Any) -> str:
    tools = getattr(info, "output_tools", None) or []
    for tool in tools:
        name = getattr(tool, "name", None)
        if name:
            return str(name)
    return "final_result"


def _dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def raw_to_model_response(raw: Any, *, output_tool_name: str = "final_result") -> ModelResponse:
    """适配器返回 → ModelResponse。优先 tool_arguments（tool 档），其次 content。"""
    if not isinstance(raw, dict):
        return ModelResponse(parts=[TextPart(content=str(raw or ""))])
    tool_args = raw.get("tool_arguments")
    if tool_args is not None:
        args = tool_args if isinstance(tool_args, str) else _dumps(tool_args)
        return ModelResponse(parts=[ToolCallPart(tool_name=output_tool_name, args=args)])
    content = raw.get("content")
    if isinstance(content, dict):
        content = _dumps(content)
    return ModelResponse(parts=[TextPart(content=str(content or ""))])


def make_bridge_function(adapter: Any, *, stage: str = "generate") -> Callable:
    """生成 FunctionModel 的 function：闭包持有切片 1 适配器。

    同步形态——Agent.run_sync 在无事件循环的 worker 线程安全（方案 v1.1 R3：
    调用点保持同步形态；上游 #3899 的 run_sync+子代理死锁未修，禁子代理）。
    """

    def bridge_function(messages: List[ModelMessage], info: Any) -> ModelResponse:
        adapter_messages = messages_to_adapter(messages)
        payload = _output_tools_payload(info)
        mode = "tool" if payload else "prompted"
        try:
            if payload:
                raw = adapter.generate(adapter_messages, **payload)
            else:
                raw = adapter.generate(adapter_messages)
        except Exception as exc:  # noqa: BLE001 — 供应商异常只映射受控错误码
            code = _controlled_code(exc)
            get_content_trace().emit(
                stage, "failed", mode=mode,
                prompt_messages=adapter_messages, reason=code,
            )
            raise AdapterBridgeError(code) from exc
        answer = raw.get("content") if isinstance(raw, dict) else raw
        if answer is None and isinstance(raw, dict):
            answer = raw.get("tool_arguments")  # tool 档：结构化参数即回答
        get_content_trace().emit(
            stage, "succeeded", mode=mode,
            prompt_messages=adapter_messages, answer=answer,
        )
        return raw_to_model_response(raw, output_tool_name=_output_tool_name(info))

    return bridge_function


def make_stream_function(adapter: Any, *, stage: str = "generate") -> Callable:
    """生成 FunctionModel 的 stream_function：对接适配器 generate_stream_text。

    契约：异步生成器，yield str 增量（文本路径）；失败映射受控错误码。
    """

    async def stream_function(messages: List[ModelMessage], info: Any) -> AsyncIterator[str]:
        adapter_messages = messages_to_adapter(messages)
        buffer: List[str] = []
        try:
            for delta in adapter.generate_stream_text(adapter_messages):
                text = str(delta)
                buffer.append(text)
                yield text
        except Exception as exc:  # noqa: BLE001
            raise AdapterBridgeError(_controlled_code(exc)) from exc
        finally:
            get_content_trace().emit(
                stage, "succeeded" if buffer else "failed", mode="stream",
                prompt_messages=adapter_messages, answer="".join(buffer) or None,
            )

    return stream_function


def make_bridge_model(
    adapter: Any,
    *,
    model_name: Optional[str] = None,
    stream: bool = False,
    stage: str = "generate",
):
    """适配器 → PydanticAI FunctionModel（延迟导入；stream=True 时挂流式）。"""
    from pydantic_ai.models.function import FunctionModel

    name = model_name or str(getattr(adapter, "model_name", "") or "slice1-adapter")
    kwargs: dict = {"function": make_bridge_function(adapter, stage=stage), "model_name": name}
    if stream and hasattr(adapter, "generate_stream_text"):
        kwargs["stream_function"] = make_stream_function(adapter, stage=stage)
    return FunctionModel(**kwargs)


__all__ = [
    "AdapterBridgeError",
    "make_bridge_function",
    "make_stream_function",
    "make_bridge_model",
    "messages_to_adapter",
    "raw_to_model_response",
]
