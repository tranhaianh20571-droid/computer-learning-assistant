"""切片 3 T04 集成测试：四入口、范围解析、搜索等待、降级链、manifest、候选落库。"""

from __future__ import annotations

import json
import uuid

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.profiles import ModelProfile

from backend.agents.native import ModelCapabilityError

from common_fixtures import (
    OUTLINE_OK_PAYLOAD,
    ROUTE_OK_PAYLOAD,
    AllUnavailableAdapter,
    SkeletonThenOutlineAdapter,
    SyntheticModelAdapter,
    SyntheticSearchAdapter,
    auth_headers,
    make_user,
    run_route_generate,
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


def _run_with_intake(client, token: str, task_id: str, *, scope: str | None = "route") -> dict:
    from common_fixtures import run_route_generate
    return run_route_generate(client,token,task_id,scope=scope)


class TestScopeResolution:
    def test_explicit_scope_wins(self) -> None:
        from backend.planning.scope import ScopeSignals, resolve_scope

        assert resolve_scope("course", ScopeSignals(cross_domain_goal=True)) == "course"
        assert resolve_scope("route", ScopeSignals(single_textbook=True)) == "route"

    def test_auto_single_textbook_course(self) -> None:
        from backend.planning.scope import ScopeSignals, resolve_scope

        assert resolve_scope("auto", ScopeSignals(single_textbook=True)) == "course"
        assert resolve_scope("auto", ScopeSignals(explicit_course=True)) == "course"

    def test_auto_cross_domain_route(self) -> None:
        from backend.planning.scope import ScopeSignals, resolve_scope

        assert resolve_scope("auto", ScopeSignals(career_oriented=True)) == "route"

    def test_auto_ambiguous_waits(self) -> None:
        from backend.planning.errors import WaitStateSignal
        from backend.planning.scope import ScopeSignals, resolve_scope

        with pytest.raises(WaitStateSignal):
            resolve_scope("auto", ScopeSignals(cross_domain_goal=True, single_textbook=True))
        with pytest.raises(WaitStateSignal):
            resolve_scope("auto", ScopeSignals())

    def test_not_decided_by_material_count_or_scenario(self) -> None:
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

        body = _run_with_intake(env["client"], token, task_id)
        assert body["task_status"] == "partial"
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

        body = _run_with_intake(env["client"], token, task_id)
        assert body["task_status"] == "partial"
        assert body["wait_state"] == "awaiting_search_decision"

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
        assert r.json()["task_status"] == "partial"
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
        body = _run_with_intake(env["client"], token, task_id)
        assert body["wait_state"] == "awaiting_search_decision"

        # 适配器恢复后重试
        search.fail = None
        r = env["client"].post(
            f"/api/planning/tasks/{task_id}/decision", headers=auth_headers(token),
            json={"decision": "retry"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["task_status"] == "partial"

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
        body = _run_with_intake(env["client"], token, task_id, scope=None)
        assert body["task_status"] == "partial"
        assert body["wait_state"] == "awaiting_scope_clarification"

        r = env["client"].post(
            f"/api/planning/tasks/{task_id}/decision", headers=auth_headers(token),
            json={"resolved_scope": "route"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["task_status"] == "partial"
        assert r.json()["resolved_scope"] == "route"

    def test_outline_generate_and_skip_existing(self, env) -> None:
        user_id, token, route_id = _setup_route(env)
        env["container"]["model"] = SkeletonThenOutlineAdapter(payload=ROUTE_OK_PAYLOAD, outline_payload=OUTLINE_OK_PAYLOAD)
        env["container"]["search"] = SyntheticSearchAdapter()
        client = env["client"]

        # 先确认一条路线（复用 route flow）
        task_id = client.post(
            "/api/planning/tasks", headers=auth_headers(token),
            json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "route_generate",
                  "planning_scope": "route", "target_route_id": route_id},
        ).json()["task_id"]
        body = _run_with_intake(client, token, task_id)
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

        # ADR-029: a materialized confirmed outline cannot be regenerated.
        blocked = client.post("/api/planning/tasks", headers=auth_headers(token),
            json={"idempotency_key":uuid.uuid4().hex,"task_kind":"outline_generate",
                  "planning_scope":"course","target_course_id":course_id})
        assert blocked.status_code == 409 and blocked.json()["error_code"] == "PLANNING_LOCKED"
        assert client.get(f"/api/courses/{course_id}/overview",headers=auth_headers(token)).json()["outline_confirmed"]

    def test_native_to_tool_to_prompted(self, env) -> None:
        class UnsupportedOutputModel(FunctionModel):
            def __init__(self):
                self.modes = []

                def respond(messages, info):
                    mode = info.model_request_parameters.output_mode
                    self.modes.append(mode)
                    if mode in ("native", "tool"):
                        raise ModelCapabilityError("synthetic output protocol unsupported")
                    return ModelResponse(parts=[TextPart(json.dumps(ROUTE_OK_PAYLOAD))])

                super().__init__(respond, profile=ModelProfile(
                    supports_json_schema_output=True, supports_tools=True))

        user_id, token, route_id = _setup_route(env)
        adapter = UnsupportedOutputModel()
        env["container"]["model"] = adapter
        env["container"]["search"] = SyntheticSearchAdapter()

        task_id = env["client"].post(
            "/api/planning/tasks", headers=auth_headers(token),
            json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "route_generate",
                  "planning_scope": "route", "target_route_id": route_id},
        ).json()["task_id"]
        body = _run_with_intake(env["client"], token, task_id)
        assert body["task_status"] == "partial"
        assert body["mode"] == "prompted"
        assert body["degraded_from"] == ["native", "tool"]
        assert adapter.modes[:3] == ["native", "tool", "prompted"]

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
        body = _run_with_intake(env["client"], token, task_id)
        assert body["task_status"] == "failed"
        assert body["error_code"] == "MODEL_CAPABILITY_UNAVAILABLE"
        assert adapter.generate_calls == []  # 未发起任何模型调用

    def test_prompted_retry_bounded(self) -> None:
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
        _run_with_intake(client, token, task_id)
        # 恢复（离线继续）
        client.post(f"/api/planning/tasks/{task_id}/decision", headers=auth_headers(token),
                    json={"decision": "offline_continue"})
        binding_after = client.get(f"/api/tasks/{task_id}/prompt", headers=auth_headers(token)).json()
        assert binding_after["binding_id"] == binding_before["binding_id"]
        assert binding_after["template_sha256"] == binding_before["template_sha256"]
        assert binding_after["source"] == "local_fallback"


class TestDraftScenePreview:
    """草稿预览场景：生成后即可渲染导图，不必先确认草稿；标题 id 污染兜底剥除。"""

    def _run_route_task(self, env, token: str, route_id: str, payload: dict) -> dict:
        env["container"]["model"] = SyntheticModelAdapter(payload=payload)
        env["container"]["search"] = SyntheticSearchAdapter()
        task_id = env["client"].post(
            "/api/planning/tasks", headers=auth_headers(token),
            json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "route_generate",
                  "planning_scope": "route", "target_route_id": route_id},
        ).json()["task_id"]
        return _run_with_intake(env["client"], token, task_id)

    def test_draft_scene_renderable_before_confirm(self, env) -> None:
        _, token, route_id = _setup_route(env)
        body = self._run_route_task(env, token, route_id, ROUTE_OK_PAYLOAD)
        assert body["task_status"] == "partial", body
        draft_id = body["draft"]["draft_id"]

        r = env["client"].get(f"/api/planning/drafts/{draft_id}/scene", headers=auth_headers(token))
        assert r.status_code == 200, r.text
        scene = r.json()
        assert scene["elements"], "draft scene should contain elements"
        texts = [el.get("text", "") for el in scene["elements"] if el.get("type") == "text"]
        # 新契约：阶段不再单独成卡；大标题根卡 + 课程卡
        assert any("数据库基础" in t for t in texts)
        assert any("自学数据库路线" in t for t in texts)

    def test_draft_scene_owner_only(self, env) -> None:
        _, token, route_id = _setup_route(env)
        body = self._run_route_task(env, token, route_id, ROUTE_OK_PAYLOAD)
        draft_id = body["draft"]["draft_id"]
        _, other_token, _ = _setup_route(env)

        r = env["client"].get(f"/api/planning/drafts/{draft_id}/scene", headers=auth_headers(other_token))
        assert r.status_code == 404

    def test_candidate_titles_stripped_of_internal_ids(self) -> None:
        from backend.planning.outputs import RouteCandidate, validate_route_candidate

        candidate = RouteCandidate.model_validate({
            "stages": [{"title": "基础构建：route_8a471d50aada 系统架构", "courses": [
                {"title": "route_8a471d50aada 导论与核心概念", "goal": ""},
            ]}],
            "prerequisites": [],
        })
        validate_route_candidate(candidate)
        assert "route_8a471d50aada" not in candidate.stages[0].title
        assert "route_8a471d50aada" not in candidate.stages[0].courses[0].title
        assert candidate.stages[0].title.startswith("基础构建")

    def test_content_json_stripped_in_place_for_confirm(self) -> None:
        from backend.planning.outputs import validate_content_json

        content = {
            "stages": [{"title": "机制进阶：route_8a471d50aada 系统架构与运行流程",
                        "courses": [{"title": "系统架构分解与模块协作", "goal": ""}]}],
            "prerequisites": [],
        }
        validate_content_json("route", content)
        assert "route_8a471d50aada" not in content["stages"][0]["title"]

    def test_contaminated_payload_produces_clean_scene(self, env) -> None:
        _, token, route_id = _setup_route(env)
        polluted = {
            "stages": [
                {"title": "基础构建：route_8a471d50aada 系统架构与运行流程", "courses": [
                    {"title": "系统导论与核心概念", "goal": ""},
                ]},
                {"title": "原理深挖：底层实现原理与优化", "courses": [
                    {"title": "存储与索引原理", "goal": ""},
                ]},
            ],
            "prerequisites": [{"from_index": 0, "to_index": 1}],
        }
        body = self._run_route_task(env, token, route_id, polluted)
        assert body["task_status"] == "partial", body
        draft_id = body["draft"]["draft_id"]

        r = env["client"].get(f"/api/planning/drafts/{draft_id}/scene", headers=auth_headers(token))
        assert r.status_code == 200
        texts = [el.get("text", "") for el in r.json()["elements"] if el.get("type") == "text"]
        joined = "\n".join(texts)
        assert "route_8a471d50aada" not in joined
        # 阶段标题不再渲染；课程卡 + 大标题根卡（路由标题）保留
        assert "系统导论与核心概念" in joined
        assert "存储与索引原理" in joined
        assert "自学数据库路线" in joined


class TestRouteRefineFanOut:
    """v2 生成链路：骨架后逐课细化补齐单元/讲次；单课失败降级为课程粒度。"""

    def _create_task(self, env, token: str, route_id: str) -> str:
        r = env["client"].post(
            "/api/planning/tasks", headers=auth_headers(token),
            json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "route_generate",
                  "planning_scope": "route", "target_route_id": route_id},
        )
        assert r.status_code == 201, r.text
        return r.json()["task_id"]

    def test_refine_merges_units_into_route_draft(self, env) -> None:
        user_id, token, route_id = _setup_route(env)
        env["container"]["model"] = SkeletonThenOutlineAdapter(
            payload=ROUTE_OK_PAYLOAD, outline_payload=OUTLINE_OK_PAYLOAD,
        )
        env["container"]["search"] = SyntheticSearchAdapter()
        task_id = self._create_task(env, token, route_id)
        body = run_route_generate(env["client"], token, task_id)
        assert body["task_status"] == "succeeded", body
        assert body["summary"]["lesson_count"] == 16  # 4 课 × 2 单元 × 2 讲

        state = env["client"].app.state.slice3
        draft = state.drafts.get_draft(body["draft"]["draft_id"], actor_id=user_id)
        flat = [c for s in draft["content"]["stages"] for c in s["courses"]]
        assert all(len(c["units"]) == 2 for c in flat)
        assert flat[0]["units"][0]["lessons"][0]["title"] == "第 1 讲：环境准备"

        # 草稿场景为四层图：大标题根卡 + 课程 + 单元 + 讲次
        scene = env["client"].get(
            f"/api/planning/drafts/{body['draft']['draft_id']}/scene",
            headers=auth_headers(token),
        ).json()
        keys = set(scene["node_positions"].keys())
        assert "route_root" in keys
        assert any("unit_" in k for k in keys)
        assert any("lesson_" in k for k in keys)

    def test_refine_failure_degrades_to_course_level(self, env) -> None:
        user_id, token, route_id = _setup_route(env)
        env["container"]["model"] = SkeletonThenOutlineAdapter(
            payload=ROUTE_OK_PAYLOAD, outline_payload=OUTLINE_OK_PAYLOAD, fail_refine=True,
        )
        env["container"]["search"] = SyntheticSearchAdapter()
        task_id = self._create_task(env, token, route_id)
        body = run_route_generate(env["client"], token, task_id)
        assert body["task_status"] == "partial", body

        state = env["client"].app.state.slice3
        draft = state.drafts.get_draft(body["draft"]["draft_id"], actor_id=user_id)
        flat = [c for s in draft["content"]["stages"] for c in s["courses"]]
        assert all(c["units"] == [] for c in flat)

        scene = env["client"].get(
            f"/api/planning/drafts/{body['draft']['draft_id']}/scene",
            headers=auth_headers(token),
        ).json()
        keys = set(scene["node_positions"].keys())
        assert "route_root" in keys
        assert not any("unit_" in k for k in keys)


def test_refinement_prompt_and_repair(env):
    user_id, token, route_id = _setup_route(env)
    env["container"]["model"] = SkeletonThenOutlineAdapter(
        payload=ROUTE_OK_PAYLOAD, outline_payload=OUTLINE_OK_PAYLOAD, fail_refine=True,
    )
    env["container"]["search"] = SyntheticSearchAdapter()
    tid = env["client"].post("/api/planning/tasks", headers=auth_headers(token), json={
        "idempotency_key": uuid.uuid4().hex, "task_kind": "route_generate",
        "planning_scope": "route", "target_route_id": route_id,
    }).json()["task_id"]
    body = run_route_generate(env["client"], token, tid)
    assert body["task_status"] == "partial"
    draft = env["state"].drafts.get_draft(body["draft"]["draft_id"], actor_id=user_id)
    # Repair belongs to the editable draft phase, before formal confirmation.
    env["container"]["model"] = SkeletonThenOutlineAdapter(payload=ROUTE_OK_PAYLOAD, outline_payload=OUTLINE_OK_PAYLOAD)
    repair = env["client"].post(f"/api/planning/drafts/{draft['draft_id']}/refine", headers=auth_headers(token),
        json={"revision": draft["revision"]})
    assert repair.status_code == 200, repair.text
    result = env["client"].post(f"/api/planning/tasks/{repair.json()['task_id']}/run", headers=auth_headers(token))
    assert result.json()["task_status"] == "succeeded", result.text
    updated = env["state"].drafts.get_draft(draft["draft_id"], actor_id=user_id)
    assert updated["revision"] > draft["revision"]
    assert all(c["units"] for st in updated["content"]["stages"] for c in st["courses"])


def test_refine_uses_snapshot_budget_and_preserves_edges(env):
    class RecordingAdapter(SkeletonThenOutlineAdapter):
        def generate(self, messages, **options):
            text = str(messages)
            if "课程名称：" in text:
                assert "课程结构图的子树" in text
                assert "本课程讲次总数不得超过" in text
            return super().generate(messages, **options)

    user_id, token, route_id = _setup_route(env)
    env["container"]["model"] = RecordingAdapter(payload=ROUTE_OK_PAYLOAD, outline_payload=OUTLINE_OK_PAYLOAD)
    env["container"]["search"] = SyntheticSearchAdapter()
    tid = TestRouteRefineFanOut()._create_task(env, token, route_id)
    snapshot = env["state"].prompts.resolve_for_task(tid, "outline_refine_v1")
    assert snapshot["template_snapshot"]
    body = run_route_generate(env["client"], token, tid)
    assert body["task_status"] == "succeeded"
    draft = env["state"].drafts.get_draft(body["draft"]["draft_id"], actor_id=user_id)
    assert draft["content"]["stages"][0]["courses"][0]["prerequisites"] == OUTLINE_OK_PAYLOAD["prerequisites"]
    response = env["client"].post(f"/api/planning/drafts/{draft['draft_id']}/confirm", headers=auth_headers(token),
        json={"revision": draft["revision"], "idempotency_key": uuid.uuid4().hex})
    assert response.status_code == 200, response.text
    scene = env["client"].get(f"/api/routes/{route_id}/scene", headers=auth_headers(token)).json()
    assert len([e for e in scene["elements"] if e["type"] == "arrow"]) >= 4
    positions = list(scene["node_positions"].values())
    for i, a in enumerate(positions):
        for b in positions[i+1:]:
            if a["x"] == b["x"]:
                assert a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"]


def test_repair_rejects_stale_revision_and_other_owner(env):
    user_id, token, route_id = _setup_route(env)
    draft = env["state"].drafts.find_or_create_draft(owner_user_id=user_id, draft_kind="route",
        target_id=route_id, base_version_id=None, initial_content=ROUTE_OK_PAYLOAD)
    _, other_token = make_user(env["client"], env["database"])
    url = f"/api/planning/drafts/{draft['draft_id']}/refine"
    assert env["client"].post(url, headers=auth_headers(other_token), json={"revision": draft["revision"]}).status_code == 404
    assert env["client"].post(url, headers=auth_headers(token), json={"revision": draft["revision"]+1}).status_code == 409


def test_late_candidate_is_stored_without_overwriting(env):
    from backend.planning.models import PlanningCandidateRow
    from sqlalchemy import select
    user_id, token, route_id = _setup_route(env)
    draft = env["state"].drafts.find_or_create_draft(owner_user_id=user_id, draft_kind="route",
        target_id=route_id, base_version_id=None, initial_content=ROUTE_OK_PAYLOAD)
    newer = env["state"].drafts.save_draft(draft["draft_id"], actor_id=user_id,
        revision=draft["revision"], content={**ROUTE_OK_PAYLOAD, "title": "人工调整"})
    result = env["state"].drafts.apply_candidate(owner_user_id=user_id, target_id=route_id,
        candidate_kind="planning_revise", content=ROUTE_OK_PAYLOAD,
        source_task_id="synthetic-repair", base_draft_revision=draft["revision"])
    assert result["applied"] is False
    assert env["state"].drafts.get_draft(draft["draft_id"], actor_id=user_id)["content"]["title"] == "人工调整"
    with env["database"].session() as sess:
        assert sess.execute(select(PlanningCandidateRow).where(PlanningCandidateRow.candidate_id == result["candidate_id"])).scalar_one()


def test_empty_refinement_prompt_stops_before_model(env):
    from backend.planning.errors import PlanningError
    with pytest.raises(PlanningError) as exc:
        env["state"].planning._refine_course_units(None, None, None, {"template": ""})
    assert exc.value.code == "PROMPT_UNAVAILABLE"
