"""Owned synthetic resources, persistent maintenance and local process budgets."""
import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import uuid
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import select, insert

from backend.cleanup.manager import LocalResourceManager
from backend.cleanup.service import CleanupService
from backend.cleanup.models import LocalResourceRow as Object, LocalResourceJobRow as Job, utcnow
from backend.cleanup.resource_router import create_resource_router
from backend.cleanup.budget import GIB
from backend.logging_audit.db import Database
from backend.logging_audit.repositories import PgAuditLog, PgTaskEventStore
from backend.materials.models import MaterialRow
from backend.materials.storage import MaterialStorage, StorageError
from backend.tasks.service import TaskService, LearningTaskRow, LeaseError


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKUP_STORAGE_DIR", raising=False)
    db = Database()
    db.create_all()
    storage = MaterialStorage(tmp_path / "objects")
    audit = PgAuditLog(db)
    state = SimpleNamespace(db=db, storage=storage, audit=audit, tasks=TaskService(db, PgTaskEventStore(db)),
        cleanup=CleanupService(db, storage, audit))
    manager = LocalResourceManager(state)
    owner = "synthetic-"+uuid.uuid4().hex
    try:
        yield manager, owner
    finally:
        manager.close()
        db.dispose()


def material(manager, owner):
    ident = "mat_"+uuid.uuid4().hex
    with manager.db.session() as session:
        session.add(MaterialRow(material_id=ident, owner_user_id=owner, kind="txt",
            size_bytes=9, source_bytes_sha256="synthetic", filename_hash="synthetic", status="parsed"))
    key = f"orig/{ident}.bin"
    manager.storage.put(key, b"synthetic")
    manager.observe(manager.storage, key)
    with manager.db.session() as session:
        row = session.scalar(select(Object).where(Object.object_key == key))
        return row.resource_id, key, ident


def age(manager, ident):
    with manager.db.session() as session:
        row = session.get(Object, ident)
        row.first_seen_at = row.last_used_at = utcnow()-timedelta(days=10)


def job_until_done(manager, ident, limit=100):
    for _ in range(limit):
        manager.tick()
        with manager.db.session() as session:
            status = session.get(Job, ident).status
            if status in ("succeeded", "failed", "retry"):
                return status
    raise AssertionError("job did not finish")


def test_owned_snapshot_pin_and_optimistic_policy(env):
    manager, owner = env
    ident, key, _ = material(manager, owner)
    manager.pin(owner, ident, True)
    assert manager.snapshot(owner)["objects"][0]["pinned"]
    assert not manager.snapshot("synthetic-other")["objects"]
    with pytest.raises(StorageError):
        manager.pin("synthetic-other", ident, False)
    version = manager.save_policy(owner, {}, 0)
    assert version == 1
    with pytest.raises(StorageError):
        manager.save_policy(owner, {}, 0)
    assert manager.storage.get(key) == b"synthetic"


def test_persistent_scan_progress_and_seven_distinct_launches(env):
    manager, owner = env
    material(manager, owner)
    manager.storage.put("unknown/synthetic.bin", b"synthetic")
    data = {}
    done, count = manager.scan_batch(data, limit=1)
    assert not done and count == 1 and data["cursor"]
    resumed = LocalResourceManager(manager.state)
    resumed.boot = manager.boot
    for _ in range(10):
        done, _ = resumed.scan_batch(data, limit=1)
        if done:
            break
    assert done
    assert resumed.snapshot(owner, True)["successful_starts"] == 1
    for launch in range(6):
        resumed.boot = f"synthetic-boot-{launch}"
        data = {}
        for _ in range(10):
            if resumed.scan_batch(data, limit=20)[0]:
                break
    assert resumed.snapshot(owner, True)["successful_starts"] == 7
    assert not resumed.snapshot(owner, True)["observation_only"]


def test_live_material_never_cleaned_and_stale_preview_rechecks_pin(env):
    manager, owner = env
    ident, key, mid = material(manager, owner)
    age(manager, ident)
    assert manager.collect_one(ident, owner) == 0
    with manager.db.session() as session:
        session.get(MaterialRow, mid).deleted_at = utcnow()
    manager.observe(manager.storage, key)
    age(manager, ident)
    assert manager.preview(owner)["count"] == 1
    manager.pin(owner, ident, True)
    assert manager.collect_one(ident, owner) == 0
    manager.pin(owner, ident, False)
    assert manager.collect_one(ident, owner) == 9
    assert not manager.storage._resolve(key).exists()


