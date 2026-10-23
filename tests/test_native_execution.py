"""Offline official SDK execution and pinned outbound transport contracts."""
from __future__ import annotations

import asyncio
import json
import socket
from datetime import datetime, timedelta, timezone

import httpx2 as httpx
import pytest
from pydantic import BaseModel, Field
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import (
    ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.capabilities import AbstractCapability

from backend.agents import native, runner
from backend.agents.contracts import AgentDeps, GenerationInput, RunBudget
from backend.agents.native import ModelCapabilityError, SafeModelTransport, native_model_session
from backend.planning.errors import PlanningError, error
from backend.planning import model_runner as legacy_runner
from backend.slice1.adapters.base import AdapterBase
from backend.tasks.service import LeaseError


@pytest.fixture(name="native_synthetic_model_session")
def no_legacy_duck_conversion():
    """Override the root compatibility fixture without mutating native execution."""
    yield


class Candidate(BaseModel):
    value: int = Field(ge=0)


INPUT = GenerationInput.from_messages([{"role": "user", "content": "synthetic question"}])
PROFILE = {"supports_json_schema_output": True, "supports_tools": True,
           "supports_text_output": True}


def execute(model, deps=None, **kwargs):
    return asyncio.run(runner.execute_agent(model, Candidate, INPUT, deps or AgentDeps(), **kwargs))


@pytest.mark.parametrize("meaningful",[False,True])
def test_opt_in_progress_timeout_ignores_blank_keepalive_but_allows_long_real_output(meaningful):
    closed=[]
    class SlowInitialization(AbstractCapability):
        id="synthetic_slow_initialization"
        async def before_run(self,ctx):
            await asyncio.sleep(.6)
    async def stream(messages,info):
        try:
            if meaningful:
                for char in '{"value":1}':
                    yield char
                    await asyncio.sleep(.07)
            else:
                while True:
                    yield " "
                    await asyncio.sleep(.02)
        finally:
            closed.append(True)
    model=FunctionModel(stream_function=stream)
    options={"on_delta":lambda *args,**kwargs:None,"timeout_seconds":4,"progress_timeout_seconds":.5,
        "capabilities":[SlowInitialization()]}
    if meaningful:
        assert execute(model,**options).payload.value==1
    else:
        with pytest.raises(PlanningError) as caught:execute(model,**options)
        assert caught.value.code=="MODEL_TIMEOUT"
    assert closed


@pytest.mark.parametrize("streaming",[False,True])
def test_opt_in_wall_timeout_cancels_even_when_stream_keeps_arriving(streaming):
    closed=[]
    async def slow(messages,info):
        try:
            await asyncio.sleep(10)
            return ModelResponse(parts=[TextPart('{"value":1}')])
        finally:
            closed.append(True)
    async def trickle(messages,info):
        try:
            while True:
                yield " "
                await asyncio.sleep(.001)
        finally:
            closed.append(True)
    model=FunctionModel(slow,stream_function=trickle)
    with pytest.raises(PlanningError) as failure:
        execute(model,timeout_seconds=.05,on_delta=(lambda *args,**kwargs:None) if streaming else None)
    assert failure.value.code=="MODEL_TIMEOUT"
    assert closed==[True]


@pytest.mark.parametrize("failure", ["silent", "trickle"])
def test_teaching_role_bounds_silent_and_continuously_streaming_runs(monkeypatch, failure):
    from backend.agents.contracts import TeachingStepInput
    from backend.agents.roles import teaching_agent
    closed, timers = [], []
    timeout = asyncio.timeout

    def scaled_timeout(delay):
        timers.append(delay)
        return timeout(.08 if delay == 120 else .03 if delay == 60 else delay)

    monkeypatch.setattr(runner.asyncio, "timeout", scaled_timeout)

    async def stream(messages, info):
        try:
            if failure == "silent":
                await asyncio.sleep(10)
            else:
                yield '{"steps":[{"text":"'
            while True:
                yield "x"
                await asyncio.sleep(.001)
        finally:
            closed.append(True)

    with pytest.raises(PlanningError) as caught:
        teaching_agent.generate_step(FunctionModel(stream_function=stream),
            TeachingStepInput.from_messages([{"role": "user", "content": "synthetic"}]),
            AgentDeps(), on_delta=lambda *args, **kwargs: None)
    assert caught.value.code == "MODEL_TIMEOUT"
    assert 120 in timers
    assert closed == [True]


def public_dns(host, port, *args):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]


def wire(handler, *, deps=None, endpoint="https://model.invalid/v1", resolver=public_dns,
         max_bytes=1024):
    return SafeModelTransport(endpoint, deps or AgentDeps(), resolver=resolver,
        inner=httpx.MockTransport(handler), max_bytes=max_bytes)


