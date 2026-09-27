"""任务表、幂等、generation/revision、worker 租约围栏（T03，架构 §3.2～§3.3）。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, select, update
from sqlalchemy.orm import Mapped, mapped_column

from ..logging_audit.contracts import validate_error_code, validate_stage, validate_status
from ..logging_audit.db import Database
from ..logging_audit.models import Base
from ..logging_audit.task_events import AccessDeniedError, TaskEvent
from ..logging_audit.repositories import PgTaskEventStore


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LearningTaskRow(Base):
    __tablename__ = "learning_tasks"
    __table_args__ = (
        Index("idx_learning_tasks_owner", "owner_user_id"),
        Index("idx_learning_tasks_status", "status"),
        Index("uq_learning_tasks_idem", "owner_user_id", "idempotency_key", unique=True),
    )

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, default="generic")
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
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
    ) -> dict:
        validate_stage(stage)
        with self.db.session() as sess:
            existing = sess.execute(
                select(LearningTaskRow).where(
                    LearningTaskRow.owner_user_id == owner_user_id,
                    LearningTaskRow.idempotency_key == idempotency_key,
                )
            ).scalar_one_or_none()
            if existing is not None:
                # 幂等：返回同一有效任务
                return {"task_id": existing.task_id, "status": existing.status, "deduped": True}

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
                    deadline_at=now + timedelta(seconds=deadline_seconds),
                    prompt_binding_id=prompt_binding_id,
                    model_config_version=model_config_version,
                )
            )
            sess.flush()
            # 同事务写首个任务事件
            self.events.create_task(
                task_id,
                owner_user_id,
                subject_id,
                stage,
                progress_total=progress_total,
            )
            sess.commit()
            return {"task_id": task_id, "status": "queued", "deduped": False}

    def claim(
        self,
        task_id: str,
        *,
        lease_owner: str,
        lease_seconds: int = 60,
    ) -> Lease:
        """worker 行锁领取；生成不可复用 attempt_id + lease_token。"""
        with self.db.session() as sess:
            row = sess.execute(
                select(LearningTaskRow).where(LearningTaskRow.task_id == task_id).with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if row is None:
                raise LeaseError("TASK_NOT_FOUND", "task not found")
            now = utcnow()
            if row.deadline_at and row.deadline_at < now:
                row.status = "expired"
                sess.commit()
                raise LeaseError("TASK_EXPIRED", "deadline exceeded")
            if row.status not in ("queued", "leased", "running"):
                raise LeaseError("TASK_NOT_CLAIMABLE", f"status={row.status}")
            if row.cancel_requested:
                row.status = "cancelled"
                sess.commit()
                raise LeaseError("TASK_CANCELLED", "cancel requested")

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
            )

    def heartbeat(self, lease: Lease, lease_seconds: int = 60) -> datetime:
        with self.db.session() as sess:
            result = sess.execute(
                update(LearningTaskRow)
                .where(
                    LearningTaskRow.task_id == lease.task_id,
                    LearningTaskRow.lease_token == lease.lease_token,
                    LearningTaskRow.lease_owner == lease.lease_owner,
                    LearningTaskRow.generation == lease.generation,
                )
                .values(
                    heartbeat_at=utcnow(),
                    lease_until=utcnow() + timedelta(seconds=lease_seconds),
                    status="running",
                )
            )
            if result.rowcount != 1:
                raise LeaseError("LEASE_LOST", "lease token mismatch or expired")
            sess.commit()
            return utcnow() + timedelta(seconds=lease_seconds)

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
    ) -> dict:
        """条件更新提交结果：必须匹配 task_id + generation + attempt_id + lease_token。"""
        validate_stage(stage)
        validate_status(status)
        if error_code:
            validate_error_code(error_code)

        with self.db.session() as sess:
            row = sess.execute(
                select(LearningTaskRow).where(LearningTaskRow.task_id == lease.task_id).with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise LeaseError("TASK_NOT_FOUND", "task not found")

            now = utcnow()
            fence_ok = (
                row.lease_token == lease.lease_token
                and row.lease_owner == lease.lease_owner
                and row.generation == lease.generation
                and row.attempt == lease.attempt
            )
            lease_valid = row.lease_until is not None and row.lease_until >= now

            if not fence_ok or not lease_valid or diagnostics_only:
                # 迟到 worker：只写诊断
                try:
                    self.events.record(
                        lease.task_id,
                        stage=stage,
                        status="stale_discarded",
                        error_code="STALE_RESULT_DISCARDED",
                        generation=lease.generation,
                        attempt=lease.attempt,
                        diagnostics_only=True,
                    )
                except Exception:
                    pass
                sess.commit()
                return {"accepted": False, "reason": "stale_discarded", "task_id": lease.task_id}

            # 围栏通过：更新任务并写事件
            if status in ("succeeded", "partial", "failed"):
                row.status = status if status != "partial" else "succeeded"
            row.revision += 1
            row.updated_at = now
            row.lease_token = None
            row.lease_owner = None
            row.lease_until = None

            self.events.record(
                lease.task_id,
                stage=stage,
                status=status,
                error_code=error_code,
                duration_ms=duration_ms,
                progress_current=progress_current,
                generation=lease.generation,
                attempt=lease.attempt,
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
                "prompt_binding_id": row.prompt_binding_id,
                "deadline_at": row.deadline_at.isoformat() if row.deadline_at else None,
            }

    def request_cancel(self, task_id: str, *, actor_id: str) -> dict:
        with self.db.session() as sess:
            row = sess.get(LearningTaskRow, task_id)
            if row is None:
                raise LeaseError("TASK_NOT_FOUND", "task not found")
            if row.owner_user_id != actor_id:
                raise AccessDeniedError("not_owner")
            row.cancel_requested = True
            if row.status in ("queued", "leased"):
                row.status = "cancelled"
            sess.commit()
            return {"task_id": task_id, "cancel_requested": True, "status": row.status}
