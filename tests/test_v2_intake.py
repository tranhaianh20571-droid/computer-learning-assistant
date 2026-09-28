"""v2 生成前访谈：awaiting_intake 等待态、范围并入第一轮、模型追问、4 轮上限、降级。

合成适配器与合成数据；无真实凭据。流程口径：
- route_generate 任务先进入 awaiting_intake（第一轮 3 题规则问卷，范围题不可跳过）；
- decision(kind=intake) 提交答案 → 恢复任务；范围题答案直接落 resolved_scope；
- 后续轮次由模型追问（返回空 questions 即完成）；失败/无 adapter 降级直接生成；
- 轮次达到 MAX_INTAKE_ROUNDS=4 后不再提问。
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "support"))

import pytest
from fastapi.testclient import TestClient

from backend.logging_audit.db import Database
from backend.logging_audit.models import install_audit_triggers
from backend.logging_audit.repositories import PgAuditLog
from backend.slice0_app import Slice0State
from backend.slice3_app import Slice3State, create_slice3_app

from common_fixtures import (
    ROUTE_OK_PAYLOAD,
    SyntheticModelAdapter,
    auth_headers,
    make_user,
)

PASSWORD = "synthetic-password-12345"


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db


@pytest.fixture()
def env(database: Database):
    container = {"model": None, "search": None}
    state = Slice3State(
        database, PgAuditLog(database),
        model_adapter_factory=lambda owner, task_id: container["model"],
        search_adapter_factory=lambda owner, task_id: container["search"],
    )
    app = create_slice3_app(state=state)
    app.state.slice0 = Slice0State(database, PgAuditLog(database))
    return {
        "client": TestClient(app),
        "database": database,
        "container": container,
    }


def _setup_task(env) -> tuple[str, str, str]:
    """注册用户 + 建路线壳 + 建 route_generate 任务，返回 (user_id, token, task_id)。"""
    client = env["client"]
    user_id, token = make_user(client, env["database"])
    r = client.post(
        "/api/planning/routes", headers=auth_headers(token),
        json={"title": "机器学习入门路线", "scenario": "self_study"},
    )
    assert r.status_code == 201, r.text
    route_id = r.json()["route_id"]
    r = client.post(
        "/api/planning/tasks", headers=auth_headers(token),
        json={
            "idempotency_key": f"t-{uuid.uuid4().hex[:8]}",
            "task_kind": "route_generate",
            "planning_scope": "auto",
            "target_route_id": route_id,
        },
    )
    assert r.status_code == 201, r.text
    return user_id, token, r.json()["task_id"]


def _state(client, token: str, task_id: str) -> dict:
    r = client.get(f"/api/planning/tasks/{task_id}", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    return r.json()


class TestIntakeRound1:
    def test_first_run_enters_intake_with_rule_questions(self, env) -> None:
        _, token, task_id = _setup_task(env)
        r = env["client"].post(
            f"/api/planning/tasks/{task_id}/run", headers=auth_headers(token)
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["task_status"] == "partial"
        assert body["wait_state"] == "awaiting_intake"

        st = _state(env["client"], token, task_id)
        assert st["wait_state"] == "awaiting_intake"
        intake = st["intake"]
        assert intake["round"] == 1 and intake["max_rounds"] == 4
        qids = [q["question_id"] for q in intake["questions"]]
        assert qids == ["scope", "foundation", "depth"]
        scope_q = intake["questions"][0]
        assert scope_q["skippable"] is False
        assert {o["key"] for o in scope_q["options"]} == {"course", "route"}

    def test_requires_login(self, env) -> None:
        _, _, task_id = _setup_task(env)
        r = env["client"].post(f"/api/planning/tasks/{task_id}/run")
        assert r.status_code == 401, r.text


class TestIntakeAnswersResume:
    def test_answers_merge_scope_and_generate(self, env) -> None:
        client = env["client"]
        env["container"]["model"] = SyntheticModelAdapter(payload=ROUTE_OK_PAYLOAD)
        _, token, task_id = _setup_task(env)
        client.post(f"/api/planning/tasks/{task_id}/run", headers=auth_headers(token))

        r = client.post(
            f"/api/planning/tasks/{task_id}/decision", headers=auth_headers(token),
            json={
                "kind": "intake",
                "answers": [
                    {"question_id": "scope", "selections": ["route"]},
                    {"question_id": "foundation", "selections": ["some"]},
                    {"question_id": "depth", "selections": ["practice"]},
                ],
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # 无 adapter 追问能力 → 降级直接生成；答案注入后候选生成成功
        assert body["task_status"] == "succeeded", body
        assert body.get("draft", {}).get("draft_id")

        st = _state(client, token, task_id)
        # 范围题答案直接落 resolved_scope（合并旧的范围澄清）
        assert st["resolved_scope"] == "route"
        assert st["intake"]["done"] is True
        assert st["intake"]["round"] == 1

    def test_free_text_only_answer_rejected(self, env) -> None:
        _, token, task_id = _setup_task(env)
        env["client"].post(f"/api/planning/tasks/{task_id}/run", headers=auth_headers(token))
        r = env["client"].post(
            f"/api/planning/tasks/{task_id}/decision", headers=auth_headers(token),
            json={"kind": "intake", "answers": [{"question_id": "", "selections": ["x"]}]},
        )
        assert r.status_code == 400, r.text


class TestModelFollowups:
    def test_followup_round_then_complete(self, env) -> None:
        client = env["client"]
        followup_q = {
            "questions": [{
                "question_id": "time_budget",
                "question": "每周能投入多少时间？",
                "kind": "single",
                "skippable": True,
                "options": [
                    {"key": "low", "label": "3 小时以内", "detail": ""},
                    {"key": "mid", "label": "3-10 小时", "detail": ""},
                ],
            }],
        }

        class Adapter(SyntheticModelAdapter):
            """访谈追问调用返回追问集，其余调用返回路线候选。"""

            def __init__(self):
                super().__init__(payload=ROUTE_OK_PAYLOAD)
                self.rounds = [followup_q, {"questions": []}]

            def generate(self, messages, **options):
                joined = "".join(str(m.get("content", "")) for m in messages)
                if "学习者目标与已回答内容" in joined:
                    payload = self.rounds.pop(0) if self.rounds else {"questions": []}
                    # 按请求形状应答（ADR-023：Agent 路径 tool 档优先）
                    if "tools" in options:
                        return {"tool_arguments": json.dumps(payload, ensure_ascii=False)}
                    return {"content": json.dumps(payload, ensure_ascii=False)}
                return super().generate(messages, **options)

        env["container"]["model"] = Adapter()
        _, token, task_id = _setup_task(env)
        client.post(f"/api/planning/tasks/{task_id}/run", headers=auth_headers(token))

        # 第一轮答案 → 模型追问一轮（round 2）
        r = client.post(
            f"/api/planning/tasks/{task_id}/decision", headers=auth_headers(token),
            json={"kind": "intake", "answers": [
                {"question_id": "scope", "selections": ["route"]},
                {"question_id": "foundation", "selections": ["zero"]},
                {"question_id": "depth", "selections": ["concept"]},
            ]},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["task_status"] == "partial" and body["wait_state"] == "awaiting_intake"
        st = _state(client, token, task_id)
        assert st["intake"]["round"] == 2
        assert st["intake"]["questions"][0]["question_id"] == "time_budget"

        # 第二轮答案 → 模型返回空 → 完成并生成
        r = client.post(
            f"/api/planning/tasks/{task_id}/decision", headers=auth_headers(token),
            json={"kind": "intake", "answers": [
                {"question_id": "time_budget", "selections": ["mid"]},
            ]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["task_status"] == "succeeded", r.text
        st = _state(client, token, task_id)
        assert st["intake"]["done"] is True
        assert st["intake"]["round"] == 2

    def test_followup_model_error_degrades_to_generate(self, env) -> None:
        client = env["client"]

        class Adapter(SyntheticModelAdapter):
            """追问调用直接抛错 → 降级直接生成。"""

            def generate(self, messages, **options):
                joined = "".join(str(m.get("content", "")) for m in messages)
                if "学习者目标与已回答内容" in joined:
                    raise RuntimeError("timeout")
                return super().generate(messages, **options)

        env["container"]["model"] = Adapter()
        _, token, task_id = _setup_task(env)
        client.post(f"/api/planning/tasks/{task_id}/run", headers=auth_headers(token))
        r = client.post(
            f"/api/planning/tasks/{task_id}/decision", headers=auth_headers(token),
            json={"kind": "intake", "answers": [
                {"question_id": "scope", "selections": ["course"]},
                {"question_id": "foundation", "selections": ["solid"]},
                {"question_id": "depth", "selections": ["deep"]},
            ]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["task_status"] == "succeeded", r.text
        st = _state(client, token, task_id)
        assert st["intake"]["done"] is True


class TestIntakeCapAndGuards:
    def test_round_cap_stops_asking(self, env) -> None:
        client = env["client"]
        followup_q = {
            "questions": [{
                "question_id": "always", "question": "还会追问吗？", "kind": "single",
                "skippable": True,
                "options": [{"key": "yes", "label": "会", "detail": ""},
                            {"key": "no", "label": "不会", "detail": ""}],
            }],
        }

        class Adapter(SyntheticModelAdapter):
            def __init__(self):
                super().__init__(payload=ROUTE_OK_PAYLOAD)

            def generate(self, messages, **options):
                joined = "".join(str(m.get("content", "")) for m in messages)
                if "学习者目标与已回答内容" in joined:
                    return {"content": json.dumps(followup_q, ensure_ascii=False)}
                return super().generate(messages, **options)

        env["container"]["model"] = Adapter()
        _, token, task_id = _setup_task(env)
        headers = auth_headers(token)
        client.post(f"/api/planning/tasks/{task_id}/run", headers=headers)

        # 连续回答：第一轮带范围题；第 4 轮达到上限 → 直接生成；第 5 次 decision → 409
        answer_scope = {"kind": "intake", "answers": [
            {"question_id": "scope", "selections": ["route"]},
            {"question_id": "always", "selections": ["yes"]},
        ]}
        answer = {"kind": "intake", "answers": [{"question_id": "always", "selections": ["yes"]}]}
        statuses = []
        for i in range(4):
            payload = answer_scope if i == 0 else answer
            r = client.post(
                f"/api/planning/tasks/{task_id}/decision", headers=headers, json=payload,
            )
            assert r.status_code == 200, r.text
            body = r.json()
            statuses.append(body.get("wait_state") or body["task_status"])
        assert statuses[:3] == ["awaiting_intake"] * 3, statuses
        assert statuses[3] == "succeeded", statuses
        r = client.post(
            f"/api/planning/tasks/{task_id}/decision", headers=headers, json=answer,
        )
        assert r.status_code == 409, r.text  # 访谈已关闭（上限生效）
        st = _state(client, token, task_id)
        assert st["intake"]["round"] == 4 and st["intake"]["done"] is True

    def test_wrong_state_and_cross_owner(self, env) -> None:
        client = env["client"]
        owner_token, other = None, None
        _, token, task_id = _setup_task(env)
        owner_token = token
        _, other = make_user(client, env["database"])
        # 任务未进入等待态（未 run）→ 409
        r = client.post(
            f"/api/planning/tasks/{task_id}/decision", headers=auth_headers(owner_token),
            json={"kind": "intake", "answers": [{"question_id": "scope", "selections": ["route"]}]},
        )
        assert r.status_code == 409, r.text
        # 他人 → 404（不泄露存在性）
        r = client.post(
            f"/api/planning/tasks/{task_id}/decision", headers=auth_headers(other),
            json={"kind": "intake", "answers": [{"question_id": "scope", "selections": ["route"]}]},
        )
        assert r.status_code == 404, r.text
