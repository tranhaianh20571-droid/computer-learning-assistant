"""Synthetic source routing, disclosure, tenant isolation and learning reads."""
import asyncio
import json
import uuid
from types import SimpleNamespace

import httpx2
import pytest

from backend.logging_audit.db import Database
from backend.logging_audit.repositories import PgAuditLog, PgTaskEventStore
from backend.materials.attachments import AttachmentService
from backend.materials.source_clients import classify_url, scrape
from backend.materials.source_imports import SourceImports
from backend.materials.source_models import ImportedSourceRow, SourceItemRow
from backend.materials.source_router import ImportItemIn
from backend.materials.source_settings import ReaderConfigIn
from backend.materials.local_documents import parse_text
from backend.materials.storage import MaterialStorage
from backend.materials.upload import MaterialService, QuotaService
from backend.materials.study_sources import documents, freeze_sources, verify_sources
from backend.slice1.config_service import ConfigService
from backend.slice1.disclosure_service import DisclosureService
from backend.slice1.errors import Slice1Error
from backend.materials.errors import MaterialsError
from backend.tasks.service import TaskService


@pytest.fixture
def source_env(tmp_path):
    db = Database()
    db.create_all()
    owner = "synthetic_" + uuid.uuid4().hex[:12]
    configs = ConfigService(db, encryption_key="synthetic-source-encryption-key", audit=PgAuditLog(db))
    storage = MaterialStorage(tmp_path)
    attachments = AttachmentService(db, MaterialService(db, QuotaService(db), storage=storage),
        configs, DisclosureService(db, configs.audit), TaskService(db, PgTaskEventStore(db)), storage)
    service = SourceImports(attachments)
    yield SimpleNamespace(**locals())
    db.dispose()


@pytest.mark.parametrize("url,kind", [
    ("https://www.bilibili.com/video/BVsynthetic?spm_id_from=synthetic&p=2", "video"),
    ("https://www.youtube.com/watch?v=synthetic", "video"),
    ("https://www.bilibili.com/", "collection"),
    ("https://example.org/notes/tutorial", "web"),
    ("https://example.org/douyin/tutorial", "web"),
    ("https://example.org/synthetic.pdf", "file"),
    ("https://example.org/synthetic.png", "image"),
    ("https://example.org/synthetic.mp4", "video"),
])
def test_exact_url_routing(url, kind):
    result = classify_url(url)
    assert result["kind"] == kind
    if "bilibili.com/video" in url:
        assert "spm_id_from" not in result["url"] and "p=2" in result["url"]


@pytest.mark.parametrize("url", [
    "file:///synthetic", "javascript:alert(1)", "http://127.0.0.1/a",
    "http://169.254.169.254/latest", "https://[::1]/", "https://user:pass@example.org/",
    "https://localhost/a", "https://metadata.google.internal/", "https://example.org:18081/",
])
def test_unsafe_targets_rejected_without_network(url):
    with pytest.raises(MaterialsError):
        classify_url(url)


def configure_web(env):
    return env.service.settings.save(env.owner, "firecrawl", ReaderConfigIn(
        expected_revision=0, endpoint="https://example.org",
        api_key="synthetic-firecrawl-key", options={"enabled": True}))


def test_personal_settings_encrypted_and_copy_on_write(source_env):
    env = source_env
    saved = configure_web(env)
    text = json.dumps(saved)
    assert "synthetic-firecrawl-key" not in text
    old_id = saved["configuration"]["config_id"]
    changed = env.service.settings.save(env.owner, "firecrawl", ReaderConfigIn(
        expected_revision=1, endpoint="https://example.org", options={"timeout": 45}))
    assert changed["configuration"]["config_id"] != old_id
    assert env.configs.get(old_id, actor_id=env.owner, is_admin=False)["is_active"]
    assert env.configs.resolve_credentials(changed["configuration"]["config_id"])["api_key"] == "synthetic-firecrawl-key"
    assert env.service.settings.get("synthetic_other", "firecrawl")["configuration"] is None
    with pytest.raises(Slice1Error):
        env.service.settings.save(env.owner, "firecrawl", ReaderConfigIn(
            expected_revision=2, endpoint="https://example.com", options={}))


def test_video_defaults_and_independent_model_required(source_env):
    env = source_env
    profile = env.service.settings.save(env.owner, "bilinote",
        ReaderConfigIn(expected_revision=0, endpoint="builtin://bilinote"))
    assert all(profile["options"][x] for x in ("screenshot", "link", "video_understanding"))
    batch = env.service.create(env.owner, uuid.uuid4().hex, [
        ImportItemIn(client_id="video", url="https://www.bilibili.com/video/BVsynthetic")])
    with pytest.raises(Slice1Error, match="多模态模型"):
        env.service.prepare(env.owner, batch["batch_id"])


