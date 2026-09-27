"""切片 4 数据模型：教学步骤、板书场景、音频资产、播放检查点。

- 一个教学步骤存 `text`（text_version）、不可变 `board_scene`、可选 `audio`；
- 音频与字幕时标绑定同一 text_version（text_sha256 校验），缺失/不一致 → 不发布；
- `playback_checkpoints` 单调 client_seq + 乐观版本检查（架构 §5.3）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
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

STEP_STATUSES = ("generating", "ready", "failed")
AUDIO_STATUSES = ("generating", "ready", "invalid", "failed")
CONVERSATION_KINDS_EXTENDED = ("chat", "teaching")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TeachingStepRow(Base):
    __tablename__ = "teaching_steps"
    __table_args__ = (
        UniqueConstraint("conversation_id", "generation", "step_no",
                         name="uq_teaching_steps_conv_gen_no"),
        Index("idx_teaching_steps_conv", "conversation_id", "step_no"),
    )

    step_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("conversations.conversation_id", ondelete="CASCADE"), nullable=False
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    text_version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="generating")
    board_scene_id: Mapped[str | None] = mapped_column(String(64))
    audio_id: Mapped[str | None] = mapped_column(String(64))
    source_task_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class BoardSceneRow(Base):
    __tablename__ = "board_scenes"
    __table_args__ = (
        UniqueConstraint("conversation_id", "generation", "step_no", "scene_version_no",
                         name="uq_board_scenes_version"),
        Index("idx_board_scenes_conv", "conversation_id", "step_no"),
    )

    scene_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("conversations.conversation_id", ondelete="CASCADE"), nullable=False
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    scene_version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    declaration_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    scene_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AudioAssetRow(Base):
    __tablename__ = "audio_assets"
    __table_args__ = (
        UniqueConstraint("conversation_id", "generation", "step_no", "text_version_no",
                         name="uq_audio_assets_version"),
        Index("idx_audio_assets_conv", "conversation_id", "step_no"),
    )

    audio_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("conversations.conversation_id", ondelete="CASCADE"), nullable=False
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    text_version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    storage_key: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    audio_format: Mapped[str] = mapped_column(String(10), nullable=False, default="mp3")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    subtitle_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    provider_trace_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="generating")
    invalid_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class PlaybackCheckpointRow(Base):
    __tablename__ = "playback_checkpoints"
    __table_args__ = (
        UniqueConstraint("conversation_id", "client_seq", name="uq_playback_checkpoints_seq"),
        Index("idx_playback_checkpoints_conv", "conversation_id"),
    )

    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("conversations.conversation_id", ondelete="CASCADE"), nullable=False
    )
    client_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    position_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    board_state_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


__all__ = [
    "TeachingStepRow", "BoardSceneRow", "AudioAssetRow", "PlaybackCheckpointRow",
    "STEP_STATUSES", "AUDIO_STATUSES", "CONVERSATION_KINDS_EXTENDED", "utcnow",
]
