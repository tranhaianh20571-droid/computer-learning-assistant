"""任务表、幂等、generation/revision、worker 租约围栏（T03，架构 §3.2～§3.3）。

切片 2 T06：四类 lane（interactive / teaching_prefetch / ocr / maintenance）
调度隔离——interactive 保留容量只给交互任务，其他 lane 共用剩余容量；
全局/单 lane/单用户三层上限；队列等待计入创建时设定的总截止。
"""

from __future__ import annotations

import os
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from ..logging_audit.contracts import ERROR_CODES, normalize_error_code, validate_error_code, validate_stage, validate_status
from ..logging_audit.db import Database
from ..logging_audit.models import Base, TaskStateRow
from ..logging_audit.task_events import AccessDeniedError, TaskEvent
from ..logging_audit.repositories import PgTaskEventStore
from ..logging_audit.trace import business_span

LANES = ("interactive", "teaching_prefetch", "ocr", "maintenance")

ACTIVE_STATUSES = ("leased", "running")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class LaneCapacity:
    """四类 lane 的并发容量配置（可用环境变量覆盖，契约测试断言默认值）。"""

    global_limit: int = field(default_factory=lambda: _env_int("TASK_GLOBAL_LIMIT", 8))
    interactive_limit: int = field(default_factory=lambda: _env_int("TASK_INTERACTIVE_LIMIT", 4))
    lane_limits: dict = field(default_factory=lambda: {
        "interactive": _env_int("TASK_INTERACTIVE_LIMIT", 4),
        "teaching_prefetch": _env_int("TASK_PREFETCH_LIMIT", 2),
        "ocr": _env_int("TASK_OCR_LIMIT", 3),
        "maintenance": _env_int("TASK_MAINTENANCE_LIMIT", 1),
    })
    per_user_per_lane: int = field(default_factory=lambda: _env_int("TASK_PER_USER_PER_LANE", 2))

    @property
    def non_interactive_capacity(self) -> int:
        """非交互 lane 可共用的剩余容量（interactive 保留容量不被侵占）。"""
        return max(0, self.global_limit - self.interactive_limit)


