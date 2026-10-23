"""Personal encrypted model versions, real wire adapters and retrieval isolation."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.auth.models import UserRow
from backend.db import Database
from backend.memory.personal import (PersonalIn, PersonalModels, PersonalModelRevision,
    PersonalEmbeddingProfile, MemoryBankRoute, MemoryMigration, PersonalReasoningIn,
    finite_vector)
from backend.memory.models import MemorySpaceRow, MemorySettingsRow, MemorySourceRow
from backend.memory.providers import ProviderClient
from backend.memory.proxy import create_proxy_router
from backend.materials.corpus import (CorpusDocument, CorpusIndex, CorpusWorker,
    ensure_document, search_corpus, segments)
from backend.materials.models import MaterialRow, DocumentAttachmentRow
from backend.slice1.errors import Slice1Error


def body(revision=0, protocol="rerank", model="synthetic-embedding", dims=32):
    provider = lambda name: {"endpoint": f"https://{name}.example.test",
        "model": name, "api_key": "synthetic-" + name + "-key"}
    return PersonalIn(expected_revision=revision, content=provider("content"),
        embedding={**provider("embedding"), "model": model}, dimensions=dims,
        rerank_protocol=protocol, rerank=provider("rerank"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "synthetic-personal-key")
    db = Database("sqlite+pysqlite:///" + (tmp_path / "personal.db").as_posix())
    for model in (UserRow, PersonalModelRevision, PersonalEmbeddingProfile, MemoryBankRoute,
                  MemoryMigration, MemorySettingsRow, MemorySpaceRow, MemorySourceRow,
                  MaterialRow, DocumentAttachmentRow, CorpusDocument, CorpusIndex):
        model.__table__.create(db.engine)
    with db.session() as session:
        for owner in ("alice", "bob"):
            session.add(UserRow(user_id=owner, email=owner + "@example.invalid",
                password_hash="synthetic", status="approved", role="member"))
    yield SimpleNamespace(db=db, models=PersonalModels(db))
    db.dispose()


def material(env, owner="alice", ident="synthetic-doc", text="数据库事务保证原子性。"):
    state = {"pages": 1, "page_results": [{"page_no": 1, "blocks": [
        {"type": "title", "content": "数据库事务", "level": 1},
        {"type": "text", "content": text}]}]}
    with env.db.session() as session:
        m = MaterialRow(material_id=ident, owner_user_id=owner, filename_hash="synthetic",
            source_bytes_sha256="synthetic", kind="pdf", size_bytes=100, status="parsed")
        a = DocumentAttachmentRow(material_id=ident, owner_user_id=owner, upload_key=ident,
            task_id="synthetic-task", config_id="synthetic-config", config_version=1,
            result_json=json.dumps(state))
        session.add(m)
        session.flush()
        session.add(a)
        session.flush()
        ensure_document(session, owner, m, a)
    return state


def test_ordinary_members_save_encrypted_and_isolated_versions(env):
    a = env.models.save("alice", body())
    b = env.models.save("bob", body(model="bob-embedding", dims=64))
    assert a["revision"] == b["revision"] == 1
    assert a["embedding_space"] != b["embedding_space"]
    assert "synthetic-content-key" not in json.dumps(a)
    with env.db.session() as session:
        row = session.get(PersonalModelRevision, ("alice", 1))
        assert "synthetic-content-key" not in row.encrypted_keys
    assert env.models.resolve("alice")["embedding"]["model"] == "synthetic-embedding"
    assert env.models.resolve("bob")["dimensions"] == 1024
    with pytest.raises(Slice1Error):
        env.models.save("alice", body())


def test_same_space_key_rotation_and_protocol_key_fence(env):
    first = env.models.save("alice", body())
    update = body(1)
    update.embedding.api_key = None
    second = env.models.save("alice", update)
    assert first["embedding_space"] == second["embedding_space"]
    update = body(2, "jev")
    update.rerank.api_key = None
    with pytest.raises(Slice1Error):
        env.models.save("alice", update)
    assert env.models.view("alice")["revision"] == 2


@pytest.mark.parametrize("vector", [[0.0] * 32, [float("nan")] * 32,
                                   [float("inf")] * 32, [True] * 32, [1.0] * 31])
def test_invalid_vectors_rejected(vector):
    with pytest.raises(Slice1Error):
        finite_vector(vector, 32)


def test_real_provider_request_shapes_and_jev_probabilities(env, monkeypatch):
    import backend.memory.providers as providers
    calls = []
    def request(base, path, key, payload, **kwargs):
        calls.append((path, key, payload))
        if path == "/embeddings":
            return {"data": [{"index": 0, "embedding": [1.0] * 1024}]}
        if path == "/v1/systemone":
            return {"answers": {"rank": {"probabilities": {"c0": .1, "c1": .9}}}}
        return {"results": [{"index": 0, "relevance_score": .1},
                            {"index": 1, "relevance_score": .9}]}
    monkeypatch.setattr(providers, "request_json", request)
    env.models.save("alice", body())
    client = ProviderClient(env.models.resolve("alice"))
    assert len(client.embed(["synthetic"])[0]) == 1024
    assert calls[0][2]["dimensions"] == 1024
    assert client.rerank("synthetic", ["a", "b"])[0][0] == 1
    assert calls[0][1] == "synthetic-embedding-key"
    assert "user" not in calls[0][2]
    env.models.save("alice", body(1, "jev"))
    assert ProviderClient(env.models.resolve("alice")).rerank("q", ["a", "b"])[0][0] == 1
    assert calls[-1][0] == "/v1/systemone"


def test_content_reasoning_settings_persist_and_reach_provider(env, monkeypatch):
    import backend.memory.providers as providers
    calls = []
    def request(base, path, key, payload, **kwargs):
        calls.append(payload)
        return {"choices": [{"index": 0, "message": {"content": "OK"}}]}
    monkeypatch.setattr(providers, "request_json", request)
    update = body()
    update.content.reasoning_settings = {"mode": "effort", "effort": "high",
        "manual": True, "show_summary": False}
    value = env.models.save("alice", update)
    assert value["content"]["reasoning_settings"]["effort"] == "high"
    assert "reasoning_settings" not in value["embedding"]
    ProviderClient(env.models.resolve("alice")).chat([{"role": "user", "content": "synthetic"}])
    assert calls[-1]["reasoning_effort"] == "high"


def test_reasoning_settings_rejected_for_non_content_models(env):
    update = body()
    update.embedding.reasoning_settings = {"mode": "default", "show_summary": False}
    with pytest.raises(Slice1Error):
        env.models.save("alice", update)


def test_memory_reasoning_discovery_reuses_saved_content_key(env):
    env.models.save("alice", body())
    seen = {}
    class Discovery:
        async def detect(self, **kwargs):
            seen.update(kwargs)
            return {"state": "completed", "options": [], "parameter_validation": "unknown"}
    env.models._reasoning_discovery = Discovery()
    result = asyncio.run(env.models.discover_reasoning("alice",
        PersonalReasoningIn(endpoint="https://content.example.test", model="content")))
    assert result["state"] == "completed"
    assert seen["key"] == "synthetic-content-key"
    assert seen["protocol"] == "openai"
    assert seen["endpoint"] == "https://content.example.test/v1"
    with pytest.raises(Slice1Error):
        asyncio.run(env.models.discover_reasoning("alice",
            PersonalReasoningIn(endpoint="https://other.example.test", model="content")))


def test_corpus_no_zero_match_cross_owner_or_deleted_results(env):
    material(env)
    material(env, "bob", "bob-doc", "数据库属于 Bob 的私人文本。")
    hits, _ = search_corpus(env.db, "alice", ["synthetic-doc", "bob-doc"], "事务")
    assert hits and {h["material_id"] for h in hits} == {"synthetic-doc"}
    assert not search_corpus(env.db, "alice", ["synthetic-doc"], "zzzznonexistent")[0]
    assert not search_corpus(env.db, "alice", ["bob-doc"], "数据库")[0]
    from backend.memory.models import now
    with env.db.session() as session:
        session.get(MaterialRow, "synthetic-doc").deleted_at = now()
    assert not search_corpus(env.db, "alice", ["synthetic-doc"], "数据库")[0]


def test_index_checkpoints_and_embedding_space_rebuild(env, monkeypatch):
    env.models.save("alice", body())
    material(env)
    calls = []
    def embed(self, texts):
        calls.append((self.spec["owner"], self.spec["dimensions"]))
        return [[1.0] + [0.0] * (self.spec["dimensions"] - 1) for _ in texts]
    monkeypatch.setattr(ProviderClient, "embed", embed)
    monkeypatch.setattr(ProviderClient, "rerank", lambda self, q, ds: [(i, 1) for i in range(len(ds))])
    worker = CorpusWorker(env.db)
    assert worker.tick()
    with env.db.session() as session:
        assert session.scalar(select(CorpusIndex.status)) == "ready"
    assert search_corpus(env.db, "alice", ["synthetic-doc"], "transaction")[0]
    env.models.save("alice", body(1, dims=64, model="new-space"))
    search_corpus(env.db, "alice", ["synthetic-doc"], "事务")
    assert worker.tick()
    assert calls[-1] == ("alice", 1024)
    with env.db.session() as session:
        assert len(list(session.scalars(select(CorpusIndex)))) == 2


def test_structural_sections_and_long_parent_are_not_arbitrary_slices():
    state = {"page_results": [{"page_no": 1, "blocks": [
        {"type": "title", "content": "第1章 事务", "level": 1},
        {"type": "text", "content": "定义。" * 1000}]}]}
    chunks = segments(state, "synthetic")
    assert all(c["section_id"].startswith("synthetic/") for c in chunks)
    assert chunks[0]["text"] == "第1章 事务"
    page_text = "第1章 事务\n" + "定义。" * 1000
    assert all(c["text"] == page_text[c["offset"]:c["end_offset"]] for c in chunks)
    assert len(chunks) > 2 and chunks[1]["parent_id"] == chunks[2]["parent_id"]


def test_auxiliary_chart_text_is_retrievable_without_verbatim_offsets():
    state = {"page_results":[{"page_no":1,"blocks":[
        {"type":"title","content":"第1章 图示","level":1},
        {"type":"figure","content":"","image_understanding":"synthetic diagram"}]}],
        "chunks":[{"page_no":1,"kind":"image_understanding",
                   "text":"图像理解（非逐字 OCR）：synthetic diagram"}]}
    blocks = segments(state,"synthetic")
    auxiliary = next(b for b in blocks if b["kind"] == "image_understanding")
    assert auxiliary["offset"] is None and auxiliary["end_offset"] is None
    assert auxiliary["section_id"].startswith("synthetic/")


def test_proxy_authenticated_bank_routing_concurrent_keys_and_dimensions(env, monkeypatch):
    monkeypatch.setenv("MEMORY_PROVIDER_PROXY_KEY", "synthetic-proxy-key")
    for owner, dims in (("alice", 1024), ("bob", 1024)):
        env.models.save(owner, body(dims=dims))
        spec = env.models.resolve(owner)
        with env.db.session() as session:
            session.add(MemorySettingsRow(owner_user_id=owner, enabled=True, revision=1))
            session.add(MemorySpaceRow(space_id=owner, owner_user_id=owner, scope_key="general",
                bank_id=owner + "-bank", status="active", epoch=1))
            session.add(MemoryBankRoute(bank_id=owner + "-bank", owner_user_id=owner,
                revision=1, embedding_space=spec["embedding_space"]))
    observed = []
    def embed(self, texts):
        if self.guard:
            self.guard()
        observed.append((self.spec["owner"], self.spec["dimensions"], self.spec["keys"]["embedding"]))
        return [[1.0] + [0.0] * (self.spec["dimensions"] - 1) for _ in texts]
    monkeypatch.setattr(ProviderClient, "embed", embed)
    app = FastAPI()
    app.include_router(create_proxy_router(env.db))
    from backend.slice1.errors import as_detail
    from fastapi.responses import JSONResponse
    app.add_exception_handler(Slice1Error, lambda _, e: JSONResponse(as_detail(e), status_code=e.status))
    client = TestClient(app)
    def invoke(owner):
        response = client.post("/api/internal/memory/v1/embeddings",
            headers={"Authorization": "Bearer synthetic-proxy-key"},
            json={"input": ["synthetic"], "user": owner + "-bank", "model": "ignored"})
        assert response.status_code == 200
        assert len(response.json()["data"][0]["embedding"]) == 2000
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(invoke, ("alice", "bob")))
    assert {(o, d) for o, d, _ in observed} == {("alice", 1024), ("bob", 1024)}
    assert client.post("/api/internal/memory/v1/embeddings", json={}).status_code == 403
    assert client.post("/api/internal/memory/v1/embeddings",
        headers={"Authorization": "Bearer synthetic-proxy-key"},
        json={"input": ["synthetic"], "user": "unknown-bank"}).status_code == 403


def test_shadow_memory_rebuild_preserves_old_until_complete_and_cleans_old(env, monkeypatch):
    from backend.memory.migration import migration_tick
    monkeypatch.setenv("PERSONAL_MEMORY_API_URL", "http://127.0.0.1:12345")
    env.models.save("alice", body())
    with env.db.session() as session:
        session.add(MemorySettingsRow(owner_user_id="alice", enabled=True, revision=1))
        session.add(MemorySpaceRow(space_id="alice", owner_user_id="alice", scope_key="general",
            bank_id="old-bank", status="active", epoch=1))
        session.add(MemorySourceRow(source_id="source-one", owner_user_id="alice",
            scope_key="general", tag="profile", source_kind="chat_user_statement",
            source_course_id="", document_id="synthetic-one", payload_json='{"preference":"synthetic"}',
            status="completed", revision=1, settings_revision=1, epoch=1))
    env.models.rebuild_memory("alice")
    class Engine:
        client_for_bank = True
        calls = []
        async def retain(self, bank, *args):
            self.calls.append(("retain", bank))
        async def refresh(self, bank, tag):
            self.calls.append(("refresh", bank))
        async def delete_bank(self, bank):
            self.calls.append(("delete", bank))
    engine = Engine()
    service = SimpleNamespace(db=env.db, adapter=engine, audit=None)
    assert migration_tick(service)
    with env.db.session() as session:
        assert session.get(MemorySpaceRow, "alice").bank_id == "old-bank"
    for _ in range(3):
        migration_tick(service)
    with env.db.session() as session:
        space = session.get(MemorySpaceRow, "alice")
        job = session.scalar(select(MemoryMigration))
        assert space.bank_id == job.target_bank and space.epoch == 2
        assert job.status == "completed"
    assert engine.calls[-1] == ("delete", "old-bank")


@pytest.mark.parametrize("supported", [True, False])
def test_compacted_native_observations_migrate_without_replaying_empty_payload(env, monkeypatch, supported):
    from backend.memory.migration import migration_tick
    monkeypatch.setenv("PERSONAL_MEMORY_API_URL", "http://127.0.0.1:12345")
    env.models.save("alice", body())
    with env.db.session() as session:
        session.add(MemorySettingsRow(owner_user_id="alice", enabled=True, revision=1))
        session.add(MemorySpaceRow(space_id="compacted-space", owner_user_id="alice", scope_key="general",
            bank_id="old-compacted-bank", status="active", epoch=1))
        for ident in ("compacted-one", "compacted-two"):
            session.add(MemorySourceRow(source_id=ident, owner_user_id="alice", scope_key="general",
                tag="profile", source_kind="chat_user_statement", document_id=ident,
                payload_json="{}", status="compacted", revision=2, settings_revision=1, epoch=1))
    env.models.rebuild_memory("alice")
    class Engine:
        client_for_bank = True
        calls = []
        async def retain(self, *args):
            raise AssertionError("Compacted text must never be reconstructed.")
        async def transfer_compacted(self, source, target):
            self.calls.append(("transfer", source, target))
            if not supported:
                raise RuntimeError("synthetic unsupported lifecycle protocol")
        async def refresh(self, *args):
            self.calls.append(("refresh", *args))
        async def delete_bank(self, bank):
            self.calls.append(("delete", bank))
    engine = Engine()
    service = SimpleNamespace(db=env.db, adapter=engine, audit=None)
    for _ in range(6):
        migration_tick(service)
    with env.db.session() as session:
        space = session.get(MemorySpaceRow, "compacted-space")
        job = session.scalar(select(MemoryMigration))
        if supported:
            assert space.bank_id == job.target_bank and job.status == "completed"
            assert len([call for call in engine.calls if call[0] == "transfer"]) == 1
        else:
            assert space.bank_id == "old-compacted-bank" and job.status == "failed"
            assert ("delete", "old-compacted-bank") not in engine.calls


def test_legacy_bank_without_replayable_sources_cannot_be_erased_by_rebuild(env, monkeypatch):
    monkeypatch.setenv("PERSONAL_MEMORY_API_URL", "http://127.0.0.1:12345")
    env.models.save("alice", body())
    with env.db.session() as session:
        session.add(MemorySettingsRow(owner_user_id="alice", enabled=True, revision=1))
        session.add(MemorySpaceRow(space_id="alice", owner_user_id="alice", scope_key="general",
            bank_id="legacy-private-bank", status="active", epoch=1))
    with pytest.raises(Slice1Error):
        env.models.rebuild_memory("alice")
    with env.db.session() as session:
        assert session.get(MemorySpaceRow, "alice").bank_id == "legacy-private-bank"
        assert session.scalar(select(MemoryMigration)) is None


def test_pinned_wire_uses_actual_personal_key_and_no_identity(env, monkeypatch):
    import httpx2
    from backend.agents import native
    env.models.save("alice", body())
    calls = []
    async def respond(request):
        calls.append(request)
        payload = json.loads(await request.aread())
        assert "user" not in payload
        return httpx2.Response(200, json={"data":[{"index":0,"embedding":[1.0]*1024}]})
    real_transport = native.SafeModelTransport
    def transport(*args, **kwargs):
        kwargs["inner"] = httpx2.MockTransport(respond)
        kwargs["resolver"] = lambda *a: [(2,1,6,"",("8.8.8.8",443))]
        return real_transport(*args, **kwargs)
    monkeypatch.setattr(native, "SafeModelTransport", transport)
    monkeypatch.setattr("backend.slice1.adapters.base.validate_endpoint", lambda *a, **k: None)
    result = ProviderClient(env.models.resolve("alice")).embed(["synthetic"])
    assert len(result[0]) == 1024
    assert calls[0].headers["authorization"] == "Bearer synthetic-embedding-key"
    assert calls[0].headers["host"] == "embedding.example.test"
    assert calls[0].url.host == "8.8.8.8"


def test_concurrent_provider_wires_never_exchange_account_keys(env, monkeypatch):
    import httpx2
    from backend.agents import native
    a, b = body(), body(dims=64, model="bob-embedding")
    from pydantic import SecretStr
    b.embedding.api_key = SecretStr("synthetic-bob-embedding-key")
    env.models.save("alice", a)
    env.models.save("bob", b)
    seen = []
    async def respond(request):
        payload = json.loads(await request.aread())
        seen.append((payload["model"], request.headers["authorization"], payload["dimensions"]))
        return httpx2.Response(200, json={"data":[{"index":0,"embedding":[1.0]*payload["dimensions"]}]})
    real = native.SafeModelTransport
    def transport(*args, **kwargs):
        return real(*args, **{**kwargs,"inner":httpx2.MockTransport(respond),
            "resolver":lambda *a:[(2,1,6,"",("8.8.8.8",443))]})
    monkeypatch.setattr(native,"SafeModelTransport",transport)
    monkeypatch.setattr("backend.slice1.adapters.base.validate_endpoint",lambda *a,**k:None)
    def invoke(owner):
        return ProviderClient(env.models.resolve(owner)).embed(["synthetic"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(invoke,("alice","bob")))
    assert set(seen)=={
        ("synthetic-embedding","Bearer synthetic-embedding-key",1024),
        ("bob-embedding","Bearer synthetic-bob-embedding-key",1024)}


def test_personal_http_endpoints_are_member_owned_and_reject_owner_override(env):
    from backend.memory.router import create_memory_router
    from backend.slice1.errors import as_detail
    from fastapi.responses import JSONResponse
    class Audit:
        def append(self, *a, **kw):
            pass
    service = SimpleNamespace(db=env.db, audit=Audit())
    app = FastAPI()
    def resolve(request):
        return SimpleNamespace(user_id=request.headers.get("x-synthetic-user", "alice"), is_admin=False)
    app.include_router(create_memory_router(service, resolve, None))
    app.add_exception_handler(Slice1Error, lambda _, e: JSONResponse(as_detail(e), status_code=e.status))
    client = TestClient(app)
    for owner, name in (("alice", "alice-embedding"), ("bob", "bob-embedding")):
        response = client.put("/api/memory/models",
            headers={"x-synthetic-user": owner},
            json=body(model=name).model_dump(mode="json", exclude={"content"} ) | {
                "content": None,
                "embedding": {"endpoint":"https://embedding.example.test","model":name,"api_key":"synthetic-key"},
                "rerank":{"endpoint":"https://rerank.example.test","model":"synthetic-rerank","api_key":"synthetic-key"}})
        assert response.status_code == 200, response.json()
    assert client.get("/api/memory/models").json()["embedding"]["model"] == "alice-embedding"
    assert client.get("/api/memory/engine").status_code == 403
    assert client.put("/api/memory/models", json={"owner_user_id": "bob"}).status_code == 422
    assert client.delete("/api/memory/models").status_code == 200
    with pytest.raises(Slice1Error):
        env.models.resolve("alice")
    assert env.models.resolve("bob")["embedding"]["model"] == "bob-embedding"


def test_dimension_detection_uses_1024_target(env, monkeypatch):
    env.models.save("alice", body())
    seen = []
    def embed(self, texts, **kwargs):
        requested = kwargs.get("dimensions", self.spec.get("request_dimensions"))
        seen.append(requested)
        width = requested or self.spec.get("dimensions") or 4096
        return [[1.0] + [0.0] * (width - 1) for _ in texts]
    monkeypatch.setattr(ProviderClient, "embed", embed)
    value = env.models.detect("alice")
    assert value["dimensions"] == 1024
    assert value["request_dimensions"] == 1024
    assert value["embedding_status"] == "detected"
    assert seen[0] == 1024


def test_dimension_detection_falls_back_to_native(env, monkeypatch):
    env.models.save("alice", body())
    def embed(self, texts, **kwargs):
        requested = kwargs.get("dimensions") if "dimensions" in kwargs else self.spec.get("request_dimensions")
        if requested is not None:
            raise Slice1Error("capability_test_failed", "synthetic")
        return [[1.0] + [0.0] * 4095 for _ in texts]
    monkeypatch.setattr(ProviderClient, "embed", embed)
    value = env.models.detect("alice")
    assert value["dimensions"] == 4096
    assert value["request_dimensions"] is None
    assert value["memory_compatible"] is False


def test_dimension_detection_keeps_smaller_native(env, monkeypatch):
    env.models.save("alice", body())
    def embed(self, texts, **kwargs):
        return [[1.0] + [0.0] * 767 for _ in texts]
    monkeypatch.setattr(ProviderClient, "embed", embed)
    value = env.models.detect("alice")
    assert value["dimensions"] == 768
    assert value["request_dimensions"] is None
    assert value["memory_compatible"] is True
