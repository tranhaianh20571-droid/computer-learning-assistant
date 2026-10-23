"""Reasoning CRUD, ownership, audit, and version fences (isolated harness only)."""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.responses import JSONResponse

from backend.logging_audit.db import Database
from backend.slice1.config_service import ConfigService
from backend.slice1.errors import Slice1Error
from backend.slice1.models import ServiceConfigRow
from backend.slice1.reasoning import DEFAULT_SETTINGS, RULES_VERSION
from backend.slice1.router import create_slice1_router


@pytest.fixture(scope="module")
def database():
    db = Database()
    db.create_all()
    yield db
    db.dispose()


@pytest.fixture
def context(database):
    service = ConfigService(database, encryption_key="synthetic-reasoning-encryption")
    owner = "reasoning_owner_" + uuid.uuid4().hex[:12]
    app = FastAPI()
    @app.exception_handler(Slice1Error)
    async def controlled(request, exc):
        return JSONResponse({"detail": {"error_code": exc.code, "message": exc.message}}, status_code=exc.status)
    app.include_router(create_slice1_router(configs=service, capabilities=None, disclosures=None, connectors=None,
        resolve_user=lambda request: SimpleNamespace(user_id=request.headers.get("x-synthetic-owner", owner), is_admin=False)))
    with TestClient(app) as client:
        yield service, owner, client


def create(service, owner, **kwargs):
    return service.create(actor_id=owner, is_admin=False, kind="content_model", protocol="openai",
        endpoint="https://relay.invalid/v1", model_name="gpt-5.2", credentials={"api_key": "synthetic-key"}, **kwargs)


def finish(service, view, owner):
    return service.finish_reasoning_probe(view["config_id"], actor_id=owner, expected_version=view["config_version"],
        result={"evidence": "request_accepted", "error_code": None})


def test_crud_roundtrip_and_worker_metadata(context, database):
    service, owner, client = context
    value = {"mode": "effort", "effort": "high", "show_summary": False}
    response = client.post("/api/capabilities/configs", json={"kind": "content_model", "protocol": "openai",
        "endpoint": "https://relay.invalid/v1", "model_name": "gpt-5.2", "credentials": {"api_key": "synthetic-key"},
        "reasoning_settings": value})
    assert response.status_code == 201
    view = response.json()
    assert view["reasoning_settings"] == value
    assert view["reasoning_capabilities"]["verification"] == {"evidence": "unknown"}
    assert "synthetic-key" not in json.dumps(view) and "encrypted_credentials" not in view
    worker = service.active_config_for_worker(view["config_id"])
    assert worker["reasoning_settings"] == value and worker["reasoning_capabilities"]["source"] == "rule"
    with database.session() as sess:
        row = sess.get(ServiceConfigRow, view["config_id"])
        assert json.loads(row.reasoning_settings) == value
        assert "synthetic-key" not in row.encrypted_credentials
    updated = client.patch("/api/capabilities/configs/" + view["config_id"], json={"reasoning_settings": {"mode": "off", "show_summary": False}})
    assert updated.status_code == 200 and updated.json()["config_version"] == 2
    assert client.get("/api/capabilities/configs/" + view["config_id"]).json()["reasoning_settings"]["mode"] == "off"


@pytest.mark.parametrize("mutation", ["settings", "endpoint", "model", "credential", "deactivate", "rules"])
def test_evidence_invalidates_when_configuration_or_rules_change(context, database, mutation):
    service, owner, client = context
    view = create(service, owner)
    verified = finish(service, view, owner)
    assert verified["rules_version"] == RULES_VERSION and verified["settings"] == DEFAULT_SETTINGS
    assert service.get(view["config_id"], actor_id=owner, is_admin=False)["reasoning_capabilities"]["verification"]["evidence"] == "request_accepted"
    config_id = view["config_id"]
    if mutation == "deactivate":
        changed = service.deactivate(config_id, actor_id=owner, is_admin=False)
    elif mutation == "rules":
        with database.session() as sess:
            row = sess.get(ServiceConfigRow, config_id)
            old = json.loads(row.reasoning_validation)
            old["rules_version"] = "synthetic-obsolete-rules"
            row.reasoning_validation = json.dumps(old)
        changed = service.get(config_id, actor_id=owner, is_admin=False)
    else:
        values = {"settings": {"reasoning_settings": {"mode": "effort", "effort": "low"}},
                  "endpoint": {"endpoint": "https://other.invalid/v1"}, "model": {"model_name": "gpt-5.1"},
                  "credential": {"credentials": {"api_key": "synthetic-rotated-key"}}}[mutation]
        changed = service.update(config_id, actor_id=owner, is_admin=False, **values)
    assert changed["reasoning_capabilities"]["verification"] == {"evidence": "unknown"}
    stale = finish(service, view, owner)
    if mutation != "rules":
        assert stale["error_code"] == "reasoning_config_stale"


