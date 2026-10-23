"""Planning facade over the personal-search service and explicit synthetic adapters."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .errors import PlanningError
from ..search.contracts import Failed, Skipped, SearchOutcome, SearchArgs, RawSearchEnvelope
from ..search.contracts import SearchProviderError
from ..search.policy import derive_query, normalize_envelope, decide_continuation, requirement_for


@dataclass
class SearchPlanOutcome:
    """覆盖驱动的多 query 结果；保留逐 query 状态，供进度和评审使用。"""

    state: str
    results: list = field(default_factory=list)
    error_code: str | None = None
    reason: str | None = None
    queries: list[dict] = field(default_factory=list)


class CompositeSearchBinding:
    """多个 query 共享同一任务围栏；任一配置失效都拒绝发布。"""

    def __init__(self, bindings=None):
        self.bindings = [binding for binding in (bindings or []) if binding is not None]

    def check_state(self):
        for binding in self.bindings:
            binding.check_state()

    def check_in_session(self, session):
        for binding in self.bindings:
            binding.check_in_session(session)


class SearchGate:
    def __init__(self, adapter=None):
        self.adapter = adapter
        self.binding = None
        self.continuation = None

    def attempt(self, query, *, max_results=10, disabled=False, offline=False, logical_slot=1):
        requirement = requirement_for(query)
        if disabled or offline:
            outcome = Skipped(reason="disabled" if disabled else "offline_continue")
        elif self.adapter is None:
            outcome = Skipped(reason="no_config")
        elif hasattr(self.adapter, "attempt"):
            options = {"max_results": max_results}
            if logical_slot != 1:
                options["logical_slot"] = logical_slot
            outcome = self.adapter.attempt(query, **options)
            self.binding = self.adapter.binding
        else:
            # Only caller-injected, in-process synthetic adapters use this legacy test seam.
            topic = derive_query(query)
            if topic is None:
                outcome = Skipped(reason="query_unavailable")
            else:
                args = SearchArgs(query=topic, max_results=max_results)
                query_id = uuid.uuid4()
                try:
                    raw = self.adapter.search(args.query, max_results=args.max_results)
                except SearchProviderError as exc:
                    outcome = Failed(query_id=query_id, error_code=exc.code,
                        retryable=exc.retryable, external_request_id=exc.external_request_id)
                else:
                    if "data" not in raw:
                        results = raw.get("results", [])
                        raw = {"code": 0, "request_id": str(uuid.uuid4()), "data": {
                            "results": results, "metadata": {"total_results": len(results), "search_time_ms": 0}}}
                    try:
                        envelope = RawSearchEnvelope.model_validate_json(__import__("json").dumps(raw))
                        outcome = normalize_envelope(envelope, query_id=query_id)
                    except ValueError:
                        outcome = Failed(query_id=query_id, error_code="SEARCH_OUTPUT_INVALID", retryable=False)
        self.continuation = decide_continuation(outcome, requirement=requirement)
        return outcome

    def require_success_or_wait(self, outcome, *, decision=None):
        """Compatibility only: optional provider failures no longer create learner waits."""
        return outcome


__all__ = ["SearchGate", "SearchOutcome", "SearchPlanOutcome", "CompositeSearchBinding"]