def test_create_is_local_and_idempotent_and_cross_owner_hidden(source_env):
    env = source_env
    item = ImportItemIn(client_id="web", url="https://example.org/synthetic")
    key = uuid.uuid4().hex
    first = env.service.create(env.owner, key, [item])
    assert first["items"][0]["kind"] == "web" and first["status"] == "draft"
    assert env.service.create(env.owner, key, [item])["batch_id"] == first["batch_id"]
    with pytest.raises(MaterialsError):
        env.service.view("synthetic_other", first["batch_id"])
    with pytest.raises(MaterialsError):
        env.service.create(env.owner, key, [ImportItemIn(client_id="web", url="https://example.org/changed")])


def test_source_publication_retrieval_version_and_delete(source_env, monkeypatch):
    env = source_env
    configure_web(env)
    called = []
    async def synthetic_scrape(endpoint, key, url, options, deps):
        deps.check()
        called.append(url)
        state = parse_text(("## Synthetic transactions\n\n"
            "数据库事务包含原子性、一致性、隔离性与持久性。\n"
            "Why is isolation important for concurrent transactions?\n").encode(), "md")
        state["unit"] = "paragraph"
        from backend.materials.study_sources import build_study_index
        state["study_index"] = build_study_index(state)
        return state
    monkeypatch.setattr("backend.materials.source_imports.scrape", synthetic_scrape)
    batch = env.service.create(env.owner, uuid.uuid4().hex, [
        ImportItemIn(client_id="web", url="https://example.org/synthetic")])
    prepared = env.service.prepare(env.owner, batch["batch_id"])
    assert not called
    with pytest.raises(Slice1Error):
        env.service.commit(env.owner, batch["batch_id"], prepared["manifest_hash"], False)
    started = env.service.commit(env.owner, batch["batch_id"], prepared["manifest_hash"], True)
    material = started["items"][0]["material_id"]
    with env.db.session() as session:
        task = session.get(ImportedSourceRow, material).task_id
    env.service.run(task, env.owner, material)
    assert len(called) == 1
    ready = env.service.view(env.owner, batch["batch_id"])
    assert ready["status"] == "ready"
    from backend.materials.corpus import search_corpus
    hits, _ = search_corpus(env.db, env.owner, [material], "数据库事务原子性")
    assert hits and "原子性" in hits[0]["attachment_text"]
    assert hits[0]["unit"] == "paragraph"
    with env.db.session() as session:
        snapshot = freeze_sources(session, env.owner, [material])
        assert verify_sources(session, env.owner, snapshot)
        with pytest.raises(Exception):
            documents(session, "synthetic_other", [material])
    assert not env.attachments.materials.list_for_owner(env.owner)
    env.service.remove(env.owner, batch["batch_id"])
    assert env.service.view(env.owner, batch["batch_id"])["items"][0]["status"] == "failed"
    with env.db.session() as session:
        with pytest.raises(Exception):
            verify_sources(session, env.owner, snapshot)


def test_video_timestamp_locators_and_auxiliary_separation():
    from backend.materials.source_processing import video_state
    state = video_state({"segments": [{"start": 2, "end": 5, "text": "Why does a database need transactions?"}],
        "frames": [{"time_ms": 2000}], "transcript_kind": "platform_subtitle",
        "duration": 10, "visual_coverage_complete": False},
        [{"index": 0, "transcription": "Synthetic slide", "explanation": "辅助说明"}])
    assert state["study_index"]["sections"][0]["unit"] == "time"
    assert state["study_index"]["sections"][0]["start_ms"] == 2000
    assert state["chunks"][-1]["kind"] == "image_understanding"
    assert not state["visual_coverage_complete"]


def test_failed_import_retries_frozen_config_and_preserves_budget(source_env, monkeypatch):
    env = source_env
    configure_web(env)
    attempts = []
    async def scrape_once(*args):
        attempts.append(1)
        args[-1].check()
        if len(attempts) == 1:
            raise ValueError("synthetic failure")
        return parse_text(b"Synthetic database atomicity and durable transactions.", "txt")
    monkeypatch.setattr("backend.materials.source_imports.scrape", scrape_once)
    batch = env.service.create(env.owner, uuid.uuid4().hex, [
        ImportItemIn(client_id="web", url="https://example.org/synthetic")])
    prepared = env.service.prepare(env.owner, batch["batch_id"])
    started = env.service.commit(env.owner, batch["batch_id"], prepared["manifest_hash"], True)
    material = started["items"][0]["material_id"]
    with env.db.session() as session:
        previous = session.get(ImportedSourceRow, material).task_id
    env.service.run(previous, env.owner, material)
    assert env.service.view(env.owner, batch["batch_id"])["items"][0]["status"] == "failed"
    with pytest.raises(Slice1Error):
        env.service.retry(env.owner, batch["batch_id"], prepared["manifest_hash"], False)
    env.service.retry(env.owner, batch["batch_id"], prepared["manifest_hash"], True)
    with env.db.session() as session:
        current = session.get(ImportedSourceRow, material).task_id
    assert current != previous
    env.service.run(current, env.owner, material)
    assert env.service.view(env.owner, batch["batch_id"])["status"] == "ready"
    assert len(attempts) == 2


