from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from ..logging_audit.models import Base


def now():
    return datetime.now(timezone.utc)


class MemorySettingsRow(Base):
    __tablename__ = "memory_settings"
    owner_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class MemorySpaceRow(Base):
    __tablename__ = "memory_spaces"
    __table_args__ = (UniqueConstraint("owner_user_id", "scope_key", name="uq_memory_owner_scope"),)
    space_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), index=True)
    scope_key: Mapped[str] = mapped_column(String(100))
    bank_id: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="active")
    epoch: Mapped[int] = mapped_column(Integer, default=1)


class MemorySourceRow(Base):
    __tablename__ = "memory_sources"
    __table_args__ = (Index("ix_memory_source_event", "owner_user_id", "event_key", unique=True),)
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), index=True)
    scope_key: Mapped[str] = mapped_column(String(100))
    tag: Mapped[str] = mapped_column(String(100))
    source_kind: Mapped[str] = mapped_column(String(48))
    source_course_id: Mapped[str] = mapped_column(String(64), default="")
    document_id: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(24), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    operation_id: Mapped[str | None] = mapped_column(String(100))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settings_revision: Mapped[int] = mapped_column(Integer)
    epoch: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    event_key: Mapped[str | None] = mapped_column(String(64), nullable=True)


class LearningEvidenceRow(Base):
    """One assessment session per versioned capability; no student answer text."""
    __tablename__ = "memory_learning_evidence"
    __table_args__ = (Index("ix_learning_evidence_scope", "owner_user_id", "course_id", "status"),)
    evidence_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), index=True)
    course_id: Mapped[str] = mapped_column(String(64))
    lesson_id: Mapped[str] = mapped_column(String(64), default="")
    concept_id: Mapped[str] = mapped_column(String(100))
    content_version: Mapped[str] = mapped_column(String(100))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    independence: Mapped[str] = mapped_column(String(16), default="unknown")
    result: Mapped[str] = mapped_column(String(16), default="unknown")
    source_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    grading_revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="active")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MemoryMaintenancePolicy(Base):
    __tablename__ = "memory_maintenance_policy"
    owner_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), default="preview")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    minimum_age_days: Mapped[int] = mapped_column(Integer, default=90)


class MemoryMaintenanceJob(Base):
    __tablename__ = "memory_maintenance_jobs"
    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), index=True)
    space_id: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[str] = mapped_column(String(64))
    source_revision: Mapped[int] = mapped_column(Integer)
    epoch: Mapped[int] = mapped_column(Integer)
    policy_revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bundle_json: Mapped[str] = mapped_column(Text, default="{}")
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    error_code: Mapped[str] = mapped_column(String(48), default="")
    native_committed: Mapped[bool] = mapped_column(Boolean, default=False)
