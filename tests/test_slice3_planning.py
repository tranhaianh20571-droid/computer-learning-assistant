"""切片 3 T04 集成测试：四入口、范围解析、搜索等待、降级链、manifest、候选落库。"""

from __future__ import annotations

import uuid

import pytest

from slice3_common import (
    OUTLINE_OK_PAYLOAD,
    ROUTE_OK_PAYLOAD,
    AllUnavailableAdapter,
    BrokenJsonThenGoodAdapter,
    SyntheticModelAdapter,
    SyntheticSearchAdapter,
    auth_headers,
    make_user,
)

from fastapi.testclient import TestClient  # noqa: E402

from backend.logging_audit.db import Database  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.logging_audit.repositories import PgAuditLog  # noqa: E402
from backend.slice0_app import Slice0State  # noqa: E402
from backend.slice3_app import Slice3State, create_slice3_app  # noqa: E402


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db


@pytest.fixture()
def env(database: Database):
    """可变的合成适配器容器：测试内可随时切换适配器行为。"""
    container = {"model": None, "search": None}
    state = Slice3State(
        database, PgAuditLog(database),
        model_adapter_factory=lambda owner, task_id: container["model"],
        search_adapter_factory=lambda owner, task_id: container["search"],
    )
    app = create_slice3_app(state=state)
    slice0 = Slice0State(database, PgAuditLog(database))
    app.state.slice0 = slice0
    client = TestClient(app)
    return {"client": client, "state": state, "container": container, "database": database}


def _setup_route(env) -> tuple[str, str]:
    user_id, token = make_user(env["client"], env["database"])
    r = env["client"].post(
        "/api/planning/routes", headers=auth_headers(token),
        json={"title": "自学数据库路线", "scenario": "self_study"},
    )
    assert r.status_code == 201, r.text
    return user_id, token, r.json()["route_id"]


class TestScopeResolution:
    def test_explicit_scope_wins(self, env) -> None:
        from backend.planning.scope import ScopeSignals, resolve_scope

        assert resolve_scope("course", ScopeSignals(cross_domain_goal=True)) == "course"
        assert resolve_scope("route", ScopeSignals(single_textbook=True)) == "route"

    def test_auto_single_textbook_course(self, env) -> None:
        from backend.planning.scope import ScopeSignals, resolve_scope

        assert resolve_scope("auto", ScopeSignals(single_textbook=True)) == "course"
        assert resolve_scope("auto", ScopeSignals(explicit_course=True)) == "course"

    def test_auto_cross_domain_route(self, env) -> None:
        from backend.planning.scope import ScopeSignals, resolve_scope

        assert resolve_scope("auto", ScopeSignals(career_oriented=True)) == "route"

    def test_auto_ambiguous_waits(self, env) -> None:
        from backend.planning.errors import WaitStateSignal
        from backend.planning.scope import ScopeSignals, resolve_scope

        with pytest.raises(WaitStateSignal):
            resolve_scope("auto", ScopeSignals(cross_domain_goal=True, single_textbook=True))
        with pytest.raises(WaitStateSignal):
            resolve_scope("auto", ScopeSignals())

    def test_not_decided_by_material_count_or_scenario(self, env) -> None:
        # 硬约束回归：附件数量/场景不是 auto 的输入（ScopeSignals 无此类字段）
        from backend.planning.scope import ScopeSignals

        assert not any(hasattr(ScopeSignals(), f) for f in ("material_count", "scenario", "keyword"))