def send(transport, *, url="https://model.invalid/v1/chat/completions", body=b"{}"):
    async def run():
        async with httpx.AsyncClient(transport=transport, trust_env=False,
                                   follow_redirects=False) as client:
            return await client.post(url, content=body)
    return asyncio.run(run())


@pytest.mark.parametrize("failure",[httpx.ConnectTimeout,httpx.ConnectError])
def test_pinned_transport_tries_next_approved_address_only_before_sending(failure):
    seen=[]
    deps=AgentDeps()
    def resolve(*args):
        return [(socket.AF_INET,socket.SOCK_STREAM,6,"",(address,443))
                for address in ("93.184.216.34","93.184.216.35")]
    def reply(request):
        seen.append((request.url.host,request.headers["host"],request.extensions["sni_hostname"]))
        if len(seen)==1:
            raise failure("synthetic connection establishment failure",request=request)
        return httpx.Response(200,json={"synthetic":True})
    response=send(wire(reply,deps=deps,resolver=resolve))
    assert response.status_code==200
    assert seen==[(address,"model.invalid",b"model.invalid") for address in ("93.184.216.34","93.184.216.35")]
    assert deps.budget.wire_requests==2


@pytest.mark.parametrize("failure",[httpx.ReadTimeout,httpx.WriteError])
def test_pinned_transport_does_not_resubmit_after_ambiguous_send(failure):
    seen=[]
    def resolve(*args):
        return [(socket.AF_INET,socket.SOCK_STREAM,6,"",(address,443))
                for address in ("93.184.216.34","93.184.216.35")]
    def reply(request):
        seen.append(request.url.host)
        raise failure("synthetic failure after connection",request=request)
    with pytest.raises(PlanningError):
        send(wire(reply,resolver=resolve))
    assert seen==["93.184.216.34"]


@pytest.mark.parametrize("output_name", ["RouteCandidate", "OutlineCandidate", "IntakeQuestionSet",
    "LessonBlueprint", "LessonPlanCandidate", "UnitAssessment", "AssessmentFeedback"])
def test_migrated_synthetic_fixtures_honor_native_schema_with_business_tools(output_name):
    from common_fixtures import SyntheticModelAdapter, SkeletonThenOutlineAdapter, OUTLINE_OK_PAYLOAD
    from test_teaching_assessments import LearningAdapter
    from test_v2_intake import InterviewAdapter
    from native_model_support import synthetic_function_model
    from backend.planning.outputs import RouteCandidate, OutlineCandidate, IntakeQuestionSet
    from backend.teaching.learning_outputs import (
        LessonBlueprint, UnitAssessment, AssessmentFeedback, validate_new_blueprint,
    )
    from backend.teaching.outputs import LessonPlanCandidate

    adapters = {
        "RouteCandidate": (SyntheticModelAdapter(), RouteCandidate, {}, None),
        "OutlineCandidate": (SkeletonThenOutlineAdapter(outline_payload=OUTLINE_OK_PAYLOAD),
            OutlineCandidate, {"prompt": "课程名称：合成"}, None),
        "IntakeQuestionSet": (InterviewAdapter(), IntakeQuestionSet,
            {"shown_count": 0, "original_request": "synthetic", "answers": []}, None),
        "LessonBlueprint": (LearningAdapter(), LessonBlueprint,
            {"lesson_goal": "synthetic"}, validate_new_blueprint),
        "LessonPlanCandidate": (LearningAdapter(), LessonPlanCandidate,
            {"current_stage": {"phase": "intuition", "goal_ids": ["g1"]}, "context": {"lesson_title": "synthetic"},
             "is_final": False}, None),
        "UnitAssessment": (LearningAdapter(), UnitAssessment,
            {"lessons": [{"lesson_id": "synthetic"}]}, None),
        "AssessmentFeedback": (LearningAdapter(), AssessmentFeedback,
            {"questions": [{"id": "synthetic"}]}, None),
    }
    adapter, output_cls, context, validator = adapters[output_name]
    deps = AgentDeps()
    def get_context() -> dict:
        """Return synthetic authorized read-only business context."""
        return {"synthetic": True}
    result = asyncio.run(runner.execute_agent(synthetic_function_model(adapter, deps), output_cls,
        GenerationInput.from_messages([{"role": "user", "content": json.dumps(context, ensure_ascii=False)}]),
        deps, tools=[get_context], output_validator=validator))
    assert isinstance(result.payload, output_cls)
    assert result.mode == "native" and result.degraded_from == []
    assert result.requests == deps.budget.logical_requests == deps.budget.wire_requests == 1


