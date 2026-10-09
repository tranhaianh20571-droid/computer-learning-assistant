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
    from ..slice1.adapters.request_metrics import failure_code
    return failure_code(exc)


def messages_to_adapter(messages: List[ModelMessage]) -> List[dict]:
    """Preserve tool calls, returns and retries using the adapter message contract."""
    import base64
    out: List[dict] = []
    for msg in messages:
        kind = getattr(msg, "kind", "request")
        if kind == "request":
            for part in msg.parts:
                part_kind = getattr(part, "part_kind", "")
                if part_kind == "system-prompt":
                    out.append({"role": "system", "content": str(getattr(part, "content", ""))})
                elif part_kind == "user-prompt":
                    content = getattr(part, "content", "")
                    if isinstance(content, str):
                        out.append({"role": "user", "content": content})
                    else:
                        parts = []
                        for item in content:
                            if isinstance(item, str):
                                parts.append({"type": "text", "text": item})
                            elif getattr(item, "kind", None) == "binary" and item.media_type.startswith("image/"):
                                data = base64.b64encode(item.data).decode("ascii")
                                parts.append({"type": "image_url", "image_url": {"url": f"data:{item.media_type};base64,{data}"}})
                            elif getattr(item, "kind", None) == "image-url":
                                parts.append({"type": "image_url", "image_url": {"url": item.url}})
                            else:
                                raise AdapterBridgeError("MODEL_CAPABILITY_UNAVAILABLE")
                        out.append({"role": "user", "content": parts})
                elif part_kind in ("tool-return", "retry-prompt"):
                    content = part.model_response() if part_kind == "retry-prompt" else part.content
                    value = content if isinstance(content, str) else _dumps(content)
                    if getattr(part, "tool_name", None):
                        out.append({"role": "tool", "name": part.tool_name,
                                    "tool_call_id": part.tool_call_id, "content": value})
                    else:
                        out.append({"role": "user", "content": value})
        elif kind == "response":
            texts = [
                getattr(p, "content", "")
                for p in msg.parts
                if getattr(p, "part_kind", "") == "text"
            ]
            calls = [{"id": p.tool_call_id, "type": "function", "function": {
                "name": p.tool_name, "arguments": p.args if isinstance(p.args, str) else _dumps(p.args)},
                "provider_details": getattr(p, "provider_details", None) or {}}
                for p in msg.parts if getattr(p, "part_kind", "") == "tool-call"]
            if texts or calls:
                item = {"role": "assistant", "content": "\n".join(str(t) for t in texts)}
                if calls:
                    item["tool_calls"] = calls
                out.append(item)
    return out


