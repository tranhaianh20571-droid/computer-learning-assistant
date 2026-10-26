"""Version-bound learning plans, private assessments and attempts (ADR-029)."""
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
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
    persistent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")


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


class FeynmanSessionRow(Base):
    __tablename__ = "feynman_sessions"
    __table_args__ = (UniqueConstraint("conversation_id", "checkpoint_id", name="uq_feynman_checkpoint"),
        Index("idx_feynman_owner", "owner_user_id"),)
    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64), ForeignKey("conversations.conversation_id", ondelete="CASCADE"))
    owner_user_id: Mapped[str] = mapped_column(String(64))
    checkpoint_id: Mapped[str] = mapped_column(String(64))
    after_step_no: Mapped[int] = mapped_column(Integer)
    source_hash: Mapped[str] = mapped_column(String(64))
    context_json: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(24), default="preparing")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    probe_count: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[str] = mapped_column(String(24), default="")
    task_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FeynmanTurnRow(Base):
    __tablename__ = "feynman_turns"
    __table_args__ = (UniqueConstraint("session_id", "turn_no", name="uq_feynman_turn"),)
    turn_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), ForeignKey("feynman_sessions.session_id", ondelete="CASCADE"))
    turn_no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16), default="question")
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    question: Mapped[str] = mapped_column(Text, default="")
    criteria_json: Mapped[str] = mapped_column(Text, default="[]")
    answer: Mapped[str] = mapped_column(Text, default="")
    request_key: Mapped[str] = mapped_column(String(128), default="")
    feedback: Mapped[str] = mapped_column(Text, default="")
    public_text: Mapped[str] = mapped_column(Text, default="")
    evaluation_json: Mapped[str] = mapped_column(Text, default="{}")
    playback_json: Mapped[str] = mapped_column(Text, default="{}")
    audio_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("audio_assets.audio_id"))
    audio_task_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SegmentCoverageRow(Base):
    __tablename__ = "segment_coverage"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id", ondelete="CASCADE"), primary_key=True)
    step_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    text_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    audio_id: Mapped[str] = mapped_column(String(64), primary_key=True, default="")
    client_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    intervals_json: Mapped[str] = mapped_column(Text, default="[]")
    visible_chars: Mapped[int] = mapped_column(Integer, default=0)
    board_complete: Mapped[bool] = mapped_column(Boolean, default=False)


class TaughtKnowledgeRow(Base):
    __tablename__ = "taught_knowledge"
    record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), index=True)
    course_id: Mapped[str] = mapped_column(String(64), index=True)
    lesson_id: Mapped[str] = mapped_column(String(64))
    content_version_id: Mapped[str] = mapped_column(String(64))
    plan_hash: Mapped[str] = mapped_column(String(64))
    checkpoint_id: Mapped[str] = mapped_column(String(64))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id", ondelete="CASCADE"))
    after_step_no: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text, default="")
    source_json: Mapped[str] = mapped_column(Text, default="[]")
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    scope_capability_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChannelPresentationRow(Base):
    __tablename__ = "channel_presentations"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id", ondelete="CASCADE"), primary_key=True)
    channel_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    client_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    text_hash: Mapped[str] = mapped_column(String(64))
    audio_id: Mapped[str] = mapped_column(String(64), default="")
    visible_chars: Mapped[int] = mapped_column(Integer, default=0)
    position_ms: Mapped[int] = mapped_column(Integer, default=0)
    paused: Mapped[bool] = mapped_column(Boolean, default=True)


class ClassroomRecordingRow(Base):
    __tablename__ = "classroom_recordings"
    recording_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), ForeignKey("conversations.conversation_id", ondelete="CASCADE"), index=True)
    content_version_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="recording")
    mime_type: Mapped[str] = mapped_column(String(80), default="video/webm")
    chunks_json: Mapped[str] = mapped_column(Text, default="[]")
    events_json: Mapped[str] = mapped_column(Text, default="[]")
    storage_key: Mapped[str] = mapped_column(String(512), default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