def test_failure_backoff_and_resume_never_marks_missing(env, monkeypatch):
    manager, owner = env
    ident, key, mid = material(manager, owner)
    with manager.db.session() as session:
        session.get(MaterialRow, mid).deleted_at = utcnow()
    manager.observe(manager.storage, key)
    age(manager, ident)
    original = manager.storage.delete
    def broken(key):
        raise PermissionError("synthetic")
    monkeypatch.setattr(manager.storage, "delete", broken)
    request = {"resource_ids": [ident], "global": False}
    assert manager._cleanup_batch(request, owner)[0]
    with manager.db.session() as session:
        row = session.get(Object, ident)
        assert row.state == "retry" and row.next_retry_at and row.attempts == 1
        row.next_retry_at = utcnow()-timedelta(seconds=1)
    monkeypatch.setattr(manager.storage, "delete", original)
    assert manager._cleanup_batch({"resource_ids":[ident]}, owner)[0]
    with manager.db.session() as session:
        assert session.get(Object, ident).state == "missing"


def test_ten_thousand_rows_bounded_cleanup_does_not_touch_live_files(env):
    manager, owner = env
    now = utcnow()-timedelta(days=10)
    rows = [{"resource_id":"res_"+uuid.uuid4().hex, "volume_id":manager.storage.location.volume_id,
        "object_key":f"unknown/synthetic-{index}.bin", "kind":"unknown", "owner_user_id":owner,
        "size_bytes":1, "mtime_ns":"0", "reference_count":0, "pinned":False,
        "state":"present","attempts":0,"first_seen_at":now,"last_used_at":now,"observed_at":now}
        for index in range(10000)]
    with manager.db.engine.begin() as connection:
        connection.execute(insert(Object), rows)
    data={"resource_ids":[row["resource_id"] for row in rows]}
    done, count=manager._cleanup_batch(data, owner)
    assert not done and count<=200 and data["cursor"]<=200
    with manager.db.session() as session:
        assert session.get(Object, rows[-1]["resource_id"]).state == "present"


def test_migration_hash_switch_restart_and_original_preserved(env, tmp_path):
    manager, owner = env
    _, key, _ = material(manager, owner)
    old = manager.storage.base_dir
    destination = manager.validate_destination(str(tmp_path/"moved"))
    data={}
    manager.migrate(destination, data)
    assert data["switched"] and manager.storage.base_dir == Path(destination)
    assert (old/key).read_bytes() == manager.storage.get(key)
    restarted = MaterialStorage(old)
    assert restarted.base_dir == Path(destination)
    assert restarted.get(key) == b"synthetic"
    assert manager.snapshot(owner, True)["old_directories"] == [str(old)]
    # Crash after the root switch but before the job row commit is idempotent.
    manager.migrate(destination,{})
    assert manager.snapshot(owner, True)["old_directories"] == [str(old)]


def test_migration_copy_failure_retains_source(env, tmp_path, monkeypatch):
    manager, owner = env
    _, key, _ = material(manager, owner)
    source = manager.storage.base_dir
    original = os.replace
    def broken(left,right):
        if str(right).startswith(str(tmp_path/"moved")):
            raise OSError("synthetic")
        return original(left,right)
    monkeypatch.setattr(os,"replace",broken)
    with pytest.raises(OSError):
        manager.migrate(str(tmp_path/"moved"),{})
    assert manager.storage.base_dir == source
    assert (source/key).read_bytes() == b"synthetic"
    monkeypatch.setattr(os,"replace",original)
    manager.migrate(str(tmp_path/"moved"),{})
    assert manager.storage.get(key)==b"synthetic"


@pytest.mark.parametrize("kind",["inside","parent","relative","nonempty"])
def test_migration_rejects_unsafe_destination(env,tmp_path,kind):
    manager,_=env
    nonempty=tmp_path/"nonempty"
    nonempty.mkdir()
    (nonempty/"existing").write_bytes(b"synthetic")
    target={"inside":manager.storage.base_dir/"child","parent":manager.storage.base_dir.parent,
        "relative":Path("relative"),"nonempty":nonempty}[kind]
    with pytest.raises(StorageError):
        manager.validate_destination(str(target))


def test_api_authentication_global_admin_and_private_projection(env):
    manager, owner = env
    ident,_,_=material(manager,owner)
    app=FastAPI()
    user=SimpleNamespace(user_id=owner,is_admin=False)
    app.include_router(create_resource_router(manager,lambda request:user))
    @app.exception_handler(StorageError)
    async def handled(request,exc):
        return JSONResponse(status_code=403 if exc.code=="local_resources_admin_only" else 404,
            content={"error_code":exc.code})
    client=TestClient(app)
    response=client.get("/api/local-resources")
    assert response.status_code==200 and response.headers["cache-control"]=="no-store"
    assert "directory" not in response.json() and response.json()["policy"] is None
    for path in ("scan","backup","migration"):
        assert client.post("/api/local-resources/"+path,json={"destination":"synthetic"}).status_code==403
    user.user_id="synthetic-other"
    assert client.put(f"/api/local-resources/objects/{ident}/pin",json={"pinned":True}).status_code==404


