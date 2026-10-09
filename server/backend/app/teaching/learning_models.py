"""Version-bound learning plans, private assessments and attempts (ADR-029)."""
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from ..logging_audit.models import Base
from .models import utcnow


class LessonBlueprintRow(Base):
    __tablename__ = "lesson_blueprints"
    conversation_id: Mapped[str] = mapped_column(String(64), ForeignKey("conversations.conversation_id", ondelete="CASCADE"), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    lesson_id: Mapped[str] = mapped_column(String(64), nullable=False)
    outline_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_json: Mapped[str] = mapped_column(Text, nullable=False)
    context_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PreparedLessonPlanRow(Base):
    __tablename__ = "prepared_lesson_plans"
    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    course_id: Mapped[str] = mapped_column(String(64), ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False)
    lesson_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_json: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="lightweight")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PreparedFirstSegmentRow(Base):
    __tablename__ = "prepared_first_segments"
    __table_args__ = (UniqueConstraint("owner_user_id","course_id","lesson_id","text_key","audio_key",name="uq_first_segment_owner_lesson_keys"),)
    cache_id: Mapped[str] = mapped_column(String(64),primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64),nullable=False)
    course_id: Mapped[str] = mapped_column(String(64),ForeignKey("courses.course_id",ondelete="CASCADE"),nullable=False)
    lesson_id: Mapped[str] = mapped_column(String(64),nullable=False)
    text_key: Mapped[str] = mapped_column(String(64),nullable=False)
    audio_key: Mapped[str] = mapped_column(String(64),nullable=False,default="")
    status: Mapped[str] = mapped_column(String(24),nullable=False,default="queued")
    task_id: Mapped[str] = mapped_column(String(64),nullable=False)
    context_json: Mapped[str] = mapped_column(Text,nullable=False)
    plan_json: Mapped[str] = mapped_column(Text,nullable=False)
    candidate_json: Mapped[str] = mapped_column(Text,nullable=False,default="{}")
    audio_json: Mapped[str] = mapped_column(Text,nullable=False,default="{}")
    audio_object_key: Mapped[str] = mapped_column(String(200),nullable=False,default="")
    audio_bytes: Mapped[int] = mapped_column(BigInteger,nullable=False,default=0)
    consumed_conversation_id: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),nullable=False,default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),nullable=False)


class TeachingPreviewRow(Base):
    __tablename__ = "teaching_content_previews"
    task_id: Mapped[str] = mapped_column(String(64), ForeignKey("learning_tasks.task_id", ondelete="CASCADE"), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64), ForeignKey("conversations.conversation_id", ondelete="CASCADE"), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class UnitAssessmentRow(Base):
    __tablename__ = "unit_assessments"
    __table_args__ = (Index("idx_unit_assessments_owner_unit", "owner_user_id", "unit_id"),)
    assessment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    course_id: Mapped[str] = mapped_column(String(64), nullable=False)
    unit_id: Mapped[str] = mapped_column(String(64), nullable=False)
    outline_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    context_json: Mapped[str] = mapped_column(Text, nullable=False)
    questions_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AssessmentAttemptRow(Base):
    __tablename__ = "assessment_attempts"
    __table_args__ = (UniqueConstraint("owner_user_id", "assessment_id", "idempotency_key", name="uq_assessment_attempt_idem"),)
    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    assessment_id: Mapped[str] = mapped_column(String(64), ForeignKey("unit_assessments.assessment_id", ondelete="CASCADE"), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    answers_json: Mapped[str] = mapped_column(Text, nullable=False)
    feedback_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
