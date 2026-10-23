from datetime import datetime, timezone
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from ..logging_audit.models import Base


def utcnow():
    return datetime.now(timezone.utc)


class CharacterCardRow(Base):
    __tablename__ = "character_cards"
    card_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), index=True)
    current_revision: Mapped[int] = mapped_column(Integer, default=1)
    published_revision: Mapped[int | None] = mapped_column(Integer)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CharacterRevisionRow(Base):
    __tablename__ = "character_revisions"
    card_id: Mapped[str] = mapped_column(ForeignKey("character_cards.card_id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_json: Mapped[str] = mapped_column(Text)


class CharacterLibraryRow(Base):
    __tablename__ = "character_library"
    owner_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    card_id: Mapped[str] = mapped_column(ForeignKey("character_cards.card_id"), primary_key=True)


class CharacterSystemDefaultRow(Base):
    __tablename__ = "character_system_default"
    key: Mapped[str] = mapped_column(String(24), primary_key=True)
    card_id: Mapped[str] = mapped_column(ForeignKey("character_cards.card_id"))
    revision: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)


class CharacterAssetRow(Base):
    __tablename__ = "character_assets"
    asset_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    card_id: Mapped[str] = mapped_column(ForeignKey("character_cards.card_id"), index=True)
    owner_user_id: Mapped[str] = mapped_column(String(64))
    source_revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(12))
    scene: Mapped[str] = mapped_column(String(24), default="neutral")
    storage_key: Mapped[str] = mapped_column(String(200))
    mime_type: Mapped[str] = mapped_column(String(64))
    confirmed: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CharacterBindingRow(Base):
    __tablename__ = "character_bindings"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id"), primary_key=True)
    card_id: Mapped[str] = mapped_column(ForeignKey("character_cards.card_id"))
    revision: Mapped[int] = mapped_column(Integer)
    effective_step_no: Mapped[int] = mapped_column(Integer)
    profile_json: Mapped[str] = mapped_column(Text)


class CharacterMediaJobRow(Base):
    __tablename__ = "character_media_jobs"
    task_id: Mapped[str] = mapped_column(ForeignKey("learning_tasks.task_id"), primary_key=True)
    card_id: Mapped[str] = mapped_column(ForeignKey("character_cards.card_id"), index=True)
    owner_user_id: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer)
    config_id: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[int] = mapped_column(Integer)
    request_json: Mapped[str] = mapped_column(Text)
    request_hash: Mapped[str] = mapped_column(String(64))
    provider_job_id: Mapped[str | None] = mapped_column(String(200))
    submitted: Mapped[bool] = mapped_column(default=False)
    result_json: Mapped[str] = mapped_column(Text, default="[]")


class CharacterSegmentRow(Base):
    __tablename__ = "character_segments"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id"), primary_key=True)
    step_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    card_id: Mapped[str] = mapped_column(ForeignKey("character_cards.card_id"))
    revision: Mapped[int] = mapped_column(Integer)
    profile_json: Mapped[str] = mapped_column(Text)
    speech_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    expression_json: Mapped[str] = mapped_column(Text, default="{}")


class CharacterPreferenceRow(Base):
    __tablename__ = "character_preferences"
    owner_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    card_id: Mapped[str | None] = mapped_column(String(64))
    revision: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CharacterExpressionRow(Base):
    __tablename__ = "character_expressions"
    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    profile_hash: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(40))
    plan_json: Mapped[str] = mapped_column(Text)


class CharacterVoiceChoiceRow(Base):
    __tablename__ = "character_voice_choices"
    owner_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    card_id: Mapped[str] = mapped_column(ForeignKey("character_cards.card_id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    voice_id: Mapped[str] = mapped_column(String(200))
    config_id: Mapped[str] = mapped_column(String(64))


class ClassroomPlaybackRow(Base):
    __tablename__ = "classroom_playback_revisions"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    effective_step_no: Mapped[int] = mapped_column(Integer)
    profile_json: Mapped[str] = mapped_column(Text, default="{}")


class ClassroomHistoryRow(Base):
    __tablename__ = "classroom_segment_history"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    step_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_json: Mapped[str] = mapped_column(Text)
