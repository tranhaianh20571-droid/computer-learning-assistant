"""Tombstone-scoped media collection; active references always win."""
import json

from sqlalchemy import select

from ..characters.models import (
    CharacterAssetRow, CharacterBindingRow, CharacterCardRow, CharacterSegmentRow,
    CharacterSystemDefaultRow, ClassroomHistoryRow,
)
from ..materials.storage import StorageError
from ..planning.models import ConversationRow, CourseRow
from ..teaching.learning_models import PreparedFirstSegmentRow
from ..teaching.models import AudioAssetRow


class ReferencedMedia(Exception):
    """Keep the tombstone pending until surviving references are released."""


def _delete_keys(storage, keys, allowed_prefixes):
    for key in sorted(set(keys)):
        if not key:
            continue
        if not any(key.startswith(prefix) for prefix in allowed_prefixes):
            raise StorageError("invalid_object_key")
        storage.delete(key)


def cleanup_audio(session, storage, *, owner, conversation_id=None, course_id=None):
    conversations = list(session.scalars(select(ConversationRow).where(
        ConversationRow.conversation_id == conversation_id if conversation_id else
        ConversationRow.course_id == course_id
    ).with_for_update()))
    if any(row.deleted_at is None or row.owner_user_id != owner for row in conversations):
        raise ReferencedMedia()
    ids = [row.conversation_id for row in conversations]
    if conversation_id and conversation_id not in ids:
        ids.append(conversation_id)
    assets = list(session.scalars(select(AudioAssetRow).where(AudioAssetRow.conversation_id.in_(ids))))
    prepared = list(session.scalars(select(PreparedFirstSegmentRow).where(
        PreparedFirstSegmentRow.course_id == course_id if course_id else
        PreparedFirstSegmentRow.consumed_conversation_id == conversation_id
    ).with_for_update()))
    if any(row.owner_user_id != owner for row in prepared):
        raise ReferencedMedia()
    keys = {asset.storage_key for asset in assets if asset.storage_key}
    keys.update(row.audio_object_key for row in prepared if row.audio_object_key)
    prefixes = [f"audio/{ident}/" for ident in ids]
    for prefix in prefixes:
        keys.update(key for key, _ in storage.iter_files(prefix, reject_links=True))
    # Consumed prewarm audio is stored outside audio/<conversation>.
    protected = set(session.scalars(select(AudioAssetRow.storage_key).join(
        ConversationRow, ConversationRow.conversation_id == AudioAssetRow.conversation_id
    ).where(ConversationRow.deleted_at.is_(None), AudioAssetRow.storage_key.in_(keys))))
    protected.update(session.scalars(select(PreparedFirstSegmentRow.audio_object_key).join(
        CourseRow, CourseRow.course_id == PreparedFirstSegmentRow.course_id
    ).where(CourseRow.deleted_at.is_(None),
            PreparedFirstSegmentRow.cache_id.not_in([row.cache_id for row in prepared]),
            PreparedFirstSegmentRow.audio_object_key.in_(keys))))
    asset_ids = {asset.audio_id for asset in assets}
    for snapshot in session.scalars(select(ClassroomHistoryRow.snapshot_json).join(
            ConversationRow, ConversationRow.conversation_id == ClassroomHistoryRow.conversation_id
    ).where(ConversationRow.deleted_at.is_(None))):
        if json.loads(snapshot).get("audio_id") in asset_ids:
            raise ReferencedMedia()
    if protected:
        raise ReferencedMedia()
    _delete_keys(storage, keys, ["audio/", "prepared/"])
    for row in prepared:
        session.delete(row)


def cleanup_character(session, storage, card_id, owner):
    card = session.get(CharacterCardRow, card_id, with_for_update=True)
    if card and (card.owner_user_id != owner or card.deleted_at is None):
        raise ReferencedMedia()
    for model in (CharacterBindingRow, CharacterSegmentRow):
        if session.scalar(select(model.conversation_id).join(
                ConversationRow, ConversationRow.conversation_id == model.conversation_id
        ).where(ConversationRow.deleted_at.is_(None), model.card_id == card_id).limit(1)):
            raise ReferencedMedia()
    if session.scalar(select(CharacterSystemDefaultRow.key).where(
            CharacterSystemDefaultRow.card_id == card_id).limit(1)):
        raise ReferencedMedia()
    # Archived mentor changes store the character inside a private JSON snapshot.
    for snapshot in session.scalars(select(ClassroomHistoryRow.snapshot_json).join(
            ConversationRow, ConversationRow.conversation_id == ClassroomHistoryRow.conversation_id
    ).where(ConversationRow.deleted_at.is_(None))):
        character = json.loads(snapshot).get("character")
        if character and character.get("card_id") == card_id:
            raise ReferencedMedia()
    assets = list(session.scalars(select(CharacterAssetRow).where(CharacterAssetRow.card_id == card_id)))
    keys = {asset.storage_key for asset in assets}
    prefix = f"characters/{card_id}/"
    keys.update(key for key, _ in storage.iter_files(prefix, reject_links=True))
    if session.scalar(select(CharacterAssetRow.asset_id).where(
            CharacterAssetRow.card_id != card_id, CharacterAssetRow.storage_key.in_(keys)).limit(1)):
        raise ReferencedMedia()
    _delete_keys(storage, keys, [prefix])
