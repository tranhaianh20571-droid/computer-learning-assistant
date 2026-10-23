"""Owned, atomic purpose connections with immutable task routing."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.agents.model_routing import (
    ModelRoleRow, TaskModelRoleRow, role_config, role_views, role_for_task,
    save_role_configuration, reveal_content_key, task_role_config,
)
from backend.slice1.router import RoleConfigurationIn
from backend.slice1.errors import Slice1Error
from backend.tasks.service import TaskService
from backend.logging_audit.repositories import PgTaskEventStore
from test_character_cards import env  # noqa: F401


def initial(cards, owner):
    return cards.configs.create(actor_id=owner, is_admin=False, kind="content_model",
        protocol="openai", endpoint="https://original.example.test/v1", model_name="original",
        credentials={"api_key": "synthetic-owned-credential"})


def request_for(configs, owner, role, **changes):
    row = next(row for row in role_views(configs, owner) if row["role"] == role)
    config = row["configuration"]
    return RoleConfigurationIn.model_validate({
        "expected_revision": row["revision"], "config_id": row["config_id"],
        "config_version": config["config_version"] if config else None,
        "protocol": "openai", "endpoint": "https://new.example.test/v1",
        "model_name": role + "-new", **changes,
    })


@pytest.mark.parametrize("role", ["planning","teaching","dialogue","research","research_search"])
@pytest.mark.parametrize("suffix", ["","/v1"])
def test_responses_role_saves_without_probe_and_preserves_existing_bindings(env,role,suffix):
    cards,_=env
    owner="synthetic-responses-"+uuid.uuid4().hex
    original=initial(cards,owner)
    body=request_for(cards.configs,owner,role,protocol="openai_responses",
        endpoint="http://127.0.0.1:9999"+suffix,api_key="synthetic-responses-credential")
    save_role_configuration(cards.configs,owner,role,body)
    chosen=role_config(cards.configs,owner,role)
    assert chosen["protocol"]=="openai_responses" and chosen["endpoint"]==body.endpoint
    assert all(value=="unknown" or isinstance(value,dict) and value.get("state")=="unknown"
               for value in chosen["capability_status"].values())
    assert cards.configs.get(original["config_id"],actor_id=owner,is_admin=False)["is_active"]
    for other in ("planning","teaching","dialogue"):
        if other!=role:
            assert role_config(cards.configs,owner,other)["config_id"]==original["config_id"]


def test_purpose_save_freezes_legacy_fallback_and_preserves_old_config(env):
    cards, _ = env
    owner = "synthetic-settings-" + uuid.uuid4().hex
    original = initial(cards, owner)
    save_role_configuration(cards.configs, owner, "teaching", request_for(cards.configs, owner, "teaching"))
    lecture = role_config(cards.configs, owner, "teaching")
    assert lecture["config_id"] != original["config_id"]
    assert role_config(cards.configs, owner, "planning")["config_id"] == original["config_id"]
    assert role_config(cards.configs, owner, "dialogue")["config_id"] == original["config_id"]
    assert role_config(cards.configs, owner, "research") is None
    assert cards.configs.get(original["config_id"], actor_id=owner, is_admin=False)["config_version"] == 1
    assert reveal_content_key(cards.configs, owner, lecture["config_id"]) == "synthetic-owned-credential"
    save_role_configuration(cards.configs, owner, "dialogue",
        request_for(cards.configs, owner, "dialogue", api_key="synthetic-chat-credential"))
    assert role_config(cards.configs, owner, "teaching")["config_id"] == lecture["config_id"]
    assert role_config(cards.configs, owner, "dialogue")["model_name"] == "dialogue-new"


def test_new_purpose_configuration_does_not_enable_other_empty_roles(env):
    cards, _ = env
    owner = "synthetic-empty-" + uuid.uuid4().hex
    save_role_configuration(cards.configs, owner, "dialogue",
        request_for(cards.configs, owner, "dialogue", api_key="synthetic-key"))
    assert role_config(cards.configs, owner, "dialogue")
    assert role_config(cards.configs, owner, "planning") is None
    assert role_config(cards.configs, owner, "teaching") is None


@pytest.mark.parametrize("protocol", ["openai", "anthropic", "gemini"])
def test_ordinary_owner_saves_http_purpose_with_manual_reasoning(env, protocol):
    cards, _ = env
    owner = "synthetic-http-settings-" + uuid.uuid4().hex
    settings = {"mode": "effort", "effort": "low", "manual": True, "show_summary": False}
    save_role_configuration(cards.configs, owner, "teaching",
        request_for(cards.configs, owner, "teaching", protocol=protocol,
            endpoint="http://relay.example.test:9999/v1", api_key="synthetic-http-key",
            reasoning_settings=settings))
    config = role_config(cards.configs, owner, "teaching")
    assert config["endpoint"] == "http://relay.example.test:9999/v1"
    assert config["protocol"] == protocol and config["reasoning_settings"] == settings
    assert role_config(cards.configs, owner, "planning") is None
    with pytest.raises(Slice1Error):
        cards.configs.get(config["config_id"], actor_id="synthetic-other-owner", is_admin=False)


def test_stale_save_and_source_version_change_leave_no_orphan(env):
    cards, _ = env
    owner = "synthetic-stale-" + uuid.uuid4().hex
    original = initial(cards, owner)
    body = request_for(cards.configs, owner, "planning")
    cards.configs.update(original["config_id"], actor_id=owner, is_admin=False, model_name="changed")
    with pytest.raises(Slice1Error) as stale:
        save_role_configuration(cards.configs, owner, "planning", body)
    assert stale.value.status == 409
    assert len(cards.configs.list(actor_id=owner, is_admin=False)) == 1
    body = request_for(cards.configs, owner, "planning")
    save_role_configuration(cards.configs, owner, "planning", body)
    with pytest.raises(Slice1Error) as conflict:
        save_role_configuration(cards.configs, owner, "planning", body)
    assert conflict.value.status == 409
    assert len(cards.configs.list(actor_id=owner, is_admin=False)) == 2


def test_audit_failure_rolls_back_new_configuration_and_all_bindings(env, monkeypatch):
    cards, _ = env
    owner = "synthetic-rollback-" + uuid.uuid4().hex
    original = initial(cards, owner)
    body = request_for(cards.configs, owner, "teaching")
    append = cards.configs.audit.append
    def fail(event, **kwargs):
        if kwargs.get("object_type") == "model_role":
            raise RuntimeError("synthetic audit failure")
        return append(event, **kwargs)
    monkeypatch.setattr(cards.configs.audit, "append", fail)
    with pytest.raises(RuntimeError):
        save_role_configuration(cards.configs, owner, "teaching", body)
    assert [row["config_id"] for row in cards.configs.list(actor_id=owner, is_admin=False)] == [original["config_id"]]
    with cards.db.session() as sess:
        assert not list(sess.scalars(select(ModelRoleRow).where(ModelRoleRow.owner_user_id == owner)))


def test_concurrent_purpose_save_accepts_one_revision(env):
    cards, _ = env
    owner = "synthetic-concurrent-" + uuid.uuid4().hex
    initial(cards, owner)
    body = request_for(cards.configs, owner, "planning")
    def save():
        try:
            save_role_configuration(cards.configs, owner, "planning", body)
            return 200
        except Slice1Error as error:
            return error.status
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: save(), range(2))) == [200, 409]
    assert len(cards.configs.list(actor_id=owner, is_admin=False)) == 2


def test_credential_reveal_owned_active_content_only_and_public_view_stays_masked(env):
    cards, _ = env
    owner = "synthetic-reveal-" + uuid.uuid4().hex
    config = initial(cards, owner)
    assert "synthetic-owned-credential" not in str(cards.configs.list(actor_id=owner, is_admin=False))
    with pytest.raises(Slice1Error) as denied:
        reveal_content_key(cards.configs, "another-owner", config["config_id"])
    assert denied.value.status == 404
    assert reveal_content_key(cards.configs, owner, config["config_id"]) == "synthetic-owned-credential"
    cards.configs.deactivate(config["config_id"], actor_id=owner, is_admin=False)
    with pytest.raises(Slice1Error):
        reveal_content_key(cards.configs, owner, config["config_id"])


def test_chat_task_uses_dialogue_and_keeps_pinned_destination_after_save(env):
    cards, _ = env
    owner = "synthetic-chat-route-" + uuid.uuid4().hex
    initial(cards, owner)
    save_role_configuration(cards.configs, owner, "dialogue", request_for(cards.configs, owner, "dialogue"))
    assert role_for_task(SimpleNamespace(kind="chat_answer")) == "dialogue"
    tasks = TaskService(cards.db, PgTaskEventStore(cards.db))
    task = tasks.create_task(owner_user_id=owner, kind="chat_answer", subject_id="synthetic-conversation",
        stage="generate", idempotency_key=uuid.uuid4().hex)
    before = task_role_config(cards.configs, owner, task["task_id"])
    save_role_configuration(cards.configs, owner, "dialogue",
        request_for(cards.configs, owner, "dialogue", model_name="later-chat"))
    assert task_role_config(cards.configs, owner, task["task_id"])["config_id"] == before["config_id"]
    assert role_config(cards.configs, owner, "dialogue")["model_name"] == "later-chat"
    with cards.db.session() as sess:
        assert sess.get(TaskModelRoleRow, (task["task_id"], "dialogue"))
    assert "api_key" not in before


def test_legacy_chat_keeps_its_planning_snapshot(env):
    from backend.agents.model_routing import pin_role
    cards, _ = env
    owner = "synthetic-legacy-chat-" + uuid.uuid4().hex
    original = initial(cards, owner)
    task = TaskService(cards.db, PgTaskEventStore(cards.db)).create_task(
        owner_user_id=owner, kind="chat_answer", subject_id="synthetic-legacy-conversation",
        stage="generate", idempotency_key=uuid.uuid4().hex)
    with cards.db.session() as sess:
        pin_role(sess, task["task_id"], "planning", original)
    save_role_configuration(cards.configs, owner, "dialogue",
        request_for(cards.configs, owner, "dialogue"))
    assert task_role_config(cards.configs, owner, task["task_id"])["config_id"] == original["config_id"]


def test_role_http_save_reveal_cache_control_and_cross_owner_denial(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from fastapi.responses import JSONResponse
    from backend.slice1.router import create_slice1_router
    cards, _ = env
    owner = "synthetic-http-" + uuid.uuid4().hex
    original = initial(cards, owner)
    actor = SimpleNamespace(user_id=owner, is_admin=False)
    app = FastAPI()
    app.include_router(create_slice1_router(configs=cards.configs, capabilities=None,
        disclosures=None, connectors=None, resolve_user=lambda request:actor))
    @app.exception_handler(Slice1Error)
    async def handled(request, exception):
        return JSONResponse({"error_code":exception.code}, status_code=exception.status)
    with TestClient(app) as client:
        body = request_for(cards.configs, owner, "dialogue").model_dump(mode="json", exclude_none=True, exclude={"api_key"})
        saved = client.put("/api/capabilities/model-roles/dialogue/configuration", json=body)
        assert saved.status_code == 200
        assert "synthetic-owned-credential" not in saved.text
        view = next(row["configuration"] for row in saved.json()["data"] if row["role"] == "dialogue")
        path = "/api/capabilities/configs/" + view["config_id"] + "/credential/reveal"
        reveal = client.post(path)
        assert reveal.status_code == 200
        assert reveal.headers["cache-control"] == "no-store"
        assert reveal.json()["api_key"] == "synthetic-owned-credential"
        actor.user_id = "synthetic-other"
        assert client.post(path).status_code == 404
        assert client.put("/api/capabilities/model-roles/dialogue/configuration", json={
            **body, "config_id":original["config_id"]}).status_code == 409