def test_sdk_usage_one_success_is_not_double_counted():
    budget = RunBudget(request_limit=2)
    result = execute(TestModel(custom_output_args={"value": 1}), AgentDeps(budget=budget))
    assert result.payload.value == 1
    assert result.requests == budget.usage.requests == budget.logical_requests == 1
    assert budget.usage.tool_calls == budget.tool_calls == 0


def test_sdk_two_calls_share_usage_without_double_counting():
    budget = RunBudget(request_limit=2)
    deps = AgentDeps(budget=budget)
    for _ in range(2):
        assert execute(TestModel(custom_output_args={"value": 2}), deps).payload.value == 2
    assert budget.usage.requests == budget.logical_requests == 2
    with pytest.raises(PlanningError) as exc:
        execute(TestModel(custom_output_args={"value": 3}), deps)
    assert exc.value.code == "AGENT_LOOP_LIMIT"
    assert budget.logical_requests == 2


def test_output_validator_retry_uses_sdk_history_and_exact_request_debits():
    calls, debits = [], []
    def function(messages, info):
        calls.append(messages)
        return ModelResponse(parts=[TextPart('{"value": 1}')])
    def validator(ctx, output):
        if len(calls) == 1:
            raise ModelRetry("synthetic field constraint retry")
        return output
    budget = RunBudget(request_limit=2, debit=debits.append)
    result = execute(FunctionModel(function, profile=PROFILE), AgentDeps(budget=budget),
                     output_validator=validator)
    assert result.payload.value == 1
    assert budget.logical_requests == budget.usage.requests == result.requests == 2
    assert debits == ["logical", "logical"]
    assert "synthetic field constraint retry" in str(calls[1])


def test_per_run_request_cap_keeps_task_budget_available_for_next_run():
    calls = []
    def function(messages, info):
        calls.append(messages)
        return ModelResponse(parts=[TextPart('{"value": -1}')])
    budget = RunBudget(request_limit=3)
    deps = AgentDeps(budget=budget, run_request_limit=1)
    with pytest.raises(PlanningError) as exc:
        execute(FunctionModel(function, profile=PROFILE), deps)
    assert exc.value.code == "AGENT_LOOP_LIMIT"
    assert len(calls) == budget.logical_requests == budget.usage.requests == 1
    result = execute(FunctionModel(lambda messages, info:
        ModelResponse(parts=[TextPart('{"value": 8}')]), profile=PROFILE), deps)
    assert result.payload.value == 8 and budget.logical_requests == budget.usage.requests == 2


def test_native_then_tool_share_failed_attempt_budget():
    modes = []
    def function(messages, info):
        modes.append(info.model_request_parameters.output_mode)
        if len(modes) == 1:
            raise ModelCapabilityError("synthetic explicit unsupported protocol")
        return ModelResponse(parts=[ToolCallPart("submit_candidate", {"value": 9})])
    budget = RunBudget(request_limit=2)
    result = execute(FunctionModel(function, profile=PROFILE), AgentDeps(budget=budget))
    assert result.payload.value == 9
    assert result.mode == "tool" and result.degraded_from == ["native"]
    assert budget.logical_requests == 2
    assert budget.usage.requests == 1
    assert modes == ["native", "tool"]


def test_shared_request_limit_prevents_fallback_after_one_try():
    calls = []
    def function(messages, info):
        calls.append(info.model_request_parameters.output_mode)
        raise ModelCapabilityError("synthetic explicit unsupported protocol")
    budget = RunBudget(request_limit=1)
    with pytest.raises(PlanningError) as exc:
        execute(FunctionModel(function, profile=PROFILE), AgentDeps(budget=budget))
    assert exc.value.code == "AGENT_LOOP_LIMIT"
    assert calls == ["native"]


@pytest.mark.parametrize("code", ["MODEL_RATE_LIMITED", "MODEL_CREDENTIAL_INVALID",
    "MODEL_PROVIDER_ERROR", "MODEL_TIMEOUT", "MODEL_CONNECTION_ERROR"])
def test_operational_failures_do_not_select_other_output_modes(code):
    calls = []
    def function(messages, info):
        calls.append(info.model_request_parameters.output_mode)
        raise error(code)
    with pytest.raises(PlanningError) as exc:
        execute(FunctionModel(function, profile=PROFILE))
    assert exc.value.code == code
    assert calls == ["native"]


def test_output_validation_failure_neither_degrades_nor_poison_caches():
    calls = []
    def function(messages, info):
        calls.append(info.model_request_parameters.output_mode)
        return ModelResponse(parts=[TextPart('{"value": -1}')])
    model = FunctionModel(function, profile=PROFILE)
    cache_before = dict(legacy_runner._MODE_FAIL_CACHE)
    with pytest.raises(PlanningError) as exc:
        execute(model)
    assert exc.value.code == "OUTPUT_INVALID"
    assert calls == ["native"] * 3
    assert legacy_runner._MODE_FAIL_CACHE == cache_before
    good = FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart('{"value": 4}')]),
                         profile=PROFILE)
    assert execute(good).mode == "native"


