"""Durable, bounded local maintenance and private storage management."""
import hashlib
import json
import os
from pathlib import Path
from threading import Event, Thread
from time import monotonic
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, func, text, inspect, or_

from ..logging_audit.audit import AuditWriteError
from ..materials.storage import StorageError
from ..tasks.service import LearningTaskRow
from ..teaching.learning_models import PreparedFirstSegmentRow
from .budget import ResourceBudget, disk_state, GIB
from .inventory import describe
from .models import LocalResourceRow as Object, LocalResourceJobRow as Job, LocalResourcePolicyRow as Policy, BackupRunRow, utcnow
from .policy import ResourcePolicy, aware


def _id(prefix):
    return prefix + uuid.uuid4().hex


class LocalResourceManager:
    def __init__(self, state):
        self.state, self.db = state, state.db
        self.storage, self.cleanup = state.storage, state.cleanup
        self.audit = state.audit
        self.budget = ResourceBudget(self.storage)
        self.stop = Event()
        self.thread = None
        self.error = None
        self.boot = _id("boot_")
        self.started_at = monotonic()
        self.last_schedule = 0
        self.last_access = {}

    def require_ready(self):
        if not inspect(self.db.engine).has_table(Policy.__tablename__):
            raise StorageError("local_resources_migration_required")

    def policy(self, session=None):
        if session is None:
            with self.db.session() as owned:
                return self.policy(owned)
        row = session.get(Policy, "local")
        return ResourcePolicy.model_validate_json(row.settings_json) if row else ResourcePolicy()

    def save_policy(self, owner, values, version):
        with self.db.session() as session:
            row = session.get(Policy, "local", with_for_update=True)
            if not row:
                row = Policy(key="local", version=0)
                session.add(row)
                session.flush()
            if row.version != version:
                raise StorageError("local_resource_conflict")
            row.settings_json = ResourcePolicy.model_validate(values).model_dump_json()
            row.version += 1
            self._audit(session, owner, "local_policy", "updated", "config.changed")
            return row.version

    def _audit(self, session, owner, ident, result, event="cleanup.completed"):
        if self.audit is not None:
            self.audit.append(event, actor_id=owner, object_type="local_resource",
                object_id=ident, result=result, session=session)

    def observe(self, storage, key, access=False):
        if access and monotonic() - self.last_access.get(key, 0) < 30:
            return True
        try:
            stat = storage._resolve(key).stat()
            with self.db.session() as session:
                row = session.scalar(select(Object).where(
                    Object.volume_id == storage.location.volume_id, Object.object_key == key).with_for_update())
                kind, owner, refs = describe(session, key, backup=storage.location.volume_id != self.storage.location.volume_id)
                if row is None:
                    row = Object(resource_id=_id("res_"), volume_id=storage.location.volume_id,
                        object_key=key, kind=kind, owner_user_id=owner)
                    session.add(row)
                elif row.mtime_ns != str(stat.st_mtime_ns) or row.size_bytes != stat.st_size:
                    row.first_seen_at = utcnow()
                changed = row.mtime_ns != str(stat.st_mtime_ns) or row.size_bytes != stat.st_size
                row.kind, row.owner_user_id, row.reference_count = kind, owner, refs
                row.mtime_ns, row.size_bytes = str(stat.st_mtime_ns), stat.st_size
                row.observed_at, row.state = utcnow(), "present"
                if access or changed:
                    row.last_used_at = utcnow()
            if access:
                if len(self.last_access) > 2048:
                    self.last_access.clear()
                self.last_access[key] = monotonic()
            return True
        except FileNotFoundError:
            return True
        except Exception:
            self.error = "ledger_write_failed"
            return False

    def enqueue(self, owner, kind, request=None, request_key=None):
        if kind not in ("scan", "cleanup", "backup", "migrate"):
            raise StorageError("invalid_object_key")
        self.cleanup.require_not_locked()
        with self.db.session() as session:
            if session.bind.dialect.name == "postgresql":
                session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 61021002})
            if request_key:
                existing = session.scalar(select(Job).where(Job.request_key == request_key))
                if existing:
                    return existing.job_id
            if session.scalar(select(func.count()).select_from(Job).where(
                    Job.status.in_(("queued", "running", "retry")))) >= 20:
                raise StorageError("local_resource_queue_full")
            row = Job(job_id=_id("resjob_"), owner_user_id=owner, kind=kind,
                request_key=request_key or _id("request_"), request_json=json.dumps(request or {}))
            session.add(row)
            self._audit(session, owner, row.job_id, "queued")
            return row.job_id

    def job_view(self, row):
        return {name: getattr(row, name) for name in (
            "job_id", "kind", "status", "progress", "total", "error_code", "attempts")}

    def snapshot(self, owner, admin=False):
        with self.db.session() as session:
            query = select(Object.kind, func.sum(Object.size_bytes), func.count()).where(Object.state != "missing")
            if not admin:
                query = query.where(Object.owner_user_id == owner)
            groups = session.execute(query.group_by(Object.kind)).all()
            query = select(Object).where(Object.state != "missing")
            if not admin:
                query = query.where(Object.owner_user_id == owner)
            objects = list(session.scalars(query.order_by(Object.last_used_at.desc()).limit(100)))
            jobs = select(Job).order_by(Job.created_at.desc()).limit(20)
            if not admin:
                jobs = jobs.where(Job.owner_user_id == owner)
            settings = session.get(Policy, "local")
            result = {
                "categories": [{"kind": kind, "bytes": int(size), "count": count} for kind, size, count in groups],
                "objects": [{"resource_id": row.resource_id, "kind": row.kind, "bytes": row.size_bytes,
                    "pinned": row.pinned, "references": row.reference_count, "state": row.state}
                    for row in objects],
                "jobs": [self.job_view(row) for row in session.scalars(jobs)],
                "successful_starts": settings.successful_starts if settings else 0,
                "observation_only": not settings or settings.successful_starts < 7,
                "audio_warning": any(kind == "audio" and size >= self.policy(session).audio_warning_mib*1024**2
                    for kind, size, count in groups),
                "policy": self.policy(session).model_dump() if admin else None,
                "version": settings.version if settings else 0, "error_code": self.error,
            }
        if admin:
            result.update(runtime=self.budget.snapshot(), directory=str(self.storage.base_dir),
                backup_directory=str(self.cleanup.backup_storage.base_dir),
                old_directories=self.storage.location.config().get("old_roots", []))
            with self.db.session() as session:
                if session.bind.dialect.name == "postgresql":
                    size = session.scalar(text(
                        "SELECT coalesce(sum(pg_total_relation_size(c.oid)),0) FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname=current_schema() AND c.relkind IN ('r','m')"))
                    result["database_bytes"] = int(size)
        return result

    def pin(self, owner, ident, value, admin=False):
        with self.db.session() as session:
            row = session.get(Object, ident, with_for_update=True)
            if row is None or not admin and row.owner_user_id != owner:
                raise StorageError("local_resource_not_found")
            row.pinned = value
            self._audit(session, owner, ident, "pinned" if value else "unpinned", "config.changed")

    def has_pinned(self, kind, ident):
        prefix = {"material": f"orig/{ident}.bin", "conversation": f"audio/{ident}/",
                  "character_card": f"characters/{ident}/"}.get(kind)
        with self.db.session() as session:
            if kind == "course":
                from ..planning.models import ConversationRow
                ids = list(session.scalars(select(ConversationRow.conversation_id).where(ConversationRow.course_id == ident)))
                return any(self.has_pinned("conversation", item) for item in ids)
            if not prefix:
                return False
            if kind == "conversation":
                from ..teaching.models import AudioAssetRow
                keys = select(AudioAssetRow.storage_key).where(AudioAssetRow.conversation_id == ident)
                if session.scalar(select(Object.resource_id).where(
                        Object.object_key.in_(keys), Object.pinned.is_(True), Object.state != "missing").limit(1)):
                    return True
                if session.scalar(select(Object.resource_id).where(
                        Object.volume_id == self.storage.location.volume_id,
                        Object.object_key.like(f"recordings/{ident}/%"),
                        Object.pinned.is_(True), Object.state != "missing").limit(1)):
                    return True
            return bool(session.scalar(select(Object.resource_id).where(Object.pinned.is_(True),
                Object.state != "missing",
                Object.volume_id == self.storage.location.volume_id,
                Object.object_key.like(prefix+"%") if prefix.endswith("/") else Object.object_key == prefix).limit(1)))

    def candidates(self, owner=None):
        now = utcnow()
        with self.db.session() as session:
            policy = self.policy(session)
            rows = list(session.scalars(select(Object).where(
                Object.pinned.is_(False), Object.reference_count == 0, Object.owner_user_id.is_not(None),
                Object.state != "missing", Object.kind.in_(("prewarm", "downloads", "temporary", "audio", "characters", "materials", "recordings")),
                Object.owner_user_id == owner if owner else True,
                or_(Object.next_retry_at.is_(None), Object.next_retry_at <= now)
            ).order_by(Object.last_used_at).limit(10000)))
            totals, counts = {}, {}
            for kind, size, count in session.execute(select(Object.kind, func.sum(Object.size_bytes), func.count())
                    .where(Object.state != "missing").group_by(Object.kind)):
                totals[kind], counts[kind] = size, count
            chosen = []
            for row in rows:
                age = (now - aware(row.last_used_at)).total_seconds()
                observed = (now - aware(row.first_seen_at)).total_seconds()
                limit = policy.prewarm_mib * 1024**2 if row.kind == "prewarm" else policy.download_mib * 1024**2
                eligible = False
                if row.kind in ("prewarm", "downloads"):
                    days = policy.prewarm_days if row.kind == "prewarm" else policy.download_days
                    eligible = age >= days * 86400 or totals.get(row.kind, 0) > limit or (
                        row.kind == "prewarm" and counts.get(row.kind, 0) > policy.prewarm_entries)
                elif row.kind == "temporary":
                    eligible = age >= policy.temporary_hours * 3600
                else:
                    eligible = observed >= policy.orphan_days * 86400
                if eligible and observed >= min(86400, policy.orphan_days * 86400):
                    chosen.append(row.resource_id)
                    totals[row.kind] = totals.get(row.kind, 0) - row.size_bytes
                    counts[row.kind] = counts.get(row.kind, 0) - 1
            return chosen

    def preview(self, owner, admin=False):
        ids = self.candidates(None if admin else owner)[:200]
        with self.db.session() as session:
            objects = list(session.scalars(select(Object).where(Object.resource_id.in_(ids))))
            return {"resource_ids": ids, "bytes": sum(row.size_bytes for row in objects),
                    "count": len(ids)}

    def _store(self, volume):
        return self.storage if volume == self.storage.location.volume_id else self.cleanup.backup_storage

    def scan_batch(self, data, *, limit=200, seconds=2):
        started = monotonic()
        data.setdefault("started_at", utcnow().isoformat())
        volume = data.get("volume", "objects")
        if volume == "verify":
            with self.db.session() as session:
                rows = list(session.scalars(select(Object).where(
                    Object.volume_id.in_((self.storage.location.volume_id,
                        self.cleanup.backup_storage.location.volume_id)),
                    Object.observed_at < datetime.fromisoformat(data["started_at"]),
                    Object.state != "missing",
                    Object.resource_id > data.get("verify_cursor","")
                ).order_by(Object.resource_id).limit(limit)))
                for row in rows:
                    if monotonic()-started >= seconds:
                        return False, 0
                    try:
                        self._store(row.volume_id)._resolve(row.object_key).stat()
                    except FileNotFoundError:
                        row.state, row.size_bytes, row.observed_at = "missing", 0, utcnow()
                    data["verify_cursor"] = row.resource_id
                if rows:
                    return False, len(rows)
            with self.db.session() as session:
                settings = session.get(Policy, "local", with_for_update=True)
                if settings is None:
                    settings = Policy(key="local", version=0, settings_json="{}",
                        successful_starts=0, last_boot="")
                    session.add(settings)
                if settings.last_boot != self.boot:
                    settings.successful_starts += 1
                    settings.last_boot = self.boot
            return True, 0
        storage = self.storage if volume == "objects" else self.cleanup.backup_storage
        cursor, passed, count = data.get("cursor"), not data.get("cursor"), 0
        for key, _ in storage.iter_files():
            if not passed:
                passed = key == cursor
                continue
            if not self.observe(storage, key):
                raise StorageError("local_resource_ledger_failed")
            data["cursor"] = key
            count += 1
            if count >= limit or monotonic() - started >= seconds:
                return False, count
        if not passed:
            data.pop("cursor", None)
            return False, 0
        if volume == "objects":
            data.update(volume="backups", cursor=None)
            return False, count
        data.update(volume="verify", cursor=None)
        return False, count

    def collect_one(self, ident, owner, admin=False):
        storage = self.storage
        with storage.location.lock, self.db.session() as session:
            row = session.get(Object, ident, with_for_update=True)
            if not row or row.pinned or row.state == "missing" or not admin and row.owner_user_id != owner:
                return 0
            if row.volume_id != storage.location.volume_id or row.kind not in (
                    "prewarm", "downloads", "temporary", "audio", "characters", "materials", "recordings"):
                return 0
            kind, actual_owner, refs = describe(session, row.object_key)
            if refs or not actual_owner or actual_owner != row.owner_user_id or kind != row.kind:
                row.reference_count = refs
                return 0
            # A live owner task conservatively protects all of that owner's intermediates.
            active = session.scalar(select(LearningTaskRow.task_id).where(
                LearningTaskRow.owner_user_id == actual_owner,
                LearningTaskRow.status.in_(("leased", "running")),
                LearningTaskRow.lease_until > utcnow()).limit(1))
            if active:
                return 0
            path = storage._resolve(row.object_key)
            try:
                stat = path.stat()
            except FileNotFoundError:
                row.state, row.size_bytes, row.observed_at = "missing", 0, utcnow()
                return 0
            if row.mtime_ns != str(stat.st_mtime_ns) or row.size_bytes != stat.st_size:
                row.first_seen_at = utcnow()
                return 0
            # Recheck live ownership/reference/pin in the same locked transaction.
            if kind == "prewarm":
                for prepared in session.scalars(select(PreparedFirstSegmentRow).where(
                        PreparedFirstSegmentRow.audio_object_key == row.object_key).with_for_update()):
                    if prepared.consumed_conversation_id:
                        return 0
                    if prepared.persistent:
                        prepared.audio_object_key=""
                        prepared.audio_bytes=0
                        prepared.audio_json="{}"
                        prepared.error_code=None
                        prepared.status="text_ready"
                    else:
                        session.delete(prepared)
            self._audit(session, owner, ident, "delete_started")
            storage.delete(row.object_key)
            row.state, row.size_bytes, row.observed_at = "missing", 0, utcnow()
            self._audit(session, owner, ident, "cleaned")
            return stat.st_size

    def _cleanup_batch(self, data, owner):
        if "resource_ids" not in data:
            data["resource_ids"] = self.candidates()
        ids = data["resource_ids"]
        eligible = set(self.candidates(None if data.get("global") else owner))
        cursor, released = data.get("cursor", 0), data.get("released_bytes", 0)
        started = monotonic()
        checked = 0
        while cursor < len(ids) and checked < 200 and monotonic() - started < 2:
            ident = ids[cursor]
            try:
                if ident in eligible:
                    released += self.collect_one(ident, owner, admin=data.get("global", False))
            except AuditWriteError:
                raise
            except (OSError, StorageError):
                with self.db.session() as session:
                    row = session.get(Object, ident, with_for_update=True)
                    if row:
                        row.attempts += 1
                        row.state = "retry"
                        delay = (60, 300, 1800, 7200)[min(row.attempts-1, 3)] if row.attempts <= 4 else 86400
                        row.next_retry_at = utcnow()+timedelta(seconds=delay)
                        self._audit(session, owner, ident, "failed")
            cursor += 1
            checked += 1
        data.update(cursor=cursor, released_bytes=released)
        return cursor >= len(ids), checked

    def migrate(self, target, data, progress=None):
        location = self.storage.location
        destination = Path(target)
        with location.lock:
            source = self.storage.base_dir
            if data.get("switched") or source.resolve() == destination.resolve():
                data["switched"] = True
                return
            marker = location.config().get("migration", {})
            if marker.get("destination") != str(destination):
                self.validate_destination(str(destination))
                location.save(migration={"destination":str(destination), "source":str(source)})
            for ancestor in (destination,*destination.parents):
                if ancestor.is_symlink() or ancestor.is_junction():
                    raise StorageError("invalid_object_key")
            destination.mkdir(parents=True, exist_ok=True)
            for key, path in self.storage.iter_files(reject_links=True):
                if self.stop.is_set():
                    raise StorageError("local_resource_interrupted")
                output = destination / key
                for ancestor in (output,*output.parents):
                    if ancestor == destination.parent:
                        break
                    if ancestor.is_symlink() or ancestor.is_junction():
                        raise StorageError("invalid_object_key")
                output.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                temporary = output.with_name(output.name + ".part")
                with path.open("rb") as incoming, temporary.open("wb") as outgoing:
                    while chunk := incoming.read(1024 * 1024):
                        if self.stop.is_set():
                            raise StorageError("local_resource_interrupted")
                        outgoing.write(chunk)
                        digest.update(chunk)
                check = hashlib.sha256()
                with temporary.open("rb") as incoming:
                    while chunk := incoming.read(1024 * 1024):
                        check.update(chunk)
                if check.digest() != digest.digest():
                    raise StorageError("local_resource_checksum_failed")
                os.replace(temporary, output)
                data["copied"] = data.get("copied", 0)+1
                if progress:
                    progress(data["copied"])
            # Keep originals. In-flight readers may still hold their old local paths.
            backup_root = str(self.cleanup.backup_storage.base_dir)
            old = location.config().get("old_roots", [])
            location.save(root=str(destination), old_roots=[*old, str(source)], backup_root=backup_root,
                migration={})
            data["switched"] = True

    def validate_destination(self, target):
        path = Path(target)
        if not path.is_absolute():
            raise StorageError("invalid_object_key")
        for ancestor in (path, *path.parents):
            if ancestor.is_symlink() or ancestor.is_junction():
                raise StorageError("invalid_object_key")
        path = path.resolve()
        forbidden = (self.storage.base_dir, self.cleanup.backup_storage.base_dir, self.storage.location.control)
        if path == Path(path.anchor) or any(path == root or path.is_relative_to(root) or root.is_relative_to(path)
                for root in forbidden):
            raise StorageError("invalid_object_key")
        if path.exists() and any(path.iterdir()):
            raise StorageError("local_resource_destination_not_empty")
        parent = path.parent
        while not parent.exists():
            parent = parent.parent
        with self.db.session() as session:
            size = session.scalar(select(func.coalesce(func.sum(Object.size_bytes), 0)).where(
                Object.volume_id == self.storage.location.volume_id, Object.state != "missing"))
        if disk_state(parent, required=int(size))["state"] == "protected":
            raise StorageError("local_storage_full")
        return str(path)

    def create_backup(self):
        from .backup import create_object_backup
        policy = self.policy()
        destination = self.cleanup.backup_storage
        with self.db.session() as session:
            size = session.scalar(select(func.coalesce(func.sum(Object.size_bytes), 0)).where(
                Object.volume_id == self.storage.location.volume_id, Object.state != "missing"))
            used = session.scalar(select(func.coalesce(func.sum(BackupRunRow.payload_bytes), 0)))
        # Conservative reservation: incompressible data plus bounded archive overhead.
        required = int(size) + 16 * 1024**2
        if used+required > policy.backup_gib*GIB or disk_state(destination.base_dir, required=required)["state"] != "normal":
            raise StorageError("local_backup_budget_full")
        result = create_object_backup(self.cleanup, 60)
        self.cleanup.set_state("local_last_backup_fingerprint", self.fingerprint())
        self.observe(destination, result["backup_key"])
        with self.db.session() as session:
            runs = list(session.scalars(select(BackupRunRow).order_by(BackupRunRow.created_at.desc())))
            days, weeks = set(), set()
            for index, row in enumerate(runs):
                when = aware(row.created_at)
                day, week = when.strftime("%Y-%m-%d"), when.strftime("%G-%V")
                pinned = session.scalar(select(Object.resource_id).where(
                    Object.object_key == row.backup_key, Object.pinned.is_(True)))
                retain = index == 0 or pinned or (
                    when >= utcnow()-timedelta(days=7) and day not in days and len(days) < 7) or (
                    when >= utcnow()-timedelta(days=60) and week not in weeks and len(weeks) < 4)
                if retain:
                    days.add(day)
                    weeks.add(week)
                    continue
                try:
                    destination.delete(row.backup_key)
                except OSError:
                    continue
                self._audit(session, "system:cleanup", row.run_id, "rotated")
                session.delete(row)
        return result

    def fingerprint(self):
        digest = hashlib.sha256()
        with self.db.session() as session:
            for key, size, stamp in session.execute(select(Object.object_key, Object.size_bytes, Object.mtime_ns).where(
                    Object.volume_id == self.storage.location.volume_id, Object.state != "missing")
                    .order_by(Object.object_key)):
                digest.update(json.dumps([key,size,stamp]).encode())
        return digest.hexdigest()

    def tick(self):
        from filelock import FileLock, Timeout
        lock = FileLock(self.storage.location.control / "maintenance.lock", timeout=0)
        try:
            with lock:
                return self._tick()
        except Timeout:
            return

    def _tick(self):
        if self.cleanup.is_restore_locked():
            return
        with self.db.session() as session:
            if session.bind.dialect.name == "postgresql":
                if not session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": 61021003}):
                    return
            row = session.scalar(select(Job).where(
                or_(Job.status.in_(("queued", "retry")),
                    (Job.status == "running") & (Job.lease_until < utcnow())),
                or_(Job.next_retry_at.is_(None), Job.next_retry_at <= utcnow())
            ).order_by(Job.created_at).limit(1).with_for_update(skip_locked=True))
            if not row:
                return
            if row.kind == "backup" and not self.budget.permits_heavy():
                return
            row.status, row.lease_until = "running", utcnow()+timedelta(minutes=5)
            row.attempts += 1
            job_id, kind, owner, data = row.job_id, row.kind, row.owner_user_id, json.loads(row.request_json)
        try:
            if kind == "scan":
                done, count = self.scan_batch(data)
            elif kind == "cleanup":
                done, count = self._cleanup_batch(data, owner)
                if data.get("global"):
                    self.cleanup.run_online_cleanup(limit=200, seconds=2)
            elif kind == "backup":
                data["backup"] = self.create_backup()
                done, count = True, 1
            else:
                def progress(count):
                    with self.db.session() as session:
                        row = session.get(Job, job_id, with_for_update=True)
                        row.progress = count
                        row.lease_until = utcnow()+timedelta(minutes=5)
                self.migrate(data["destination"], data, progress=progress)
                done, count = True, 0
            with self.db.session() as session:
                row = session.get(Job, job_id, with_for_update=True)
                row.request_json, row.result_json = json.dumps(data), json.dumps({
                    key: value for key, value in data.items() if key in ("released_bytes", "copied", "backup")})
                row.progress += count
                row.status, row.error_code = ("succeeded" if done else "queued"), None
                row.updated_at = utcnow()
            if done:
                self.error = None
        except Exception as exc:
            with self.db.session() as session:
                row = session.get(Job, job_id, with_for_update=True)
                row.status = "failed" if row.attempts >= 5 or isinstance(exc, AuditWriteError) else "retry"
                row.error_code = "AUDIT_WRITE_FAILED" if isinstance(exc, AuditWriteError) else "LOCAL_RESOURCE_FAILED"
                row.next_retry_at = utcnow()+timedelta(seconds=(60, 300, 1800, 7200)[min(row.attempts-1, 3)])
                row.updated_at = utcnow()
            self.error = row.error_code

    def prune_metadata(self, *, limit=200):
        from sqlalchemy import delete
        cutoff = utcnow()-timedelta(days=30)
        with self.db.session() as session:
            jobs = list(session.scalars(select(Job.job_id).where(
                Job.status.in_(("succeeded", "failed")), Job.updated_at < cutoff
            ).order_by(Job.updated_at).limit(limit)))
            if jobs:
                session.execute(delete(Job).where(Job.job_id.in_(jobs)))
            objects = list(session.scalars(select(Object.resource_id).where(
                Object.state == "missing", Object.pinned.is_(False),
                Object.observed_at < cutoff,
                Object.volume_id.in_((self.storage.location.volume_id,
                    self.cleanup.backup_storage.location.volume_id))
            ).order_by(Object.observed_at).limit(limit)))
            if objects:
                session.execute(delete(Object).where(Object.resource_id.in_(objects)))
            if jobs or objects:
                self._audit(session, "system:cleanup", "local_metadata", "pruned")
            return len(jobs), len(objects)

    def schedule(self):
        if self.cleanup.is_restore_locked():
            return
        now = utcnow()
        policy = self.policy()
        self.prune_metadata()
        preparation = getattr(getattr(self.state, "teaching", None), "preparation", None)
        if preparation:
            preparation.max_bytes = policy.prewarm_mib*1024**2
            preparation.max_entries = policy.prewarm_entries
            preparation.ttl_days = policy.prewarm_days
        self.enqueue("system:cleanup", "scan", request_key=f"scan:{self.boot}")
        self.enqueue("system:cleanup", "scan", request_key="scan:"+now.strftime("%Y-%m-%d"))
        with self.db.session() as session:
            settings = session.get(Policy, "local")
            warmed = settings and settings.successful_starts >= 7
            active = session.scalar(select(func.count()).select_from(LearningTaskRow).where(
                LearningTaskRow.status.in_(("leased", "running")), LearningTaskRow.lease_until > now))
        if policy.automatic_cleanup and warmed and not self.error:
            self.enqueue("system:cleanup", "cleanup", {"global": True},
                request_key=f"cleanup:{int(now.timestamp())//900}")
        if policy.automatic_backup and not active:
            changed = self.fingerprint() != self.cleanup.get_state("local_last_backup_fingerprint")
            if changed:
                self.enqueue("system:cleanup", "backup", request_key="backup:"+now.strftime("%Y-%m-%d"))

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        try:
            self.require_ready()
        except StorageError:
            self.error = "local_resources_migration_required"
            return
        self.storage.location.observer = self.observe
        self.storage.location.write_guard = self.budget.check_write
        self.budget.start()
        self.error = None
        self.cleanup.resource_manager = self
        self.state.tasks.resource_budget = self.budget
        self.stop.clear()
        self.started_at = monotonic()
        def loop():
            while not self.stop.is_set():
                if monotonic()-self.started_at >= 60:
                    try:
                        if monotonic()-self.last_schedule > 60:
                            self.schedule()
                            self.last_schedule = monotonic()
                        self.tick()
                    except Exception:
                        self.error = "LOCAL_RESOURCE_FAILED"
                self.stop.wait(1)
        self.thread = Thread(target=loop, name="local-resource-maintenance", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        self.budget.close()
        if self.storage.location.observer == self.observe:
            self.storage.location.observer = None
            self.storage.location.write_guard = None
