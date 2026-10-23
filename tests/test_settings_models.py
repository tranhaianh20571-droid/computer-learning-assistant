"""Synthetic provider configuration, catalog and public registration contracts."""
import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.db import Database
from backend.auth.models import UserRow
from backend.logging_audit.repositories import PgAuditLog
from backend.slice0_app import Slice0State, create_slice0_app
from backend.slice1.errors import Slice1Error
from backend.slice1.model_catalog import normalize_base, discover_models
from backend.memory import engine_config
from backend.memory.models import MemorySpaceRow


@pytest.mark.parametrize("suffix", ["", "/", "/v1", "/v1/", "/v1/chat/completions",
                                    "/v1/embeddings", "/v1/rerank", "/v1/models"])
def test_base_url_normalizes_once(suffix):
    assert normalize_base("https://synthetic.example" + suffix) == "https://synthetic.example/v1"


def test_jev_and_cloud_version_paths():
    assert normalize_base("https://synthetic.example/v1/systemone", "jev") == "https://synthetic.example"
    assert normalize_base("https://synthetic.example", "gemini").endswith("/v1beta")
    assert normalize_base("https://synthetic.example/api/v3").endswith("/api/v3")
    assert normalize_base("http://127.0.0.1:9999", allow_local=True).endswith("/v1")
    assert normalize_base("http://127.0.0.1:9999").endswith(":9999/v1")
    for address in ("ftp://127.0.0.1", "https://user:secret@synthetic.example",
                    "https://synthetic.example?key=synthetic", "http://169.254.169.254"):
        with pytest.raises(Slice1Error):
            normalize_base(address)


@pytest.mark.parametrize("protocol", ["openai", "anthropic", "gemini"])
@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:9999", "http://10.0.0.1",
                                     "http://synthetic.example:9999"])
def test_content_catalog_http_has_no_admin_or_loopback_requirement(monkeypatch, protocol, endpoint):
    client_type = httpx.Client
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "synthetic-model"}]})
    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return client_type(**kwargs, transport=httpx.MockTransport(respond))
    monkeypatch.setattr(httpx, "Client", client)
    assert discover_models(endpoint, protocol, "synthetic-key") == {"models": ["synthetic-model"]}
    assert len(seen) == 1 and seen[0].url.scheme == "http"
    assert seen[0].url.host == httpx.URL(endpoint).host


@pytest.mark.parametrize("protocol", ["openai_image", "seedance", "embedding", "rerank", "jev"])
def test_other_catalogs_keep_nonlocal_http_restriction(protocol):
    with pytest.raises(Slice1Error):
        normalize_base("http://synthetic.example", protocol)


def test_catalog_uses_headers_normalized_path_and_controls_failures(monkeypatch):
    client_type = httpx.Client
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "synthetic-model"}, {"id": "synthetic-model"},
                                                 {"id": "bad\nmodel"}]})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_type(
        **kwargs, transport=httpx.MockTransport(respond)))
    result = discover_models("https://synthetic.example", "openai", "synthetic-secret-key")
    assert result == {"models": ["synthetic-model"]}
    assert str(seen[0].url) == "https://synthetic.example/v1/models"
    assert seen[0].headers["authorization"] == "Bearer synthetic-secret-key"
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_type(
        **kwargs, transport=httpx.MockTransport(lambda _: httpx.Response(
            302, headers={"location": "http://169.254.169.254"}, text="synthetic-private-response"))))
    with pytest.raises(Slice1Error) as failed:
        discover_models("https://synthetic.example/v1", "openai", "synthetic-secret-key")
    assert "synthetic-private-response" not in str(failed.value)


@pytest.mark.parametrize("suffix", ["", "/", "/v1", "/v1/", "/v1/models",
                                    "/v1beta", "/v1beta/", "/v1beta/models"])
def test_gemini_catalog_matches_generation_route_and_auth(monkeypatch, suffix):
    from backend.slice1.adapters.gemini import GeminiAdapter

    client_type = httpx.Client
    seen = []
    def respond(request):
        seen.append(request)
        assert request.url.path == "/gateway/v1beta/models"
        assert request.headers["x-goog-api-key"] == "synthetic-gemini-key"
        assert "authorization" not in request.headers and not request.url.query
        return httpx.Response(200, json={"models": [
            {"name": "models/synthetic-gemini"}, {"name": "models/synthetic-gemini"},
            {"name": "models/bad\nmodel"}]})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_type(
        **kwargs, transport=httpx.MockTransport(respond)))
    endpoint = "https://synthetic.example/gateway" + suffix
    assert discover_models(endpoint, "gemini", "synthetic-gemini-key") == {
        "models": ["synthetic-gemini"]}
    assert len(seen) == 1
    base = normalize_base(endpoint, "gemini")
    adapter = GeminiAdapter(endpoint=base, model_name="synthetic-gemini")
    assert adapter._model_url("generateContent") == str(seen[0].url) + "/synthetic-gemini:generateContent"