def test_business_tool_and_complete_sdk_history_survive_native_loop():
    seen = []
    tool_calls = []
    history = [ModelRequest(parts=[UserPromptPart("synthetic previous question")]),
        ModelResponse(parts=[ToolCallPart("lookup", {"query": "old"}, "old-call")]),
        ModelRequest(parts=[ToolReturnPart("lookup", "synthetic previous result", "old-call")]),
        ModelResponse(parts=[TextPart("synthetic previous answer")])]
    def lookup(query: str) -> str:
        """Return a synthetic authorized context snippet."""
        tool_calls.append(query)
        return "synthetic new result"
    def function(messages, info):
        seen.append(messages)
        if len(seen) == 1:
            return ModelResponse(parts=[ToolCallPart("lookup", {"query": "new"}, "new-call")])
        return ModelResponse(parts=[TextPart('{"value": 7}')])
    deps = AgentDeps(budget=RunBudget(request_limit=2, tool_limit=1))
    result = asyncio.run(runner.execute_agent(FunctionModel(function, profile=PROFILE), Candidate,
        GenerationInput.from_messages([{"role": "user", "content": "next"}], history), deps,
        tools=[lookup]))
    assert result.payload.value == 7
    assert tool_calls == ["new"]
    assert seen[0][:4] == history
    assert any(isinstance(p, ToolReturnPart) and p.tool_call_id == "new-call"
               for m in seen[1] for p in m.parts)
    assert deps.budget.logical_requests == deps.budget.usage.requests == 2
    assert deps.budget.tool_calls == deps.budget.usage.tool_calls == 1
    assert "old-call" in result.messages_json and "new-call" in result.messages_json


def test_tool_limit_fails_before_business_side_effect():
    called = []
    def lookup(query: str) -> str:
        """Record synthetic business invocation."""
        called.append(query)
        return "synthetic"
    model = FunctionModel(lambda messages, info: ModelResponse(parts=[
        ToolCallPart("lookup", {"query": "x"})]), profile=PROFILE)
    with pytest.raises(PlanningError) as exc:
        execute(model, AgentDeps(budget=RunBudget(tool_limit=0)), tools=[lookup])
    assert exc.value.code == "AGENT_LOOP_LIMIT" and called == []


def test_stream_preview_is_not_result_and_final_is_validated():
    previews = []
    finished = []
    async def function(messages, info):
        yield '{"value":'
        assert finished == []
        yield '5}'
    def validator(ctx, output):
        finished.append(output.value)
        return output
    result = execute(FunctionModel(stream_function=function, profile=PROFILE),
        on_delta=lambda text, **kwargs: previews.append((text, kwargs)), output_validator=validator)
    assert result.payload.value == 5 and result.mode == "prompted"
    assert finished == [5]
    assert previews[0] == ("", {"reset": True})
    assert any(text for text, kwargs in previews)


def test_invalid_stream_resets_previews_and_never_returns_success():
    previews = []
    calls = []
    async def function(messages, info):
        calls.append(info.model_request_parameters.output_mode)
        yield '{"value": -1}'
    with pytest.raises(PlanningError) as exc:
        execute(FunctionModel(stream_function=function, profile=PROFILE),
            on_delta=lambda text, **kwargs: previews.append((text, kwargs)))
    assert exc.value.code == "OUTPUT_INVALID"
    assert calls == ["prompted"] * 3
    assert len([x for x in previews if x[1].get("reset")]) == 3


def test_cancel_after_preview_fences_sdk_final_result():
    cancelled = []
    def check():
        if cancelled:
            raise LeaseError("TASK_CANCEL_REQUESTED", "synthetic cancellation")
    async def function(messages, info):
        yield '{"value": 5}'
    def delta(text, **kwargs):
        if text:
            cancelled.append(True)
    with pytest.raises(LeaseError) as exc:
        execute(FunctionModel(stream_function=function, profile=PROFILE),
                AgentDeps(check_state=check), on_delta=delta)
    assert exc.value.code == "TASK_CANCEL_REQUESTED"


def test_deadline_prevents_model_invocation():
    called = []
    model = FunctionModel(lambda messages, info: called.append(1) or
                          ModelResponse(parts=[TextPart('{"value": 1}')]), profile=PROFILE)
    deps = AgentDeps(budget=RunBudget(deadline=datetime.now(timezone.utc) - timedelta(seconds=1)))
    with pytest.raises(LeaseError) as exc:
        execute(model, deps)
    assert exc.value.code == "TASK_DEADLINE_EXCEEDED" and called == []


