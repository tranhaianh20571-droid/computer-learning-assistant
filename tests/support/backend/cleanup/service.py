"""切片 5：在线清理、删除流、备份轮换与恢复（墓碑重放）。

- 删除即撤销访问（软删 + 墓碑 + 索引/对象下线，前序切片已落）；
- 在线清理执行器：24 小时内把 pending 墓碑推进到 cleaned（删除受控对象、
  派生产物），失败保留 pending 可重试；
- 备份轮换：打包（数据库快照由部署层完成，此处固化清单与对象卷）+
  30 天保留；备份打包时排除已删除满 30 天的数据（备份中不留已清数据）；
- 恢复：`restore_locked` 期间拒绝用户访问 → 从独立墓碑源重放（重新删除
  被恢复出来的已删对象）→ 核验通过后解锁。墓碑不过备份，恢复不复活删除。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select

from ..logging_audit.db import Database
from ..logging_audit.repositories import PgAuditLog
from ..materials.models import DeletionTombstoneRow
from ..materials.storage import MaterialStorage, StorageError
from .errors import CleanupError, cerr
from .models import SystemStateRow, utcnow

ONLINE_CLEANUP_DELAY_SECONDS = 24 * 3600
BACKUP_RETENTION_DAYS = 30
RESTORE_LOCK_KEY = "restore_locked"


class CleanupService:
    def __init__(
        self,
        db: Database,
        storage: Optional[MaterialStorage] = None,
        audit: Optional[PgAuditLog] = None,
    ) -> None:
        self.db = db
        self.storage = storage
        self.audit = audit
        self._backup_storage = None

    @property
    def backup_storage(self):
        if self._backup_storage is None:
            from .backup import backup_store
            if self.storage is None:
                raise StorageError("object_not_found")
            self._backup_storage = backup_store(self.storage)
        return self._backup_storage

    # ------------------------------------------------------------------
    # 系统状态（恢复锁）
    # ------------------------------------------------------------------
    def get_state(self, key: str) -> Optional[str]:
        with self.db.session() as sess:
            row = sess.get(SystemStateRow, key)
            return row.value if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.db.session() as sess:
            row = sess.get(SystemStateRow, key)
            if row is None:
                row = SystemStateRow(key=key, value=value)
                sess.add(row)
            else:
                row.value = value
            row.updated_at = utcnow()
            sess.commit()

    def is_restore_locked(self) -> bool:
        return self.get_state(RESTORE_LOCK_KEY) == "1"

    def require_not_locked(self) -> None:
        if self.is_restore_locked():
            raise cerr("restore_locked", "system is locked for restore", status=423)

    # ------------------------------------------------------------------
    # 在线清理（24 小时内推进墓碑）
    # ------------------------------------------------------------------
    def run_online_cleanup(self, *, now: Optional[datetime] = None, limit=200, seconds=2) -> dict:
        self.require_not_locked()
        from time import monotonic
        started = monotonic()
        now = now or utcnow()
        from ..teaching.learning_models import TeachingPreviewRow
        from sqlalchemy import delete
        with self.db.session() as sess:
            sess.execute(delete(TeachingPreviewRow).where(TeachingPreviewRow.updated_at < now-timedelta(minutes=10)))
        cutoff = now - timedelta(seconds=ONLINE_CLEANUP_DELAY_SECONDS)
        cleaned = 0
        skipped = 0
        manager = getattr(self, "resource_manager", None)
        cursor = self.get_state("local_tombstone_cursor") if manager else None
        with self.db.session() as sess:
            tombstones = (
                sess.execute(
                    select(DeletionTombstoneRow).where(
                        DeletionTombstoneRow.cleanup_stage == "pending",
                        DeletionTombstoneRow.deleted_at <= cutoff,
                        DeletionTombstoneRow.tombstone_id > cursor if cursor else True,
                    ).order_by(DeletionTombstoneRow.tombstone_id).limit(limit)
                )
                .scalars()
                .all()
            )
            targets = [(t.tombstone_id, t.object_type, t.object_id, t.owner_user_id)
                       for t in tombstones]
        for tombstone_id, object_type, object_id, owner in targets:
            if monotonic()-started >= seconds:
                break
            if manager:
                self.set_state("local_tombstone_cursor", tombstone_id)
            retry_key = "cleanup_retry:" + tombstone_id
            retry = self.get_state(retry_key)
            if retry:
                import json
                value = json.loads(retry)
                if datetime.fromisoformat(value["after"]) > now:
                    continue
            if manager and manager.has_pinned(object_type, object_id):
                skipped += 1
                continue
            try:
                self._cleanup_object(object_type, object_id, owner)
                with self.db.session() as sess:
                    row = sess.get(DeletionTombstoneRow, tombstone_id)
                    if row is not None:
                        row.cleanup_stage = "cleaned"
                    retry_row = sess.get(SystemStateRow, retry_key)
                    if retry_row:
                        sess.delete(retry_row)
                    if self.audit is not None:
                        self.audit.append("cleanup.completed", actor_id="system:cleanup",
                            object_type=object_type, object_id=object_id, result="cleaned", session=sess)
                    sess.commit()
                cleaned += 1
            except Exception as exc:  # noqa: BLE001 - 失败保留 pending 可重试
                from ..logging_audit.audit import AuditWriteError
                from .media import ReferencedMedia
                if isinstance(exc, AuditWriteError):
                    raise
                if self.audit is not None:
                    self.audit.append("cleanup.completed", actor_id="system:cleanup",
                        object_type=object_type, object_id=object_id,
                        result="deferred" if isinstance(exc, ReferencedMedia) else "failed",
                        reason="state_invalid" if isinstance(exc, ReferencedMedia) else "INTERNAL_ERROR")
                import json
                count = json.loads(retry).get("attempts", 0)+1 if retry else 1
                delay = (60, 300, 1800, 7200)[min(count-1, 3)] if count <= 4 else 86400
                # Only managed automatic runs back off; explicit retries remain immediate.
                if manager:
                    self.set_state(retry_key, json.dumps({"attempts": count,
                        "after": (now+timedelta(seconds=delay)).isoformat()}))
                skipped += 1
        if manager and not targets:
            self.set_state("local_tombstone_cursor", "")
        return {"cleaned": cleaned, "skipped": skipped}

    def _cleanup_object(self, object_type: str, object_id: str, owner: str) -> None:
        """重放/清理：确保对象回到「已删除且不可访问」状态（DB + 受控对象）。"""
        from ..materials.chunk_index import ChunkIndexService
        from ..materials.models import MaterialRow
        from ..planning.models import ConversationRow, CourseRow, LearningRouteRow

        now = utcnow()
        with self.db.session() as sess:
            model = {"material": MaterialRow, "route": LearningRouteRow,
                     "course": CourseRow, "conversation": ConversationRow}.get(object_type)
            if model:
                target = sess.get(model, object_id, with_for_update=True)
                if target and target.owner_user_id != owner:
                    raise StorageError("invalid_object_key")
            elif object_type != "character_card":
                raise StorageError("invalid_object_key")
            if object_type == "material":
                row = sess.get(MaterialRow, object_id)
                if row is not None and row.deleted_at is None:
                    row.deleted_at = now  # 重放：重新删除被恢复出来的对象
                from ..materials.models import DocumentAttachmentRow
                attachment = sess.get(DocumentAttachmentRow, object_id)
                if attachment:
                    attachment.result_json = "{}"
                from ..materials.source_models import ImportedSourceRow
                imported = sess.get(ImportedSourceRow, object_id)
                if imported:
                    imported.result_json = "{}"
            elif object_type == "route":
                row = sess.get(LearningRouteRow, object_id)
                if row is not None and row.deleted_at is None:
                    row.deleted_at = now
            elif object_type == "course":
                row = sess.get(CourseRow, object_id)
                if row is not None and row.deleted_at is None:
                    row.deleted_at = now
                    from ..planning.deletion import _purge_course
                    _purge_course(sess, object_id, owner)
            elif object_type == "conversation":
                row = sess.get(ConversationRow, object_id)
                if row is not None and row.deleted_at is None:
                    row.deleted_at = now
            elif object_type == "character_card":
                from ..characters.models import CharacterCardRow
                card = sess.get(CharacterCardRow, object_id, with_for_update=True)
                if card and card.owner_user_id != owner:
                    raise StorageError("invalid_object_key")
                if card:
                    card.deleted_at = card.deleted_at or now
            if object_type in ("route", "course"):
                from ..planning.deletion import delete_feature_data
                delete_feature_data(sess, object_type, object_id)
            # Access revocation survives a deferred physical collection on restore.
            sess.commit()
            if self.storage is not None:
                from .media import cleanup_audio, cleanup_character
                if object_type == "conversation":
                    cleanup_audio(sess, self.storage, owner=owner, conversation_id=object_id)
                elif object_type == "course":
                    cleanup_audio(sess, self.storage, owner=owner, course_id=object_id)
                elif object_type == "character_card":
                    cleanup_character(sess, self.storage, object_id, owner)
            sess.commit()
        if object_type == "material":
            ChunkIndexService(self.db).delete_for_material(
                object_id, owner_user_id=owner, reason="restore_replay"
            )
        if self.storage is not None and object_type == "material":
            self.storage.delete(MaterialStorage.original_key(object_id))
            self._delete_prefix(f"fig/{object_id}/")
            self.storage.delete_source_assets(object_id)

    def _delete_prefix(self, prefix: str) -> None:
        if self.storage is None:
            return
        for key, _ in self.storage.iter_files(prefix, reject_links=True):
            self.storage.delete(key)

    # ------------------------------------------------------------------
    # 备份轮换（30 天保留；排除已删除满 30 天的数据）
    # ------------------------------------------------------------------
    def create_backup(self) -> dict:
        """Stream an object-volume backup; database snapshots remain external."""
        from .backup import create_object_backup
        self.require_not_locked()
        try:
            manager = getattr(self, "resource_manager", None)
            if manager:
                return manager.create_backup()
            return create_object_backup(self, BACKUP_RETENTION_DAYS)
        except (OSError, StorageError):
            if self.audit is not None:
                self.audit.append("cleanup.completed", actor_id="system:cleanup",
                    object_type="backup", object_id="backup", result="failed",
                    reason="INTERNAL_ERROR")
            raise cerr("INTERNAL_ERROR", "Object backup failed", status=500) from None

    # ------------------------------------------------------------------
    # 恢复：restore_locked → 墓碑重放 → 核验 → 解锁
    # ------------------------------------------------------------------
    def begin_restore(self, *, operator_id: str) -> dict:
        self.set_state(RESTORE_LOCK_KEY, "1")
        self._audit("restore.locked", operator_id, "locked")
        return {"restore_locked": True}

    def replay_tombstones(self, *, operator_id: str) -> dict:
        """从独立墓碑源重放：被恢复出来的已删对象重新删除；核验计数返回。"""
        if not self.is_restore_locked():
            raise cerr("restore_not_locked", "begin restore first", status=409)
        with self.db.session() as sess:
            tombstones = (
                sess.execute(select(DeletionTombstoneRow))
                .scalars()
                .all()
            )
            total = len(tombstones)
            conflicts = 0
            for t in tombstones:
                alive = self._object_alive(t.object_type, t.object_id, t.owner_user_id)
                if alive:
                    conflicts += 1
                from .media import ReferencedMedia
                try:
                    self._cleanup_object(t.object_type, t.object_id, t.owner_user_id)
                except ReferencedMedia:
                    # Shared media may outlive the deleted resource; access stays revoked.
                    pass
        self._audit("restore.replayed", operator_id, f"tombstones={total},conflicts={conflicts}")
        return {"tombstones": total, "conflicts_replayed": conflicts}

    def verify_and_unlock(self, *, operator_id: str) -> dict:
        """核验：所有墓碑对象确认不可访问后才解锁。"""
        if not self.is_restore_locked():
            raise cerr("restore_not_locked", "begin restore first", status=409)
        with self.db.session() as sess:
            tombstones = (
                sess.execute(select(DeletionTombstoneRow))
                .scalars()
                .all()
            )
            remaining = 0
            for t in tombstones:
                if self._object_alive(t.object_type, t.object_id, t.owner_user_id):
                    remaining += 1
        if remaining:
            self._audit("restore.verify_failed", operator_id, f"alive={remaining}")
            raise cerr("restore_verify_failed", f"{remaining} deleted objects alive", status=409)
        self.set_state(RESTORE_LOCK_KEY, "0")
        self._audit("restore.unlocked", operator_id, "verified")
        return {"restore_locked": False, "verified": True}

    def _object_alive(self, object_type: str, object_id: str, owner: str) -> bool:
        with self.db.session() as sess:
            from ..materials.models import MaterialRow
            from ..planning.models import ConversationRow, CourseRow, LearningRouteRow

            if object_type == "material":
                row = sess.get(MaterialRow, object_id)
                return row is not None and row.deleted_at is None
            if object_type == "route":
                row = sess.get(LearningRouteRow, object_id)
                return row is not None and row.deleted_at is None
            if object_type == "course":
                row = sess.get(CourseRow, object_id)
                return row is not None and row.deleted_at is None
            if object_type == "conversation":
                row = sess.get(ConversationRow, object_id)
                return row is not None and row.deleted_at is None
            if object_type == "character_card":
                from ..characters.models import CharacterCardRow
                row = sess.get(CharacterCardRow, object_id)
                return row is not None and row.deleted_at is None
            return False

    def _audit(self, event: str, operator_id: str, result: str) -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(
                event, actor_id=operator_id, object_type="system",
                object_id=event, result=result or "ok",
            )
        except Exception:  # noqa: BLE001 - 审计失败不吞业务结果但必须可见
            raise


__all__ = [
    "CleanupService", "CleanupError", "cerr",
    "ONLINE_CLEANUP_DELAY_SECONDS", "BACKUP_RETENTION_DAYS", "RESTORE_LOCK_KEY",
]