def test_source_storage_quota_and_draft_expiration(source_env):
    from datetime import timedelta
    from backend.materials.models import MaterialUploadQuotaRow, utcnow
    from backend.materials.source_models import SourceBatchRow
    env = source_env
    configure_web(env)
    batch = env.service.create(env.owner, uuid.uuid4().hex, [
        ImportItemIn(client_id="web", url="https://example.org/synthetic")])
    prepared = env.service.prepare(env.owner, batch["batch_id"])
    env.attachments.materials.quotas.snapshot(env.owner)
    with env.db.session() as session:
        session.get(MaterialUploadQuotaRow, env.owner).quota_bytes = 1024
    with pytest.raises(MaterialsError):
        env.service.commit(env.owner, batch["batch_id"], prepared["manifest_hash"], True)
    with env.db.session() as session:
        assert session.get(MaterialUploadQuotaRow, env.owner).reserved_bytes == 0
        session.get(SourceBatchRow, batch["batch_id"]).created_at = utcnow() - timedelta(hours=25)
    env.service.expire_drafts()
    assert env.service.view(env.owner, batch["batch_id"])["status"] == "cancelled"


def test_video_grouping_preserves_full_transcript_and_time_range():
    from backend.materials.source_processing import video_state
    segments = [{"start": index, "end": index + 1, "text": f"synthetic token {index}"} for index in range(120)]
    state = video_state({"segments": segments, "frames": [], "transcript_kind": "asr"}, [])
    assert state["transcript_segments"] == segments
    assert len(state["page_results"]) == 3
    assert state["page_results"][-1]["blocks"][0]["end_ms"] == 120000


def test_video_auxiliary_chunks_keep_locator_without_becoming_original():
    from backend.materials.source_processing import video_state
    from backend.materials.corpus import segments
    state = video_state({"segments": [{"start": 0, "end": 60, "text": "Synthetic spoken transcript"}],
        "frames": [{"time_ms": 30000}], "transcript_kind": "asr"}, [
            {"index": 0, "transcription": "Synthetic chart text", "explanation": "Synthetic visual explanation"}])
    state["source_url"] = "https://example.org/synthetic.mp4"
    blocks = segments(state, "synthetic_material")
    auxiliary = next(block for block in blocks if block.get("auxiliary"))
    assert auxiliary["start_ms"] == 30000
    assert auxiliary["asset_index"] == 0
    assert auxiliary["source_url"] == state["source_url"]
    assert auxiliary["offset"] is None


def test_source_asset_delete_clears_private_checkpoints(source_env):
    env = source_env
    env.storage.put("source-assets/mat_synthetic/0.jpg", b"synthetic image")
    env.storage.put("source-checkpoints/mat_synthetic.json", b"synthetic transcript")
    env.storage.put("source-checkpoints/mat_synthetic-visual.json", b"synthetic visual")
    env.storage.delete_source_assets("mat_synthetic")
    assert not env.storage._resolve("source-assets/mat_synthetic").exists()
    assert not env.storage._resolve("source-checkpoints/mat_synthetic.json").exists()


def test_private_source_api_owner_headers_and_delete(source_env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.materials.models import MaterialRow
    from backend.materials.source_router import create_source_router
    from backend.materials.router import install_materials_error_handlers
    env = source_env
    ident = "mat_" + uuid.uuid4().hex[:16]
    with env.db.session() as session:
        session.add(MaterialRow(material_id=ident, owner_user_id=env.owner, filename_hash="synthetic",
            source_bytes_sha256="synthetic", kind="image", size_bytes=0, status="parsed"))
        session.flush()
        session.add(ImportedSourceRow(material_id=ident, owner_user_id=env.owner,
            task_id="synthetic", source_version="synthetic", result_json=json.dumps({
                "assets": [{"index": 0}], "source_kind": "image"})))
    env.storage.put(f"source-assets/{ident}/0.jpg", b"synthetic-image-bytes")
    app = FastAPI()
    app.include_router(create_source_router(env.service,
        lambda request: SimpleNamespace(user_id=request.headers.get("X-Synthetic-Owner", env.owner))))
    install_materials_error_handlers(app)
    with TestClient(app) as client:
        result = client.get(f"/api/source-materials/{ident}")
        assert result.status_code == 200 and result.headers["Cache-Control"] == "no-store"
        image = client.get(f"/api/source-materials/{ident}/assets/0")
        assert image.status_code == 200 and image.headers["X-Content-Type-Options"] == "nosniff"
        assert image.content == b"synthetic-image-bytes"
        assert client.get(f"/api/source-materials/{ident}/assets/0",
            headers={"X-Synthetic-Owner": "synthetic-other"}).status_code == 404
        assert client.get(f"/api/source-materials/{ident}/assets/-1").status_code == 404
        env.attachments.materials.delete(ident, actor_id=env.owner)
        assert client.get(f"/api/source-materials/{ident}").status_code == 404
        assert not env.storage._resolve(f"source-assets/{ident}/0.jpg").exists()