def test_wire_public_dns_pin_host_sni_authorized_actual_body():
    bodies = []
    sent = []
    scope = {"material_ids": ["synthetic-material"]}
    deps = AgentDeps(disclosure_scope=scope,
        authorize_payload=lambda body, destination, actual: bodies.append((body, destination, actual)))
    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"ok": True})
    body = b'{"messages":[{"role":"user","content":"synthetic"}]}'
    response = send(wire(handler, deps=deps), body=body)
    assert response.json() == {"ok": True}
    assert bodies == [(body, "https://model.invalid/v1/chat/completions", scope)] * 2
    assert len(sent) == deps.budget.wire_requests == 1
    assert sent[0].url.host == "8.8.8.8"
    assert sent[0].headers["host"] == "model.invalid"
    assert sent[0].extensions["sni_hostname"] == b"model.invalid"
    assert sent[0].content == body


@pytest.mark.parametrize("endpoint,port", [
    ("http://model.invalid/v1", 80), ("http://model.invalid:9999/v1", 9999),
    ("https://model.invalid/v1", 443)])
def test_wire_scheme_default_port_and_http_destination_fence(endpoint, port):
    resolved, sent = [], []
    def resolver(host, actual_port, *args):
        resolved.append((host, actual_port))
        return public_dns(host, actual_port, *args)
    transport = wire(lambda request: sent.append(request) or httpx.Response(200),
                     endpoint=endpoint, resolver=resolver)
    assert send(transport, url=endpoint + "/chat/completions").status_code == 200
    assert resolved == [("model.invalid", port)]
    assert sent[0].url.scheme == endpoint.split(":")[0]
    assert sent[0].url.port == (port if port not in (80, 443) else None)
    assert sent[0].headers["host"] == endpoint.split("/")[2]


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["10.0.0.1"], ["198.18.0.23"],
    ["192.0.2.1"], ["::1"], ["fc00::1"], ["8.8.8.8", "127.0.0.1"]])
def test_wire_allows_nonpublic_or_mixed_dns_and_keeps_host_sni(addresses):
    sent = []
    resolver = lambda host, port, *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port))
                                        for addr in addresses]
    assert send(wire(lambda request: sent.append(request) or httpx.Response(200),
                     resolver=resolver)).status_code == 200
    assert len(sent) == 1
    assert sent[0].url.host == addresses[0]
    assert sent[0].headers["host"] == "model.invalid"
    assert sent[0].extensions["sni_hostname"] == b"model.invalid"


@pytest.mark.parametrize("addresses", [[], ["169.254.169.254"],
    ["8.8.8.8", "169.254.169.254"], ["::ffff:169.254.169.254"]])
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_wire_rejects_empty_or_metadata_dns_before_connect(addresses, scheme):
    sent = []
    resolver = lambda host, port, *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port))
                                        for addr in addresses]
    with pytest.raises(PlanningError) as exc:
        send(wire(lambda request: sent.append(request) or httpx.Response(200), resolver=resolver,
                  endpoint=scheme + "://model.invalid/v1"),
             url=scheme + "://model.invalid/v1/chat/completions")
    assert exc.value.code == "MODEL_PROVIDER_ERROR" and sent == []


@pytest.mark.parametrize("endpoint", ["https://169.254.169.254/v1",
    "https://metadata.google.internal/v1", "https://[::ffff:169.254.169.254]/v1"])
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_wire_metadata_hostname_rejected_even_when_dns_is_public(endpoint, scheme):
    endpoint = endpoint.replace("https://", scheme + "://")
    sent = []
    with pytest.raises(PlanningError) as exc:
        send(wire(lambda request: sent.append(request) or httpx.Response(200), endpoint=endpoint),
             url=endpoint + "/chat/completions")
    assert exc.value.code == "MODEL_PROVIDER_ERROR" and not sent


@pytest.mark.parametrize("url", ["http://model.invalid/v1/chat/completions",
    "https://other.invalid/v1/chat/completions", "https://model.invalid:444/v1/chat/completions",
    "https://model.invalid/v10/chat/completions", "https://model.invalid/admin",
    "https://user:password@model.invalid/v1/chat/completions"])
def test_wire_destination_scope_rejects_cross_origin_and_path(url):
    sent = []
    with pytest.raises(PlanningError) as exc:
        send(wire(lambda request: sent.append(request) or httpx.Response(200)), url=url)
    assert exc.value.code == "MODEL_PROVIDER_ERROR" and sent == []


