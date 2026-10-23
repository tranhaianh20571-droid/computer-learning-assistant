"""Synthetic object backup and deletion regression; never use live storage."""
import json
import os
import tarfile
import tracemalloc
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.characters.models import (
    CharacterAssetRow, CharacterBindingRow, CharacterSegmentRow,
    CharacterSystemDefaultRow, ClassroomHistoryRow,
)
from backend.characters.schemas import Profile
from backend.characters.service import CharacterService
from backend.cleanup.errors import CleanupError
from backend.cleanup.models import BackupRunRow, utcnow
from backend.cleanup.service import CleanupService
from backend.logging_audit.audit import AuditWriteError
from backend.logging_audit.db import Database
from backend.logging_audit.repositories import PgAuditLog, PgTaskEventStore
from backend.materials.models import DeletionTombstoneRow
from backend.materials.storage import MaterialStorage
from backend.planning.deletion import PlanningDeletionService
from backend.planning.models import ConversationRow, CourseRow
from backend.teaching.models import AudioAssetRow
from backend.teaching.learning_models import PreparedFirstSegmentRow
from backend.tasks.service import TaskService, LearningTaskRow


@pytest.fixture
def resources(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKUP_STORAGE_DIR", raising=False)
    db = Database()
    db.create_all()
    storage = MaterialStorage(tmp_path / "objects")
    audit = PgAuditLog(db)
    service = CleanupService(db, storage, audit)
    owner = "synthetic-" + uuid.uuid4().hex
    try:
        yield db, storage, service, owner
    finally:
        db.dispose()


def tombstone(db, kind, ident, owner):
    key = "tmb_" + uuid.uuid4().hex[:12]
    with db.session() as session:
        session.add(DeletionTombstoneRow(tombstone_id=key, object_type=kind,
            object_id=ident, owner_user_id=owner, deleted_at=utcnow()-timedelta(hours=25),
            cleanup_stage="pending"))
    return key


def stage(db, ident):
    with db.session() as session:
        return session.get(DeletionTombstoneRow, ident).cleanup_stage


def conversation(db, owner, *, deleted=True, course_id=None):
    ident = "conv_" + uuid.uuid4().hex
    with db.session() as session:
        session.add(ConversationRow(conversation_id=ident, owner_user_id=owner,
            course_id=course_id, deleted_at=utcnow() if deleted else None))
    return ident


def audio(db, storage, conv, *, key=None):
    ident = "aud_" + uuid.uuid4().hex
    key = key or f"audio/{conv}/{ident}.bin"
    storage.put(key, b"synthetic audio")
    with db.session() as session:
        session.add(AudioAssetRow(audio_id=ident, conversation_id=conv,
            generation=1, step_no=1, text_version_no=1, storage_key=key, status="ready"))
    return key, ident


def test_twenty_backups_never_embed_previous_backups(resources, monkeypatch, record_property):
    db, storage, cleanup, _ = resources
    storage.put("orig/synthetic.bin", os.urandom(64 * 1024))
    for key in ("backups/old.tar.gz", "tmp/old.bin", "cache/old.bin", "orig/partial.bin.part"):
        storage.put(key, b"excluded synthetic")
    def no_buffered_put(*args, **kwargs):
        raise AssertionError("backup must stream")
    monkeypatch.setattr(cleanup.backup_storage, "put", no_buffered_put)
    sizes = []
    for _ in range(20):
        result = cleanup.create_backup()
        path = cleanup.backup_storage.local_path(result["backup_key"])
        assert not path.is_relative_to(storage.base_dir)
        sizes.append(result["payload_bytes"])
        with tarfile.open(path, "r:gz") as archive:
            assert set(archive.getnames()) == {"manifest.json", "orig/synthetic.bin"}
    assert max(sizes) < min(sizes) * 1.05
    record_property("backup_sizes_bytes", json.dumps(sizes))


def test_large_backup_has_bounded_python_memory(resources, record_property):
    _, storage, cleanup, _ = resources
    path = storage._resolve("orig/large.bin")
    path.parent.mkdir(parents=True)
    with path.open("wb") as output:
        for _ in range(128):
            output.write(os.urandom(64 * 1024))
    tracemalloc.start()
    try:
        cleanup.create_backup()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * 1024 * 1024
    record_property("python_peak_bytes", peak)
    record_property("input_bytes", 8 * 1024 * 1024)


def test_backup_failure_never_publishes_partial_file(resources, monkeypatch):
    db, storage, cleanup, _ = resources
    storage.put("orig/synthetic.bin", b"synthetic")
    def broken(*args, **kwargs):
        raise OSError("synthetic failure")
    monkeypatch.setattr(tarfile.TarFile, "add", broken)
    with pytest.raises(CleanupError) as exc:
        cleanup.create_backup()
    assert exc.value.code == "INTERNAL_ERROR"
    assert not list(cleanup.backup_storage.base_dir.rglob("*.part"))
    assert not list(cleanup.backup_storage.base_dir.rglob("*.tar.gz"))


def test_backup_audit_failure_removes_uncommitted_output(resources, monkeypatch):
    _, storage, cleanup, _ = resources
    storage.put("orig/synthetic.bin", b"synthetic")
    def broken(*args, **kwargs):
        raise AuditWriteError("synthetic")
    monkeypatch.setattr(cleanup.audit, "append", broken)
    with pytest.raises(AuditWriteError):
        cleanup.create_backup()
    assert not list(cleanup.backup_storage.base_dir.rglob("*.tar.gz"))


def test_backup_skips_symlinks_and_cleanup_refuses_them(resources, tmp_path):
    db, storage, cleanup, owner = resources
    conv = conversation(db, owner)
    target = tmp_path / "outside.bin"
    target.write_bytes(b"synthetic outside")
    linked = storage.base_dir / f"audio/{conv}/linked.bin"
    linked.parent.mkdir(parents=True)
    try:
        linked.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    result = cleanup.create_backup()
    with tarfile.open(cleanup.backup_storage.local_path(result["backup_key"])) as archive:
        assert all(not member.issym() for member in archive)
        assert f"audio/{conv}/linked.bin" not in archive.getnames()
    ident = tombstone(db, "conversation", conv, owner)
    cleanup.run_online_cleanup()
    assert stage(db, ident) == "pending"
    assert target.read_bytes() == b"synthetic outside"
    linked.unlink()


@pytest.mark.parametrize("location", ["same", "child", "parent"])
def test_backup_rejects_overlapping_directory(resources, monkeypatch, location):
    _, storage, cleanup, _ = resources
    directory = {"same": storage.base_dir, "child": storage.base_dir / "nested",
                 "parent": storage.base_dir.parent}[location]
    monkeypatch.setenv("BACKUP_STORAGE_DIR", str(directory))
    with pytest.raises(CleanupError):
        cleanup.create_backup()


def test_rotation_handles_legacy_store_and_retries_file_failure(resources, monkeypatch):
    db, storage, cleanup, _ = resources
    old_id = "bk_" + uuid.uuid4().hex[:12]
    key = f"backups/{old_id}.tar.gz"
    storage.put(key, b"synthetic legacy backup")
    with db.session() as session:
        session.add(BackupRunRow(run_id=old_id, backup_key=key,
            created_at=utcnow()-timedelta(days=40), payload_bytes=23))
    original = storage.delete
    def blocked(candidate):
        if candidate == key:
            raise PermissionError("synthetic")
        return original(candidate)
    monkeypatch.setattr(storage, "delete", blocked)
    cleanup.create_backup()
    with db.session() as session:
        assert session.get(BackupRunRow, old_id)
    monkeypatch.setattr(storage, "delete", original)
    cleanup.create_backup()
    with db.session() as session:
        assert session.get(BackupRunRow, old_id) is None
    assert not storage._resolve(key).exists()


def test_deleted_course_collects_audio_but_preserves_active_course(resources):
    db, storage, cleanup, owner = resources
    course_id = "course_" + uuid.uuid4().hex
    with db.session() as session:
        session.add(CourseRow(course_id=course_id, owner_user_id=owner, title_ref="Synthetic"))
    conv = conversation(db, owner, deleted=False, course_id=course_id)
    key, _ = audio(db, storage, conv)
    live = conversation(db, owner, deleted=False)
    live_key, _ = audio(db, storage, live)
    PlanningDeletionService(db, cleanup.audit).delete_course(course_id, actor_id=owner)
    cleanup.run_online_cleanup(now=utcnow()+timedelta(hours=25))
    assert not storage._resolve(key).exists()
    assert storage._resolve(live_key).exists()


def test_consumed_prewarm_is_collected_and_surviving_prewarm_is_protected(resources):
    db, storage, cleanup, owner = resources
    course = "course_" + uuid.uuid4().hex
    with db.session() as session:
        session.add(CourseRow(course_id=course, owner_user_id=owner))
    conv = conversation(db, owner, course_id=course)
    cache = "cache_" + uuid.uuid4().hex
    key, _ = audio(db, storage, conv, key=f"prepared/{cache}/synthetic.mp3")
    survivor = "cache_" + uuid.uuid4().hex
    with db.session() as session:
        for ident, consumed in ((cache, conv), (survivor, None)):
            session.add(PreparedFirstSegmentRow(cache_id=ident, owner_user_id=owner,
                course_id=course, lesson_id=ident, text_key=ident, task_id="synthetic",
                context_json="{}", plan_json="{}", audio_object_key=key,
                consumed_conversation_id=consumed, expires_at=utcnow()+timedelta(days=1)))
    ident = tombstone(db, "conversation", conv, owner)
    cleanup.run_online_cleanup()
    assert stage(db, ident) == "pending"
    with db.session() as session:
        session.delete(session.get(PreparedFirstSegmentRow, survivor))
    cleanup.run_online_cleanup()
    assert stage(db, ident) == "cleaned"
    assert not storage._resolve(key).exists()
    with db.session() as session:
        assert session.get(PreparedFirstSegmentRow, cache) is None


def test_shared_audio_stays_pending_until_last_reference_is_deleted(resources):
    db, storage, cleanup, owner = resources
    deleted = conversation(db, owner)
    key, _ = audio(db, storage, deleted)
    live = conversation(db, owner, deleted=False)
    audio(db, storage, live, key=key)
    ident = tombstone(db, "conversation", deleted, owner)
    cleanup.run_online_cleanup()
    assert stage(db, ident) == "pending"
    assert storage._resolve(key).exists()
    with db.session() as session:
        session.get(ConversationRow, live).deleted_at = utcnow()
    other = tombstone(db, "conversation", live, owner)
    cleanup.run_online_cleanup()
    assert stage(db, ident) == "cleaned"
    assert stage(db, other) == "cleaned"
    assert not storage._resolve(key).exists()


def test_unlink_failure_cannot_be_misclassified_as_missing(tmp_path, monkeypatch):
    storage = MaterialStorage(tmp_path)
    storage.put("orig/synthetic.bin", b"synthetic")
    target = storage._resolve("orig/synthetic.bin")
    original_exists, original_unlink = Path.exists, Path.unlink
    def denied_exists(path):
        return False if path == target else original_exists(path)
    def denied_unlink(path, *args, **kwargs):
        if path == target:
            raise PermissionError("synthetic")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "exists", denied_exists)
    monkeypatch.setattr(Path, "unlink", denied_unlink)
    with pytest.raises(PermissionError):
        storage.delete("orig/synthetic.bin")


