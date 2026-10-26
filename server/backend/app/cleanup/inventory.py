"""Resolve business ownership and live references without reading content into logs."""
import json
from pathlib import PurePosixPath
from sqlalchemy import select, func

from ..materials.models import MaterialRow
from ..materials.source_models import SourceItemRow
from ..planning.models import ConversationRow
from ..characters.models import (
    CharacterCardRow, CharacterBindingRow, CharacterSegmentRow,
    CharacterSystemDefaultRow, ClassroomHistoryRow,
)
from ..teaching.models import AudioAssetRow
from ..teaching.learning_models import PreparedFirstSegmentRow
from ..tasks.service import LearningTaskRow
from .models import BackupRunRow


def describe(session, key, *, backup=False):
    parts = PurePosixPath(key).parts
    if len(parts) < 2:
        return "unknown", None, 1
    prefix, ident = parts[:2]
    ident = ident.rsplit(".", 1)[0] if prefix not in ("audio", "characters", "prepared") else ident
    if backup:
        row = session.get(BackupRunRow, parts[-1].split(".")[0])
        return "backup", None, int(row is not None)
    if prefix in ("orig", "fig", "source-assets", "source-checkpoints"):
        if prefix == "source-checkpoints":
            ident = ident.removesuffix("-visual")
        row = session.get(MaterialRow, ident)
        return "materials", row.owner_user_id if row else None, int(row is None or row.deleted_at is None)
    if prefix == "audio":
        conv = session.get(ConversationRow, ident)
        refs = session.scalar(select(func.count()).select_from(AudioAssetRow).join(
            ConversationRow, ConversationRow.conversation_id == AudioAssetRow.conversation_id
        ).where(AudioAssetRow.storage_key == key, ConversationRow.deleted_at.is_(None))) or 0
        if not refs:
            assets = set(session.scalars(select(AudioAssetRow.audio_id).where(AudioAssetRow.storage_key == key)))
            for snapshot in session.scalars(select(ClassroomHistoryRow.snapshot_json).join(
                ConversationRow, ConversationRow.conversation_id == ClassroomHistoryRow.conversation_id
            ).where(ConversationRow.deleted_at.is_(None))):
                if json.loads(snapshot).get("audio_id") in assets:
                    refs += 1
        return "audio", conv.owner_user_id if conv else None, refs
    if prefix == "recordings":
        from ..teaching.learning_models import ClassroomRecordingRow
        conv = session.get(ConversationRow, ident)
        row = session.get(ClassroomRecordingRow, parts[2]) if len(parts) > 2 else None
        live = bool(row and not row.deleted_at and conv and not conv.deleted_at)
        retained = live and (not row.storage_key or row.storage_key == key)
        return "recordings", row.owner_user_id if row else None, int(retained)
    if prefix == "characters":
        card = session.get(CharacterCardRow, ident)
        refs = int(card is None or card.deleted_at is None)
        for model in (CharacterBindingRow, CharacterSegmentRow):
            refs += session.scalar(select(func.count()).select_from(model).join(
                ConversationRow, ConversationRow.conversation_id == model.conversation_id
            ).where(model.card_id == ident, ConversationRow.deleted_at.is_(None))) or 0
        refs += session.scalar(select(func.count()).select_from(CharacterSystemDefaultRow).where(
            CharacterSystemDefaultRow.card_id == ident)) or 0
        for snapshot in session.scalars(select(ClassroomHistoryRow.snapshot_json).join(
            ConversationRow, ConversationRow.conversation_id == ClassroomHistoryRow.conversation_id
        ).where(ConversationRow.deleted_at.is_(None))):
            if (json.loads(snapshot).get("character") or {}).get("card_id") == ident:
                refs += 1
        return "characters", card.owner_user_id if card else None, refs
    if prefix == "prepared":
        row = session.get(PreparedFirstSegmentRow, ident)
        refs = session.scalar(select(func.count()).select_from(AudioAssetRow).join(
            ConversationRow, ConversationRow.conversation_id == AudioAssetRow.conversation_id
        ).where(AudioAssetRow.storage_key == key, ConversationRow.deleted_at.is_(None))) or 0
        if row and row.consumed_conversation_id:
            conv = session.get(ConversationRow, row.consumed_conversation_id)
            refs += int(bool(conv and conv.deleted_at is None))
        return "prewarm", row.owner_user_id if row else None, refs
    if prefix == "imports":
        row = session.get(SourceItemRow, ident)
        material = session.get(MaterialRow, row.material_id) if row and row.material_id else None
        reusable = bool(material and material.status == "parsed")
        return "downloads", row.owner_user_id if row else None, int(
            row is None or not reusable and row.status in ("pending", "uploaded", "uploading", "processing"))
    if prefix in ("tmp", "temp"):
        task = session.get(LearningTaskRow, ident)
        return "temporary", task.owner_user_id if task else None, int(
            task is None or task.status in ("queued", "leased", "running"))
    return "unknown", None, 1
