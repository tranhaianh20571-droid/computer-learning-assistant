"""Explicit test-only duck mocks -> SDK FunctionModel; never production fallback.

Only root pytest fixtures install this conversion. Configured AdapterBase objects
and genuine SDK Model objects retain the real native session and safety checks.
The service still holds its original source, including counters and task metadata.
"""
from __future__ import annotations

import inspect
import json
from contextlib import asynccontextmanager

from pydantic_ai.messages import TextPart, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.profiles import ModelProfile

from backend.agents.bridge import AdapterBridgeError, messages_to_adapter, raw_to_model_response
from backend.slice1.adapters.base import AdapterBase
from backend.slice1.adapters.request_metrics import failure_code


def is_synthetic_model_source(source) -> bool:
    return (not isinstance(source, (Model, AdapterBase))
            and any(callable(getattr(source, name, None))
                    for name in ("generate", "stream", "generate_stream_text")))


def _request(messages, info):
    translated = messages_to_adapter(messages)
    if info.instructions:
        translated.insert(0, {"role": "system", "content": info.instructions})
    definitions = info.function_tools + info.output_tools
    options = {}
    if definitions:
        options["tools"] = [{"type": "function", "function": {
            "name": tool.name, "description": tool.description or "synthetic tool",
            "parameters": tool.parameters_json_schema,
        }} for tool in definitions]
        options["tool_choice"] = ("auto" if info.function_tools else
            {"type": "function", "function": {"name": info.output_tools[0].name}})
    output = info.model_request_parameters.output_object
    if info.model_request_parameters.output_mode == "native" and output is not None:
        options["response_format"] = {"type": "json_schema", "json_schema": {
            "name": output.name or "synthetic_output", "schema": output.json_schema,
        }}
    return translated, options


def synthetic_function_model(source, deps=None) -> Model:
    """Convert only generate ducks; no recovery, mode fallback or repair loop."""
    if not is_synthetic_model_source(source):
        return source

    def debit():
        if deps is not None:
            deps.check()
            deps.budget.consume("wire")

    def response(messages, info):
        translated, options = _request(messages, info)
        debit()
        try:
            raw = source.generate(translated, **options)
        except Exception as exc:
            raise AdapterBridgeError(failure_code(exc)) from exc
        return raw_to_model_response(raw,
            output_tool_name=info.output_tools[0].name if info.output_tools else "final_result")

    async def streamed(messages, info):
        translated, options = _request(messages, info)
        stream = getattr(source, "stream", None) or getattr(source, "generate_stream_text", None)
        if callable(stream):
            debit()
            chunks = None
            try:
                parameters = inspect.signature(stream).parameters
                accepted = options if any(item.kind == inspect.Parameter.VAR_KEYWORD
                    for item in parameters.values()) else {key: value for key, value in options.items()
                        if key in parameters}
                chunks = stream(translated, **accepted)
                if inspect.isawaitable(chunks):
                    chunks = await chunks
                if isinstance(chunks, str):
                    chunks = [chunks]
                if hasattr(chunks, "__aiter__"):
                    async for chunk in chunks:
                        yield chunk
                else:
                    for chunk in chunks:
                        yield chunk
            except Exception as exc:
                raise AdapterBridgeError(failure_code(exc)) from exc
            finally:
                if callable(getattr(chunks, "aclose", None)):
                    await chunks.aclose()
                elif callable(getattr(chunks, "close", None)):
                    chunks.close()
            return
        result = response(messages, info)
        for index, part in enumerate(result.parts):
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                arguments = part.args if isinstance(part.args, str) else json.dumps(part.args)
                yield {index: DeltaToolCall(name=part.tool_name, json_args=arguments,
                                           tool_call_id=part.tool_call_id)}

    status = getattr(source, "capability_status", {}) or {}
    def available(name):
        state = status.get(name)
        return (state.get("state") if isinstance(state, dict) else state) != "unavailable"
    return FunctionModel(response, stream_function=streamed, model_name="synthetic-test-model",
        profile=ModelProfile(supports_json_schema_output=available("json_schema"),
            supports_tools=available("tool_call"), supports_text_output=available("text")))


def synthetic_session_factory(native_session):
    @asynccontextmanager
    async def session(source, deps):
        if is_synthetic_model_source(source):
            yield synthetic_function_model(source, deps)
        else:
            async with native_session(source, deps) as model:
                yield model
    return session