def test_wire_checks_lease_and_config_before_authorization_dns_or_send():
    activity = []
    def check():
        raise LeaseError("STALE_RESULT_DISCARDED", "synthetic config generation changed")
    deps = AgentDeps(check_state=check,
        authorize_payload=lambda *args: activity.append("authorize"))
    with pytest.raises(LeaseError):
        send(wire(lambda request: activity.append("send"), deps=deps,
            resolver=lambda *args: activity.append("dns")))
    assert activity == [] and deps.budget.wire_requests == 0


def test_wire_authorization_failure_precedes_dns_and_send():
    activity = []
    def authorize(*args):
        raise error("DISCLOSURE_REQUIRED")
    deps = AgentDeps(authorize_payload=authorize)
    with pytest.raises(PlanningError) as exc:
        send(wire(lambda request: activity.append("send"), deps=deps,
            resolver=lambda *args: activity.append("dns")))
    assert exc.value.code == "DISCLOSURE_REQUIRED"
    assert activity == [] and deps.budget.wire_requests == 0


def test_wire_request_byte_limit_precedes_authorization():
    authorized = []
    with pytest.raises(PlanningError) as exc:
        send(wire(lambda request: httpx.Response(200), max_bytes=2,
            deps=AgentDeps(authorize_payload=lambda *args: authorized.append(1))), body=b"123")
    assert exc.value.code == "QUOTA_EXCEEDED" and authorized == []


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, on_chunk=None):
        self.chunks, self.on_chunk, self.closed = chunks, on_chunk, False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.on_chunk:
                self.on_chunk()
            yield chunk

    async def aclose(self):
        self.closed = True


def test_wire_response_byte_limit_closes_stream():
    stream = Chunks([b"123", b"456"])
    with pytest.raises(PlanningError) as exc:
        send(wire(lambda request: httpx.Response(200, stream=stream), max_bytes=5))
    assert exc.value.code == "MODEL_RESPONSE_TOO_LARGE" and stream.closed


def test_wire_checks_state_between_response_chunks():
    state = []
    def check():
        if state:
            raise LeaseError("TASK_CANCEL_REQUESTED", "synthetic cancellation")
    stream = Chunks([b"first"], on_chunk=lambda: state.append(1))
    with pytest.raises(LeaseError):
        send(wire(lambda request: httpx.Response(200, stream=stream), deps=AgentDeps(check_state=check)))
    assert stream.closed


def test_wire_coalesces_tiny_packets_without_losing_bytes_or_skipping_batch_fences():
    checks = []
    chunks = [b"x"] * 10000
    stream = native._BoundedStream(Chunks(chunks), 10000,
        AgentDeps(check_state=lambda: checks.append(1)))

    async def receive():
        return [chunk async for chunk in stream]

    received = asyncio.run(receive())
    assert b"".join(received) == b"x" * 10000
    assert all(0 < len(chunk) <= 4096 for chunk in received)
    assert len(received) == len(checks) < len(chunks) // 10


def test_wire_batch_flush_does_not_wait_for_the_next_packet_and_cancels_pending_read():
    closed = []

    class DelayedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                yield b"first"
                await asyncio.sleep(10)
            finally:
                closed.append(True)

    async def receive():
        iterator = native._BoundedStream(DelayedBody(), 100, AgentDeps()).__aiter__()
        try:
            assert await asyncio.wait_for(anext(iterator), timeout=.5) == b"first"
        finally:
            await iterator.aclose()

    asyncio.run(receive())
    assert closed == [True]


def test_wire_revocation_after_first_batch_blocks_all_buffered_remaining_bytes():
    changed, delivered = [], []

    def check():
        if changed:
            raise LeaseError("TASK_CANCEL_REQUESTED", "synthetic cancellation")

    async def receive():
        stream = native._BoundedStream(Chunks([b"x" * 10000]), 10000,
            AgentDeps(check_state=check))
        async for chunk in stream:
            delivered.append(chunk)
            changed.append(True)

    with pytest.raises(LeaseError):
        asyncio.run(receive())
    assert delivered == [b"x" * 4096]


@pytest.mark.parametrize("status,code", [(301, "MODEL_PROVIDER_ERROR"),
    (307, "MODEL_PROVIDER_ERROR"), (401, "MODEL_CREDENTIAL_INVALID"),
    (403, "MODEL_CREDENTIAL_INVALID"), (429, "MODEL_RATE_LIMITED"),
    (500, "MODEL_PROVIDER_ERROR"), (503, "MODEL_PROVIDER_ERROR")])
def test_wire_no_redirects_retries_or_raw_provider_messages(status, code):
    sent = []
    def handler(request):
        sent.append(request)
        return httpx.Response(status, headers={"location": "https://other.invalid/v1/next"},
            content=b"synthetic-private-provider-error")
    deps = AgentDeps()
    with pytest.raises(PlanningError) as exc:
        send(wire(handler, deps=deps))
    assert exc.value.code == code
    assert "synthetic-private-provider-error" not in str(exc.value)
    assert len(sent) == deps.budget.wire_requests == 1