def _output_tools_payload(info: Any) -> Optional[dict]:
    """AgentInfo.output_tools → 适配器 tool 档请求参数（forced tool_choice）。"""
    function_tools = getattr(info, "function_tools", None) or []
    tools = function_tools + (getattr(info, "output_tools", None) or [])
    defs = [
        {
            "type": "function",
            "function": {
                "name": str(t.name),
                "description": getattr(t, "description", None) or "submit structured result",
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
        "tool_choice": "auto" if function_tools else {"type": "function", "function": {"name": first}},
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
    if raw.get("tool_calls"):
        parts = [TextPart(content=raw["content"])] if raw.get("content") else []
        for call in raw["tool_calls"]:
            fn = call.get("function") or call
            parts.append(ToolCallPart(tool_name=fn["name"], args=fn.get("arguments", fn.get("args", {})),
                                      tool_call_id=call.get("id") or fn["name"],
                                      provider_details=call.get("provider_details")))
        return ModelResponse(parts=parts)
    tool_args = raw.get("tool_arguments")
    if tool_args is not None:
        args = tool_args if isinstance(tool_args, str) else _dumps(tool_args)
        return ModelResponse(parts=[ToolCallPart(tool_name=output_tool_name, args=args)])
    content = raw.get("content")
    if isinstance(content, dict):
        content = _dumps(content)
    return ModelResponse(parts=[TextPart(content=str(content or ""))])


def trace_messages(messages):
    """No image bytes or function result bodies in the content trace channel."""
    result = []
    for msg in messages:
        content = msg.get("content", "")
        if msg.get("role") == "tool":
            content = "[tool result omitted]"
        elif isinstance(content, list):
            content = "\n".join(p.get("text", "") for p in content if p.get("type") == "text")
        result.append({"role": msg.get("role", "user"), "content": content})
    return result


def make_bridge_function(adapter: Any, *, stage: str = "generate") -> Callable:
    """生成 FunctionModel 的 function：闭包持有切片 1 适配器。

    同步形态——Agent.run_sync 在无事件循环的 worker 线程安全（方案 v1.1 R3：
    调用点保持同步形态；上游 #3899 的 run_sync+子代理死锁未修，禁子代理）。
    """

    def bridge_function(messages: List[ModelMessage], info: Any) -> ModelResponse:
        adapter_messages = messages_to_adapter(messages)
        if getattr(info, "instructions", None):
            adapter_messages.insert(0, {"role": "system", "content": info.instructions})
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
                prompt_messages=[] if getattr(adapter,"omit_prompt_trace",False) else trace_messages(adapter_messages), reason=code,
            )
            raise AdapterBridgeError(code) from exc
        answer = raw.get("content") if isinstance(raw, dict) else raw
        if answer is None and isinstance(raw, dict):
            answer = raw.get("tool_arguments")  # tool 档：结构化参数即回答
        get_content_trace().emit(
            stage, "succeeded", mode=mode,
            prompt_messages=[] if getattr(adapter,"omit_prompt_trace",False) else trace_messages(adapter_messages), answer=answer,
        )
        return raw_to_model_response(raw, output_tool_name=_output_tool_name(info))

    return bridge_function


def make_stream_function(adapter: Any, *, stage: str = "generate") -> Callable:
    """生成 FunctionModel 的 stream_function：对接适配器 generate_stream_text。

    契约：异步生成器，yield str 增量（文本路径）；失败映射受控错误码。
    """

    async def stream_function(messages: List[ModelMessage], info: Any) -> AsyncIterator[str]:
        adapter_messages = messages_to_adapter(messages)
        if getattr(info, "instructions", None):
            adapter_messages.insert(0, {"role": "system", "content": info.instructions})
        buffer: List[str] = []
        try:
            # Provider stream() yields real deltas; generate_stream_text() collects
            # the whole response and must never be iterated character by character.
            source = adapter.stream(adapter_messages) if hasattr(adapter,"stream") else adapter.generate_stream_text(adapter_messages)
            if isinstance(source,str):
                source = [source]
            for delta in source:
                text = str(delta)
                buffer.append(text)
                yield text
        except Exception as exc:  # noqa: BLE001
            raise AdapterBridgeError(_controlled_code(exc)) from exc
        finally:
            get_content_trace().emit(
                stage, "succeeded" if buffer else "failed", mode="stream",
                prompt_messages=[] if getattr(adapter,"omit_prompt_trace",False) else trace_messages(adapter_messages), answer="".join(buffer) or None,
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
    if stream and getattr(adapter, "supports_text_stream", False):
        kwargs["stream_function"] = make_stream_function(adapter, stage=stage)
    return FunctionModel(**kwargs)


def make_fallback_model(primary_adapter: Any, fallback_adapter: Any, *, model_name: Optional[str] = None):
    """S3：模型级兜底（默认关闭，灰度启用——方案 §3 S3）。

    FallbackModel 只换模型不重试同一模型（官方语义）；fallback_on 捕获
    桥受控错误（供应商请求失败）。已知坑（必测）：不因重试耗尽触发
    （上游 #2837）、不与适配器层重试叠加（#3267）。
    """
    from pydantic_ai.models.fallback import FallbackModel

    return FallbackModel(
        make_bridge_model(primary_adapter),
        make_bridge_model(fallback_adapter),
        fallback_on=(AdapterBridgeError,),
    )


__all__ = [
    "AdapterBridgeError",
    "make_bridge_function",
    "make_stream_function",
    "make_bridge_model",
    "make_fallback_model",
    "messages_to_adapter",
    "raw_to_model_response",
]
