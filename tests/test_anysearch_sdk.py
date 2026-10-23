"""Exercise local-only search via real Pydantic AI loop and strict tool schemas."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from pydantic_ai import Tool
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from backend.agents.contracts import AgentDeps, GenerationInput
from backend.agents.runner import execute_agent
from backend.planning.errors import PlanningError, error
from backend.search.agent_capability import anysearch_capability, anysearch_tool
from backend.search.contracts import Failed, Skipped
from backend.search.policy import decide_continuation


def test_tool_schema_rejects_coercion_and_capability_is_local_only():
    tool = Tool(anysearch_tool)
    for value in (True, "5", 0, 11):
        with pytest.raises(ValidationError):
            tool.function_schema.validator.validate_python({"query":"algebra", "max_results":value})
    cap = anysearch_capability()
    assert cap.native is False and cap.get_native_tools() == []


@pytest.mark.parametrize("state", ["skipped", "failed"])
def test_real_sdk_loop_consumes_controlled_search_outcome(state, monkeypatch):
    import backend.logging_audit.content_trace as content_trace
    monkeypatch.setattr(content_trace, "get_content_trace",
        lambda: pytest.fail("search run reached content trace channel"))
    calls = []
    outcome = Skipped(reason="no_config") if state == "skipped" else Failed(
        query_id=uuid4(), error_code="SEARCH_TIMEOUT", retryable=False)
    async def search(args):
        calls.append(args)
        return outcome, decide_continuation(outcome, requirement="optional")
    def model(messages, info):
        assert not info.model_request_parameters.native_tools
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if returns:
            assert returns[-1].content["state"] == state
            assert returns[-1].content["continuation"]["mode"] == "offline"
            return ModelResponse(parts=[TextPart("Synthetic grounded-or-offline answer")])
        assert any(t.name == "search_web" for t in info.function_tools)
        return ModelResponse(parts=[ToolCallPart("search_web", {"query":"algebra", "max_results":5}, "search-1")])
    deps = AgentDeps(search_executor=search)
    result = asyncio.run(execute_agent(FunctionModel(model), str,
        GenerationInput.from_messages([{"role":"user","content":"Synthetic topic"}]), deps,
        capabilities=[anysearch_capability()]))
    assert result.payload == "Synthetic grounded-or-offline answer"
    assert len(calls) == 1 and deps.budget.tool_calls == 1


def test_control_failure_with_final_text_never_returns_valid_answer():
    async def refused(args):
        raise error("TASK_CANCELLED", "synthetic key revoked")
    def model(messages, info):
        return ModelResponse(parts=[TextPart("Do not publish this"),
            ToolCallPart("search_web", {"query":"algebra"}, "search-1")])
    with pytest.raises(PlanningError) as failure:
        asyncio.run(execute_agent(FunctionModel(model), str,
            GenerationInput.from_messages([{"role":"user","content":"Synthetic topic"}]),
            AgentDeps(search_executor=refused, omit_content_trace=True),
            capabilities=[anysearch_capability()]))
    assert failure.value.code == "TASK_CANCELLED"