def test_jobs_reclaim_expired_lease_and_idempotency(env):
    manager,owner=env
    first=manager.enqueue(owner,"scan",request_key="synthetic-"+uuid.uuid4().hex)
    with manager.db.session() as session:
        row=session.get(Job,first)
        row.status="running"
        row.lease_until=utcnow()-timedelta(seconds=1)
        request=row.request_key
    assert manager.enqueue(owner,"scan",request_key=request)==first
    assert job_until_done(manager,first)=="succeeded"


def test_explicit_job_retry_requires_owner_and_resets_only_job_retry(env):
    manager,owner=env
    ident=manager.enqueue(owner,"cleanup",{"resource_ids":[]})
    with manager.db.session() as session:
        row=session.get(Job,ident)
        row.status,row.error_code,row.attempts="failed","LOCAL_RESOURCE_FAILED",5
    app=FastAPI()
    app.include_router(create_resource_router(manager,lambda request:SimpleNamespace(
        user_id=owner,is_admin=False)))
    result=TestClient(app).post(f"/api/local-resources/jobs/{ident}/retry")
    assert result.status_code==200
    with manager.db.session() as session:
        row=session.get(Job,ident)
        assert row.status=="queued" and row.attempts==0


def test_disk_and_memory_pressure_blocks_claim_and_queue_is_bounded(env):
    from sqlalchemy import func
    from backend.cleanup.budget import HEAVY_KINDS
    manager,owner=env
    manager.budget.sampler=lambda:{"physical_bytes":8*GIB,"available_bytes":GIB//2,"rss_bytes":2*GIB,
        "limit_bytes":2*GIB,"warning_bytes":int(1.5*GIB),"heavy_limit":1,"memory_paused":True}
    tasks=manager.state.tasks
    tasks.resource_budget=manager.budget
    with manager.db.session() as session:
        pending = session.scalar(select(func.count()).select_from(LearningTaskRow).where(
            LearningTaskRow.kind.in_(HEAVY_KINDS), LearningTaskRow.status == "queued"))
    ids=[tasks.create_task(owner_user_id=owner,kind="material_parse",subject_id="synthetic",
        idempotency_key=uuid.uuid4().hex,stage="parse")["task_id"] for _ in range(20-pending)]
    assert ids, "The shared synthetic queue must have room for this test's owned task."
    with pytest.raises(LeaseError) as rejected:
        tasks.create_task(owner_user_id=owner,kind="material_parse",subject_id="synthetic",
            idempotency_key=uuid.uuid4().hex,stage="parse")
    assert rejected.value.code=="LANE_CAPACITY_FULL"
    with pytest.raises(LeaseError):
        tasks.claim(ids[0],lease_owner="synthetic")
    with manager.db.session() as session:
        assert session.get(LearningTaskRow,ids[0]).status=="queued"


def test_restore_lock_stops_scheduling(env):
    manager,owner=env
    manager.cleanup.set_state("restore_locked","1")
    try:
        manager.schedule()
        with pytest.raises(Exception):
            manager.enqueue(owner,"scan")
    finally:
        manager.cleanup.set_state("restore_locked","0")


@pytest.mark.parametrize("free_gib,fraction,expected",[
    (30,.3,"normal"),(9,.3,"warning"),(4,.3,"restricted"),(1,.3,"protected"),
    (30,.04,"restricted")])
def test_disk_thresholds(env,monkeypatch,free_gib,fraction,expected):
    from backend.cleanup import budget
    manager,_=env
    free=int(free_gib*GIB)
    monkeypatch.setattr(budget.shutil,"disk_usage",lambda path:SimpleNamespace(
        total=int(free/fraction),free=free))
    assert budget.disk_state(manager.storage.base_dir)["state"]==expected


def test_scan_marks_externally_removed_file_missing(env):
    manager,owner=env
    ident,key,_=material(manager,owner)
    manager.storage.delete(key)
    data={}
    for _ in range(12):
        if manager.scan_batch(data)[0]:
            break
    with manager.db.session() as session:
        assert session.get(Object,ident).state=="missing"


def test_pinned_backup_survives_time_rotation(env):
    from backend.cleanup.models import BackupRunRow
    manager,owner=env
    key="backups/bk_synthetic.tar.gz"
    manager.cleanup.backup_storage.put(key,b"synthetic")
    with manager.db.session() as session:
        session.add(BackupRunRow(run_id="bk_synthetic",backup_key=key,payload_bytes=9,
            created_at=utcnow()-timedelta(days=80)))
    manager.observe(manager.cleanup.backup_storage,key)
    with manager.db.session() as session:
        ident=session.scalar(select(Object.resource_id).where(Object.object_key==key))
    manager.pin(owner,ident,True,True)
    from backend.cleanup.backup import create_object_backup
    create_object_backup(manager.cleanup,60)
    assert manager.cleanup.backup_storage.local_path(key).read_bytes()==b"synthetic"