def test_backup_keeps_shared_audio_under_old_deleted_conversation_prefix(resources):
    db, storage, cleanup, owner = resources
    deleted = conversation(db, owner)
    key, _ = audio(db, storage, deleted)
    live = conversation(db, owner, deleted=False)
    audio(db, storage, live, key=key)
    ident = tombstone(db, "conversation", deleted, owner)
    with db.session() as session:
        session.get(DeletionTombstoneRow, ident).deleted_at = utcnow()-timedelta(days=40)
    result = cleanup.create_backup()
    with tarfile.open(cleanup.backup_storage.local_path(result["backup_key"])) as archive:
        assert key in archive.getnames()


def test_active_classroom_history_protects_audio(resources):
    db, storage, cleanup, owner = resources
    deleted = conversation(db, owner)
    key, audio_id = audio(db, storage, deleted)
    live = conversation(db, owner, deleted=False)
    with db.session() as session:
        session.add(ClassroomHistoryRow(conversation_id=live, version=1, step_no=1,
            snapshot_json=json.dumps({"audio_id": audio_id})))
    ident = tombstone(db, "conversation", deleted, owner)
    cleanup.run_online_cleanup()
    assert stage(db, ident) == "pending"
    assert storage._resolve(key).exists()


def test_course_cleanup_collects_unconsumed_prewarm(resources):
    db, storage, cleanup, owner = resources
    course = "course_" + uuid.uuid4().hex
    cache = "cache_" + uuid.uuid4().hex
    key = f"prepared/{cache}/synthetic.mp3"
    storage.put(key, b"synthetic prewarm")
    with db.session() as session:
        session.add(CourseRow(course_id=course, owner_user_id=owner))
        session.flush()
        session.add(PreparedFirstSegmentRow(cache_id=cache, owner_user_id=owner,
            course_id=course, lesson_id="synthetic", text_key="synthetic", task_id="synthetic",
            context_json="{}", plan_json="{}", audio_object_key=key,
            expires_at=utcnow()+timedelta(days=1)))
    PlanningDeletionService(db, cleanup.audit).delete_course(course, actor_id=owner)
    cleanup.run_online_cleanup(now=utcnow()+timedelta(hours=25))
    assert not storage._resolve(key).exists()