LANE_CAPACITY = LaneCapacity()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LearningTaskRow(Base):
    __tablename__ = "learning_tasks"
    __table_args__ = (
        Index("idx_learning_tasks_owner", "owner_user_id"),
        Index("idx_learning_tasks_status", "status"),
        Index("idx_learning_tasks_lane_status", "status", "lane"),
        Index("uq_learning_tasks_idem", "owner_user_id", "idempotency_key", unique=True),
    )

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, default="generic")
    # 切片 2 调度四类：interactive | teaching_prefetch | ocr | maintenance
    lane: Mapped[str] = mapped_column(String(20), nullable=False, default="interactive")
    # OCR 任务指针（仅 lane=ocr 使用）
    material_id: Mapped[str | None] = mapped_column(String(64))
    page_no: Mapped[int | None] = mapped_column(Integer)
    subject_id: Mapped[str] = mapped_column(String(129), nullable=False)
    stage: Mapped[str] = mapped_column(String(40), nullable=False, default="upload")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="queued")
    # queued | leased | running | succeeded | failed | cancelled | expired
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_id: Mapped[str | None] = mapped_column(String(64))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prompt_binding_id: Mapped[str | None] = mapped_column(String(64))
    model_config_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class TaskAttemptRow(Base):
    __tablename__ = "task_attempts"
    __table_args__ = (Index("idx_task_attempts_task", "task_id"),)

    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("learning_tasks.task_id", ondelete="CASCADE"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_token: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_owner: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_status: Mapped[str | None] = mapped_column(String(40))


@dataclass
class Lease:
    task_id: str
    attempt_id: str
    lease_token: str
    lease_owner: str
    generation: int
    attempt: int
    lease_until: datetime
    deadline_at: Optional[datetime]
    owner_user_id: Optional[str] = None


class LeaseError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TaskService:
    def __init__(self, db: Database, events: PgTaskEventStore) -> None:
        self.db = db
        self.events = events

    def create_task(
        self,
        *,
        owner_user_id: str,
        kind: str,
        subject_id: str,
        idempotency_key: str,
        stage: str = "upload",
        prompt_binding_id: Optional[str] = None,
        model_config_version: Optional[str] = None,
        deadline_seconds: int = 3600,
        progress_total: int = 0,
        lane: str = "interactive",
        session: Optional[Session] = None,
    ) -> dict:
        validate_stage(stage)
        if lane not in LANES:
            raise LeaseError("INVALID_LANE", f"unknown lane {lane}")
        with business_span("queue"), (nullcontext(session) if session is not None else self.db.session()) as sess:
            existing = sess.execute(
                select(LearningTaskRow).where(
                    LearningTaskRow.owner_user_id == owner_user_id,
                    LearningTaskRow.idempotency_key == idempotency_key,
                )
            ).scalar_one_or_none()
            if existing is not None:
                # 幂等：返回同一有效任务
                return {"task_id": existing.task_id, "status": existing.status, "deduped": True}
            from ..cleanup.budget import current_budget, HEAVY_KINDS
            budget = getattr(self, "resource_budget", None) or current_budget()
            if budget and kind in HEAVY_KINDS:
                if sess.bind.dialect.name == "postgresql":
                    from sqlalchemy import text
                    sess.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 61021004})
                pending = sess.scalar(select(func.count()).select_from(LearningTaskRow).where(
                    LearningTaskRow.kind.in_(HEAVY_KINDS), LearningTaskRow.status == "queued"))
                if pending >= 20:
                    raise LeaseError("LANE_CAPACITY_FULL", "local heavy queue full")

            task_id = f"task_{uuid.uuid4().hex[:12]}"
            now = utcnow()
            sess.add(
                LearningTaskRow(
                    task_id=task_id,
                    owner_user_id=owner_user_id,
                    kind=kind,
                    subject_id=subject_id,
                    stage=stage,
                    status="queued",
                    idempotency_key=idempotency_key,
                    generation=1,
                    revision=1,
                    lane=lane,
                    deadline_at=now + timedelta(seconds=deadline_seconds),
                    prompt_binding_id=prompt_binding_id,
                    model_config_version=model_config_version,
                )
            )
            sess.flush()
            # Bind task-wide native request accounting in the same transaction.
            from ..agents.task_binding import AgentBudgetRow
            from ..logging_audit.trace import capture_task_traceparent
            sess.add(AgentBudgetRow(task_id=task_id, traceparent=capture_task_traceparent()))
            sess.flush()
            # 同事务写首个任务事件
            self.events.create_task(
                task_id,
                owner_user_id,
                subject_id,
                stage,
                progress_total=progress_total,
                session=sess,
            )
            if session is None:
                sess.commit()
            return {"task_id": task_id, "status": "queued", "deduped": False}

    @staticmethod
    def _failure_code(code, *, task_id=None, attempt=None) -> str:
        normalized = normalize_error_code(code)
        if normalized == "INTERNAL_ERROR" and code != "INTERNAL_ERROR":
            from ..logging_audit.metrics import record_task_diagnostic
            record_task_diagnostic(reason="unknown_error_code", error_code=normalized,
                task_id=task_id, attempt=attempt)
        return normalized

    def _terminal(self, sess, row, *, status, error_code, now, stage=None):
        """One locked transaction owns task, attempts, state and event."""
        row.status = status
        row.stage = stage or row.stage
        row.revision += 1
        row.updated_at = now
        row.lease_token = row.lease_owner = row.lease_until = None
        attempts = sess.scalars(select(TaskAttemptRow).where(
            TaskAttemptRow.task_id == row.task_id, TaskAttemptRow.finished_at.is_(None)))
        for attempt in attempts:
            attempt.finished_at = now
            attempt.result_status = status
        self.events.record(row.task_id, stage=row.stage,
            status="cancelled" if status == "cancelled" else "failed", error_code=error_code,
            generation=row.generation, attempt=row.attempt, session=sess)
        return {"task_id": row.task_id, "status": status, "error_code": error_code,
            "generation": row.generation, "revision": row.revision}

    def fail_queued(self, task_id: str, *, expected_generation: int, expected_revision: int,
            error_code: str) -> dict:
        """Fail only the observed queue revision; never take over a worker lease."""
        code = self._failure_code(error_code, task_id=task_id)
        with self.db.session() as sess:
            row = sess.scalar(select(LearningTaskRow).where(LearningTaskRow.task_id == task_id)
                .with_for_update().execution_options(populate_existing=True))
            if row is None:
                raise LeaseError("TASK_NOT_FOUND", "task not found")
            if (row.status != "queued" or row.generation != expected_generation
                    or row.revision != expected_revision):
                return {"accepted": False, "task_id": task_id, "reason": "stale_discarded"}
            now = utcnow()
            if row.cancel_requested:
                status, code = "cancelled", "TASK_CANCELLED"
            elif row.deadline_at is not None and self._as_utc(row.deadline_at) <= now:
                status, code = "expired", "TASK_EXPIRED"
            else:
                status = "failed"
            return {**self._terminal(sess, row, status=status, error_code=code, now=now),
                "accepted": True}

    def sweep_expired_tasks(self, *, now: Optional[datetime] = None, limit: int = 100,
            kinds: Optional[list[str]] = None, task_ids: Optional[list[str]] = None) -> list[dict]:
        """Bounded sweep; callers can scope maintenance to their own kinds/tasks."""
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        now = self._as_utc(now or utcnow())
        query = select(LearningTaskRow).where(
            LearningTaskRow.status.in_(("queued", *ACTIVE_STATUSES)),
            LearningTaskRow.deadline_at.is_not(None), LearningTaskRow.deadline_at <= now)
        if kinds is not None:
            query = query.where(LearningTaskRow.kind.in_(kinds))
        if task_ids is not None:
            query = query.where(LearningTaskRow.task_id.in_(task_ids))
        with self.db.session() as sess:
            rows = sess.scalars(query.order_by(LearningTaskRow.deadline_at, LearningTaskRow.task_id)
                .limit(limit).with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)).all()
            result = []
            for row in rows:
                cancelled = row.cancel_requested
                result.append(self._terminal(sess, row, status="cancelled" if cancelled else "expired",
                    error_code="TASK_CANCELLED" if cancelled else "TASK_EXPIRED", now=now))
            return result

    def recover_worker_failure(self, lease: Lease, *, stage: str = "generate",
            error_code: str = "INTERNAL_ERROR", max_attempts: int = 2) -> dict:
        """Retry only a terminal write, never model execution or business publication."""
        validate_stage(stage)
        if not 1 <= max_attempts <= 2:
            raise ValueError("max_attempts must be 1 or 2")
        code = self._failure_code(error_code, task_id=lease.task_id, attempt=lease.attempt)
        for recovery in range(max_attempts):
            try:
                with self.db.session() as sess:
                    row = sess.scalar(select(LearningTaskRow).where(
                        LearningTaskRow.task_id == lease.task_id).with_for_update()
                        .execution_options(populate_existing=True))
                    if row is None:
                        raise LeaseError("TASK_NOT_FOUND", "task not found")
                    matching = (row.generation == lease.generation and row.attempt == lease.attempt
                        and row.attempt_id == lease.attempt_id and row.lease_token == lease.lease_token
                        and row.lease_owner == lease.lease_owner and bool(lease.lease_token)
                        and bool(lease.attempt_id) and bool(lease.lease_owner)
                        and lease.owner_user_id == row.owner_user_id)
                    if not matching or row.status not in ACTIVE_STATUSES:
                        return {"accepted": False, "task_id": lease.task_id, "reason": "stale_discarded"}
                    now = utcnow()
                    if row.cancel_requested:
                        status, terminal_code = "cancelled", "TASK_CANCELLED"
                    elif row.deadline_at is not None and self._as_utc(row.deadline_at) <= now:
                        status, terminal_code = "expired", "TASK_EXPIRED"
                    else:
                        status, terminal_code = "failed", code
                    result = self._terminal(sess, row, status=status, error_code=terminal_code,
                        now=now, stage=stage)
                    sess.commit()
                    return {**result, "accepted": True, "recovery_attempts": recovery + 1}
            except (LeaseError, AccessDeniedError):
                raise
            except Exception:
                from ..logging_audit.metrics import record_task_diagnostic
                exhausted = recovery + 1 == max_attempts
                record_task_diagnostic(reason="worker_recovery_failed" if exhausted else "worker_recovery_retry",
                    error_code="WORKER_RECOVERY_FAILED" if exhausted else "INTERNAL_ERROR",
                    task_id=lease.task_id, attempt=lease.attempt)
                if exhausted:
                    raise LeaseError("WORKER_RECOVERY_FAILED", "terminal task write unavailable") from None
        raise LeaseError("WORKER_RECOVERY_EXHAUSTED", "terminal recovery exhausted")

    def claim(
        self,
        task_id: str,
        *,
        lease_owner: str,
        lease_seconds: int = 60,
        allow_reclaim_active: bool = True,
    ) -> Lease:
        """worker 行锁领取；生成不可复用 attempt_id + lease_token。"""
        with self.db.session() as sess:
            row = sess.execute(
                select(LearningTaskRow).where(LearningTaskRow.task_id == task_id).with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if row is None:
                raise LeaseError("TASK_NOT_FOUND", "task not found")
            now = utcnow()
            if row.owner_user_id.startswith("guest_"):
                self._terminal(sess, row, status="cancelled", error_code="TASK_CANCELLED", now=now)
                sess.commit()
                raise LeaseError("TASK_CANCELLED", "guest access retired")
            if row.status not in ("queued", "leased", "running"):
                raise LeaseError("TASK_NOT_CLAIMABLE", f"status={row.status}")
            if row.deadline_at and self._as_utc(row.deadline_at) <= now:
                self._terminal(sess, row, status="expired", error_code="TASK_EXPIRED", now=now)
                sess.commit()
                raise LeaseError("TASK_EXPIRED", "deadline exceeded")
            if not allow_reclaim_active and row.status in ("leased", "running") and row.lease_until and self._as_utc(row.lease_until) > now:
                raise LeaseError("TASK_NOT_CLAIMABLE", "task already has a live worker")
            if row.cancel_requested:
                self._terminal(sess, row, status="cancelled", error_code="TASK_CANCELLED", now=now)
                sess.commit()
                raise LeaseError("TASK_CANCELLED", "cancel requested")
            self._resource_capacity(sess, row)

            attempt = row.attempt + 1
            attempt_id = f"att_{uuid.uuid4().hex[:12]}"
            lease_token = uuid.uuid4().hex  # 不可复用
            lease_until = now + timedelta(seconds=lease_seconds)
            row.attempt = attempt
            row.attempt_id = attempt_id  # type: ignore[attr-defined]
            row.lease_owner = lease_owner
            row.lease_token = lease_token
            row.lease_until = lease_until
            row.claimed_at = now
            row.heartbeat_at = now
            row.status = "leased"
            row.generation = row.generation  # 保持
            sess.add(
                TaskAttemptRow(
                    attempt_id=attempt_id,
                    task_id=task_id,
                    attempt=attempt,
                    lease_token=lease_token,
                    lease_owner=lease_owner,
                    generation=row.generation,
                    lease_until=lease_until,
                )
            )
            sess.commit()
            return Lease(
                task_id=task_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                lease_owner=lease_owner,
                generation=row.generation,
                attempt=attempt,
                lease_until=lease_until,
                deadline_at=row.deadline_at,
                owner_user_id=row.owner_user_id,
            )

    def claim_next(
        self,
        lane: str,
        *,
        lease_owner: str,
        lease_seconds: int = 60,
        capacity: Optional[LaneCapacity] = None,
        task_ids: Optional[list[str]] = None,
    ) -> Lease:
        """按 lane 领取最旧排队任务（切片 2 T06 调度隔离）。

        - interactive 保留容量只分配给 interactive 任务；
        - 其他 lane 共用「全局 − interactive 保留」的剩余容量，且各自受
          lane 上限与单用户单 lane 上限约束；
        - 达到任务总截止（含队列等待）→ expired，不无限排队。
        """
        if lane not in LANES:
            raise LeaseError("INVALID_LANE", f"unknown lane {lane}")
        cfg = capacity or LANE_CAPACITY
        now = utcnow()
        self.sweep_expired_tasks(now=now)
        with self.db.session() as sess:
            counts = dict(
                sess.execute(
                    select(LearningTaskRow.lane, func.count())
                    .where(LearningTaskRow.status.in_(ACTIVE_STATUSES))
                    .group_by(LearningTaskRow.lane)
                ).all()
            )
            active_lane = int(counts.get(lane, 0))
            interactive_active = int(counts.get("interactive", 0))
            active_total = int(sum(counts.values()))
            if lane == "interactive":
                if active_lane >= cfg.lane_limits["interactive"]:
                    raise LeaseError("LANE_CAPACITY_FULL", "interactive capacity full")
            else:
                non_interactive_active = active_total - interactive_active
                if (
                    active_lane >= cfg.lane_limits.get(lane, 1)
                    or non_interactive_active >= cfg.non_interactive_capacity
                ):
                    raise LeaseError("LANE_CAPACITY_FULL", f"lane {lane} capacity full")

            candidates = (
                sess.execute(
                    select(LearningTaskRow)
                    .where(
                        LearningTaskRow.status == "queued",
                        LearningTaskRow.lane == lane,
                        LearningTaskRow.task_id.in_(task_ids) if task_ids is not None else True,
                    )
                    .order_by(LearningTaskRow.created_at)
                    .limit(16)
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            row = None
            for candidate in candidates:
                if candidate.deadline_at and self._as_utc(candidate.deadline_at) <= now:
                    self._terminal(sess, candidate, status="expired", error_code="TASK_EXPIRED", now=now)
                    sess.flush()
                    continue
                if candidate.cancel_requested:
                    self._terminal(sess, candidate, status="cancelled", error_code="TASK_CANCELLED", now=now)
                    sess.flush()
                    continue
                if cfg.per_user_per_lane > 0:
                    user_active = int(
                        sess.execute(
                            select(func.count())
                            .select_from(LearningTaskRow)
                            .where(
                                LearningTaskRow.owner_user_id == candidate.owner_user_id,
                                LearningTaskRow.lane == lane,
                                LearningTaskRow.status.in_(ACTIVE_STATUSES),
                            )
                        ).scalar_one()
                    )
                    if user_active >= cfg.per_user_per_lane:
                        continue
                row = candidate
                break
            if row is None:
                sess.commit()
                raise LeaseError("NO_QUEUED_TASK", f"no claimable task in lane {lane}")
            self._resource_capacity(sess, row)

            attempt = row.attempt + 1
            attempt_id = f"att_{uuid.uuid4().hex[:12]}"
            lease_token = uuid.uuid4().hex
            lease_until = now + timedelta(seconds=lease_seconds)
            row.attempt = attempt
            row.attempt_id = attempt_id
            row.lease_owner = lease_owner
            row.lease_token = lease_token
            row.lease_until = lease_until
            row.claimed_at = now
            row.heartbeat_at = now
            row.status = "leased"
            sess.add(
                TaskAttemptRow(
                    attempt_id=attempt_id,
                    task_id=row.task_id,
                    attempt=attempt,
                    lease_token=lease_token,
                    lease_owner=lease_owner,
                    generation=row.generation,
                    lease_until=lease_until,
                )
            )
            sess.commit()
            return Lease(
                task_id=row.task_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                lease_owner=lease_owner,
                generation=row.generation,
                attempt=attempt,
                lease_until=lease_until,
                deadline_at=row.deadline_at,
                owner_user_id=row.owner_user_id,
            )

    def _resource_capacity(self, session, row):
        from ..cleanup.budget import current_budget, HEAVY_KINDS
        budget = getattr(self, "resource_budget", None) or current_budget()
        if not budget or row.kind not in (*HEAVY_KINDS, "lesson_first_prepare", "lesson_plan_refine"):
            return
        if session.bind.dialect.name == "postgresql":
            from sqlalchemy import text
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 61021004})
        if not budget.permits_heavy():
            raise LeaseError("LANE_CAPACITY_FULL", "local resource pressure")
        if row.kind in HEAVY_KINDS:
            active = session.scalar(select(func.count()).select_from(LearningTaskRow).where(
                LearningTaskRow.kind.in_(HEAVY_KINDS), LearningTaskRow.task_id != row.task_id,
                LearningTaskRow.status.in_(ACTIVE_STATUSES), LearningTaskRow.lease_until > utcnow()))
            if active >= budget.snapshot()["heavy_limit"]:
                raise LeaseError("LANE_CAPACITY_FULL", "local heavy capacity full")

    def validate_lease(
        self, lease: Lease, *, actor_id: Optional[str] = None,
        session: Optional[Session] = None,
    ) -> LearningTaskRow:
        """Read current database fences; supplied sessions retain the row lock.

        No lease field from a startup snapshot substitutes for current state.
        The returned row is a snapshot when this method owns the session.
        """
        with business_span("commit_fence", task_id=lease.task_id, attempt=lease.attempt):
            if session is None:
                with self.db.session() as sess:
                    return self._validate_lease(lease, actor_id=actor_id, session=sess)
            return self._validate_lease(lease, actor_id=actor_id, session=session)

    def _validate_lease(
        self, lease: Lease, *, actor_id: Optional[str], session: Session,
    ) -> LearningTaskRow:
        # Do not flush a cached task before refreshing its current fences.
        with session.no_autoflush:
            row = session.execute(
                select(LearningTaskRow).where(LearningTaskRow.task_id == lease.task_id)
                .with_for_update().execution_options(populate_existing=True)
            ).scalar_one_or_none()
        if row is None:
            raise LeaseError("TASK_NOT_FOUND", "task not found")
        now = utcnow()
        if row.owner_user_id.startswith("guest_"):
            raise LeaseError("TASK_CANCELLED", "guest access retired")
        if actor_id is not None and actor_id != row.owner_user_id:
            raise AccessDeniedError("not_owner")
        if lease.owner_user_id is not None and lease.owner_user_id != row.owner_user_id:
            raise LeaseError("LEASE_LOST", "task owner changed")
        if row.cancel_requested or row.status == "cancelled":
            raise LeaseError("TASK_CANCELLED", "cancel requested")
        if row.deadline_at is not None and self._as_utc(row.deadline_at) <= now:
            raise LeaseError("TASK_EXPIRED", "deadline exceeded")
        if (
            row.status not in ACTIVE_STATUSES
            or not lease.attempt_id or not lease.lease_token or not lease.lease_owner
            or row.attempt_id != lease.attempt_id
            or row.attempt != lease.attempt
            or row.generation != lease.generation
            or row.lease_owner != lease.lease_owner
            or row.lease_token != lease.lease_token
            or row.lease_until is None or self._as_utc(row.lease_until) <= now
        ):
            raise LeaseError("LEASE_LOST", "lease is no longer active")
        return row

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    def heartbeat(self, lease: Lease, lease_seconds: int = 60) -> datetime:
        if lease_seconds <= 0:
            raise LeaseError("LEASE_LOST", "heartbeat must extend a live lease")
        with self.db.session() as sess:
            row = self.validate_lease(lease, session=sess)
            now = utcnow()
            lease_until = now + timedelta(seconds=lease_seconds)
            if row.deadline_at is not None:
                lease_until = min(lease_until, self._as_utc(row.deadline_at))
            row.heartbeat_at = now
            row.lease_until = lease_until
            row.status = "running"
            attempt = sess.get(TaskAttemptRow, lease.attempt_id)
            if attempt is not None:
                attempt.lease_until = lease_until
            sess.commit()
            return lease_until

    def submit_result(
        self,
        lease: Lease,
        *,
        stage: str,
        status: str,
        error_code: Optional[str] = None,
        duration_ms: Optional[int] = None,
        progress_current: Optional[int] = None,
        diagnostics_only: bool = False,
        preserve_partial: bool = False,
        publish=None,
    ) -> dict:
        """条件更新提交结果：必须匹配 task_id + generation + attempt_id + lease_token。"""
        validate_stage(stage)
        validate_status(status)
        if error_code:
            if status == "failed":
                error_code = self._failure_code(error_code, task_id=lease.task_id, attempt=lease.attempt)
            validate_error_code(error_code)

        with (
            business_span("save", task_id=lease.task_id, attempt=lease.attempt),
            business_span("transaction", task_id=lease.task_id, attempt=lease.attempt),
            self.db.session() as sess,
        ):
            row = sess.execute(
                select(LearningTaskRow).where(LearningTaskRow.task_id == lease.task_id).with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise LeaseError("TASK_NOT_FOUND", "task not found")

            try:
                self.validate_lease(lease, session=sess)
                fence_ok = not diagnostics_only
            except LeaseError:
                fence_ok = False
            now = utcnow()
            if not fence_ok:
                self.events.record(
                    lease.task_id,
                    stage=stage,
                    status="stale_discarded",
                    error_code="STALE_RESULT_DISCARDED",
                    generation=lease.generation,
                    attempt=lease.attempt,
                    diagnostics_only=True,
                    session=sess,
                )
                sess.commit()
                return {"accepted": False, "reason": "stale_discarded", "task_id": lease.task_id}

            # 围栏通过：更新任务并写事件
            if publish is not None:
                with business_span("publish", task_id=lease.task_id, attempt=lease.attempt):
                    publish(sess)
            if status in ("succeeded", "partial", "failed"):
                row.status = status if status != "partial" or preserve_partial else "succeeded"
            row.revision += 1
            row.updated_at = now
            row.lease_token = None
            row.lease_owner = None
            row.lease_until = None

            row.stage = stage
            attempt = sess.get(TaskAttemptRow, lease.attempt_id)
            if attempt is not None:
                attempt.finished_at = now
                attempt.result_status = status
            self.events.record(
                lease.task_id,
                stage=stage,
                status=status,
                error_code=error_code,
                duration_ms=duration_ms,
                progress_current=progress_current,
                generation=lease.generation,
                attempt=lease.attempt,
                session=sess,
            )
            sess.commit()
            return {
                "accepted": True,
                "task_id": lease.task_id,
                "status": row.status,
                "revision": row.revision,
            }

    def get_task(self, task_id: str, *, actor_id: Optional[str] = None) -> dict:
        with self.db.session() as sess:
            row = sess.get(LearningTaskRow, task_id)
            if row is None:
                raise LeaseError("TASK_NOT_FOUND", "task not found")
            if actor_id is not None and actor_id != row.owner_user_id:
                raise AccessDeniedError("not_owner")
            state = sess.get(TaskStateRow, task_id)
            error_code = normalize_error_code(state.error_code) if state and state.error_code else None
            return {
                "task_id": row.task_id,
                "owner_user_id": row.owner_user_id,
                "kind": row.kind,
                "subject_id": row.subject_id,
                "stage": row.stage,
                "status": row.status,
                "generation": row.generation,
                "revision": row.revision,
                "attempt": row.attempt,
                "cancel_requested": row.cancel_requested,
                "error_code": error_code,
                "retryable": bool(error_code and ERROR_CODES[error_code].retryable),
                "prompt_binding_id": row.prompt_binding_id,
                "deadline_at": row.deadline_at.isoformat() if row.deadline_at else None,
            }

    def request_cancel(self, task_id: str, *, actor_id: str) -> dict:
        with self.db.session() as sess:
            row = sess.scalar(select(LearningTaskRow).where(LearningTaskRow.task_id == task_id)
                .with_for_update().execution_options(populate_existing=True))
            if row is None:
                raise LeaseError("TASK_NOT_FOUND", "task not found")
            if row.owner_user_id != actor_id:
                raise AccessDeniedError("not_owner")
            if row.status in ("queued", *ACTIVE_STATUSES):
                row.cancel_requested = True
                self._terminal(sess, row, status="cancelled", error_code="TASK_CANCELLED", now=utcnow())
            sess.commit()
            return {"task_id": task_id, "cancel_requested": row.cancel_requested, "status": row.status}

    def requeue_after_wait(self, task_id: str, *, actor_id: str, deadline_seconds: int = 3600) -> dict:
        """业务等待（awaiting_*）决策后重排队：generation+1、状态回 queued。

        围栏语义延续：旧 generation worker 的迟到结果仍会被拒绝。
        用户作答间隔不计入总截止：awaiting_* 是等人交互而非排队，恢复时刷新
        `deadline_at`，避免访谈停留超过原截止后作答必然 `TASK_EXPIRED`。
        """
        with self.db.session() as sess:
            row = sess.execute(
                select(LearningTaskRow).where(LearningTaskRow.task_id == task_id).with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise LeaseError("TASK_NOT_FOUND", "task not found")
            if row.owner_user_id != actor_id:
                raise AccessDeniedError("not_owner")
            if row.cancel_requested or row.status == "cancelled":
                raise LeaseError("TASK_CANCELLED", "cancel requested")
            row.generation += 1
            row.status = "queued"
            row.lease_token = None
            row.lease_owner = None
            row.lease_until = None
            row.deadline_at = utcnow() + timedelta(seconds=deadline_seconds)
            row.updated_at = utcnow()
            sess.commit()
            return {"task_id": task_id, "generation": row.generation, "status": row.status}

    def requeue_for_retry(
        self,
        task_id: str,
        *,
        actor_id: str,
        deadline_seconds: int = 3600,
    ) -> dict:
        """用户主动重试失败/过期任务：generation+1、状态回 queued、刷新总截止。

        架构 §3.2「重试只替换失败或过期尝试」：只对 failed/expired 生效，由用户
        显式触发，不自动无限重试。规划状态（访谈答案、resolved_scope 等）保留在
        同一 task_id 下，供重试继续；非可重试状态原样返回（由调用方按既有语义处理）。
        """
        with self.db.session() as sess:
            row = sess.execute(
                select(LearningTaskRow).where(LearningTaskRow.task_id == task_id).with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise LeaseError("TASK_NOT_FOUND", "task not found")
            if row.owner_user_id != actor_id:
                raise AccessDeniedError("not_owner")
            now = utcnow()
            if row.cancel_requested or row.status == "cancelled":
                raise LeaseError("TASK_CANCELLED", "cancel requested")
            deadline_passed = row.deadline_at is not None and self._as_utc(row.deadline_at) <= now
            if deadline_passed and row.status in ("queued", *ACTIVE_STATUSES):
                self._terminal(sess, row, status="expired", error_code="TASK_EXPIRED", now=now)
            elif row.status in ACTIVE_STATUSES and (
                    row.lease_until is None or self._as_utc(row.lease_until) <= now):
                self._terminal(sess, row, status="failed", error_code="TASK_LEASE_EXPIRED", now=now)
            retryable = row.status in ("failed", "expired")
            if not retryable:
                return {
                    "task_id": task_id,
                    "generation": row.generation,
                    "status": row.status,
                    "requeued": False,
                }
            row.generation += 1
            row.status = "queued"
            row.lease_token = None
            row.lease_owner = None
            row.lease_until = None
            row.deadline_at = now + timedelta(seconds=deadline_seconds)
            row.updated_at = now
            self.events.record(task_id, stage=row.stage, status="started",
                generation=row.generation, attempt=row.attempt, session=sess)
            row.revision = sess.get(TaskStateRow, task_id).revision
            sess.commit()
            return {
                "task_id": task_id,
                "generation": row.generation,
                "status": row.status,
                "requeued": True,
            }