@pytest.mark.parametrize("status", [400, 404, 422])
@pytest.mark.parametrize("provider_code,expected", [
    ("unsupported_response_format", ModelCapabilityError),
    ("unsupported_tool_choice", ModelCapabilityError),
    ("invalid_request", PlanningError), ("unknown_model", PlanningError)])
def test_wire_only_explicit_protocol_code_is_capability_failure(status, provider_code, expected):
    with pytest.raises(expected) as exc:
        send(wire(lambda request: httpx.Response(status,
            json={"error": {"code": provider_code, "message": "synthetic-private-error"}})))
    assert "synthetic-private-error" not in str(exc.value)
    if expected is PlanningError:
        assert exc.value.code == "MODEL_PROVIDER_ERROR"


def test_wire_state_changed_during_dns_fails_before_actual_outbound():
    changed, sent = [], []
    def check():
        if changed:
            raise LeaseError("STALE_RESULT_DISCARDED", "synthetic config revoked during DNS")
    def resolver(host, port, *args):
        changed.append(True)
        return public_dns(host, port)
    deps = AgentDeps(check_state=check)
    with pytest.raises(LeaseError):
        send(wire(lambda request: sent.append(request) or httpx.Response(200),
                  deps=deps, resolver=resolver))
    assert sent == []


@pytest.mark.parametrize("exception,code", [(httpx.ReadTimeout("synthetic timeout"), "MODEL_TIMEOUT"),
    (httpx.ConnectError("synthetic connection error"), "MODEL_CONNECTION_ERROR"),
    (socket.gaierror("synthetic DNS error"), "MODEL_CONNECTION_ERROR")])
def test_wire_operational_errors_are_controlled(exception, code):
    def fail(request):
        raise exception
    with pytest.raises(PlanningError) as exc:
        send(wire(fail))
    assert exc.value.code == code


@pytest.mark.parametrize("failure,code", [
    (httpx.ReadTimeout, "MODEL_TIMEOUT"),
    (httpx.ReadError, "MODEL_CONNECTION_ERROR"),
])
def test_wire_response_body_failures_remain_retryable(failure, code):
    class InterruptedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b" "
            raise failure("synthetic-private-response-failure")
    with pytest.raises(PlanningError) as exc:
        send(wire(lambda request: httpx.Response(200, stream=InterruptedBody())))
    assert exc.value.code == code
    assert "synthetic-private" not in str(exc.value)


@pytest.mark.parametrize("protocol,endpoint", [
    ("openai", "https://model.invalid/v1"),
    ("anthropic", "https://model.invalid/v1"),
    ("gemini", "https://model.invalid/v1beta"),
])
@pytest.mark.parametrize("streaming", [False, True])
def test_official_provider_body_timeout_is_controlled(protocol, endpoint, streaming, monkeypatch):
    if protocol == "gemini":
        import importlib
        importlib.import_module("google.genai.types")
    sent = []
    class InterruptedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b""
            raise httpx.ReadTimeout("synthetic-private-response-failure")
    real_transport = native.SafeModelTransport
    def factory(endpoint, deps, *, max_bytes):
        def handler(request):
            sent.append(request)
            return httpx.Response(200, stream=InterruptedBody())
        return real_transport(endpoint, deps, max_bytes=max_bytes,
            inner=httpx.MockTransport(handler), resolver=public_dns)
    monkeypatch.setattr(native, "SafeModelTransport", factory)
    source = AdapterBase(endpoint=endpoint, model_name="synthetic-model",
        credentials={"api_key": "synthetic-key"})
    source.protocol = protocol
    deps = AgentDeps()
    kwargs = {"on_delta": lambda *args, **kwargs: None} if streaming else {}
    with pytest.raises(PlanningError) as exc:
        execute(source, deps, **kwargs)
    assert exc.value.code == "MODEL_TIMEOUT"
    assert len(sent) == deps.budget.wire_requests == 1
    assert "synthetic-private" not in str(exc.value)
    assert "synthetic" not in str(exc.value)


@pytest.mark.parametrize("status", [400, 422])
def test_wire_unrelated_unsupported_parameter_is_not_output_capability(status):
    with pytest.raises(PlanningError) as exc:
        send(wire(lambda request: httpx.Response(status, json={"error": {
            "code": "unsupported_parameter", "param": "temperature",
            "message": "synthetic non-output setting unsupported"}})))
    assert exc.value.code == "MODEL_PROVIDER_ERROR"