def test_file_failure_keeps_tombstone_pending_and_retry_succeeds(resources, monkeypatch):
    db, storage, cleanup, owner = resources
    conv = conversation(db, owner)
    key, _ = audio(db, storage, conv)
    ident = tombstone(db, "conversation", conv, owner)
    original = storage.delete
    def blocked(candidate):
        raise PermissionError("synthetic")
    monkeypatch.setattr(storage, "delete", blocked)
    cleanup.run_online_cleanup()
    assert stage(db, ident) == "pending"
    monkeypatch.setattr(storage, "delete", original)
    cleanup.run_online_cleanup()
    assert stage(db, ident) == "cleaned"
    assert not storage._resolve(key).exists()


@pytest.mark.parametrize("reference", ["none", "binding", "segment", "history", "default"])
def test_character_tombstone_and_live_reference_protection(resources, reference):
    db, storage, cleanup, owner = resources
    cards = CharacterService(db, None, cleanup.audit, storage)
    card = cards.create(owner, Profile(name="Synthetic"))
    card_id = card["card_id"]
    key = f"characters/{card_id}/synthetic.bin"
    storage.put(key, b"synthetic portrait")
    with db.session() as session:
        session.add(CharacterAssetRow(asset_id="asset_"+uuid.uuid4().hex, card_id=card_id,
            owner_user_id=owner, source_revision=1, kind="image", storage_key=key,
            mime_type="image/png"))
    live = conversation(db, owner, deleted=False)
    with db.session() as session:
        if reference == "binding":
            session.add(CharacterBindingRow(conversation_id=live, card_id=card_id,
                revision=1, effective_step_no=1, profile_json="{}"))
        elif reference == "segment":
            session.add(CharacterSegmentRow(conversation_id=live, step_no=1,
                card_id=card_id, revision=1, profile_json="{}"))
        elif reference == "history":
            session.add(ClassroomHistoryRow(conversation_id=live, version=1, step_no=1,
                snapshot_json=json.dumps({"character": {"card_id": card_id}})))
        elif reference == "default":
            session.add(CharacterSystemDefaultRow(key="synthetic-"+uuid.uuid4().hex[:8],
                card_id=card_id, revision=1))
    cards.delete(owner, card_id)
    with db.session() as session:
        row = session.scalar(select(DeletionTombstoneRow).where(
            DeletionTombstoneRow.object_id == card_id))
        ident = row.tombstone_id
    cleanup.run_online_cleanup(now=utcnow()+timedelta(hours=25))
    assert storage._resolve(key).exists() == (reference != "none")
    assert stage(db, ident) == ("cleaned" if reference == "none" else "pending")
    if reference in ("binding", "segment", "history"):
        with db.session() as session:
            session.get(ConversationRow, live).deleted_at = utcnow()
        cleanup.run_online_cleanup(now=utcnow()+timedelta(hours=25))
        assert not storage._resolve(key).exists()