def test_probe_owner_version_and_cooling_precede_decryption(context, monkeypatch):
    service, owner, client = context
    first = create(service, owner)
    second = create(service, owner)
    def forbidden(*args, **kwargs):
        pytest.fail("unauthorized or stale operation decrypted credentials")
    monkeypatch.setattr(service, "_decrypt", forbidden)
    for actor, version, expected in [("synthetic-other-owner", 1, 404), (owner, 2, 409)]:
        with pytest.raises(Slice1Error) as caught:
            service.reserve_reasoning_probe(first["config_id"], actor_id=actor, is_admin=True, expected_version=version)
        assert caught.value.status == expected
        with pytest.raises(Slice1Error):
            service.resolve_reasoning_credentials(first["config_id"], actor_id=actor, expected_version=version)
    service.reserve_reasoning_probe(first["config_id"], actor_id=owner, is_admin=False, expected_version=1)
    service.update(first["config_id"], actor_id=owner, is_admin=False, credentials={"api_key": "synthetic-rotated-key"})
    service.deactivate(first["config_id"], actor_id=owner, is_admin=False)
    with pytest.raises(Slice1Error) as caught:
        service.reserve_reasoning_probe(second["config_id"], actor_id=owner, is_admin=False, expected_version=1)
    assert caught.value.status == 429


def test_describe_route_is_strict_local_and_authorized(context, monkeypatch):
    service, owner, client = context
    def forbidden(*args, **kwargs):
        pytest.fail("describe attempted credential read or network")
    monkeypatch.setattr(service, "_decrypt", forbidden)
    body = {"protocol": "anthropic", "endpoint": "https://relay.invalid", "model_name": "claude-sonnet-4-6"}
    response = client.post("/api/capabilities/reasoning/describe", json=body)
    assert response.status_code == 200 and response.json()["source"] == "rule"
    assert "reasoning_capabilities" not in response.json()
    assert response.json()["effort_values"] == ["low", "medium", "high"]
    for values in ({**body, "api_key": "synthetic-secret"}, {**body, "model_name": None}):
        response = client.post("/api/capabilities/reasoning/describe", json=values)
        assert response.status_code == 422 and "synthetic-secret" not in response.text


@pytest.mark.parametrize("protocol", ["openai", "anthropic", "gemini"])
def test_manual_unknown_crud_worker_version_and_isolation(context, database, protocol):
    service, owner, client = context
    value = {"mode": "effort", "effort": "high", "show_summary": False, "manual": True}
    body = {"kind": "content_model", "protocol": protocol, "endpoint": "https://relay.invalid/v1",
        "model_name": "synthetic-unlisted", "credentials": {"api_key": "synthetic-key"}, "reasoning_settings": value}
    denied = client.post("/api/capabilities/configs", json={**body, "reasoning_settings": {k: v for k, v in value.items() if k != "manual"}})
    assert denied.status_code == 422
    created = client.post("/api/capabilities/configs", json=body)
    assert created.status_code == 201
    view = created.json()
    config_id = view["config_id"]
    assert view["reasoning_settings"] == value
    assert view["reasoning_capabilities"]["source"] == "unknown"
    assert view["reasoning_capabilities"]["supported_modes"] == ["default"]
    assert "effort" in view["reasoning_capabilities"]["manual_controls"]["supported_modes"]
    assert service.active_config_for_worker(config_id)["reasoning_settings"] == value
    assert "synthetic-key" not in json.dumps(view)
    with database.session() as sess:
        assert json.loads(sess.get(ServiceConfigRow, config_id).reasoning_settings) == value
    assert client.get("/api/capabilities/configs/" + config_id, headers={"x-synthetic-owner": "synthetic-other"}).status_code == 404
    finish(service, view, owner)
    changed = client.patch("/api/capabilities/configs/" + config_id, json={"reasoning_settings": {"mode": "default", "show_summary": False}}).json()
    assert changed["config_version"] == 2
    assert changed["reasoning_settings"] == {"mode": "default", "show_summary": False}
    assert changed["reasoning_capabilities"]["verification"] == {"evidence": "unknown"}
    assert finish(service, view, owner)["error_code"] == "reasoning_config_stale"


def test_non_content_and_invalid_settings_are_rejected(context):
    service, owner, client = context
    with pytest.raises(Slice1Error):
        service.create(actor_id=owner, is_admin=True, kind="image", protocol="openai_image", owner_scope="admin",
            endpoint="https://relay.invalid/v1", model_name="synthetic", credentials={"api_key": "synthetic-key"}, reasoning_settings={})
    view = create(service, owner)
    for values in ({"reasoning_settings": None}, {"reasoning_settings": {"mode": "effort", "effort": "imaginary"}},
                   {"reasoning_settings": {"mode": "budget", "budget_tokens": True}}):
        response = client.patch("/api/capabilities/configs/" + view["config_id"], json=values)
        assert response.status_code == 422
    response = client.post("/api/capabilities/configs/" + view["config_id"] + "/reasoning/test",
        json={"config_version": 1, "endpoint": "https://other.invalid", "credentials": {"api_key": "synthetic-secret"}})
    assert response.status_code == 422 and "synthetic-secret" not in response.text