def test_twenty_real_subprocess_text_runs_release_children_and_slots(env,tmp_path,monkeypatch,record_property):
    import psutil
    from backend.cleanup import budget as module
    from backend.materials.local_execution import execute_local
    manager,_=env
    manager.budget.sampler=lambda:{"physical_bytes":16*GIB,"available_bytes":8*GIB,"rss_bytes":1000000,
        "limit_bytes":3*GIB,"warning_bytes":int(2.5*GIB),"heavy_limit":1,"memory_paused":False}
    monkeypatch.setattr(module,"_active",manager.budget)
    source=tmp_path/"synthetic.txt"
    source.write_text("Synthetic local resource sample.\\n"*20,encoding="utf8")
    process=psutil.Process()
    rss=[]
    baseline={child.pid for child in process.children(recursive=True)}
    for index in range(20):
        result=execute_local("text",source,kind="txt",stop=Event())
        assert result["page_results"]
        rss.append(process.memory_info().rss)
    assert not ({child.pid for child in process.children(recursive=True)}-baseline)
    assert max(rss[3:])-min(rss[3:])<100*1024**2
    record_property("rss_samples_bytes",str(rss))
    record_property("rss_growth_bytes",max(rss[3:])-min(rss[3:]))


def test_audio_warning_uses_owner_inventory(env):
    manager, owner = env
    with manager.db.session() as session:
        session.add(Object(resource_id="res_large_audio", volume_id=manager.storage.location.volume_id,
            object_key="audio/synthetic/part", kind="audio", owner_user_id=owner,
            size_bytes=2*GIB))
    assert manager.snapshot(owner)["audio_warning"]
    assert not manager.snapshot("unrelated-owner")["audio_warning"]


def test_metadata_retention_is_bounded_and_preserves_pins(env):
    manager, owner = env
    old = utcnow()-timedelta(days=31)
    with manager.db.session() as session:
        for index in range(205):
            session.add(Object(resource_id=f"res_expired_{index}", volume_id=manager.storage.location.volume_id,
                object_key=f"missing/{index}", kind="temporary", owner_user_id=owner,
                state="missing", observed_at=old))
        session.add(Object(resource_id="res_pinned_missing", volume_id=manager.storage.location.volume_id,
            object_key="missing/pinned", kind="temporary", owner_user_id=owner,
            state="missing", observed_at=old, pinned=True))
    assert manager.prune_metadata() == (0, 200)
    assert manager.prune_metadata() == (0, 5)
    with manager.db.session() as session:
        assert session.get(Object, "res_pinned_missing") is not None


def test_local_pdf_eleven_pages_are_split_at_ten(monkeypatch):
    from backend.materials import document_pipeline
    monkeypatch.setenv("LOCAL_PARSER_CHILD", "1")
    monkeypatch.setattr(document_pipeline, "pdf_batch_bytes", lambda *args: b"synthetic")
    plan = document_pipeline.plan_batches("synthetic", kind="pdf", pages=11, size=100)
    assert plan["strategy"] == "batched"
    assert [(batch["start"], batch["end"]) for batch in plan["batches"]] == [(1, 10), (11, 11)]


def test_direct_ocr_uses_provider_page_limit_instead_of_local_parser_limit(monkeypatch):
    from backend.materials import document_pipeline
    monkeypatch.setenv("LOCAL_PARSER_CHILD", "1")
    monkeypatch.setattr(document_pipeline, "pdf_batch_bytes", lambda *args: b"synthetic")
    plan = document_pipeline.plan_batches("synthetic", kind="pdf", pages=36, size=954448,
        target_bytes=45_000_000, provider_bytes=50_000_000, page_limit=50)
    assert plan["strategy"] == "direct"
    assert [(b["start"], b["end"]) for b in plan["batches"]] == [(1, 36)]
    plan = document_pipeline.plan_batches("synthetic", kind="pdf", pages=51, size=100,
        page_limit=50)
    assert [(b["start"], b["end"]) for b in plan["batches"]] == [(1, 50), (51, 51)]


def test_subprocess_cancellation_reaps_child(env,tmp_path,monkeypatch):
    from backend.cleanup import budget as module
    from backend.materials.local_execution import execute_local
    manager,_=env
    monkeypatch.setattr(module,"_active",manager.budget)
    source=tmp_path/"synthetic.txt"
    source.write_bytes(b"synthetic "*100000)
    stop=Event();stop.set()
    with pytest.raises(Exception):
        execute_local("text",source,kind="txt",stop=stop)
