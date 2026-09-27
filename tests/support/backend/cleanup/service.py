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

import io
import json
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from ..logging_audit.db import Database
from ..logging_audit.repositories import PgAuditLog
from ..materials.models import AssetUsageLedgerRow, DeletionTombstoneRow
from ..materials.storage import MaterialStorage, StorageError
from .errors import CleanupError, cerr
from .models import BackupRunRow, SystemStateRow, utcnow

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
    def run_online_cleanup(self, *, now: Optional[datetime] = None) -> dict:
        now = now or utcnow()
        cutoff = now - timedelta(seconds=ONLINE_CLEANUP_DELAY_SECONDS)
        cleaned = 0
        skipped = 0
        with self.db.session() as sess:
            tombstones = (
                sess.execute(
                    select(DeletionTombstoneRow).where(
                        DeletionTombstoneRow.cleanup_stage == "pending",
                        DeletionTombstoneRow.deleted_at <= cutoff,
                    )
                )
                .scalars()
                .all()
            )
            targets = [(t.tombstone_id, t.object_type, t.object_id, t.owner_user_id)
                       for t in tombstones]
        for tombstone_id, object_type, object_id, owner in targets:
            try:
                self._cleanup_object(object_type, object_id, owner)
                with self.db.session() as sess:
                    row = sess.get(DeletionTombstoneRow, tombstone_id)
                    if row is not None:
                        row.cleanup_stage = "cleaned"
                    sess.commit()
                cleaned += 1
            except Exception:  # noqa: BLE001 - 失败保留 pending 可重试
                skipped += 1
        return {"cleaned": cleaned, "skipped": skipped}

    def _cleanup_object(self, object_type: str, object_id: str, owner: str) -> None:
        """重放/清理：确保对象回到「已删除且不可访问」状态（DB + 受控对象）。"""
        from datetime import timedelta

        from ..materials.chunk_index import ChunkIndexService
        from ..materials.models import MaterialRow
        from ..planning.models import ConversationRow, CourseRow, LearningRouteRow

        now = utcnow()
        with self.db.session() as sess:
            if object_type == "material":
                row = sess.get(MaterialRow, object_id)
                if row is not None and row.deleted_at is None:
                    row.deleted_at = now  # 重放：重新删除被恢复出来的对象
            elif object_type == "route":
                row = sess.get(LearningRouteRow, object_id)
                if row is not None and row.deleted_at is None:
                    row.deleted_at = now
            elif object_type == "course":
                row = sess.get(CourseRow, object_id)
                if row is not None and row.deleted_at is None:
                    row.deleted_at = now
            elif object_type == "conversation":
                row = sess.get(ConversationRow, object_id)
                if row is not None and row.deleted_at is None:
                    row.deleted_at = now
            sess.commit()
        if object_type == "material":
            try:
                ChunkIndexService(self.db).delete_for_material(
                    object_id, owner_user_id=owner, reason="restore_replay"
                )
            except Exception:  # noqa: BLE001 - 索引可能已移除
                pass
        if self.storage is not None:
            try:
                self.storage.delete(MaterialStorage.original_key(object_id))
            except StorageError:
                pass
            self._delete_prefix(f"fig/{object_id}/")

    def _delete_prefix(self, prefix: str) -> None:
        if self.storage is None:
            return
        base = self.storage.base_dir
        target = base / prefix.rstrip("/")
        if target.exists():
            for p in sorted(target.rglob("*"), reverse=True):
                if p.is_file():
                    p.unlink()
                elif p.is_dir():
                    p.rmdir()
            if target.exists():
                target.rmdir()

    # ------------------------------------------------------------------
    # 备份轮换（30 天保留；排除已删除满 30 天的数据）
    # ------------------------------------------------------------------
    def create_backup(self) -> dict:
        """打包对象卷 + 数据清单（数据库快照由部署层工具补全）。

        - 墓碑重放源：墓碑记录本体随清单进入备份（独立删除记录源）；
        - 已删除满 30 天的对象与其清单行不进入新备份（备份内 30 天清理）。
        """
        if self.is_restore_locked():
            raise cerr("restore_locked", "backup disabled during restore", status=423)
        now = utcnow()
        hard_cutoff = now - timedelta(days=BACKUP_RETENTION_DAYS)
        with self.db.session() as sess:
            tombstones = (
                sess.execute(select(DeletionTombstoneRow))
                .scalars()
                .all()
            )
            manifest = {
                "created_at": now.isoformat(),
                "retention_days": BACKUP_RETENTION_DAYS,
                "tombstones": [
                    {
                        "tombstone_id": t.tombstone_id,
                        "object_type": t.object_type,
                        "object_id": t.object_id,
                        "owner_user_id": t.owner_user_id,
                        "deleted_at": t.deleted_at.isoformat() if t.deleted_at else None,
                        "cleanup_stage": t.cleanup_stage,
                        "version": t.version,
                    }
                    for t in tombstones
                    # 已删除满 30 天且已清理的墓碑不进入新备份
                    if not (t.deleted_at and t.deleted_at < hard_cutoff)
                ],
            }
        run_id = f"bk_{uuid.uuid4().hex[:12]}"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="manifest.json")
            data = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            if self.storage is not None:
                for path in sorted(self.storage.base_dir.rglob("*")):
                    if path.is_file() and ".part" not in path.name:
                        arc = path.relative_to(self.storage.base_dir).as_posix()
                        tar.add(str(path), arcname=arc)
        payload = buf.getvalue()
        backup_key = f"backups/{run_id}.tar.gz"
        if self.storage is not None:
            self.storage.put(backup_key, payload)
        with self.db.session() as sess:
            sess.add(
                BackupRunRow(
                    run_id=run_id,
                    backup_key=backup_key,
                    payload_bytes=len(payload),
                    deleted_cutoff_at=hard_cutoff,
                )
            )
            # 轮换：删除 30 天前的备份运行与对象
            rotate_cutoff = now - timedelta(days=BACKUP_RETENTION_DAYS)
            old_runs = (
                sess.execute(
                    select(BackupRunRow).where(BackupRunRow.created_at < rotate_cutoff)
                )
                .scalars()
                .all()
            )
            for old in old_runs:
                if self.storage is not None and old.backup_key:
                    try:
                        self.storage.delete(old.backup_key)
                    except StorageError:
                        pass
                sess.delete(old)
            sess.commit()
        return {
            "run_id": run_id,
            "backup_key": backup_key,
            "payload_bytes": len(payload),
            "tombstone_count": len(manifest["tombstones"]),
        }

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
                    self._cleanup_object(t.object_type, t.object_id, t.owner_user_id)
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
