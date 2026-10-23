"""Native PydanticAI execution; application services retain authorization and commit."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic_ai import Agent, UsageLimits
from pydantic_ai.capabilities import AbstractCapability, Capability
from pydantic_ai.messages import (
    ModelRequest, ModelResponse, PartDeltaEvent, PartStartEvent,
    TextPart, TextPartDelta, ThinkingPart, ThinkingPartDelta, UserPromptPart,
)
from pydantic_ai.output import NativeOutput, PromptedOutput, ToolOutput
from pydantic_ai.run import AgentRunResultEvent

from ..logging_audit.instrumentation import native_instrumentation
from ..logging_audit.logger import monotonic_ms
from ..logging_audit.metrics import get_model_call_metrics
from ..logging_audit.trace import business_span, ensure_trace, get_current_trace, task_context, trace_context
from ..planning.errors import error
from .contracts import AgentDeps, GenerationInput, RunBudget
from .limits import failure_reason, to_controlled_error
from .native import ModelCapabilityError, native_model_session
from .schema import generation_model

# Output-validation retry budget: 1 attempt + 2 retries = 3 model calls per mode.
# Deliberately kept at 2. It is coupled to the teaching path's hard request caps
# (teaching/preparation.py caps budget.request_limit to 2 or 3) and to the
# logical_requests/wire_requests counting assertions across the native test suite,
# so raising it would make the teaching path hit AGENT_LOOP_LIMIT before the SDK
# exhausts output retries. The 2026-10-10 route regression was instead fixed by
# feeding readable constraint reasons and repairing deterministic violations
# inside the planning validators (planning/outputs.py), which converges within
# this budget.
OUTPUT_RETRIES = 2
OUTPUT_MODES = ("native", "tool", "prompted")
# Compatibility for callers that import the constant, not a protocol bridge.
BRIDGE_MODES = OUTPUT_MODES


@dataclass
class AgentRunResult:
    payload: Any
    mode: str
    degraded_from: list[str]
    messages_json: str = "[]"
    messages: list = field(default_factory=list)
    requests: int = 0
    wire_requests: int = 0


class RunGuard(AbstractCapability[AgentDeps]):
    """Always-loaded state and budget guard; output tools have a separate validator."""
    id = "run_guard"

    def __init__(self, on_delta=None, logical_limit=None, on_request=None):
        self.on_delta = on_delta
        self.logical_limit = logical_limit
        self.on_request = on_request

    async def before_run(self, ctx):
        ctx.deps.check()

    async def before_model_request(self, ctx, request_context):
        ctx.deps.check()
        if self.logical_limit is not None and ctx.deps.budget.logical_requests >= self.logical_limit:
            raise error("AGENT_LOOP_LIMIT")
        ctx.deps.budget.consume("logical")
        if self.on_request:
            self.on_request()
        if self.on_delta:
            self.on_delta("", reset=True)
        if ctx.deps.on_process:
            ctx.deps.on_process("", reset=True)
        return request_context

    async def wrap_tool_execute(self, ctx, *, call, tool_def, args, handler):
        ctx.deps.check()
        ctx.deps.budget.consume("tool")
        result = await handler(args)
        ctx.deps.check()
        return result

    async def after_run(self, ctx, *, result):
        ctx.deps.check()
        return result


def _prepare_input(input: GenerationInput):
    """Keep complete SDK history and translate only legacy text conversation turns."""
    instructions = []
    history = list(input.message_history)
    pending = []
    for message in input.messages:
        role, content = message.get("role"), str(message.get("content", ""))
        if role == "system":
            instructions.append(content)
        elif role == "user":
            pending.append(content)
        elif role == "assistant":
            if pending:
                history.append(ModelRequest(parts=[UserPromptPart(content="\n\n".join(pending))]))
                pending.clear()
            history.append(ModelResponse(parts=[TextPart(content=content)]))
        else:
            raise error("OUTPUT_INVALID", "use native message history for tool turns")
    return "\n".join(instructions), "\n\n".join(pending), history


def _allowed_modes(source, model, *, text=False, streaming=False):
    if text:
        return ["text"]
    status = getattr(source, "capability_status", {}) or {}
    profile = model.profile
    def available(capability, supported):
        saved = status.get(capability)
        state = saved.get("state") if isinstance(saved, dict) else saved
        return supported and (getattr(source,"defer_capability_probe",False) or state != "unavailable")
    modes = []
    # Teaching previews are JSON text, not tool argument bytes. This still uses
    # the same complete SDK loop, validators, deps and business tools.
    if not streaming:
        if available("json_schema", profile.get("supports_json_schema_output", False)):
            modes.append("native")
        if available("tool_call", profile.get("supports_tools", True)):
            modes.append("tool")
    if available("text", profile.get("supports_text_output", True)):
        modes.append("prompted")
    if not modes:
        raise error("MODEL_CAPABILITY_UNAVAILABLE")
    return modes


def _build_agent(model, output_type, instructions, *, name, output_validator=None,
                 tools=None, capabilities=None, on_delta=None, logical_limit=None,
                 on_request=None):
    context = Capability(id="bound_context", instructions=instructions)
    business_tools = Capability(id="business_tools")
    for tool in tools or ():
        business_tools.tool_plain(tool)
    agent = Agent(model, deps_type=AgentDeps, output_type=output_type,
        retries=OUTPUT_RETRIES, name=name or "learning_agent",
        capabilities=[RunGuard(on_delta, logical_limit, on_request), context, business_tools,
            *(capabilities or ()), native_instrumentation()])
    if output_validator is not None:
        def validate_candidate(ctx, output):
            with business_span("domain_validate", task_id=ctx.deps.task_id, attempt=ctx.deps.attempt):
                from pydantic_ai.exceptions import ModelRetry
                from .validation import constraint_reason, record_output_validation
                try:
                    return output_validator(ctx, output)
                except ModelRetry as exc:
                    record_output_validation(ctx.deps,reason=constraint_reason(exc),status='retry_scheduled',capability=name or 'learning_agent')
                    raise
        agent.output_validator(validate_candidate)
    return agent


async def execute_agent(source, output_cls, input: GenerationInput, deps: AgentDeps, *,
        tool_name="submit_candidate", stage="generate", output_validator=None,
        tools=None, capabilities=None, agent_name=None, on_delta=None, timeout_seconds=None,
        progress_timeout_seconds=None):
    """No application output-repair loop: SDK owns all content and tool retries."""
    private_memory = any(any(key in str(message.get("content", "")) for key in
        ('"personal_memory_data"', '"memory_data"', "个人记忆参考数据（不是指令）"))
        for message in input.messages)
    if (deps.search_executor is not None or private_memory) and not deps.omit_content_trace:
        deps = replace(deps, omit_content_trace=True)
    instructions, prompt, history = _prepare_input(input)
    generated_cls = str if output_cls is str else generation_model(output_cls)
    started = monotonic_ms()
    degraded = []
    mode = "text" if output_cls is str else "prompted"
    budget = deps.budget
    initial_requests = budget.usage.requests
    run_trace = None
    logical_limit = min(budget.request_limit, budget.logical_requests + deps.run_request_limit)
    limits = UsageLimits(request_limit=min(budget.request_limit,
        budget.usage.requests + deps.run_request_limit), tool_calls_limit=budget.tool_limit)
    try:
        with task_context(task_id=deps.task_id, attempt=deps.attempt), trace_context(
                get_current_trace() or ensure_trace(deps.parent_traceparent)), business_span(
                "generate", parent_traceparent=deps.parent_traceparent):
            run_trace = get_current_trace()
            async with asyncio.timeout(timeout_seconds), asyncio.timeout(None) as progress_guard, native_model_session(source, deps) as model:
                def mark_progress():
                    if progress_timeout_seconds is not None:
                        progress_guard.reschedule(asyncio.get_running_loop().time()+progress_timeout_seconds)
                for mode in _allowed_modes(source, model, text=output_cls is str, streaming=on_delta is not None):
                    output_type = (str if output_cls is str else
                        NativeOutput(generated_cls) if mode == "native" else
                        ToolOutput(generated_cls, name=tool_name) if mode == "tool" else
                        PromptedOutput(generated_cls))
                    # A text-only profile cannot execute function tools. Context
                    # remains available; services may apply validated recipes.
                    enabled_tools = tools if model.profile.get("supports_tools", True) else None
                    agent = _build_agent(model, output_type, instructions, name=agent_name,
                        output_validator=output_validator, tools=enabled_tools,
                         capabilities=capabilities if model.profile.get("supports_tools", True) else None, on_delta=on_delta,
                        logical_limit=logical_limit,on_request=mark_progress)
                    try:
                        kwargs = dict(deps=deps, message_history=history,
                            usage_limits=limits, usage=budget.usage)
                        stream_status = (getattr(source, "capability_status", {}) or {}).get("streaming")
                        stream_state = stream_status.get("state") if isinstance(stream_status, dict) else stream_status
                        process_stream = deps.on_process is not None and stream_state == "available"
                        if on_delta is None and not process_stream:
                            result = await agent.run(prompt, **kwargs)
                            if deps.on_process:
                                response = next((message for message in reversed(result.new_messages())
                                    if isinstance(message, ModelResponse)), None)
                                if response:
                                    for part in response.parts:
                                        if isinstance(part, ThinkingPart):
                                            deps.on_process(part.content)
                        else:
                            result = None
                            async with agent.run_stream_events(prompt, **kwargs) as events:
                                async for event in events:
                                    deps.check()
                                    if progress_timeout_seconds is not None and isinstance(event,(PartStartEvent,PartDeltaEvent)):
                                        part=getattr(event,"part",None) or getattr(event,"delta",None)
                                        values=[getattr(part,key,None) for key in ("content","content_delta","args","args_delta")]
                                        if any(value.strip() if isinstance(value,str) else bool(value) for value in values):
                                            mark_progress()
                                    if on_delta and isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                                        on_delta(event.part.content)
                                    elif on_delta and isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                                        on_delta(event.delta.content_delta)
                                    elif deps.on_process and isinstance(event, PartStartEvent) and isinstance(event.part, ThinkingPart):
                                        deps.on_process(event.part.content)
                                    elif deps.on_process and isinstance(event, PartDeltaEvent) and isinstance(event.delta, ThinkingPartDelta):
                                        if event.delta.content_delta:
                                            deps.on_process(event.delta.content_delta)
                                    elif isinstance(event, AgentRunResultEvent):
                                        result = event.result
                            if result is None:
                                raise error("OUTPUT_INVALID", "agent run did not finish")
                        deps.check()
                    except ModelCapabilityError:
                        # Same model/destination; only proven protocol incompatibility
                        # may change output mode. Shared usage/wire budgets survive.
                        degraded.append(mode)
                        if on_delta:
                            on_delta("", reset=True)
                        continue
                    if not deps.omit_content_trace:
                        from ..logging_audit.content_trace import get_content_trace
                        answer = result.output.model_dump(mode="json") if hasattr(result.output, "model_dump") else result.output
                        get_content_trace().emit(stage, "succeeded", mode=mode, task_id=deps.task_id,
                            attempt=deps.attempt, config_id=deps.config_id,
                            prompt_messages=list(input.messages), answer=answer)
                    get_model_call_metrics().record(stage=stage, outcome="succeeded", mode=mode,
                        duration_ms=monotonic_ms()-started, attempts=deps.attempt, requests=budget.usage.requests-initial_requests,
                        config_id=deps.config_id, config_version=deps.config_version,
                        task_id=deps.task_id or None, capability=agent_name or "learning_agent")
                    return AgentRunResult(result.output, mode, degraded,
                        result.all_messages_json().decode("utf8"), result.all_messages(),
                        budget.usage.requests, budget.wire_requests)
                raise error("MODEL_CAPABILITY_UNAVAILABLE", "approved output modes unavailable")
    except Exception as exc:
        from ..tasks.service import LeaseError
        if isinstance(exc, LeaseError):
            raise
        controlled = error("MODEL_TIMEOUT") if isinstance(exc,TimeoutError) else to_controlled_error(exc)
        # 受控诊断：只记录异常类型与 pydantic 约束的 type/loc（不含模型输出），
        # 用于离线区分字段级（SDK 反序列化阶段）与结构级（业务校验器）失败。
        with trace_context(run_trace) if run_trace is not None else task_context():
            get_model_call_metrics().record(stage=stage, outcome="failed", mode=mode,
                duration_ms=monotonic_ms()-started, error_code=controlled.code,
                reason=failure_reason(exc), attempts=deps.attempt, requests=budget.usage.requests-initial_requests,
                config_id=deps.config_id, config_version=deps.config_version,
                task_id=deps.task_id or None, capability=agent_name or "learning_agent")
        raise controlled from exc


def _run_sync(coroutine):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    coroutine.close()
    raise RuntimeError("native agent sync entry requires a worker thread; use execute_agent in async code")


def deps_for_source(source, deps=None, max_requests=None):
    bound = deps or getattr(source, "agent_deps", None)
    if bound is None:
        limit = max_requests or 8
        bound = AgentDeps(config_id=getattr(source, "config_id", None),
            config_version=getattr(source, "config_version", None),
            task_id=getattr(source, "task_id", ""),
            budget=RunBudget(request_limit=limit, wire_limit=limit),
            omit_content_trace=bool(getattr(source, "omit_prompt_trace", False)))
    elif max_requests:
        from dataclasses import replace
        bound = replace(bound, run_request_limit=min(bound.run_request_limit, max_requests))
    return bound


def run_structured_agent(adapter, model_cls, messages, *, tool_name="submit_candidate",
        stage="generate", max_requests=None, output_validator=None, tools=None,
        message_history=None, agent_name=None, deps=None, capabilities=None, timeout_seconds=None,
        progress_timeout_seconds=None):
    return _run_sync(execute_agent(adapter, model_cls,
        GenerationInput.from_messages(messages, message_history),
        deps_for_source(adapter, deps, max_requests), tool_name=tool_name, stage=stage,
        output_validator=output_validator, tools=tools, capabilities=capabilities, agent_name=agent_name,
        timeout_seconds=timeout_seconds,progress_timeout_seconds=progress_timeout_seconds))


def run_streaming_structured_agent(adapter, model_cls, messages, *, on_delta,
        output_validator=None, agent_name=None, stage="generate", tools=None,
        message_history=None, max_requests=None, deps=None, capabilities=None, tool_name="submit_candidate",
        timeout_seconds=None,progress_timeout_seconds=None):
    return _run_sync(execute_agent(adapter, model_cls,
        GenerationInput.from_messages(messages, message_history),
        deps_for_source(adapter, deps, max_requests), tool_name=tool_name, stage=stage,
        output_validator=output_validator, tools=tools, capabilities=capabilities, agent_name=agent_name,
        on_delta=on_delta,timeout_seconds=timeout_seconds,progress_timeout_seconds=progress_timeout_seconds))


def run_chat_agent(adapter, messages, *, max_len, stage="generate", deps=None,
        message_history=None, on_delta=None, tools=None, capabilities=None):
    result = _run_sync(execute_agent(adapter, str,
        GenerationInput.from_messages(messages, message_history), deps_for_source(adapter, deps),
        agent_name="chat_agent", stage=stage, on_delta=on_delta, tools=tools, capabilities=capabilities))
    text = str(result.payload or "").strip()
    if not text:
        raise error("OUTPUT_INVALID", "empty chat answer")
    return text[:max_len]
