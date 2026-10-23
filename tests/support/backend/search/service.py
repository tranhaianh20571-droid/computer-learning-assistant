"""Personal AnySearch orchestration. SDK owns the model loop; this owns wire policy."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from .binding import bind_search
from .contracts import AnySearchRequest, Failed, SearchProviderError, Skipped
from .policy import DEFAULT_POLICY, decide_continuation, derive_query, normalize_envelope, requirement_for
from .contracts import DEFAULT_RESULTS
from time import monotonic
import random
from .repository import SearchRepository


class SearchService:
    def __init__(self, db, configs, *, owner_user_id, task_id, adapter_factory=None):
        self.db, self.configs = db, configs
        self.owner_user_id, self.task_id = owner_user_id, task_id
        self.adapter_factory = adapter_factory
        self.repository = SearchRepository(db)
        self.binding = None
        self.continuation = None
        self.outcome = None
        self._query = None
        self._view = None
        self._planning_snapshot = None
        self._execution_lock = asyncio.Lock()

    def for_query(self):
        """Keep the selected configuration while giving each query its own binding."""
        service = SearchService(self.db, self.configs, owner_user_id=self.owner_user_id,
            task_id=self.task_id, adapter_factory=self.adapter_factory)
        service._view = self._view
        service._planning_snapshot = self._planning_snapshot
        return service

    def attempt(self, topic, *, max_results=DEFAULT_RESULTS, disabled=False, offline=False, logical_slot=1):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.execute(topic, max_results=max_results, disabled=disabled,
                offline=offline, logical_slot=logical_slot))
        raise RuntimeError("synchronous search requires a worker thread; use execute")

    async def execute(self, topic, *, max_results=DEFAULT_RESULTS, disabled=False, offline=False, logical_slot=1):
        from ..planning.errors import PlanningError
        from ..tasks.service import LeaseError
        from ..logging_audit.trace import trace_context, get_current_trace, ensure_trace
        with trace_context(get_current_trace() or ensure_trace()):
            async with self._execution_lock:
                try:
                    return await self._execute(topic, max_results=max_results, disabled=disabled,
                        offline=offline, logical_slot=logical_slot)
                except (PlanningError, LeaseError) as exc:
                    self.repository.audit_rejection(self.owner_user_id, self.task_id, exc.code)
                    if self.binding:
                        query_id = getattr(self, "_active_query_id", None)
                        counts = self.repository.counts(self.binding, query_id)
                        self._log("search.execution.completed", status="failed", error_code=exc.code,
                            query_id=query_id, reason="interrupted", result_count=0,
                            requests=counts["shared_requests"], retry_count=counts["shared_retry_count"],
                            duration_ms=max(0, int((monotonic()-getattr(self, "_search_started", monotonic()))*1000)))
                    raise

    async def _execute(self, topic, *, max_results=DEFAULT_RESULTS, disabled=False, offline=False, logical_slot=1):
        if self.binding:
            self.binding.check_state()
        requirement = requirement_for(topic)
        if disabled or offline:
            return self._complete(Skipped(reason="disabled" if disabled else "offline_continue"), requirement)
        query = derive_query(topic)
        if query is None:
            return self._complete(Skipped(reason="query_unavailable"), requirement)
        if self._query is not None and query != self._query:
            from ..planning.errors import error
            raise error("TASK_CANCELLED", "search topic outside execution binding")
        self._query = query
        request = AnySearchRequest(query=query, max_results=max_results)
        # Scope/identity and config choice occur before credentials are decrypted.
        view = self._view or self.configs.active_search_for_owner(self.owner_user_id)
        if not view:
            return self._complete(Skipped(reason="no_config"), requirement)
        if self.binding is None:
            self.binding = bind_search(self.db, view, owner_user_id=self.owner_user_id,
                task_id=self.task_id, query=query, requirement=requirement,
                topic_check=self._topic_matches)
            self._view = view
        self._search_started = monotonic()
        self._active_query_id = None
        query_id, deadline, refusal = self.repository.reserve(self.binding, logical_slot=logical_slot)
        if refusal:
            counts = self.repository.counts(self.binding, None)
            reason = {"SEARCH_INTERRUPTED": "interrupted", "SEARCH_TIMEOUT": "deadline_exhausted"}.get(refusal, "request_exhausted")
            self._log("search.execution.completed", status="failed", error_code=refusal,
                reason=reason, duration_ms=max(0, int((monotonic()-self._search_started)*1000)),
                requests=counts["shared_requests"], retry_count=counts["shared_retry_count"], result_count=0)
            return self._complete(Failed(query_id=uuid.uuid4(), error_code=refusal, retryable=False), requirement)
        self._active_query_id = query_id
        query_uuid = uuid.UUID(query_id)
        self._terminal_reason = None
        # Freshness is a UI hint. Untested/expired configurations are validated by
        # this real request; known permanent failures remain an explicit block.
        status = self.configs.get(view["config_id"], actor_id=self.owner_user_id, is_admin=False).get("capability_status") or {}
        blocked = self._status_block(status, view["config_version"])
        if blocked:
            return self._finish(query_id, Failed(query_id=query_uuid, error_code=blocked, retryable=False), requirement)
        provider_block = self.repository.provider_block(self.binding)
        if provider_block:
            return self._finish(query_id, Failed(query_id=query_uuid,
                error_code=provider_block, retryable=False), requirement)
        from ..slice1.errors import Slice1Error
        try:
            credentials = self.configs.resolve_search_credentials(view["config_id"],
                owner_user_id=self.owner_user_id, expected_version=view["config_version"])
        except Slice1Error as exc:
            self.binding.check_state()
            if exc.code not in ("credential_invalid", "invalid_field"):
                raise
            return self._finish(query_id, Failed(query_id=query_uuid,
                error_code="SEARCH_CREDENTIAL_INVALID", retryable=False), requirement)
        if self.adapter_factory is None:
            from ..slice1.adapters.anysearch import AnySearchAdapter
            adapter = AnySearchAdapter(endpoint=view["endpoint"], credentials=credentials)
        else:
            adapter = self.adapter_factory(view, credentials)
        del credentials

        def remaining():
            return max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())

        def outbound(body, url):
            self.binding.check_payload(body, url, self.binding.scope_snapshot)

        retry_after = None
        try:
            for wire in range(DEFAULT_POLICY.retry_limit + 1):
                started = monotonic()
                try:
                    self.binding.check_state()
                    if remaining() <= 0:
                        raise SearchProviderError("SEARCH_TIMEOUT")
                    async with asyncio.timeout(min(DEFAULT_POLICY.hard_timeout_seconds, remaining())):
                        raw = await self._search_once(adapter, request, outbound, query_id, remaining)
                    self.binding.check_state()
                    outcome = normalize_envelope(raw, query_id=query_uuid)
                    self._request_log(query_id, started, outcome=outcome)
                    retry_after = None
                    self._terminal_reason = "success" if outcome.state in ("succeeded", "empty") else "permanent_error"
                    break
                except TimeoutError:
                    exc = SearchProviderError("SEARCH_TIMEOUT", retryable=True)
                except SearchProviderError as failure:
                    exc = failure
                self.binding.check_state()
                self._request_log(query_id, started, error_code=exc.code)
                retry_after = exc.retry_after
                outcome = Failed(query_id=query_uuid, error_code=exc.code,
                    retryable=exc.retryable, external_request_id=exc.external_request_id)
                if remaining() <= 0:
                    self._terminal_reason = "deadline_exhausted"
                    outcome = Failed(query_id=query_uuid, error_code="SEARCH_TIMEOUT", retryable=False)
                    break
                temporary = exc.code in ("SEARCH_CONNECTION_ERROR", "SEARCH_TIMEOUT",
                    "SEARCH_PROVIDER_UNAVAILABLE", "SEARCH_RATE_LIMITED")
                if not temporary or not (exc.retryable or exc.unsent):
                    self._terminal_reason = "permanent_error"
                    break
                try:
                    retry_no = self.repository.consume_retry(self.binding, query_id)
                except SearchProviderError as exhausted:
                    self._terminal_reason = "deadline_exhausted" if exhausted.code == "SEARCH_TIMEOUT" else "retry_exhausted"
                    if exhausted.code == "SEARCH_TIMEOUT":
                        outcome = Failed(query_id=query_uuid, error_code="SEARCH_TIMEOUT", retryable=False)
                    break
                delay = .5 * (2 ** (retry_no - 1)) + random.uniform(0, .1)
                if retry_after is not None:
                    delay = max(delay, (retry_after - datetime.now(timezone.utc)).total_seconds())
                if delay >= remaining():
                    self._terminal_reason = "deadline_exhausted"
                    outcome = Failed(query_id=query_uuid, error_code="SEARCH_TIMEOUT", retryable=False)
                    break
                await self._wait_for_retry(delay)
            self.binding.check_state()
            return self._finish(query_id, outcome, requirement, retry_after=retry_after)
        finally:
            close = getattr(adapter, "aclose", None)
            if close:
                await close()
            elif getattr(adapter, "close", None):
                adapter.close()

    async def _search_once(self, adapter, request, outbound, query_id, remaining):
        pending = asyncio.create_task(adapter.search(request, check_outbound=outbound,
            before_wire=lambda: self.repository.consume_wire(self.binding, query_id),
            remaining_seconds=remaining))
        try:
            while True:
                done, _ = await asyncio.wait((pending,), timeout=.25)
                self.binding.check_state()
                if done:
                    return await pending
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def _wait_for_retry(self, delay):
        while delay > 0:
            self.binding.check_state()
            step = min(delay, .25)
            await asyncio.sleep(step)
            delay -= step
        self.binding.check_state()

    def _log(self, event, **fields):
        from ..logging_audit.logger import StructuredLogger
        from ..logging_audit.exporter import HEALTH
        try:
            StructuredLogger("api").log(event, level="WARN" if fields.get("error_code") else "INFO",
                stage="search", task_id=self.task_id, attempt=self._task_attempt(),
                config_id=self.binding.config_id, config_version=self.binding.config_version,
                capability="anysearch", **fields)
        except Exception:
            HEALTH.add("measurement_failures")

    def _task_attempt(self):
        from ..tasks.service import LearningTaskRow
        with self.db.session() as sess:
            row = sess.get(LearningTaskRow, self.task_id)
            return row.attempt if row else None

    def _request_log(self, query_id, started, *, outcome=None, error_code=None):
        counts = self.repository.counts(self.binding, query_id)
        self._log("search.request.failed" if error_code or getattr(outcome, "error_code", None) else "search.request.completed",
            query_id=query_id, duration_ms=max(0, int((monotonic()-started)*1000)),
            status="failed" if error_code or getattr(outcome, "error_code", None) else "succeeded",
            error_code=error_code or getattr(outcome, "error_code", None),
            requests=counts["requests"], retry_count=counts["retry_count"],
            result_count=len(getattr(outcome, "results", ())))

    def _finish(self, query_id, outcome, requirement, *, retry_after=None):
        continuation = decide_continuation(outcome, requirement=requirement)
        self.repository.finish(self.binding, query_id, outcome, continuation,
            retry_after=retry_after)
        counts = self.repository.counts(self.binding, query_id)
        self._log("search.execution.completed", query_id=query_id,
            status="failed" if outcome.state == "failed" else "succeeded",
            duration_ms=max(0, int((monotonic()-self._search_started)*1000)),
            error_code=getattr(outcome, "error_code", None),
            reason=self._terminal_reason or ("permanent_error" if outcome.state == "failed" else "success"),
            requests=counts["shared_requests"], retry_count=counts["shared_retry_count"],
            result_count=len(getattr(outcome, "results", ())))
        return self._complete(outcome, requirement)

    def _complete(self, outcome, requirement):
        self.outcome = outcome
        self.continuation = decide_continuation(outcome, requirement=requirement)
        return outcome

    @staticmethod
    def _status_block(status, version):
        text = status.get("text", {}) if isinstance(status, dict) else {}
        if not isinstance(text, dict) or text.get("config_version") != version:
            return None
        code = text.get("error_code") or text.get("detail")
        if text.get("state") != "available" and code in ("SEARCH_CREDENTIAL_INVALID", "SEARCH_QUOTA_EXCEEDED"):
            return code
        return None

    def _topic_matches(self, sess, query):
        from ..tasks.service import LearningTaskRow
        from ..planning.models import PlanningTaskStateRow, LearningRouteRow, CourseRow, ConversationRow, MessageRow
        from sqlalchemy import select
        task = sess.get(LearningTaskRow, self.task_id)
        if task is None:
            return False
        if task.kind=="character_build_stage":
            from ..characters.distillation_models import CharacterBuildStageRow,CharacterBuildRow
            stage=sess.get(CharacterBuildStageRow,task.task_id)
            build=sess.get(CharacterBuildRow,stage.build_id) if stage else None
            if not build:return False
            data=__import__("json")
            expected=data.loads(stage.state_json or "{}").get("query") or data.loads(build.request_json)["topic"]
            return derive_query(expected)==query
        if task.kind == "chat_answer":
            conv = sess.get(ConversationRow, task.subject_id)
            if conv is None:
                return False
            message = sess.scalars(select(MessageRow).where(
                MessageRow.conversation_id == conv.conversation_id,
                MessageRow.generation == conv.generation, MessageRow.role == "user")
                .order_by(MessageRow.created_at.desc())).first()
            return message is not None and derive_query(message.content_ref) == query
        state = sess.get(PlanningTaskStateRow, self.task_id)
        if state is None:
            return False
        cls = CourseRow if state.target_kind == "course" else LearningRouteRow
        target = sess.get(cls, state.target_id)
        if target is None or target.owner_user_id != self.owner_user_id or target.deleted_at:
            return False
        topic = derive_query(target.title_ref)
        import json
        signals = json.loads(state.scope_signals_json or "{}")
        snapshot = signals.get("search_plan_v1")
        if snapshot is None:
            return topic == query
        if not isinstance(snapshot, dict):
            return False
        queries = snapshot.get("queries")
        if (task.kind not in ("route_generate", "outline_generate", "planning_revise")
                or snapshot.get("generation") != task.generation
                or snapshot.get("target_id") != state.target_id
                or snapshot.get("target_kind") != state.target_kind
                or snapshot.get("topic") != topic
                or not isinstance(queries, list) or not 1 <= len(queries) <= DEFAULT_POLICY.logical_limit
                or any(not isinstance(item, str) or derive_query(item) != item for item in queries)
                or query not in queries):
            return False
        if self._planning_snapshot is None:
            self._planning_snapshot = snapshot
        return self._planning_snapshot == snapshot


def configured_search_factory(configs):
    """No admin/environment/anonymous/provider fallback."""
    return lambda owner_user_id, task_id: SearchService(configs.db, configs,
        owner_user_id=owner_user_id, task_id=task_id)
