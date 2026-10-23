"""Opt-in Pydantic AI 2 local WebSearch; pre-search remains the default."""
from __future__ import annotations

from typing import Annotated

from pydantic import Field, StrictInt, StrictStr
from pydantic_ai import RunContext, Tool
from pydantic_ai.capabilities import WebSearch

from ..agents.contracts import AgentDeps
from ..planning.errors import error
from .contracts import SearchArgs
from .policy import evidence_payload


async def anysearch_tool(ctx: RunContext[AgentDeps], query: StrictStr,
                         max_results: Annotated[StrictInt, Field(ge=1, le=10)] = 10) -> dict:
    """Search the bound learning topic using the task owner's configured AnySearch key."""
    ctx.deps.check()
    args = SearchArgs(query=query, max_results=max_results)
    if ctx.deps.search_executor is None:
        raise error("TASK_CANCELLED", "search executor unavailable")
    # Executor owns topic equality, task/config checks, persistent slots and wire budget.
    # Security/cancel exceptions deliberately propagate instead of becoming ModelRetry.
    outcome, continuation = await ctx.deps.search_executor(args)
    ctx.deps.check()
    return {"state": outcome.state, "continuation": continuation.model_dump(mode="json"),
        "external_results": evidence_payload(outcome)}


def anysearch_capability():
    return WebSearch(native=False, local=Tool(anysearch_tool, name="search_web", max_retries=1))


def bind_agent_search(service, topic):
    """One run-local executor. A model may not expand the approved minimal topic."""
    from .policy import derive_query
    query = derive_query(topic)
    next_slot = 2  # pre-search occupies slot 1; retries never allocate a new slot

    async def execute(args):
        nonlocal next_slot
        if query is None or args.query != query or args.max_results > 10:
            raise error("TASK_CANCELLED", "search topic outside server binding")
        if service.binding:
            service.binding.check_state()
        cached = service.outcome
        if cached is not None:
            from .contracts import Succeeded
            if isinstance(cached, Succeeded):
                cached = cached.model_copy(update={"results": cached.results[:args.max_results]})
            return cached, service.continuation
        slot = next_slot
        next_slot += 1
        outcome = await service.execute(topic, max_results=args.max_results, logical_slot=slot)
        return outcome, service.continuation

    return execute