@pytest.mark.parametrize("status", [400, 422])
def test_wire_output_parameter_explicitly_unsupported_is_capability(status):
    with pytest.raises(ModelCapabilityError):
        send(wire(lambda request: httpx.Response(status, json={"error": {
            "code": "unsupported_parameter", "param": "response_format",
            "message": "synthetic output setting unsupported"}})))


def test_wire_rechecks_dns_and_budget_for_each_actual_try():
    dns, sent = [], []
    def resolver(host, port, *args):
        dns.append(host)
        return public_dns(host, port)
    deps = AgentDeps(budget=RunBudget(wire_limit=1))
    transport = wire(lambda request: sent.append(request) or httpx.Response(200),
                     deps=deps, resolver=resolver)
    assert send(transport).status_code == 200
    with pytest.raises(PlanningError) as exc:
        send(transport)
    assert exc.value.code == "AGENT_LOOP_LIMIT"
    assert len(dns) == len(sent) == deps.budget.wire_requests == 1


@pytest.mark.parametrize("endpoint", ["ftp://model.invalid/v1", "https://user:pass@model.invalid/v1",
    "https://model.invalid/v1?q=synthetic", "https://model.invalid/v1#fragment"])
def test_native_provider_rejects_invalid_configuration(endpoint):
    source = AdapterBase(endpoint=endpoint, credentials={"api_key": "synthetic-key"})
    source.protocol = "openai"
    async def run():
        async with native_model_session(source, AgentDeps()):
            pytest.fail("invalid configuration reached model construction")
    with pytest.raises(PlanningError) as exc:
        asyncio.run(run())
    assert exc.value.code == "MODEL_PROVIDER_ERROR"


def test_native_provider_requires_credentials_before_network():
    source = AdapterBase(endpoint="https://model.invalid/v1")
    source.protocol = "openai"
    async def run():
        async with native_model_session(source, AgentDeps()):
            pytest.fail("missing credential reached model construction")
    with pytest.raises(PlanningError) as exc:
        asyncio.run(run())
    assert exc.value.code == "MODEL_CREDENTIAL_INVALID"


@pytest.mark.parametrize("protocol,endpoint,path", [
    ("openai", "https://model.invalid/v1", "/v1/chat/completions"),
    ("anthropic", "https://model.invalid/v1", "/v1/messages"),
    ("gemini", "https://model.invalid/v1beta", "/v1beta/models/synthetic-model:generateContent")])
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_official_provider_endpoint_auth_and_no_sdk_retries(protocol, endpoint, path, monkeypatch, scheme):
    endpoint = endpoint.replace("https://", scheme + "://")
    if protocol == "gemini":
        # HttpOptions must capture the real client types before the temporary spy.
        import importlib
        importlib.import_module("google.genai.types")
    sent, authorized, clients = [], [], []
    real_transport = native.SafeModelTransport
    real_client = httpx.AsyncClient
    def transport_factory(endpoint, deps, *, max_bytes):
        def handler(request):
            sent.append(request)
            return httpx.Response(429, json={"error": {"code": "synthetic_rate_limit",
                "message": "synthetic-private-provider-body"}})
        return real_transport(endpoint, deps, max_bytes=max_bytes,
            inner=httpx.MockTransport(handler), resolver=public_dns)
    class CapturingClient(real_client):
        def __init__(self, *args, **kwargs):
            clients.append(kwargs)
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(native, "SafeModelTransport", transport_factory)
    monkeypatch.setattr(native.httpx, "AsyncClient", CapturingClient)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    source = AdapterBase(endpoint=endpoint, model_name="synthetic-model",
                         credentials={"api_key": "synthetic-key"})
    source.protocol = protocol
    deps = AgentDeps(authorize_payload=lambda body, destination, scope:
                     authorized.append((body, destination)))
    with pytest.raises(PlanningError) as exc:
        execute(source, deps)
    assert exc.value.code == "MODEL_RATE_LIMITED", (repr(exc.value.__cause__),
        [str(request.url) for request in sent])
    assert len(sent) == deps.budget.wire_requests == 1
    assert len(authorized) == 2 and authorized[0] == authorized[1]
    assert sent[0].url.path == path and sent[0].url.host == "8.8.8.8"
    assert authorized[0][0] == sent[0].content
    assert authorized[0][1] == scheme + "://model.invalid" + path
    assert json.loads(sent[0].content)
    assert clients[0]["trust_env"] is False and clients[0]["follow_redirects"] is False
    assert "synthetic-private-provider-body" not in str(exc.value)
    if protocol == "openai":
        assert sent[0].headers["authorization"] == "Bearer synthetic-key"
    elif protocol == "anthropic":
        assert sent[0].headers["x-api-key"] == "synthetic-key"
    else:
        assert sent[0].headers["x-goog-api-key"] == "synthetic-key"
