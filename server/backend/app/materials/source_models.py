"""Private source import state; content never enters task events or logs."""
from __future__ import annotations

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..logging_audit.models import Base
from .models import utcnow


class SourceProfileRow(Base):
    __tablename__ = "source_profiles"
    owner_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(24), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    config_id: Mapped[str | None] = mapped_column(String(64))
    options_json: Mapped[str] = mapped_column(Text, default="{}")


class SourceBatchRow(Base):
    __tablename__ = "source_import_batches"
    __table_args__ = (UniqueConstraint("owner_user_id", "upload_key", name="uq_source_batch_key"),)
    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), index=True)
    upload_key: Mapped[str] = mapped_column(String(64))
    manifest_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="draft")
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceItemRow(Base):
    __tablename__ = "source_import_items"
    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("source_import_batches.batch_id", ondelete="CASCADE"), index=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), index=True)
    client_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24))
    encrypted_input: Mapped[str] = mapped_column(Text, default="")
    source_hash: Mapped[str] = mapped_column(String(64), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    material_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    failure_code: Mapped[str | None] = mapped_column(String(64))


class ImportedSourceRow(Base):
    __tablename__ = "imported_sources"
    material_id: Mapped[str] = mapped_column(ForeignKey("materials.material_id", ondelete="CASCADE"), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), index=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    source_version: Mapped[str] = mapped_column(String(64))
    binding_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    failure_code: Mapped[str | None] = mapped_column(String(64))


class SourceChatTurnRow(Base):
    __tablename__ = "source_chat_turns"
    task_id: Mapped[str] = mapped_column(ForeignKey("learning_tasks.task_id", ondelete="CASCADE"), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64))
    materials_json: Mapped[str] = mapped_column(Text)


def source_records(session, owner, ids, *, parsed=False):
    """Adapt old documents and new imports into the same private read surface."""
    from sqlalchemy import select
    from .models import MaterialRow, DocumentAttachmentRow
    rows = session.execute(select(MaterialRow, DocumentAttachmentRow)
        .outerjoin(DocumentAttachmentRow, MaterialRow.material_id == DocumentAttachmentRow.material_id)
        .where(MaterialRow.owner_user_id == owner, MaterialRow.material_id.in_(ids),
               MaterialRow.deleted_at.is_(None))).all()
    # Legacy document-only readers do not require the additive source schema.
    missing = [material.material_id for material, document in rows if document is None]
    imported = {row.material_id: row for row in session.scalars(select(ImportedSourceRow)
        .where(ImportedSourceRow.material_id.in_(missing), ImportedSourceRow.owner_user_id == owner))} if missing else {}
    return [(material, document or imported.get(material.material_id)) for material, document in rows
            if (document or imported.get(material.material_id)) is not None
            and (document or imported[material.material_id]).owner_user_id == owner
            and (not parsed or material.status == "parsed")]