class TestPlanningWorker:
    def test_route_generate_end_to_end(self, env) -> None:
        user_id, token, route_id = _setup_route(env)
        env["container"]["model"] = SyntheticModelAdapter(payload=ROUTE_OK_PAYLOAD)
        env["container"]["search"] = SyntheticSearchAdapter()

        r = env["client"].post(
            "/api/planning/tasks", headers=auth_headers(token),
            json={
                "idempotency_key": f"t{uuid.uuid4().hex[:8]}",
                "task_kind": "route_generate",
                "planning_scope": "route",
                "target_route_id": route_id,
            },
        )
        assert r.status_code == 201, r.text
        task_id = r.json()["task_id"]
        assert r.json()["prompt_binding_id"]

        r = env["client"].post(f"/api/planning/tasks/{task_id}/run", headers=auth_headers(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["task_status"] == "succeeded"
        assert body["mode"] == "native"

        # 草稿与候选落库
        state = env["client"].app.state.slice3
        draft = state.drafts.get_draft(body["draft"]["draft_id"], actor_id=user_id)
        assert draft["draft_kind"] == "route"
        assert draft["content"]["stages"]

        # 任务事件序列：retrieve/search 阶段成功事件（经事件补取接口读取）
        events_resp = env["client"].get(
            f"/tasks/{task_id}/events.json",
            headers={"X-Actor-Id": user_id},
        )
        assert events_resp.status_code == 200
        stages = {e["stage"] for e in events_resp.json()["events"]}
        assert "retrieve" in stages and "search" in stages and "generate" in stages

    def test_task_creation_idempotent(self, env) -> None:
        _, token, route_id = _setup_route(env)
        env["container"]["model"] = SyntheticModelAdapter(payload=ROUTE_OK_PAYLOAD)
        env["container"]["search"] = SyntheticSearchAdapter()
        idem = f"idem-{uuid.uuid4().hex[:8]}"
        payload = {
            "idempotency_key": idem, "task_kind": "route_generate",
            "planning_scope": "route", "target_route_id": route_id,
        }
        r1 = env["client"].post("/api/planning/tasks", headers=auth_headers(token), json=payload)
        r2 = env["client"].post("/api/planning/tasks", headers=auth_headers(token), json=payload)
        assert r1.status_code == 201 and r2.status_code == 201
        assert r1.json()["task_id"] == r2.json()["task_id"]
        assert r2.json()["deduped"] is True

    def test_search_failure_waits_then_offline_recovers(self, env) -> None:
        user_id, token, route_id = _setup_route(env)
        env["container"]["model"] = SyntheticModelAdapter(payload=ROUTE_OK_PAYLOAD)
        env["container"]["search"] = SyntheticSearchAdapter(fail="timeout")

        task_id = env["client"].post(
            "/api/planning/tasks", headers=auth_headers(token),
            json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "route_generate",
                  "planning_scope": "route", "target_route_id": route_id},
        ).json()["task_id"]

        r = env["client"].post(f"/api/planning/tasks/{task_id}/run", headers=auth_headers(token))
        assert r.json()["task_status"] == "partial"
        assert r.json()["wait_state"] == "awaiting_search_decision"

        state = env["client"].get(
            f"/api/planning/tasks/{task_id}", headers=auth_headers(token)
        ).json()
        assert state["wait_state"] == "awaiting_search_decision"

        # 决策：离线继续 → 任务走通（不伪造外部结果）
        r = env["client"].post(
            f"/api/planning/tasks/{task_id}/decision", headers=auth_headers(token),
            json={"decision": "offline_continue"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["task_status"] == "succeeded"
        # manifest 无 external 引用
        from sqlalchemy import select

        from backend.planning.models import ContextManifestRow

        with env["database"].session() as sess:
            rows = sess.execute(
                select(ContextManifestRow).where(ContextManifestRow.task_id == task_id)
            ).scalars().all()
        assert rows
        refs = []
        for row in rows:
            import json

            refs += json.loads(row.retrieval_refs_json)
        assert all(ref.get("source") != "external" for ref in refs)

    def test_search_retry_after_adapter_recovers(self, env) -> None:
        _, token, route_id = _setup_route(env)
        env["container"]["model"] = SyntheticModelAdapter(payload=ROUTE_OK_PAYLOAD)
        search = SyntheticSearchAdapter(fail="timeout")
        env["container"]["search"] = search

        task_id = env["client"].post(
            "/api/planning/tasks", headers=auth_headers(token),
            json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "route_generate",
                  "planning_scope": "route", "target_route_id": route_id},
        ).json()["task_id"]
        r = env["client"].post(f"/api/planning/tasks/{task_id}/run", headers=auth_headers(token))
        assert r.json()["wait_state"] == "awaiting_search_decision"

        # 适配器恢复后重试
        search.fail = None
        r = env["client"].post(
            f"/api/planning/tasks/{task_id}/decision", headers=auth_headers(token),
            json={"decision": "retry"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["task_status"] == "succeeded"

    def test_scope_ambiguity_waits_then_decision(self, env) -> None:
        _, token, route_id = _setup_route(env)
        env["container"]["model"] = SyntheticModelAdapter(payload=ROUTE_OK_PAYLOAD)
        env["container"]["search"] = SyntheticSearchAdapter()

        task_id = env["client"].post(
            "/api/planning/tasks", headers=auth_headers(token),
            json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "route_generate",
                  "planning_scope": "auto", "target_route_id": route_id,
                  "signals": {"cross_domain_goal": True, "single_textbook": True}},
        ).json()["task_id"]
        r = env["client"].post(f"/api/planning/tasks/{task_id}/run", headers=auth_headers(token))
        assert r.json()["task_status"] == "partial"
        assert r.json()["wait_state"] == "awaiting_scope_clarification"

        r = env["client"].post(
            f"/api/planning/tasks/{task_id}/decision", headers=auth_headers(token),
            json={"resolved_scope": "route"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["task_status"] == "succeeded"
        assert r.json()["resolved_scope"] == "route"

    def test_outline_generate_and_skip_existing(self, env) -> None:
        user_id, token, route_id = _setup_route(env)
        env["container"]["model"] = SyntheticModelAdapter(payload=ROUTE_OK_PAYLOAD)
        env["container"]["search"] = SyntheticSearchAdapter()
        client = env["client"]

        # 先确认一条路线（复用 route flow）
        task_id = client.post(
            "/api/planning/tasks", headers=auth_headers(token),
            json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "route_generate",
                  "planning_scope": "route", "target_route_id": route_id},
        ).json()["task_id"]
        body = client.post(f"/api/planning/tasks/{task_id}/run", headers=auth_headers(token)).json()
        draft_id = body["draft"]["draft_id"]
        draft = state = None
        state = client.app.state.slice3
        draft = state.drafts.get_draft(draft_id, actor_id=user_id)
        confirmed = client.post(
            f"/api/planning/drafts/{draft_id}/confirm", headers=auth_headers(token),
            json={"revision": draft["revision"], "idempotency_key": f"c{uuid.uuid4().hex[:8]}"},
        ).json()
        overview = client.get(f"/api/routes/{route_id}/overview", headers=auth_headers(token)).json()
        course_id = overview["stages"][0]["courses"][0]["course_id"]

        # 大纲生成
        env["container"]["model"] = SyntheticModelAdapter(payload=OUTLINE_OK_PAYLOAD)
        t2 = client.post(
            "/api/planning/tasks", headers=auth_headers(token),
            json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "outline_generate",
                  "planning_scope": "course", "target_course_id": course_id},
        ).json()["task_id"]
        body2 = client.post(f"/api/planning/tasks/{t2}/run", headers=auth_headers(token)).json()
        assert body2["task_status"] == "succeeded"

        # 确认大纲版本
        outline_draft = state.drafts.get_draft(body2["draft"]["draft_id"], actor_id=user_id)
        confirmed2 = client.post(
            f"/api/planning/drafts/{body2['draft']['draft_id']}/confirm", headers=auth_headers(token),
            json={"revision": outline_draft["revision"], "idempotency_key": f"c{uuid.uuid4().hex[:8]}"},
        ).json()
        assert confirmed2["deduped"] is False

        # 同一课程已有大纲：重选节点不进生成流程
        t3 = client.post(
            "/api/planning/tasks", headers=auth_headers(token),
            json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "outline_generate",
                  "planning_scope": "course", "target_course_id": course_id,
                  "source_route_node_id": "rcn_x"},
        ).json()["task_id"]
        body3 = client.post(f"/api/planning/tasks/{t3}/run", headers=auth_headers(token)).json()
        assert body3.get("skipped") is True and body3["reason"] == "outline_exists"


class TestDegradation:
    def test_native_to_tool_to_prompted(self, env) -> None:
        user_id, token, route_id = _setup_route(env)
        adapter = BrokenJsonThenGoodAdapter(payload=ROUTE_OK_PAYLOAD)
        env["container"]["model"] = adapter
        env["container"]["search"] = SyntheticSearchAdapter()

        task_id = env["client"].post(
            "/api/planning/tasks", headers=auth_headers(token),
            json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "route_generate",
                  "planning_scope": "route", "target_route_id": route_id},
        ).json()["task_id"]
        r = env["client"].post(f"/api/planning/tasks/{task_id}/run", headers=auth_headers(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["task_status"] == "succeeded"
        assert body["mode"] == "prompted"           # 降级到第三档
        assert body["degraded_from"] == ["native", "tool"]

    def test_model_capability_unavailable_no_infinite_retry(self, env) -> None:
        _, token, route_id = _setup_route(env)
        adapter = AllUnavailableAdapter()
        env["container"]["model"] = adapter
        env["container"]["search"] = SyntheticSearchAdapter()

        task_id = env["client"].post(
            "/api/planning/tasks", headers=auth_headers(token),
            json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "route_generate",
                  "planning_scope": "route", "target_route_id": route_id},
        ).json()["task_id"]
        r = env["client"].post(f"/api/planning/tasks/{task_id}/run", headers=auth_headers(token))
        body = r.json()
        assert body["task_status"] == "failed"
        assert body["error_code"] == "MODEL_CAPABILITY_UNAVAILABLE"
        assert adapter.generate_calls == []  # 未发起任何模型调用

    def test_prompted_retry_bounded(self, env) -> None:
        from backend.planning.model_runner import PROMPTED_PARSE_RETRIES, run_structured
        from backend.planning.outputs import RouteCandidate

        class AlwaysGarbage(SyntheticModelAdapter):
            def generate(self, messages, **options):
                self.generate_calls.append(options)
                return {"content": "这不是 JSON"}

        adapter = AlwaysGarbage(payload=ROUTE_OK_PAYLOAD)
        with pytest.raises(Exception) as exc:
            run_structured(
                adapter, RouteCandidate, [{"role": "user", "content": "x"}],
                modes=["prompted"],
            )
        assert exc.value.code == "MODEL_CAPABILITY_UNAVAILABLE"
        # 重试有上限：prompted 至多 PROMPTED_PARSE_RETRIES+1 次，不无限重试
        assert len(adapter.generate_calls) == PROMPTED_PARSE_RETRIES + 1

    def test_prompt_version_fixed_on_resume(self, env) -> None:
        """恢复任务不得重新解析 production 标签：绑定快照哈希不变。"""
        user_id, token, route_id = _setup_route(env)
        env["container"]["model"] = SyntheticModelAdapter(payload=ROUTE_OK_PAYLOAD)
        env["container"]["search"] = SyntheticSearchAdapter(fail="timeout")
        client = env["client"]

        task_id = client.post(
            "/api/planning/tasks", headers=auth_headers(token),
            json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "route_generate",
                  "planning_scope": "route", "target_route_id": route_id},
        ).json()["task_id"]
        binding_before = client.get(f"/api/tasks/{task_id}/prompt", headers=auth_headers(token)).json()
        client.post(f"/api/planning/tasks/{task_id}/run", headers=auth_headers(token))
        # 恢复（离线继续）
        client.post(f"/api/planning/tasks/{task_id}/decision", headers=auth_headers(token),
                    json={"decision": "offline_continue"})
        binding_after = client.get(f"/api/tasks/{task_id}/prompt", headers=auth_headers(token)).json()
        assert binding_after["binding_id"] == binding_before["binding_id"]
        assert binding_after["template_sha256"] == binding_before["template_sha256"]
        assert binding_after["source"] == "local_fallback"
