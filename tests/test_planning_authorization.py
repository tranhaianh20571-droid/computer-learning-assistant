"""Offline production-factory planning authorization and native HTTP regression.

Run only through run_native_regression.py: credentials and owner data are synthetic,
all model traffic terminates in MockTransport, and background probes are disabled.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json
import socket
from types import SimpleNamespace
import uuid

import httpx2 as httpx
from fastapi.testclient import TestClient
from pydantic import BaseModel
import pytest
from sqlalchemy import select

from backend.agents import native
from backend.agents.contracts import GenerationInput
from backend.agents.runner import execute_agent
from backend.auth.models import UserRow
from backend.logging_audit.db import Database
from backend.logging_audit.models import TaskStateRow
from backend.logging_audit.repositories import PgAuditLog
from backend.materials.models import MaterialRow
from backend.planning.authorization import PlanningAuthorization
from backend.planning.models import (CourseRow, PlanningDraftRow, PlanningTaskStateRow,
    RouteVersionRow, OutlineVersionRow)
from backend.slice0_app import Slice0State
from backend.slice1.config_service import ConfigService
from backend.slice1.disclosure_service import DisclosureService
from backend.slice1.models import DisclosureGrantRow, ServiceConfigRow
from backend.slice3_app import Slice3State, _config_model_factory, create_slice3_app
from backend.tasks.service import LearningTaskRow, utcnow
from common_fixtures import OUTLINE_OK_PAYLOAD, ROUTE_OK_PAYLOAD, synthetic_output_schema
from test_v2_intake import questions


COVERAGE_TEST_PAYLOAD = {
    "scope_contract": {
        "target_outcome": "完成数据库学习项目",
        "in_scope": ["数据库"],
        "out_of_scope": [],
        "assumptions": [],
        "time_budget": {"weeks": 8, "hours_per_week": 6, "total_hours": 48},
    },
    "coverage_areas": [
        {"area": "数据库", "depth": "核心", "search_query": "数据库基础", "required": True},
    ],
    "prerequisite_policy": {"assumed_general": [], "bridge_required": [], "notes": ""},
    # Authorization suites are not quality-gate acceptance; keep the deterministic
    # gate from turning their legacy route payload into a confirm blocker.
    "status": "fallback",
}

REVIEW_TEST_PAYLOAD = {
    "verdict": "pass",
    "dimensions": {
        "coverage": "pass", "prerequisites": "pass", "sequencing": "pass",
        "actionability": "pass", "specificity": "pass", "transparency": "pass",
    },
    "issues": [],
    "repairs": [],
    "acknowledgement_required": False,
}


class OfflineWire:
    """Real OpenAI protocol response, with the native transport checks left intact."""
    def __init__(self):
        self.calls = []
        self.scope = "route"
        self.interviews = []

    def handle(self, request):
        body = json.loads(request.content)
        self.calls.append({"host": request.headers["host"], "body": body})
        schema = synthetic_output_schema(body)
        properties = schema.get("properties", {})
        if "ready_to_plan" in properties:
            context = next(json.loads(message["content"]) for message in body["messages"]
                if message["role"] == "user" and isinstance(message["content"], str)
                and message["content"].startswith("{"))
            self.interviews.append(context)
            first = context["shown_count"] == 0
            payload = {"questions": questions() if first else [], "ready_to_plan": not first,
                "planning_scope": self.scope, "learner_profile": {
                    "learning_goal": context["original_request"],
                    "detail_preference": "详细解释并提供例子",
                    "detail_status": "confirmed" if context["answers"] else "unknown"},
                "assumptions": []}
        elif "action" in properties:
            payload = {"action": "revision_request", "answer": "按要求修改课程。",
                "instruction": "修改第一门课程标题", "options": []}
        elif "operations" in properties:
            context, _ = json.JSONDecoder().raw_decode(next(m["content"] for m in body["messages"]
                if m["role"] == "user"))
            ref = next(n["node_ref"] for n in context["nodes"] if n["role"] == "course")
            payload = {"summary": "更新课程标题", "operations": [
                {"op": "update", "node_ref": ref, "title": "合成更新后的课程"}]}
        elif "scope_contract" in properties:
            payload = deepcopy(COVERAGE_TEST_PAYLOAD)
        elif "verdict" in properties:
            payload = deepcopy(REVIEW_TEST_PAYLOAD)
        elif "units" in properties:
            payload = deepcopy(OUTLINE_OK_PAYLOAD)
        elif "stages" in properties:
            payload = deepcopy(ROUTE_OK_PAYLOAD)
        elif "value" in properties:
            payload = {"value": 1}
        else:
            raise AssertionError("unexpected synthetic model output schema")
        message = {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)}
        if "response_format" not in body and body.get("tools"):
            tool = body["tools"][-1]["function"]
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "synthetic_" + uuid.uuid4().hex, "type": "function", "function": {
                    "name": tool["name"], "arguments": json.dumps(payload, ensure_ascii=False)}}]}
        return httpx.Response(200, json={"id": "synthetic_completion", "object": "chat.completion",
            "created": 1, "model": body["model"], "choices": [{"index": 0,
                "message": message, "finish_reason": "tool_calls" if "tool_calls" in message else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})


@pytest.fixture(scope="module")
def database():
    db = Database()
    db.create_all()
    yield db
    db.dispose()


@pytest.fixture
def env(database, monkeypatch):
    wire = OfflineWire()
    transport = native.SafeModelTransport
    def resolver(*args):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
    monkeypatch.setattr(native, "SafeModelTransport", lambda endpoint, deps, **kwargs:
        transport(endpoint, deps, inner=httpx.MockTransport(wire.handle), resolver=resolver, **kwargs))
    configs = ConfigService(database, encryption_key="synthetic-planning-encryption-key")
    disclosures = DisclosureService(database, PgAuditLog(database))
    factory = _config_model_factory(configs)
    # Capability probes are separate from task execution and have no place in this offline suite.
    monkeypatch.setattr(factory.refresher, "register", lambda *args: None)
    state = Slice3State(database, PgAuditLog(database), model_adapter_factory=factory,
        search_adapter_factory=lambda *args: None)
    state.planning.authorization = PlanningAuthorization(configs, disclosures)
    monkeypatch.setattr(state.planning.background, "start", lambda: None)
    app = create_slice3_app(state)
    app.state.slice0 = Slice0State(database, PgAuditLog(database))
    client = TestClient(app)
    from common_fixtures import make_user, auth_headers
    owner, token = make_user(client, database)
    client.headers.update(auth_headers(token))
    result = SimpleNamespace(db=database, state=state, client=client, owner=owner,
        configs=configs, disclosures=disclosures, factory=factory, wire=wire)
    result.config = make_config(result)
    result.route, result.task = make_task(result)
    yield result
    state.planning.background.close()
    factory.refresher.close()
    client.close()


def make_config(env, *, owner=None, endpoint="https://planning.example.test/v1"):
    return env.configs.create(actor_id=owner or env.owner, is_admin=False,
        kind="content_model", protocol="openai", endpoint=endpoint, model_name="gpt-test",
        credentials={"api_key": "synthetic-planning-api-key"})


def make_task(env, *, kind="route_generate", target=None, materials=None):
    if target is None:
        if kind == "outline_generate":
            target = "course_" + uuid.uuid4().hex
            with env.db.session() as sess:
                sess.add(CourseRow(course_id=target, owner_user_id=env.owner, title_ref="合成数据库课程"))
        else:
            response = env.client.post("/api/planning/routes", json={
                "title": "合成数据库路线", "input": "希望详细学习数据库并独立完成项目"})
            assert response.status_code == 201
            target = response.json()["route_id"]
    created = env.state.planning.create_planning_task(owner_user_id=env.owner, task_kind=kind,
        scenario="self_study", planning_scope="course" if kind == "outline_generate" else "auto",
        target_id=target, material_ids=materials or [], idempotency_key=uuid.uuid4().hex)
    return target, created["task_id"]


def grants(env, task=None):
    return env.disclosures.list_for_task(task or env.task, actor_id=env.owner)


def prepare(env, task=None, *, automatic=False, dispatch=True):
    return env.state.planning.workspace.resume(
        task or env.task, env.owner, automatic=automatic, dispatch=dispatch,
    )


def mutate(env, model, ident, **values):
    with env.db.session() as sess:
        row = sess.get(model, ident)
        for key, value in values.items():
            setattr(row, key, value)


def failed(env, code="DISCLOSURE_REQUIRED"):
    mutate(env, LearningTaskRow, env.task, status="failed", lease_token=None, lease_until=None)
    mutate(env, TaskStateRow, env.task, status="failed", error_code=code)


def material(env, owner=None):
    ident = "mat_" + uuid.uuid4().hex
    with env.db.session() as sess:
        sess.add(MaterialRow(material_id=ident, owner_user_id=owner or env.owner,
            filename_hash=uuid.uuid4().hex * 2, source_bytes_sha256=uuid.uuid4().hex * 2,
            kind="txt", size_bytes=1))
    return ident


class TinyOutput(BaseModel):
    value: int


def execute_bound(env, source):
    return asyncio.run(execute_agent(source, TinyOutput,
        GenerationInput.from_messages([{"role": "user", "content": "synthetic"}]), source.agent_deps))


def claim_source(env, task=None):
    ident = task or env.task
    env.state.tasks.claim(ident, lease_owner="synthetic-offline-worker")
    return env.factory(env.owner, ident)


def assert_denied(call, code):
    with pytest.raises(Exception) as caught:
        call()
    assert getattr(caught.value, "code", "").lower() == code.lower()


@pytest.mark.parametrize("scope", ["route", "course"])
def test_guest_real_factory_interview_decision_persists_route_or_outline(env, scope):
    env.wire.scope = scope
    assert grants(env) == []
    with env.db.session() as sess:
        assert sess.get(UserRow, env.owner).status == "approved"
        encrypted = sess.get(ServiceConfigRow, env.config["config_id"]).encrypted_credentials
        assert "synthetic-planning-api-key" not in encrypted
    initial = prepare(env, automatic=True)
    grant = grants(env)[0]
    assert initial["task_status"] == "queued" and grant["config_id"] == env.config["config_id"]
    assert grant["scope_snapshot"] == {"material_ids": []} and env.wire.calls == []
    response = env.client.post(f"/api/planning/tasks/{env.task}/run")
    assert response.status_code == 200
    assert response.json()["wait_state"] == "awaiting_intake"
    pending = env.client.get(f"/api/planning/tasks/{env.task}").json()["intake"]["questions"]
    assert len(pending) == 5 and len(env.wire.calls) == 1
    answers = [{"question_id": question["question_id"], "selections": ["a"],
        "free_text": "合成学习经历" if question["purpose"] == "prior_experience" else ""} for question in pending]
    answers[-1] = {"question_id": "learning_time_budget", "time_budget": {"weeks": 8, "hours_per_week": 6}}
    response = env.client.post(f"/api/planning/tasks/{env.task}/decision", json={"kind": "intake", "answers": answers})
    assert response.status_code == 200
    current = env.client.get(f"/api/planning/tasks/{env.task}").json()
    assert current["task_status"] == "succeeded", current.get("error_code")
    assert current["intake"]["done"] and len(current["intake"]["answers"]) == 5
    assert len(grants(env)) == 1 and grants(env)[0]["grant_id"] == grant["grant_id"]
    with env.db.session() as sess:
        draft = sess.get(PlanningDraftRow, current["draft_id"])
        assert draft is not None and draft.saved_state == "saved"
        assert draft.draft_kind == ("route" if scope == "route" else "course_outline")
    count = len(env.wire.calls)
    for _ in range(3):
        assert env.client.get("/api/planning/current").status_code == 200
        assert env.client.get(f"/api/planning/tasks/{env.task}").status_code == 200
    assert len(env.wire.calls) == count and len(grants(env)) == 1
    confirmed = env.client.post(f"/api/planning/drafts/{current['draft_id']}/confirm", json={
        "revision": 1, "idempotency_key": uuid.uuid4().hex})
    assert confirmed.status_code == 200
    with env.db.session() as sess:
        draft = sess.get(PlanningDraftRow, current["draft_id"])
        cls = RouteVersionRow if scope == "route" else OutlineVersionRow
        column = cls.route_id if scope == "route" else cls.course_id
        version = sess.scalar(select(cls).where(column == draft.target_id))
        assert version is not None
        assert version.source_task_id.startswith("confirm:")
    assert env.wire.interviews[1]["answers"][1]["free_text"] == "合成学习经历"
    assert all(call["host"] == "planning.example.test" for call in env.wire.calls)


def test_synchronous_outline_run_prepares_without_prior_resume(env):
    _, task = make_task(env, kind="outline_generate")
    assert grants(env, task) == []
    response = env.client.post(f"/api/planning/tasks/{task}/run")
    assert response.status_code == 200 and response.json()["task_status"] == "succeeded"
    assert len(grants(env, task)) == 1 and len(env.wire.calls) >= 1


def test_get_and_automatic_old_disclosure_failure_never_grant_or_requeue(env):
    failed(env)
    before = env.state.planning.get_planning_state(env.task, actor_id=env.owner)
    for _ in range(3):
        env.client.get("/api/planning/current")
        env.client.get(f"/api/planning/tasks/{env.task}")
        current = prepare(env, automatic=True)
        assert current["task_status"] == "failed" and current["auto_retry_count"] == 0
        assert current["generation"] == before["generation"]
    assert grants(env) == [] and env.wire.calls == []
    current = prepare(env)
    assert current["task_id"] == env.task and current["task_status"] == "queued"
    assert current["generation"] == before["generation"] + 1 and len(grants(env)) == 1


def test_repeated_concurrent_prepare_creates_one_grant(env):
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: prepare(env, automatic=True), range(3)))
    assert all(result["task_status"] == "queued" for result in results)
    assert len(grants(env)) == 1 and env.wire.calls == []


def test_factory_without_entry_preparation_never_uses_latest_or_sends(env):
    assert_denied(lambda: env.factory(env.owner, env.task), "disclosure_required")
    assert grants(env) == [] and env.wire.calls == []


def test_active_grant_pins_original_config_when_newer_exists(env):
    prepare(env)
    original = grants(env)[0]
    newer = make_config(env, endpoint="https://newer.example.test/v1")
    assert newer["config_id"] != original["config_id"]
    prepare(env, automatic=True)
    source = claim_source(env)
    assert source.config_id == original["config_id"]
    execute_bound(env, source)
    assert len(env.wire.calls) == 1 and env.wire.calls[0]["host"] == "planning.example.test"
    assert len(grants(env)) == 1


@pytest.mark.parametrize("change,code", [("revoke", "disclosure_revoked"),
    ("expire", "disclosure_expired"), ("version", "model_credential_invalid"),
    ("owner", "TASK_CANCELLED"), ("material_scope", "TASK_CANCELLED")])
def test_native_bound_factory_rechecks_authority_before_wire(env, change, code):
    prepare(env)
    record = grants(env)[0]
    source = claim_source(env)
    if change == "revoke":
        env.disclosures.revoke(record["grant_id"], actor_id=env.owner)
    elif change == "expire":
        mutate(env, DisclosureGrantRow, record["grant_id"], expires_at=utcnow() - timedelta(seconds=1))
    elif change == "version":
        env.configs.update(env.config["config_id"], actor_id=env.owner, is_admin=False, model_name="gpt-updated")
    elif change == "owner":
        mutate(env, PlanningTaskStateRow, env.task, owner_user_id="synthetic-other-owner")
    else:
        mutate(env, PlanningTaskStateRow, env.task, material_ids_json=json.dumps([material(env)]))
    assert_denied(lambda: execute_bound(env, source), code)
    assert env.wire.calls == [] and source.agent_deps.budget.wire_requests == 0


@pytest.mark.parametrize("change", ["expire", "version"])
def test_explicit_preparation_refreshes_expiry_or_changed_config_version(env, change):
    prepare(env)
    old = grants(env)[0]
    if change == "expire":
        mutate(env, DisclosureGrantRow, old["grant_id"], expires_at=utcnow() - timedelta(seconds=1))
    else:
        env.configs.update(env.config["config_id"], actor_id=env.owner, is_admin=False, model_name="gpt-updated")
    failed(env)
    assert prepare(env, automatic=True)["task_status"] == "failed"
    assert len(grants(env)) == 1
    assert prepare(env)["task_status"] == "queued"
    assert len(grants(env)) == 2
    source = claim_source(env)
    execute_bound(env, source)
    assert len(env.wire.calls) == 1
    assert source.config_version == (2 if change == "version" else 1)


def test_revocation_is_not_overridden_by_explicit_retry(env):
    prepare(env)
    env.disclosures.revoke(grants(env)[0]["grant_id"], actor_id=env.owner)
    failed(env)
    assert_denied(lambda: prepare(env), "disclosure_revoked")
    assert len(grants(env)) == 1 and env.wire.calls == []


def test_cross_owner_resume_and_factory_are_denied(env):
    other = "guest_" + uuid.uuid4().hex
    assert_denied(lambda: env.state.planning.workspace.resume(env.task, other), "task_not_found")
    assert_denied(lambda: env.factory(other, env.task), "task_not_found")
    assert grants(env) == [] and env.wire.calls == []


def test_material_scope_is_exact_and_foreign_material_never_grants(env):
    first, second = material(env), material(env)
    _, task = make_task(env, materials=[second, first, first])
    prepare(env, task)
    record = grants(env, task)[0]
    assert record["content_category"] == "material"
    assert record["scope_snapshot"] == {"material_ids": sorted([first, second])}
    foreign = material(env, owner="synthetic-other-owner")
    mutate(env, PlanningTaskStateRow, env.task, material_ids_json=json.dumps([foreign]))
    assert_denied(lambda: prepare(env), "access_denied")
    assert grants(env) == [] and env.wire.calls == []


def test_repair_derives_material_subset_and_root_revocation_blocks_new_repair(env):
    first, second = material(env), material(env)
    mutate(env, PlanningTaskStateRow, env.task, material_ids_json=json.dumps([first, second]))
    prepare(env)
    root = grants(env)[0]
    _, repair = make_task(env, kind="planning_revise", target=env.route, materials=[first])
    prepare(env, repair)
    child = grants(env, repair)[0]
    assert child["parent_grant_id"] == root["grant_id"]
    assert child["scope_snapshot"] == {"material_ids": [first]}
    source = claim_source(env, repair)
    env.disclosures.revoke(root["grant_id"], actor_id=env.owner)
    assert_denied(lambda: execute_bound(env, source), "disclosure_revoked")
    _, newer = make_task(env, kind="planning_revise", target=env.route, materials=[first])
    assert_denied(lambda: prepare(env, newer), "disclosure_revoked")
    assert grants(env, newer) == [] and env.wire.calls == []
