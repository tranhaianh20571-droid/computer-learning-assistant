"""Test-only conversion contracts; real SDK execution, synthetic objects, no I/O."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.output import NativeOutput, ToolOutput

from backend.agents.contracts import AgentDeps
from backend.agents.runner import run_chat_agent, run_streaming_structured_agent, run_structured_agent
from backend.slice1.adapters.base import AdapterBase
from native_model_support import (
    is_synthetic_model_source, synthetic_function_model, synthetic_session_factory,
)


class Candidate(BaseModel):
    answer: str


class Duck:
    config_id = "synthetic_config"
    task_id = "synthetic_task"
    capability_status = {"json_schema": "available", "tool_call": "available", "text": "available"}

    def __init__(self):
        self.calls = []
        self.stream_calls = []

    def generate(self, messages, **options):
        self.calls.append((messages, options))
        if options.get("tools"):
            return {"tool_arguments": {"answer": "synthetic answer"}}
        return {"content": '{"answer":"synthetic answer"}'}

    def stream(self, messages, **options):
        self.stream_calls.append((messages, options))
        yield '{"answer":'
        yield '"synthetic answer"}'


def test_convert_only_generate_ducks_preserving_genuine_models():
    for source in (None, object(), AdapterBase.__new__(AdapterBase), TestModel(),
                   FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("ok")]))):
        assert not is_synthetic_model_source(source)
        assert synthetic_function_model(source) is source
    duck = Duck()
    model = synthetic_function_model(duck)
    assert isinstance(model, FunctionModel)
    assert duck.config_id == "synthetic_config" and duck.task_id == "synthetic_task"
    assert duck.calls == []


@pytest.mark.parametrize("source", [None, AdapterBase.__new__(AdapterBase), TestModel()])
def test_genuine_source_delegates_original_session_without_wire_bypass(source):
    calls = []
    marker = object()
    @asynccontextmanager
    async def original(value, deps):
        calls.append((value, deps))
        yield marker
    async def exercise():
        deps = AgentDeps()
        async with synthetic_session_factory(original)(source, deps) as received:
            assert received is marker
        assert calls == [(source, deps)]
    asyncio.run(exercise())


@pytest.mark.parametrize("output_type,option", [
    (NativeOutput(Candidate), "response_format"),
    (ToolOutput(Candidate, name="submit_fixture"), "tools"),
])
def test_sdk_output_mode_and_source_counters_are_preserved(output_type, option):
    source = Duck()
    result = Agent(synthetic_function_model(source), output_type=output_type,
                   instructions="synthetic bound instructions").run_sync("synthetic prompt")
    assert result.output.answer == "synthetic answer"
    messages, options = source.calls[0]
    assert option in options
    assert messages[0] == {"role": "system", "content": "synthetic bound instructions"}
    assert source.config_id == "synthetic_config"
    if option == "tools":
        assert options["tools"][0]["function"]["name"] == "submit_fixture"


def test_autouse_injection_uses_native_executor_and_shared_budget():
    source = Duck()
    deps = AgentDeps()
    result = run_structured_agent(source, Candidate, [{"role": "user", "content": "prompt"}], deps=deps)
    assert result.payload.answer == "synthetic answer"
    assert result.requests == result.wire_requests == deps.budget.wire_requests == 1
    assert len(source.calls) == 1


def test_stream_chunks_counters_and_original_source_survive():
    source = Duck()
    emitted = []
    def delta(text, reset=False):
        emitted.append((text, reset))
    result = run_streaming_structured_agent(source, Candidate,
        [{"role": "user", "content": "prompt"}], on_delta=delta)
    assert result.payload.answer == "synthetic answer"
    assert source.calls == [] and len(source.stream_calls) == 1
    assert "".join(text for text, reset in emitted if not reset) == '{"answer":"synthetic answer"}'
    assert any(reset for text, reset in emitted)


def test_sdk_validator_retry_and_real_business_tools_remain_native():
    source = Duck()
    source.capability_status = {"json_schema": "unavailable", "tool_call": "available"}
    count = []
    def lookup(value: str) -> str:
        count.append(value)
        return "tool result"
    def generate(messages, **options):
        source.calls.append((messages, options))
        if len(source.calls) == 1:
            return {"tool_calls": [{"id": "call_1", "function": {"name": "lookup", "arguments": {"value": "synthetic"}}}]}
        return {"tool_arguments": {"answer": "synthetic answer"}}
    source.generate = generate
    result = run_structured_agent(source, Candidate, [{"role": "user", "content": "prompt"}], tools=[lookup])
    assert result.payload.answer == "synthetic answer" and count == ["synthetic"]
    assert result.requests == 2
    assert any(message.get("role") == "tool" for message in source.calls[1][0])


def test_synthetic_error_retains_controlled_mapping():
    from backend.planning.errors import PlanningError
    source = Duck()
    def fail(*args, **kwargs):
        exc = TimeoutError("synthetic private error")
        exc.code = "timeout"
        raise exc
    source.generate = fail
    with pytest.raises(PlanningError) as caught:
        run_chat_agent(source, [{"role": "user", "content": "prompt"}], max_len=100)
    assert caught.value.code == "MODEL_TIMEOUT"
    assert "synthetic private error" not in str(caught.value)