def test_cleanup_audit_failure_is_visible_and_does_not_mark_cleaned(resources, monkeypatch):
    db, storage, cleanup, owner = resources
    conv = conversation(db, owner)
    audio(db, storage, conv)
    ident = tombstone(db, "conversation", conv, owner)
    def broken(*args, **kwargs):
        raise AuditWriteError("synthetic")
    monkeypatch.setattr(cleanup.audit, "append", broken)
    with pytest.raises(AuditWriteError):
        cleanup.run_online_cleanup()
    assert stage(db, ident) == "pending"


def test_unknown_type_and_wrong_owner_cannot_collect_media(resources):
    db, storage, cleanup, owner = resources
    conv = conversation(db, owner)
    key, _ = audio(db, storage, conv)
    wrong = tombstone(db, "conversation", conv, "synthetic-other")
    unknown = tombstone(db, "unknown", conv, owner)
    cleanup.run_online_cleanup()
    assert stage(db, wrong) == stage(db, unknown) == "pending"
    assert storage._resolve(key).exists()
    with db.session() as session:
        for ident in (wrong, unknown):
            session.delete(session.get(DeletionTombstoneRow, ident))


def test_character_delete_cancels_root_stage_and_media_tasks(resources):
    from backend.characters.distillation_models import CharacterBuildRow, CharacterBuildStageRow
    from backend.characters.models import CharacterMediaJobRow
    db, storage, cleanup, owner = resources
    cards = CharacterService(db, None, cleanup.audit, storage)
    card = cards.create(owner, Profile(name="Synthetic"))["card_id"]
    tasks = TaskService(db, PgTaskEventStore(db))
    created = [tasks.create_task(owner_user_id=owner, kind=kind, subject_id=card,
        idempotency_key=uuid.uuid4().hex, stage="generate")["task_id"]
        for kind in ("character_build", "character_build_stage", "character_media")]
    build = "build_" + uuid.uuid4().hex
    with db.session() as session:
        session.add(CharacterBuildRow(build_id=build, owner_user_id=owner, task_id=created[0],
            card_id=card, card_revision=1, request_key=uuid.uuid4().hex, request_json="{}",
            request_hash="synthetic", config_id="synthetic", config_version=1))
        session.flush()
        session.add(CharacterBuildStageRow(task_id=created[1], build_id=build,
            stage_key="synthetic", input_hash="synthetic"))
        session.add(CharacterMediaJobRow(task_id=created[2], card_id=card, owner_user_id=owner,
            revision=1, config_id="synthetic", config_version=1, request_json="{}",
            request_hash="synthetic"))
    cards.delete(owner, card)
    with db.session() as session:
        assert all(session.get(LearningTaskRow, ident).cancel_requested for ident in created)


def test_restore_replay_collects_resurrected_file_even_if_metadata_stays_deleted(resources):
    db, storage, cleanup, owner = resources
    conv = conversation(db, owner)
    key, _ = audio(db, storage, conv)
    ident = tombstone(db, "conversation", conv, owner)
    cleanup.run_online_cleanup()
    assert stage(db, ident) == "cleaned"
    storage.put(key, b"synthetic restored bytes")
    cleanup.begin_restore(operator_id=owner)
    try:
        cleanup.replay_tombstones(operator_id=owner)
        assert not storage._resolve(key).exists()
        cleanup.verify_and_unlock(operator_id=owner)
    finally:
        cleanup.set_state("restore_locked", "0")
