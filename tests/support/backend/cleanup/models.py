"""切片 5 数据模型：系统状态（恢复锁）、备份运行记录与观测删除登记。

- `system_state`：部署级键值（restore_locked 等）；恢复期间拒绝用户访问；
- `backup_runs`：备份/轮换运行账本（备份产物键、保留策略、删除截止）；
- `observability_deletion_refs`（ADR-022）：外部观测内容（trace）删除任务，
  用户资源删除时登记、清理过程核销，失败可重试、状态可审计。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..logging_audit.models import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SystemStateRow(Base):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class BackupRunRow(Base):
    __tablename__ = "backup_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    backup_key: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="full")
    payload_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deleted_cutoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ObservabilityDeletionRefRow(Base):
    """ADR-022：外部观测内容删除任务（pending → completed/failed）。"""

    __tablename__ = "observability_deletion_refs"
    __table_args__ = (Index("idx_obsdel_owner_resource", "owner_user_id", "resource_id"),)

    ref_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    platform: Mapped[str] = mapped_column(String(30), nullable=False, default="langfuse")
    trace_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reason: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LocalResourceRow(Base):
    __tablename__ = "local_resource_objects"
    __table_args__ = (
        UniqueConstraint("volume_id", "object_key", name="uq_local_resource_key"),
        Index("idx_local_resource_owner_kind", "owner_user_id", "kind"),
        Index("idx_local_resource_state_due", "state", "next_retry_at"),
    )
    resource_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    volume_id: Mapped[str] = mapped_column(String(32))
    object_key: Mapped[str] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(24))
    owner_user_id: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    mtime_ns: Mapped[str] = mapped_column(String(32), default="0")
    reference_count: Mapped[int] = mapped_column(Integer, default=0)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(24), default="present")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LocalResourceJobRow(Base):
    __tablename__ = "local_resource_jobs"
    __table_args__ = (UniqueConstraint("request_key", name="uq_local_resource_request"),)
    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64))
    request_key: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), default="queued")
    request_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LocalResourcePolicyRow(Base):
    __tablename__ = "local_resource_policy"
    key: Mapped[str] = mapped_column(String(24), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    settings_json: Mapped[str] = mapped_column(Text, default="{}")
    successful_starts: Mapped[int] = mapped_column(Integer, default=0)
    last_boot: Mapped[str] = mapped_column(String(64), default="")


__all__ = ["SystemStateRow", "BackupRunRow", "ObservabilityDeletionRefRow", "utcnow"]