class Audit:
    def __init__(self):
        self.rows = []
    def append(self, event, **fields):
        self.rows.append((event, fields))


@pytest.fixture
def engine(tmp_path, monkeypatch):
    path = tmp_path / ".env.memory"
    path.write_text(
        "HINDSIGHT_API_LLM_BASE_URL='https://synthetic.example/v1'\n"
        "HINDSIGHT_API_LLM_MODEL='synthetic-content'\n"
        "HINDSIGHT_API_LLM_API_KEY='synthetic-content-key'\n"
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL='https://synthetic.example/v1'\n"
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL='synthetic-embedding'\n"
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY='synthetic-embedding-key'\n"
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS='1024'\n"
        "HINDSIGHT_API_RERANKER_PROVIDER='typesafe'\n"
        "HINDSIGHT_API_RERANKER_TYPESAFE_BASE_URL='https://synthetic.example'\n"
        "HINDSIGHT_API_RERANKER_TYPESAFE_MODEL='synthetic-jev'\n"
        "HINDSIGHT_API_RERANKER_TYPESAFE_API_KEY='synthetic-rerank-key'\n",
        encoding="utf8")
    monkeypatch.setenv("MEMORY_CONFIG_FILE", str(path))
    db = Database("sqlite+pysqlite:///" + (tmp_path / "engine.db").as_posix())
    MemorySpaceRow.__table__.create(db.engine)
    audit = Audit()
    yield db, audit, path
    db.dispose()


def draft():
    current = engine_config.public_settings()
    return engine_config.EngineIn(expected_revision=current["revision"],
        content={"endpoint": current["content"]["endpoint"], "model": current["content"]["model"]},
        embedding={"endpoint": current["embedding"]["endpoint"], "model": current["embedding"]["model"]},
        rerank_protocol=current["rerank_protocol"],
        rerank={"endpoint": current["rerank"]["endpoint"], "model": current["rerank"]["model"]})


def test_engine_mask_revision_round_trip_and_provider_switch(engine):
    db, audit, path = engine
    original = engine_config.public_settings()
    assert "synthetic-content-key" not in json.dumps(original)
    body = draft()
    body.content.model = "synthetic-content-new"
    saved = engine_config.save_settings(body, db, audit, "synthetic-admin")
    assert saved["content"]["model"] == "synthetic-content-new"
    assert engine_config.values()["HINDSIGHT_API_LLM_API_KEY"] == "synthetic-content-key"
    assert saved["status"] == "restart_required"
    path.with_name(path.name + ".applied").write_text(saved["revision"])
    assert engine_config.public_settings()["status"] == "active"
    with pytest.raises(Slice1Error) as stale:
        engine_config.save_settings(body, db, audit, "synthetic-admin")
    assert stale.value.status == 409
    body = draft()
    body.rerank_protocol = "rerank"
    body.rerank = engine_config.ProviderIn(endpoint="https://synthetic.example/v1/rerank",
        model="synthetic-rerank", api_key="synthetic-new-key")
    engine_config.save_settings(body, db, audit, "synthetic-admin")
    config = engine_config.values()
    assert config["HINDSIGHT_API_RERANKER_PROVIDER"] == "siliconflow"
    assert config["HINDSIGHT_API_RERANKER_SILICONFLOW_BASE_URL"] == "https://synthetic.example/v1"
    assert "synthetic-new-key" not in json.dumps(audit.rows)


def test_embedding_change_with_existing_space_rejected_and_key_not_forwarded(engine):
    db, audit, path = engine
    with db.session() as session:
        session.add(MemorySpaceRow(space_id="synthetic-space", owner_user_id="synthetic-user",
            scope_key="general", bank_id="synthetic-bank", epoch=1, status="active"))
    before = path.read_bytes()
    body = draft()
    body.embedding.model = "different-embedding"
    with pytest.raises(Slice1Error) as blocked:
        engine_config.save_settings(body, db, audit, "synthetic-admin")
    assert blocked.value.status == 409
    assert path.read_bytes() == before
    body = draft()
    body.content.endpoint = "https://another-synthetic.example"
    with pytest.raises(Slice1Error):
        engine_config.save_settings(body, db, audit, "synthetic-admin")
    assert path.read_bytes() == before


