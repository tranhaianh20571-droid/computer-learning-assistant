"""Persistent private planning turns and cover assets (ADR-028)."""
from sqlalchemy import DateTime, Integer, LargeBinary, String, Text, Index
from sqlalchemy.orm import Mapped, mapped_column
from ..logging_audit.models import Base
from .models import utcnow


class PlanningDialogueRow(Base):
    __tablename__ = "planning_dialogue_turns"
    __table_args__ = (Index("idx_planning_dialogue_draft", "draft_id", "created_at"),)
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    draft_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    base_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    user_content: Mapped[str] = mapped_column(Text, nullable=False)
    selected_node: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    base_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    messages_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    decision: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class CoverStateRow(Base):
    __tablename__ = "planning_cover_states"
    target_kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    latest_task_id: Mapped[str | None] = mapped_column(String(64))
    current_asset_id: Mapped[str | None] = mapped_column(String(64))


class CoverAssetRow(Base):
    __tablename__ = "planning_cover_assets"
    __table_args__ = (Index("idx_planning_cover_target", "target_kind", "target_id"),)
    asset_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    target_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(20), nullable=False, default="procedural")
    recipe_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    image_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
