"""Synthetic personal AnySearch config/API privacy, fencing and fail-closed audits."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.auth.models import UserRow
from backend.logging_audit.audit import AuditWriteError
from backend.logging_audit.db import Database
from backend.logging_audit.repositories import PgAuditLog
from backend.slice1.adapters import CapabilityTestService, ADAPTERS, build_adapter
from backend.slice1.adapters.base import ProbeResult
from backend.slice1.config_service import AnySearchCredentials, ConfigService
from backend.slice1.errors import Slice1Error, as_detail
from backend.slice1.models import ANYSEARCH_ENDPOINT, ADMIN_KINDS, USER_KINDS, DisclosureGrantRow, ServiceConfigRow
from backend.slice1.router import create_slice1_router

KEY = "synthetic-search-key-abcdef123456"
OTHER_KEY = "synthetic-rotated-key-abcdef123456"


@pytest.fixture(scope="module")
def db():
    database = Database()
    database.create_all()
    yield database
    database.engine.dispose()


@pytest.fixture
def env(db):
    actors = ["as_" + uuid.uuid4().hex for _ in range(3)]
    with db.session() as sess:
        for i, actor in enumerate(actors):
            sess.add(UserRow(user_id=actor, email=actor + "@example.test", password_hash="synthetic",
                status="approved", is_admin=i == 2))
    audit = PgAuditLog(db)
    configs = ConfigService(db, encryption_key="synthetic-encryption-material", audit=audit)
    return SimpleNamespace(db=db, configs=configs, audit=audit, owner=actors[0], other=actors[1], admin=actors[2])


def create(env, owner=None, **kwargs):
    values = dict(actor_id=owner or env.owner, is_admin=False, kind="search", protocol="anysearch",
        endpoint=ANYSEARCH_ENDPOINT, model_name="", credentials={"api_key": KEY})
    values.update(kwargs)
    return env.configs.create(**values)


def client(env, actor=None, is_admin=False, factory=None, is_guest=False):
    app = FastAPI()
    app.include_router(create_slice1_router(configs=env.configs,
        capabilities=CapabilityTestService(env.configs, adapter_factory=factory or (lambda *_a, **_k: None)),
        disclosures=None, connectors=None,
        resolve_user=lambda _request: SimpleNamespace(user_id=actor or env.owner, is_admin=is_admin, is_guest=is_guest)))
    @app.exception_handler(Slice1Error)
    async def controlled(_request, exc):
        return JSONResponse(status_code=exc.status, content={"detail": as_detail(exc)})
    @app.exception_handler(AuditWriteError)
    async def audit_failure(_request, _exc):
        return JSONResponse(status_code=503, content={"detail": "audit_write_failed"})
    return TestClient(app)


def body(**kwargs):
    values = dict(kind="search", protocol="anysearch", endpoint=ANYSEARCH_ENDPOINT,
        model_name="", owner_scope="user", credentials={"api_key": KEY})
    values.update(kwargs)
    return values


@pytest.mark.parametrize("changes", [{"credentials": {"api_key": None}},
    {"kind": "content_model"}, {"protocol": "openai"}, {"owner_user_id": "victim"}])
def test_guest_search_uses_strict_validation_without_echo(env, changes):
    response = client(env, actor="guest_" + uuid.uuid4().hex, is_guest=True).post(
        "/api/capabilities/configs", json=body(**changes))
    assert response.status_code == 422
    assert KEY not in response.text and "input" not in response.text


def test_guest_search_create_update_probe_and_cooldown(env):
    guest = "guest_" + uuid.uuid4().hex
    calls = []
    class Adapter:
        def probe(self, capability):
            calls.append(capability)
            return ProbeResult(capability, "available")
        def close(self):
            pass
    browser = client(env, actor=guest, is_guest=True, factory=lambda *_a, **_k: Adapter())
    saved = browser.post("/api/capabilities/configs", json=body())
    assert saved.status_code == 201 and KEY not in saved.text
    path = "/api/capabilities/configs/" + saved.json()["config_id"]
    assert browser.get("/api/capabilities/configs?kind=search").json()["data"] == [saved.json()]
    assert browser.post(path + "/test", json={}).status_code == 200
    rotated = browser.patch(path, json={"credentials": {"api_key": OTHER_KEY}})
    assert rotated.status_code == 200 and rotated.json()["config_version"] == 2
    assert OTHER_KEY not in rotated.text
    assert browser.post(path + "/test", json={}).status_code == 429
    assert browser.delete(path).status_code == 200
    replacement = browser.post("/api/capabilities/configs", json=body())
    assert replacement.status_code == 201
    assert browser.post("/api/capabilities/configs/" + replacement.json()["config_id"] + "/test", json={}).status_code == 429
    assert calls == ["text"]
    with env.db.session() as sess:
        assert sess.get(UserRow, guest) is None


def test_real_login_search_and_logout_isolation(env):
    from backend.slice0_app import Slice0State
    from backend.slice1_app import Slice1State, create_slice1_app
    from common_fixtures import FAKE_PASSWORD, make_user, auth_headers

    state = Slice1State(env.db, env.audit, encryption_key="synthetic-encryption-material")
    app = create_slice1_app(state=state, slice0_state=Slice0State(env.db, env.audit))
    browser = TestClient(app)
    assert browser.get("/api/auth/guest").status_code == 410
    assert browser.get("/api/auth/me").status_code == 401
    first_owner, first_token = make_user(browser, env.db)
    browser.headers.update(auth_headers(first_token))
    saved_guest = browser.post("/api/capabilities/configs", json=body())
    assert saved_guest.status_code == 201 and KEY not in saved_guest.text
    guest_config_id = saved_guest.json()["config_id"]
    assert browser.get("/api/capabilities/configs?kind=search").json()["data"] == [saved_guest.json()]
    with env.db.session() as sess:
        assert sess.get(UserRow, first_owner).status == "approved"
        assert sess.get(ServiceConfigRow, guest_config_id).owner_user_id == first_owner

    # Existing guest model data stays with the guest, not the subsequently logged-in user.
    model = browser.post("/api/capabilities/configs", json=body(kind="content_model",
        protocol="openai", endpoint="https://api.example.test/v1", model_name="synthetic-model"))
    assert model.status_code == 201
    model_id = model.json()["config_id"]
    email = "synthetic." + uuid.uuid4().hex + "@example.com"
    registered = app.state.slice0.auth.register(email, FAKE_PASSWORD, display_name="synthetic")
    owner = registered["user_id"]
    with env.db.session() as sess:
        sess.get(UserRow, owner).status = "approved"
    login = browser.post("/api/auth/login", json={"email": email, "password": FAKE_PASSWORD})
    assert login.status_code == 200
    browser.headers.pop("Authorization", None)
    assert browser.get("/api/auth/me").json()["user_id"] == owner
    assert browser.get("/api/capabilities/configs/" + model_id).status_code == 404
    assert browser.get("/api/capabilities/configs/" + guest_config_id).status_code == 404
    saved = browser.post("/api/capabilities/configs", json=body())
    assert saved.status_code == 201 and KEY not in saved.text
    config_id = saved.json()["config_id"]
    with env.db.session() as sess:
        assert sess.get(ServiceConfigRow, config_id).owner_user_id == owner
        assert sess.get(ServiceConfigRow, model_id).owner_user_id == first_owner
    assert browser.post("/api/auth/logout").status_code == 200
    assert browser.get("/api/auth/guest").status_code == 410
    assert browser.get("/api/capabilities/configs/" + model_id).status_code == 401
    browser.headers.update(auth_headers(first_token))
    assert browser.get("/api/capabilities/configs/" + model_id).status_code == 200
    assert browser.get("/api/capabilities/configs/" + config_id).status_code == 404
    assert browser.post("/api/capabilities/configs/" + config_id + "/test", json={}).status_code == 404
    assert browser.get("/api/capabilities/configs/" + guest_config_id).status_code == 200
    assert browser.post("/api/capabilities/configs", json=body()).status_code == 409
    other_guest = TestClient(app)
    _, other_token = make_user(other_guest, env.db)
    other_guest.headers.update(auth_headers(other_token))
    assert other_guest.get("/api/capabilities/configs/" + model_id).status_code == 404
    assert other_guest.get("/api/capabilities/configs/" + config_id).status_code == 404
    guest_path = "/api/capabilities/configs/" + guest_config_id
    assert other_guest.get(guest_path).status_code == 404
    assert other_guest.patch(guest_path, json={"credentials": {"api_key": OTHER_KEY}}).status_code == 404
    assert other_guest.post(guest_path + "/test", json={}).status_code == 404
    assert other_guest.delete(guest_path).status_code == 404
    assert other_guest.post("/api/capabilities/configs", json=body()).status_code == 201


def test_protocol_registry_and_factory_reject_legacy_and_custom_destination():
    assert "search" in USER_KINDS and "search" not in ADMIN_KINDS
    assert "anysearch" in ADAPTERS and "tavily_hikari" not in ADAPTERS
    for protocol, endpoint, private in [("tavily_hikari", ANYSEARCH_ENDPOINT, False),
        ("anysearch", "https://api.anysearch.com.evil.test", False),
        ("anysearch", ANYSEARCH_ENDPOINT + "/v1/search", False),
        ("anysearch", ANYSEARCH_ENDPOINT, True)]:
        with pytest.raises(Slice1Error):
            build_adapter(protocol, endpoint=endpoint, credentials={"api_key": KEY}, allow_private=private)


@pytest.mark.parametrize("credentials", [None, {}, {"api_key": None}, {"api_key": ""},
    {"api_key": "****"}, {"api_key": "sk-ab****xy"}, {"api_key": "••••"},
    {"api_key": " whitespace"}, {"api_key": "line\nfeed"}, {"api_key": True},
    {"api_key": 123}, {"api_key": [KEY]}, {"api_key": KEY, "proxy": KEY}])
def test_api_invalid_credentials_never_echo_key(env, credentials):
    response = client(env).post("/api/capabilities/configs", json=body(credentials=credentials))
    assert response.status_code == 422
    assert KEY not in response.text
    assert "input" not in response.text


@pytest.mark.parametrize("changes", [{"owner_scope": "admin"}, {"owner_user_id": "victim"},
    {"protocol": "tavily_hikari"}, {"endpoint": "https://evil.test"},
    {"endpoint": ANYSEARCH_ENDPOINT + "/"}, {"model_name": "model"}, {"grant_id": KEY}])
def test_api_strict_personal_config(env, changes):
    response = client(env).post("/api/capabilities/configs", json=body(**changes))
    assert response.status_code == 422 and KEY not in response.text


def test_personal_config_owner_encryption_fixed_mask_and_worker_projection(env):
    response = client(env).post("/api/capabilities/configs", json=body())
    assert response.status_code == 201
    view = response.json()
    assert view["credential_mask"] == "****" and KEY not in response.text
    projection = env.configs.active_search_for_owner(env.owner)
    assert projection["owner_user_id"] == env.owner and projection["owner_scope"] == "user"
    assert projection["config_version"] == 1 and projection["is_active"] is True
    assert "credentials" not in projection and "encrypted_credentials" not in projection
    assert env.configs.active_search_for_owner(env.other) is None
    with env.db.session() as sess:
        row = sess.get(ServiceConfigRow, view["config_id"])
        assert KEY not in row.encrypted_credentials and row.owner_user_id == env.owner
    assert env.configs.resolve_search_credentials(view["config_id"], owner_user_id=env.owner,
        expected_version=1) == {"api_key": KEY}


def test_duplicate_service_and_database_constraint(env):
    view = create(env)
    with pytest.raises(Slice1Error) as failure:
        create(env)
    assert failure.value.status == 409
    with pytest.raises(IntegrityError), env.db.session() as sess:
        sess.add(ServiceConfigRow(config_id="duplicate_" + uuid.uuid4().hex, owner_user_id=env.owner,
            owner_scope="user", kind="search", protocol="anysearch", endpoint=ANYSEARCH_ENDPOINT))
        sess.flush()
    assert env.configs.active_search_for_owner(env.owner)["config_id"] == view["config_id"]


@pytest.mark.parametrize("owner,scope", [(None, "user"), ("", "user"), ("orphan", "user"), (None, "admin")])
def test_database_rejects_invalid_active_search_owners(env, owner, scope):
    with pytest.raises(IntegrityError), env.db.session() as sess:
        sess.add(ServiceConfigRow(config_id="bad_" + uuid.uuid4().hex, owner_user_id=owner,
            owner_scope=scope, kind="search", protocol="anysearch", endpoint=ANYSEARCH_ENDPOINT))
        sess.flush()


@pytest.mark.parametrize("guest", [False, True])
def test_concurrent_create_is_serialized_by_owner_and_unique_index(env, guest):
    if guest:
        env.owner = "guest_" + uuid.uuid4().hex
    def attempt(_):
        try:
            return create(env)["config_id"]
        except Slice1Error as exc:
            return exc.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, range(2)))
    assert sum(isinstance(item, str) for item in outcomes) == 1
    assert outcomes.count(409) == 1


def test_other_user_and_admin_cannot_read_update_delete_probe_or_list_private_config(env):
    view = create(env)
    path = "/api/capabilities/configs/" + view["config_id"]
    for actor, admin in [(env.other, False), (env.admin, True)]:
        browser = client(env, actor=actor, is_admin=admin)
        for method, suffix, payload in [("get", "", None), ("patch", "", {"credentials": {"api_key": OTHER_KEY}}),
            ("delete", "", None), ("post", "/test", {})]:
            response = getattr(browser, method)(path + suffix, **({"json": payload} if payload is not None else {}))
            assert response.status_code == 404 and KEY not in response.text and OTHER_KEY not in response.text
        assert browser.get("/api/capabilities/configs?kind=search").json()["data"] == []
    assert env.configs.active_search_for_owner(env.owner)["config_version"] == 1


def test_wrong_owner_and_stale_version_fail_before_decrypt(env, monkeypatch):
    view = create(env)
    calls = []
    monkeypatch.setattr(env.configs, "_decrypt", lambda _cipher: calls.append(True))
    for owner, version in [(env.other, 1), (env.owner, 2), (env.owner, True)]:
        with pytest.raises(Slice1Error):
            env.configs.resolve_search_credentials(view["config_id"], owner_user_id=owner, expected_version=version)
    assert calls == []


@pytest.mark.parametrize("payload", [{"credentials": None}, {"credentials": {}},
    {"credentials": {"api_key": "****"}}, {"endpoint": None}, {"model_name": None}, {},
    {"owner_scope": "admin"}, {"config_version": 999}])
def test_update_null_mask_empty_extra_rejected_safely(env, payload):
    view = create(env)
    response = client(env).patch("/api/capabilities/configs/" + view["config_id"], json=payload)
    assert response.status_code == 422 and KEY not in response.text
    assert env.configs.resolve_search_credentials(view["config_id"], owner_user_id=env.owner,
        expected_version=1) == {"api_key": KEY}


def test_omitted_credentials_retain_key_and_rotation_fences_old_snapshot(env):
    view = create(env)
    # A fixed endpoint is harmless but no actual field change is rejected.
    response = client(env).patch("/api/capabilities/configs/" + view["config_id"], json={"endpoint": ANYSEARCH_ENDPOINT})
    assert response.status_code == 422
    assert env.configs.resolve_search_credentials(view["config_id"], owner_user_id=env.owner,
        expected_version=1) == {"api_key": KEY}
    rotated = env.configs.update(view["config_id"], actor_id=env.owner, is_admin=False, credentials={"api_key": OTHER_KEY})
    assert rotated["config_version"] == 2 and rotated["capability_status"]["text"] == "unknown"
    with pytest.raises(Slice1Error):
        env.configs.resolve_search_credentials(view["config_id"], owner_user_id=env.owner, expected_version=1)
    env.configs.deactivate(view["config_id"], actor_id=env.owner, is_admin=False)
    assert env.configs.active_search_for_owner(env.owner) is None


def test_probe_is_bounded_closes_and_cannot_reset_rate_by_rotation_or_recreation(env):
    view = create(env)
    calls, closed = [], []
    class Adapter:
        def probe(self, capability):
            calls.append(capability)
            return ProbeResult(capability, "available")
        def close(self):
            closed.append(True)
    service = CapabilityTestService(env.configs, adapter_factory=lambda *_a, **_k: Adapter())
    result = service.run(view["config_id"], actor_id=env.owner, is_admin=False)
    assert result["capability_status"]["text"]["state"] == "available" and calls == ["text"] and closed == [True]
    env.configs.update(view["config_id"], actor_id=env.owner, is_admin=False, credentials={"api_key": OTHER_KEY})
    with pytest.raises(Slice1Error) as failure:
        service.run(view["config_id"], actor_id=env.owner, is_admin=False)
    assert failure.value.status == 429 and calls == ["text"]
    env.configs.deactivate(view["config_id"], actor_id=env.owner, is_admin=False)
    replacement = create(env)
    with pytest.raises(Slice1Error) as failure:
        service.run(replacement["config_id"], actor_id=env.owner, is_admin=False)
    assert failure.value.status == 429 and calls == ["text"]


def test_late_probe_cannot_mark_rotated_key_available(env):
    view = create(env)
    closed = []
    class Adapter:
        def probe(self, capability):
            env.configs.update(view["config_id"], actor_id=env.owner, is_admin=False, credentials={"api_key": OTHER_KEY})
            return ProbeResult(capability, "available")
        def close(self):
            closed.append(True)
    result = CapabilityTestService(env.configs, adapter_factory=lambda *_a, **_k: Adapter()).run(
        view["config_id"], actor_id=env.owner, is_admin=False)
    assert result["config_version"] == 2 and result["capability_status"]["text"] == "unknown"
    assert closed == [True]


def test_probe_unknown_provider_detail_is_not_returned(env):
    view = create(env)
    class Adapter:
        def probe(self, capability):
            return ProbeResult(capability, "available", KEY)
        def close(self):
            pass
    result = CapabilityTestService(env.configs, adapter_factory=lambda *_a, **_k: Adapter()).run(
        view["config_id"], actor_id=env.owner, is_admin=False)
    assert KEY not in json.dumps(result) and result["capability_status"]["text"]["state"] == "unavailable"


@pytest.mark.parametrize("payload", [{"query": KEY}, {"capabilities": None},
    {"capabilities": ["text", "text"]}, {"capabilities": ["image"]}, {"credentials": {"api_key": KEY}}])
def test_probe_api_closed_nonprivate_contract(env, payload):
    view = create(env)
    response = client(env).post("/api/capabilities/configs/" + view["config_id"] + "/test", json=payload)
    assert response.status_code == 422 and KEY not in response.text


def test_probe_api_runs_sync_probe_off_event_loop(env):
    import asyncio
    view = create(env)
    threads = []
    class Adapter:
        def probe(self, capability):
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            threads.append(True)
            return ProbeResult(capability, "available")
        def close(self):
            pass
    response = client(env, factory=lambda *_a, **_k: Adapter()).post(
        "/api/capabilities/configs/" + view["config_id"] + "/test", json={})
    assert response.status_code == 200 and threads == [True]


def test_audit_failure_rolls_back_create_and_probe_reservation_before_io(env):
    env.audit.set_fail_write(True)
    with pytest.raises(AuditWriteError):
        create(env)
    assert env.configs.active_search_for_owner(env.owner) is None
    env.audit.set_fail_write(False)
    view = create(env)
    calls = []
    env.audit.set_fail_write(True)
    with pytest.raises(AuditWriteError):
        CapabilityTestService(env.configs, adapter_factory=lambda *_a, **_k: calls.append(True)).run(
            view["config_id"], actor_id=env.owner, is_admin=False)
    assert calls == []
    with env.db.session() as sess:
        assert sess.get(ServiceConfigRow, view["config_id"]).search_test_requested_at is None


@pytest.mark.parametrize("action", ["update", "deactivate"])
def test_audit_failure_rolls_back_configuration_and_grant_revocation(env, action):
    view = create(env)
    grant_id = "grant_" + uuid.uuid4().hex
    with env.db.session() as sess:
        sess.add(DisclosureGrantRow(grant_id=grant_id, owner_user_id=env.owner, task_id="synthetic-task",
            config_id=view["config_id"], config_version=1, service_kind="search",
            service_endpoint=ANYSEARCH_ENDPOINT, content_category="query"))
    env.audit.set_fail_write(True)
    with pytest.raises(AuditWriteError):
        if action == "update":
            env.configs.update(view["config_id"], actor_id=env.owner, is_admin=False, credentials={"api_key": OTHER_KEY})
        else:
            env.configs.deactivate(view["config_id"], actor_id=env.owner, is_admin=False)
    with env.db.session() as sess:
        row = sess.get(ServiceConfigRow, view["config_id"])
        assert row.config_version == 1 and row.is_active is True
        assert sess.get(DisclosureGrantRow, grant_id).revoked_at is None
    assert env.configs.resolve_search_credentials(view["config_id"], owner_user_id=env.owner,
        expected_version=1) == {"api_key": KEY}


def test_successful_change_revokes_old_grants_without_confirmation(env):
    view = create(env)
    grant_id = "grant_" + uuid.uuid4().hex
    with env.db.session() as sess:
        sess.add(DisclosureGrantRow(grant_id=grant_id, owner_user_id=env.owner, task_id="synthetic-task",
            config_id=view["config_id"], config_version=1, service_kind="search",
            service_endpoint=ANYSEARCH_ENDPOINT, content_category="query"))
    env.configs.update(view["config_id"], actor_id=env.owner, is_admin=False, credentials={"api_key": OTHER_KEY})
    with env.db.session() as sess:
        assert sess.get(DisclosureGrantRow, grant_id).revoked_at is not None


def test_probe_completion_audit_failure_preserves_unknown_and_closes(env, monkeypatch):
    view = create(env)
    closed = []
    original = env.audit.append
    def audit(event, **kwargs):
        if kwargs.get("result") == "probe_completed":
            raise AuditWriteError("synthetic_completion_failure")
        return original(event, **kwargs)
    monkeypatch.setattr(env.audit, "append", audit)
    class Adapter:
        def probe(self, capability):
            return ProbeResult(capability, "available")
        def close(self):
            closed.append(True)
    with pytest.raises(AuditWriteError):
        CapabilityTestService(env.configs, adapter_factory=lambda *_a, **_k: Adapter()).run(
            view["config_id"], actor_id=env.owner, is_admin=False)
    assert closed == [True]
    assert env.configs.get(view["config_id"], actor_id=env.owner, is_admin=False)["capability_status"]["text"] == "unknown"


@pytest.mark.parametrize("state", ["unknown", "unavailable"])
def test_failed_probe_replaces_previous_available_status(env, state):
    view = create(env)
    from backend.slice1.capability_refresh import stamp_status
    env.configs.set_capability_status(view["config_id"],
        stamp_status({"text": {"state": "available", "detail": ""}}, 1),
        expected_version=1, actor_id=env.owner)
    class Adapter:
        def probe(self, capability):
            return ProbeResult(capability, state)
        def close(self):
            pass
    result = CapabilityTestService(env.configs, adapter_factory=lambda *_a, **_k: Adapter()).run(
        view["config_id"], actor_id=env.owner, is_admin=False)
    assert result["capability_status"]["text"]["state"] == state
    assert env.configs.get(view["config_id"], actor_id=env.owner,
        is_admin=False)["capability_status"]["text"]["state"] == state


def test_credentials_repr_and_validation_text_hide_input():
    assert KEY not in repr(AnySearchCredentials(api_key=KEY))
    with pytest.raises(ValidationError) as failure:
        AnySearchCredentials(api_key=KEY + "\n")
    assert KEY not in str(failure.value)