def test_engine_admin_boundary(engine):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from backend.memory.router import create_memory_router
    db, audit, _ = engine
    app = FastAPI()
    from backend.slice1.errors import as_detail
    from fastapi.responses import JSONResponse
    @app.exception_handler(Slice1Error)
    async def handler(_, exc):
        return JSONResponse(status_code=exc.status, content={"detail": as_detail(exc)})
    app.include_router(create_memory_router(SimpleNamespace(db=db, audit=audit),
        lambda request: SimpleNamespace(user_id="synthetic-user", is_admin=request.headers.get("x-test-admin") == "1"), None))
    with TestClient(app) as client:
        assert client.get("/api/memory/engine").status_code == 403
        response = client.get("/api/memory/engine", headers={"x-test-admin": "1"})
        assert response.status_code == 200
        assert "synthetic-content-key" not in response.text


def test_public_registration_and_legacy_login_need_no_mail(monkeypatch):
    db = Database()
    state = Slice0State(db, PgAuditLog(db))
    app = create_slice0_app(state)
    email = "public." + uuid.uuid4().hex + "@example.com"
    password = "Synthetic-public-password-12345"
    with TestClient(app) as client:
        created = client.post("/api/auth/register", json={"email": email, "password": password})
        assert created.status_code == 201
        assert created.json()["status"] == "approved"
        assert "token" not in json.dumps(created.json())
        assert client.post("/api/auth/login", json={"email": email, "password": password}).status_code == 200
        assert not client.get("/api/auth/me").json()["is_admin"]
        legacy = state.auth.register("legacy." + email, password)
        assert client.post("/api/auth/login", json={"email": "legacy." + email, "password": "wrong"}).status_code == 401
        with db.session() as session:
            assert session.get(UserRow, legacy["user_id"]).status == "pending_email"
        assert client.post("/api/auth/login", json={"email": "legacy." + email, "password": password}).status_code == 200
        with db.session() as session:
            session.get(UserRow, legacy["user_id"]).status = "disabled"
        assert client.post("/api/auth/login", json={"email": "legacy." + email, "password": password}).status_code == 401
    db.dispose()


def test_saved_catalog_snapshot_is_owned_and_uses_one_revision():
    from backend.slice1.config_service import ConfigService
    db = Database()
    suffix = uuid.uuid4().hex
    owner, other = "catalog_" + suffix, "other_" + suffix
    with db.session() as session:
        for user in (owner, other):
            session.add(UserRow(user_id=user, email=user + "@example.test",
                password_hash="synthetic-unused", status="approved", is_admin=False))
    configs = ConfigService(db, encryption_key="synthetic-catalog-encryption")
    saved = configs.create(actor_id=owner, is_admin=False, kind="content_model", protocol="openai",
        endpoint="https://model.synthetic.test/v1", model_name="synthetic-model",
        credentials={"api_key": "synthetic-owned-key"})
    view, credentials = configs.catalog_snapshot(saved["config_id"], actor_id=owner, is_admin=False)
    assert view["config_version"] == 1 and credentials["api_key"] == "synthetic-owned-key"
    with pytest.raises(Slice1Error):
        configs.catalog_snapshot(saved["config_id"], actor_id=other, is_admin=False)
    configs.deactivate(saved["config_id"], actor_id=owner, is_admin=False)
    with pytest.raises(Slice1Error):
        configs.catalog_snapshot(saved["config_id"], actor_id=owner, is_admin=False)
    db.dispose()


def test_eight_character_registration_and_reset_boundaries():
    db = Database()
    state = Slice0State(db, PgAuditLog(db))
    email = "boundary." + uuid.uuid4().hex + "@example.com"
    with TestClient(create_slice0_app(state)) as client:
        for password in ("shortpw", "x" * 129):
            assert client.post("/api/auth/register", json={
                "email": email, "password": password}).status_code == 422
        assert client.post("/api/auth/register", json={
            "email": email, "password": "eight-pw"}).status_code == 201
        assert client.post("/api/auth/login", json={
            "email": email, "password": "eight-pw"}).status_code == 200
        token = state.auth.request_password_reset(email)["reset_token"]
        for password in ("shortpw", "x" * 129):
            assert client.post("/api/auth/password-reset/confirm", json={
                "token": token, "new_password": password}).status_code == 422
        assert client.post("/api/auth/password-reset/confirm", json={
            "token": token, "new_password": "reset-pw"}).status_code == 200
        assert client.get("/api/auth/me").status_code == 401
        assert client.post("/api/auth/login", json={
            "email": email, "password": "reset-pw"}).status_code == 200
    db.dispose()
