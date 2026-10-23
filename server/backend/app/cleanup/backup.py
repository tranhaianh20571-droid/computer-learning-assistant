"""Stream object backups into a separate store; retain retryable rotation rows."""
import io
import json
import os
import tarfile
import uuid
from datetime import timedelta, timezone
from pathlib import Path
from threading import Lock

from sqlalchemy import select, text

from ..materials.models import DeletionTombstoneRow
from ..materials.storage import MaterialStorage, StorageError
from ..planning.models import ConversationRow, CourseRow
from ..teaching.models import AudioAssetRow
from ..teaching.learning_models import PreparedFirstSegmentRow
from .models import BackupRunRow, utcnow

_BACKUP_LOCK = Lock()
EXCLUDED_DIRS = frozenset({
    "backups", "cache", "caches", "tmp", "temp", "logs", ".runtime-logs", "__pycache__",
})


def backup_store(storage):
    configured = os.environ.get("BACKUP_STORAGE_DIR", "").strip() or storage.location.config().get("backup_root", "")
    source = storage.base_dir.resolve()
    directory = Path(configured).resolve() if configured else source.with_name(source.name + "-backups")
    if directory == source or directory.is_relative_to(source) or source.is_relative_to(directory):
        raise StorageError("invalid_object_key")
    return MaterialStorage(directory)


def create_object_backup(service, retention_days):
    storage, destination = service.storage, service.backup_storage
    if storage is None:
        raise StorageError("object_not_found")
    now = utcnow()
    cutoff = now - timedelta(days=retention_days)
    run_id = "bk_" + uuid.uuid4().hex[:12]
    key = f"backups/{run_id}.tar.gz"
    published = False
    committed = False
    with _BACKUP_LOCK, storage.location.lock, service.db.session() as session:
        if session.bind.dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 61021001})
        service.require_not_locked()
        tombstones = list(session.scalars(select(DeletionTombstoneRow)))
        def deleted_at(row):
            return row.deleted_at.replace(tzinfo=timezone.utc) if row.deleted_at.tzinfo is None else row.deleted_at
        manifest = {
            "created_at": now.isoformat(), "retention_days": retention_days,
            "tombstones": [
                {name: getattr(row, name).isoformat() if name == "deleted_at" else getattr(row, name)
                 for name in ("tombstone_id", "object_type", "object_id", "owner_user_id",
                              "deleted_at", "cleanup_stage", "version")}
                for row in tombstones if deleted_at(row) >= cutoff
            ],
        }
        deleted_prefixes = []
        protected_audio = set(session.scalars(select(AudioAssetRow.storage_key).join(
            ConversationRow, ConversationRow.conversation_id == AudioAssetRow.conversation_id
        ).where(ConversationRow.deleted_at.is_(None))))
        protected_audio.update(session.scalars(select(PreparedFirstSegmentRow.audio_object_key).join(
            CourseRow, CourseRow.course_id == PreparedFirstSegmentRow.course_id
        ).where(CourseRow.deleted_at.is_(None))))
        from ..characters.models import ClassroomHistoryRow
        for snapshot in session.scalars(select(ClassroomHistoryRow.snapshot_json).join(
            ConversationRow, ConversationRow.conversation_id == ClassroomHistoryRow.conversation_id
        ).where(ConversationRow.deleted_at.is_(None))):
            audio_id = json.loads(snapshot).get("audio_id")
            asset = session.get(AudioAssetRow, audio_id) if audio_id else None
            if asset:
                protected_audio.add(asset.storage_key)
        for row in tombstones:
            if deleted_at(row) >= cutoff:
                continue
            if row.object_type == "material":
                deleted_prefixes.extend((f"orig/{row.object_id}.bin", f"fig/{row.object_id}/",
                    f"source-assets/{row.object_id}/", f"source-checkpoints/{row.object_id}.json",
                    f"source-checkpoints/{row.object_id}-visual.json"))
            elif row.object_type == "conversation":
                deleted_prefixes.append(f"audio/{row.object_id}/")
            # Character files can still belong to a valid classroom snapshot.
        try:
            with destination.atomic_output(key) as output:
                with tarfile.open(fileobj=output, mode="w|gz") as archive:
                    data = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
                    info = tarfile.TarInfo("manifest.json")
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
                    for object_key, path in storage.iter_files(excluded_dirs=EXCLUDED_DIRS):
                        if (path.suffix in (".part", ".tmp", ".log") or
                                (object_key not in protected_audio and any(object_key == prefix or
                                    (prefix.endswith("/") and object_key.startswith(prefix))
                                    for prefix in deleted_prefixes))):
                            continue
                        archive.add(path, arcname=object_key, recursive=False)
            published = True
            size = destination.local_path(key).stat().st_size
            session.add(BackupRunRow(run_id=run_id, backup_key=key,
                payload_bytes=size, deleted_cutoff_at=cutoff))
            if service.audit is not None:
                service.audit.append("cleanup.completed", actor_id="system:cleanup",
                    object_type="backup", object_id=run_id, result="backup_created", session=session)
            session.commit()
            committed = True
        finally:
            if published and not committed:
                destination.delete(key)
        # Publish a valid replacement before rotating; deletion failures retain the row.
        for old in session.scalars(select(BackupRunRow).where(BackupRunRow.created_at < cutoff)):
            from .models import LocalResourceRow
            from sqlalchemy import inspect
            if inspect(session.bind).has_table(LocalResourceRow.__tablename__):
                if session.scalar(select(LocalResourceRow.resource_id).where(
                        LocalResourceRow.object_key == old.backup_key, LocalResourceRow.pinned.is_(True)).limit(1)):
                    continue
            if not old.backup_key.startswith("backups/") or not old.backup_key.endswith(".tar.gz"):
                continue
            try:
                destination.delete(old.backup_key)
                storage.delete(old.backup_key)  # Legacy backups used the business volume.
            except (OSError, StorageError):
                if service.audit is not None:
                    service.audit.append("cleanup.completed", actor_id="system:cleanup",
                        object_type="backup", object_id=old.run_id,
                        result="rotation_failed", reason="INTERNAL_ERROR", session=session)
                continue
            if service.audit is not None:
                service.audit.append("cleanup.completed", actor_id="system:cleanup",
                    object_type="backup", object_id=old.run_id, result="rotated", session=session)
            session.delete(old)
        return {"run_id": run_id, "backup_key": key, "payload_bytes": size,
                "tombstone_count": len(manifest["tombstones"])}
