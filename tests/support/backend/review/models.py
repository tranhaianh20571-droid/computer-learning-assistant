from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..logging_audit.models import Base
from ..planning.models import utcnow


class ReviewCardRow(Base):
    __tablename__ = "review_cards"
    __table_args__ = (Index("ix_review_cards_owner_due", "owner_user_id", "due_at"),)
    record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    course_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    card_json: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ReviewSessionRow(Base):
    __tablename__ = "review_sessions"
    __table_args__ = (UniqueConstraint("owner_user_id", "idempotency_key", name="uq_review_session_key"),)
    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    course_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    context_json: Mapped[str] = mapped_column(Text, nullable=False)
    questions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    draft_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ReviewAnswerRow(Base):
    __tablename__ = "review_answers"
    __table_args__ = (UniqueConstraint("session_id", "position", name="uq_review_answer_position"),)
    answer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("review_sessions.session_id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    assisted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    feedback_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ReviewLogRow(Base):
    __tablename__ = "review_logs"
    answer_id: Mapped[str] = mapped_column(ForeignKey("review_answers.answer_id", ondelete="CASCADE"), primary_key=True)
    record_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    card_before_json: Mapped[str] = mapped_column(Text, nullable=False)
    card_after_json: Mapped[str] = mapped_column(Text, nullable=False)
    log_json: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm: Mapped[str] = mapped_column(String(64), nullable=False, default="fsrs-6.3.2-default-v1")
