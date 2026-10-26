"""Durable course preparation obligations, independent of learning sessions."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..logging_audit.models import Base
from ..planning.models import utcnow


class CoursePreparationRow(Base):
    __tablename__ = "course_preparations"
    outline_version_id: Mapped[str] = mapped_column(
        ForeignKey("outline_versions.outline_version_id", ondelete="CASCADE"),
        primary_key=True,
    )
    course_id: Mapped[str] = mapped_column(
        ForeignKey("courses.course_id", ondelete="CASCADE"), index=True
    )
    owner_user_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class PreparationItemRow(Base):
    __tablename__ = "course_preparation_items"
    __table_args__ = (Index("ix_preparation_due", "state", "next_run_at", "priority"),)
    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    outline_version_id: Mapped[str] = mapped_column(
        ForeignKey("course_preparations.outline_version_id", ondelete="CASCADE"),
        index=True,
    )
    owner_user_id: Mapped[str] = mapped_column(String(64))
    course_id: Mapped[str] = mapped_column(String(64))
    unit_id: Mapped[str] = mapped_column(String(64))
    lesson_id: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    priority: Mapped[int] = mapped_column(Integer, default=100)
    state: Mapped[str] = mapped_column(String(24), default="waiting")
    task_id: Mapped[str | None] = mapped_column(String(64), index=True)
    artifact_id: Mapped[str | None] = mapped_column(String(64))
    input_key: Mapped[str] = mapped_column(String(64), default="")
    connection_key: Mapped[str] = mapped_column(String(64), default="")
    failures: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class PreparationConnectionRow(Base):
    __tablename__ = "preparation_connections"
    connection_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64))
    failures: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    probe_task_id: Mapped[str | None] = mapped_column(String(64))
    budget_day: Mapped[str] = mapped_column(String(10), default="")
    wire_requests: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
