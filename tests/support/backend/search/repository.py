"""Durable counters and body-free search executions; no queries or query hashes."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..logging_audit.models import Base
from ..planning.errors import error
from ..tasks.service import LearningTaskRow, TaskService
from ..logging_audit.repositories import PgTaskEventStore
from .binding import utc


def now():
    return datetime.now(timezone.utc)


class SearchTaskBudgetRow(Base):
    __tablename__ = "search_task_budgets"
    __table_args__ = (CheckConstraint("logical_count >= 0 AND logical_count <= 3 AND wire_count >= 0 AND wire_count <= 6 AND retry_count >= 0 AND retry_count <= 5", name="ck_search_budget_counts"),)
    task_id: Mapped[str] = mapped_column(String(64), ForeignKey("learning_tasks.task_id", ondelete="CASCADE"), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    logical_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    wire_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    budget_policy_version: Mapped[str] = mapped_column(String(32), nullable=False, default="personal-v2")
    search_window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    search_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SearchCallRow(Base):
    __tablename__ = "search_calls"
    __table_args__ = (
        UniqueConstraint("task_id", "logical_slot", name="uq_search_call_slot"),
        CheckConstraint("logical_slot >= 1 AND logical_slot <= 3 AND wire_count >= 0 AND wire_count <= 6 AND retry_count >= 0 AND retry_count <= 5", name="ck_search_call_counts"),
        CheckConstraint("state IN ('reserved','sending','succeeded','empty','failed','skipped','interrupted')", name="ck_search_call_state"),
    )
    query_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), ForeignKey("learning_tasks.task_id", ondelete="CASCADE"), nullable=False, index=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(64), ForeignKey("task_attempts.attempt_id", ondelete="CASCADE"), nullable=False)
    config_id: Mapped[str] = mapped_column(String(64), ForeignKey("service_configs.config_id", ondelete="RESTRICT"), nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    logical_slot: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="reserved")
    wire_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    external_request_id: Mapped[str | None] = mapped_column(String(36))
    continuation_mode: Mapped[str | None] = mapped_column(String(24))
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SearchRepository:
    def __init__(self, db):
        self.db = db

    def provider_block(self, binding):
        from sqlalchemy import select, or_
        with self.db.session() as sess:
            binding.check_in_session(sess)
            call = sess.scalars(select(SearchCallRow).where(
                SearchCallRow.owner_user_id == binding.owner_user_id,
                SearchCallRow.config_id == binding.config_id,
                SearchCallRow.config_version == binding.config_version,
                or_(SearchCallRow.error_code.in_(["SEARCH_CREDENTIAL_INVALID", "SEARCH_QUOTA_EXCEEDED"]),
                    SearchCallRow.retry_after.is_not(None)))
                .order_by(SearchCallRow.created_at.desc()).limit(1)).first()
            if call is None:
                return None
            if call.error_code in ("SEARCH_CREDENTIAL_INVALID", "SEARCH_QUOTA_EXCEEDED"):
                from ..slice1.models import ServiceConfigRow
                config = sess.get(ServiceConfigRow, binding.config_id)
                text_status = config.status_dict().get("text", {}) if config else {}
                try:
                    checked = datetime.fromisoformat(text_status.get("checked_at", ""))
                except (TypeError, ValueError):
                    checked = None
                if (checked is not None and text_status.get("state") == "available"
                        and text_status.get("config_version") == binding.config_version
                        and utc(checked) > utc(call.finished_at or call.created_at)):
                    return None
                return call.error_code
            if call.retry_after and utc(call.retry_after) > now():
                return "SEARCH_RATE_LIMITED"
            return None

    def reserve(self, binding, *, logical_slot=1):
        """Task row lock serializes initial budget creation and all reservations."""
        from .policy import DEFAULT_POLICY
        with self.db.session() as sess:
            binding.check_in_session(sess)
            task = sess.get(LearningTaskRow, binding.task_id)
            budget = sess.get(SearchTaskBudgetRow, binding.task_id)
            if budget is None:
                started = now()
                budget = SearchTaskBudgetRow(task_id=binding.task_id,
                    owner_user_id=binding.owner_user_id, logical_count=0, wire_count=0, retry_count=0,
                    budget_policy_version="personal-v2", search_window_started_at=started,
                    search_deadline_at=min(started + timedelta(seconds=DEFAULT_POLICY.window_seconds), binding.deadline_at))
                sess.add(budget)
            existing = sess.query(SearchCallRow).filter_by(task_id=binding.task_id,
                logical_slot=logical_slot).one_or_none()
            if existing:
                # Any previous execution is a reference only. Never replay its body or wire.
                if existing.state in ("reserved", "sending"):
                    existing.state = "interrupted"
                    existing.error_code = "SEARCH_INTERRUPTED"
                    existing.finished_at = now()
                return None, utc(budget.search_deadline_at), "SEARCH_INTERRUPTED"
            if now() >= utc(budget.search_deadline_at):
                return None, utc(budget.search_deadline_at), "SEARCH_TIMEOUT"
            if budget.logical_count >= DEFAULT_POLICY.logical_limit or not 1 <= logical_slot <= DEFAULT_POLICY.logical_limit:
                return None, utc(budget.search_deadline_at), "SEARCH_BUDGET_EXCEEDED"
            budget.logical_count += 1
            query_id = str(uuid.uuid4())
            sess.add(SearchCallRow(query_id=query_id, task_id=task.task_id,
                owner_user_id=binding.owner_user_id, attempt_id=binding.attempt_id,
                config_id=binding.config_id, config_version=binding.config_version,
                logical_slot=logical_slot, state="reserved", wire_count=0, result_count=0))
            return query_id, utc(budget.search_deadline_at), None

    def consume_wire(self, binding, query_id):
        with self.db.session() as sess:
            binding.check_in_session(sess)
            budget = sess.get(SearchTaskBudgetRow, binding.task_id)
            call = sess.get(SearchCallRow, query_id)
            if (not budget or not call or call.attempt_id != binding.attempt_id
                    or call.state not in ("reserved", "sending")):
                raise error("TASK_CANCELLED", "search execution changed")
            if now() >= utc(budget.search_deadline_at):
                from .contracts import SearchProviderError
                raise SearchProviderError("SEARCH_TIMEOUT")
            from .policy import DEFAULT_POLICY
            if budget.wire_count >= DEFAULT_POLICY.wire_limit:
                from .contracts import SearchProviderError
                raise SearchProviderError("SEARCH_BUDGET_EXCEEDED")
            budget.wire_count += 1
            call.wire_count += 1
            call.state = "sending"
            self.audit(binding, "sending", session=sess)

    def consume_retry(self, binding, query_id):
        """Reserve a retry under the same task lock as wires; never refund on restart."""
        from .contracts import SearchProviderError
        from .policy import DEFAULT_POLICY
        with self.db.session() as sess:
            binding.check_in_session(sess)
            budget = sess.get(SearchTaskBudgetRow, binding.task_id)
            call = sess.get(SearchCallRow, query_id)
            if (not budget or not call or call.attempt_id != binding.attempt_id
                    or call.state not in ("reserved", "sending")):
                raise error("TASK_CANCELLED", "search execution changed")
            if now() >= utc(budget.search_deadline_at):
                raise SearchProviderError("SEARCH_TIMEOUT")
            if budget.retry_count >= DEFAULT_POLICY.retry_limit or budget.wire_count >= DEFAULT_POLICY.wire_limit:
                raise SearchProviderError("SEARCH_BUDGET_EXCEEDED")
            budget.retry_count += 1
            call.retry_count += 1
            self.audit(binding, "retry_scheduled", session=sess)
            return budget.retry_count

    def counts(self, binding, query_id):
        """Return metadata for diagnostics, without loading evidence or credentials."""
        with self.db.session() as sess:
            budget = sess.get(SearchTaskBudgetRow, binding.task_id)
            call = sess.get(SearchCallRow, query_id) if query_id else None
            return {"requests": call.wire_count if call else 0,
                    "retry_count": call.retry_count if call else 0,
                    "shared_requests": budget.wire_count if budget else 0,
                    "shared_retry_count": budget.retry_count if budget else 0}

    def audit_rejection(self, owner_user_id, task_id, code):
        from ..logging_audit.contracts import ERROR_CODES
        from ..logging_audit.repositories import PgAuditLog
        PgAuditLog(self.db).append("search.execution", actor_id="system:search",
            target_actor_id=owner_user_id, object_type="learning_task", object_id=task_id,
            result="rejected", reason=code if code in ERROR_CODES else "INTERNAL_ERROR", role="system")

    def audit(self, binding, result, *, reason=None, session=None):
        from ..logging_audit.repositories import PgAuditLog
        PgAuditLog(self.db).append("search.execution", actor_id="system:search",
            target_actor_id=binding.owner_user_id, object_type="learning_task",
            object_id=binding.task_id, result=result, reason=reason,
            role="system", session=session)

    def _record_validation(self, sess, binding, call):
        """Only real wires update UI freshness; an older success cannot lift a block."""
        import json
        from sqlalchemy import select
        from ..slice1.models import ServiceConfigRow
        from ..logging_audit.repositories import PgAuditLog
        if not call.wire_count:
            return
        success = call.state in ("succeeded", "empty")
        if not success and call.error_code not in ("SEARCH_CREDENTIAL_INVALID", "SEARCH_QUOTA_EXCEEDED"):
            return
        config = sess.get(ServiceConfigRow, binding.config_id, with_for_update=True)
        if not config or config.config_version != binding.config_version:
            return
        status = config.status_dict()
        current = status.get("text", {})
        if success:
            try:
                checked = datetime.fromisoformat(current.get("checked_at", ""))
            except (AttributeError, TypeError, ValueError):
                checked = None
            newer_failure = sess.scalars(select(SearchCallRow).where(
                SearchCallRow.config_id == binding.config_id,
                SearchCallRow.config_version == binding.config_version,
                SearchCallRow.query_id != call.query_id,
                SearchCallRow.error_code.in_(["SEARCH_CREDENTIAL_INVALID", "SEARCH_QUOTA_EXCEEDED"]),
                SearchCallRow.finished_at >= call.created_at).limit(1)).first()
            if newer_failure or (checked and utc(checked) > utc(call.created_at)):
                return
        status["text"] = {"state": "available" if success else "unavailable",
            "detail": "ok" if success else call.error_code,
            "config_version": binding.config_version,
            "checked_at": utc(call.finished_at).isoformat(),
            "expires_at": (utc(call.finished_at) + timedelta(hours=6)).isoformat()}
        config.capability_status = json.dumps(status, separators=(",", ":"))
        PgAuditLog(self.db).append("config.changed", actor_id="system:search",
            target_actor_id=binding.owner_user_id, object_type="search_config",
            object_id=binding.config_id, result="succeeded",
            reason="runtime_validated" if success else "runtime_blocked", role="system", session=sess)

    def finish(self, binding, query_id, outcome, continuation, *, retry_after=None):
        with self.db.session() as sess:
            binding.check_in_session(sess)
            call = sess.get(SearchCallRow, query_id)
            if not call or call.attempt_id != binding.attempt_id:
                raise error("TASK_CANCELLED", "search execution changed")
            call.state = outcome.state
            call.error_code = getattr(outcome, "error_code", None)
            external = getattr(outcome, "external_request_id", None)
            call.external_request_id = str(external) if external else None
            call.result_count = len(getattr(outcome, "results", ()))
            call.continuation_mode = continuation.mode
            call.retry_after = retry_after
            call.finished_at = now()
            self._record_validation(sess, binding, call)
            self.audit(binding, outcome.state, reason=call.error_code, session=sess)
            PgTaskEventStore(self.db).record(binding.task_id, stage="search",
                status="failed" if outcome.state == "failed" else "succeeded",
                error_code=call.error_code, progress_current=call.result_count,
                generation=binding.execution_generation, diagnostics_only=True,
                session=sess)
