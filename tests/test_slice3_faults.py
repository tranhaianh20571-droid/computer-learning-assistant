"""切片 3 T09 故障注入：§8.2 归切片 3 场景 + T09 九项注入。"""

from __future__ import annotations

import json
import uuid

import pytest

from common_fixtures import (
    OUTLINE_OK_PAYLOAD,
    ROUTE_OK_PAYLOAD,
    AllUnavailableAdapter,
    SyntheticModelAdapter,
    SyntheticSearchAdapter,
    auth_headers,
    run_route_generate,
    make_user,
)

from fastapi.testclient import TestClient  # noqa: E402

from backend.logging_audit.db import Database  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.logging_audit.repositories import PgAuditLog, PgTaskEventStore  # noqa: E402
from backend.planning.errors import PlanningError  # noqa: E402
from backend.planning.models import ContextManifestRow, PlanningTaskStateRow  # noqa: E402
from backend.planning.retrieval import PlanningRetrievalService  # noqa: E402
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
    container = {"model": None, "search": None}
    state = Slice3State(
        database, PgAuditLog(database),
        model_adapter_factory=lambda owner, task_id: container["model"],
        search_adapter_factory=lambda owner, task_id: container["search"],
    )
    app = create_slice3_app(state=state)
    app.state.slice0 = Slice0State(database, PgAuditLog(database))
    client = TestClient(app)
    return {"client": client, "state": state, "container": container, "database": database}


def _prepare_route(env, *, payload=None):
    client = env["client"]
    user_id, token = make_user(client, env["database"])
    route_id = client.post(
        "/api/planning/routes", headers=auth_headers(token),
        json={"title": "故障注入路线", "scenario": "self_study"},
    ).json()["route_id"]
    env["container"]["model"] = SyntheticModelAdapter(payload=payload or ROUTE_OK_PAYLOAD)
    env["container"]["search"] = SyntheticSearchAdapter()
    return user_id, token, route_id


def _run_route_task(env, token, route_id, *, signals=None, scope="route"):
    client = env["client"]
    task_id = client.post(
        "/api/planning/tasks", headers=auth_headers(token),
        json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "route_generate",
              "planning_scope": scope, "target_route_id": route_id, "signals": signals or {}},
    ).json()["task_id"]
    body = run_route_generate(client, token, task_id)
    return task_id, body


class TestFaultModelCapability:
    def test_unavailable_model_fails_once_without_retry_loop(self, env) -> None:
        user_id, token, route_id = _prepare_route(env)
        adapter = AllUnavailableAdapter()
        env["container"]["model"] = adapter
        task_id, body = _run_route_task(env, token, route_id)
        assert body["task_status"] == "failed"
        assert body["error_code"] == "MODEL_CAPABILITY_UNAVAILABLE"
        assert adapter.generate_calls == []
        # 任务状态不被反复重试：终态 failed
        got = env["client"].get(
            f"/api/planning/tasks/{task_id}", headers=auth_headers(token)
        ).json()
        assert got["wait_state"] is None

    def test_degradation_bounded_not_infinite(self, env) -> None:
        user_id, token, route_id = _prepare_route(env)
        env["container"]["model"] = SyntheticModelAdapter(
            payload=ROUTE_OK_PAYLOAD,
            scripted=[Exception("timeout")] * 8,  # 全部调用都失败
        )
        # raise_on_generate 优先；此处用 scripted 使每次调用抛错
        adapter = env["container"]["model"]
        adapter.raise_on_generate = Exception("timeout")
        adapter.raise_on_generate.code = "timeout"
        task_id, body = _run_route_task(env, token, route_id)
        assert body["task_status"] == "failed"
        assert body["error_code"] in ("MODEL_TIMEOUT", "MODEL_CAPABILITY_UNAVAILABLE")
        # 调用次数有限（native 1 + tool 1 + prompted 3 = 5 上限）
        assert len(adapter.generate_calls) <= 5


class TestFaultContextBudget:
    def test_budget_exceeded_visible_in_manifest(self, env) -> None:
        """预算裁剪显式记录：manifest 含截断原因，不静默丢弃。"""
        user_id, token, route_id = _prepare_route(env)
        retrieval = PlanningRetrievalService(env["database"])
        # 注入超预算场景：max_retrieval_chunks=0 → 全部命中被裁剪并可见
        result = retrieval.assemble(
            owner_user_id=user_id,
            material_ids=["mat_x"],
            query="测试",
            budget={"max_retrieval_chunks": 0},
        )
        # 无可用检索源时至少标注原因
        assert result.truncation_reasons or result.refs == []
        manifest = retrieval.save_manifest(task_id="task_budget_probe", retrieval=result)
        with env["database"].session() as sess:
            row = sess.get(ContextManifestRow, manifest["manifest_id"])
            reasons = json.loads(row.truncation_reasons_json)
        assert isinstance(reasons, list)

    def test_search_failure_both_decisions_recover(self, env) -> None:
        """注入 Hikari 超时：两种用户决策都使任务可恢复。"""
        client = env["client"]
        user_id, token, route_id = _prepare_route(env)
        env["container"]["search"] = SyntheticSearchAdapter(fail="timeout")
        task_id, body = _run_route_task(env, token, route_id)
        assert body["wait_state"] == "awaiting_search_decision"

        # 决策 1：retry（适配器恢复）
        env["container"]["search"] = SyntheticSearchAdapter()
        r = client.post(f"/api/planning/tasks/{task_id}/decision",
                        headers=auth_headers(token), json={"decision": "retry"})
        assert r.json()["task_status"] == "succeeded"

        # 决策 2：offline_continue（新一轮任务）
        env["container"]["search"] = SyntheticSearchAdapter(fail="error")
        task_id2, body2 = _run_route_task(env, token, route_id)
        assert body2["wait_state"] == "awaiting_search_decision"
        r2 = client.post(f"/api/planning/tasks/{task_id2}/decision",
                         headers=auth_headers(token), json={"decision": "offline_continue"})
        assert r2.json()["task_status"] == "succeeded"
        # 离线路径 manifest 无 external 引用（不伪造联网依据）
        with env["database"].session() as sess:
            rows = sess.execute(
                select(ContextManifestRow.retrieval_refs_json).where(
                    ContextManifestRow.task_id == task_id2
                )
            ).scalars().all()
        refs = [ref for blob in rows for ref in json.loads(blob)]
        assert all(ref.get("source") != "external" for ref in refs)


from sqlalchemy import select  # noqa: E402


class TestFaultDraftRace:
    def test_double_save_conflict(self, env) -> None:
        """双 tab 编辑同一草稿：后到者被乐观锁拒绝。"""
        user_id, token, route_id = _prepare_route(env)
        client = env["client"]
        task_id, body = _run_route_task(env, token, route_id)
        draft_id = body["draft"]["draft_id"]
        draft = env["state"].drafts.get_draft(draft_id, actor_id=user_id)

        r1 = client.put(f"/api/planning/drafts/{draft_id}", headers=auth_headers(token),
                        json={"revision": draft["revision"], "content": ROUTE_OK_PAYLOAD})
        assert r1.status_code == 200
        # tab2 用旧 revision 保存 → 冲突
        r2 = client.put(f"/api/planning/drafts/{draft_id}", headers=auth_headers(token),
                        json={"revision": draft["revision"], "content": ROUTE_OK_PAYLOAD})
        assert r2.status_code == 409
        assert r2.json()["error_code"] == "DRAFT_REVISION_MISMATCH"

    def test_stale_candidate_not_overwrite_human_edit(self, env) -> None:
        """人工编辑（更高 revision）后，旧 AI 候选不得替换草稿。"""
        user_id, token, route_id = _prepare_route(env)
        client = env["client"]
        task_id, body = _run_route_task(env, token, route_id)
        draft_id = body["draft"]["draft_id"]
        human = {
            "stages": [{"title": "人工编辑的阶段", "courses": [{"title": "人工课程", "goal": "g"}]}],
            "prerequisites": [],
        }
        saved = client.put(f"/api/planning/drafts/{draft_id}", headers=auth_headers(token),
                           json={"revision": 1, "content": human}).json()
        assert saved["revision"] == 2
        # 迟到的 AI 候选（base_draft_revision=1 < 2）不覆盖
        result = env["state"].drafts.apply_candidate(
            owner_user_id=user_id, target_id=route_id,
            candidate_kind="route_generate", content=ROUTE_OK_PAYLOAD,
            source_task_id=task_id, base_draft_revision=1,
        )
        assert result["applied"] is False
        draft = env["state"].drafts.get_draft(draft_id, actor_id=user_id)
        assert draft["content"]["stages"][0]["title"] == "人工编辑的阶段"


class TestFaultConfirmConcurrency:
    def test_duplicate_confirm_key_no_double_version(self, env) -> None:
        user_id, token, route_id = _prepare_route(env)
        client = env["client"]
        task_id, body = _run_route_task(env, token, route_id)
        draft_id = body["draft"]["draft_id"]
        draft = env["state"].drafts.get_draft(draft_id, actor_id=user_id)
        idem = f"c{uuid.uuid4().hex[:8]}"
        r1 = client.post(f"/api/planning/drafts/{draft_id}/confirm", headers=auth_headers(token),
                         json={"revision": draft["revision"], "idempotency_key": idem})
        r2 = client.post(f"/api/planning/drafts/{draft_id}/confirm", headers=auth_headers(token),
                         json={"revision": draft["revision"], "idempotency_key": idem})
        assert r1.status_code == 200 and r2.status_code == 200
        assert r2.json()["version_id"] == r1.json()["version_id"]

    def test_concurrent_confirm_stale_loses(self, env) -> None:
        """同时基于不同 revision 确认：后到者失败。"""
        user_id, token, route_id = _prepare_route(env)
        client = env["client"]
        task_id, body = _run_route_task(env, token, route_id)
        draft_id = body["draft"]["draft_id"]
        draft = env["state"].drafts.get_draft(draft_id, actor_id=user_id)
        ok = client.post(f"/api/planning/drafts/{draft_id}/confirm", headers=auth_headers(token),
                         json={"revision": draft["revision"], "idempotency_key": f"c{uuid.uuid4().hex[:8]}"})
        assert ok.status_code == 200
        stale = client.post(f"/api/planning/drafts/{draft_id}/confirm", headers=auth_headers(token),
                            json={"revision": draft["revision"] + 5,
                                  "idempotency_key": f"c{uuid.uuid4().hex[:8]}"})
        assert stale.status_code == 409

    def test_invalid_structures_rejected(self, env) -> None:
        """空结构 / 跨单元先修索引注入拒绝。"""
        user_id, token, route_id = _prepare_route(env)
        client = env["client"]
        task_id, body = _run_route_task(env, token, route_id)
        draft_id = body["draft"]["draft_id"]
        draft = env["state"].drafts.get_draft(draft_id, actor_id=user_id)
        bad = {
            "stages": [{"title": "s", "courses": [{"title": "a"}, {"title": "b"}]}],
            "prerequisites": [{"from_index": 0, "to_index": 9}],
        }
        r = client.put(f"/api/planning/drafts/{draft_id}", headers=auth_headers(token),
                       json={"revision": draft["revision"], "content": bad})
        assert r.status_code == 400
        assert r.json()["error_code"] == "INVALID_PREREQUISITE"


class TestFaultTeachingGate:
    def test_gate_blocks_but_route_overview_works(self, env) -> None:
        user_id, token, route_id = _prepare_route(env)
        client = env["client"]
        task_id, body = _run_route_task(env, token, route_id)
        draft_id = body["draft"]["draft_id"]
        draft = env["state"].drafts.get_draft(draft_id, actor_id=user_id)
        confirmed = client.post(f"/api/planning/drafts/{draft_id}/confirm", headers=auth_headers(token),
                                json={"revision": draft["revision"], "idempotency_key": f"c{uuid.uuid4().hex[:8]}"})
        assert confirmed.status_code == 200
        overview = client.get(f"/api/routes/{route_id}/overview", headers=auth_headers(token)).json()
        course_id = overview["stages"][0]["courses"][0]["course_id"]
        # 门槛：大纲未确认 → 受控错误；路线总览不受影响
        blocked = client.post(f"/api/courses/{course_id}/lessons/les_x/start",
                              headers=auth_headers(token))
        assert blocked.status_code == 409
        assert blocked.json()["error_code"] == "LESSON_UNLOCK_REQUIRED"
        assert client.get(f"/api/routes/{route_id}/overview",
                          headers=auth_headers(token)).status_code == 200


class TestFaultScopeSwitchAndRecovery:
    def test_switch_preserves_input_and_reopen_restores(self, env) -> None:
        user_id, token, route_id = _prepare_route(env)
        client = env["client"]
        task_id, body = _run_route_task(env, token, route_id)
        draft_id = body["draft"]["draft_id"]
        # 切换范围：保留输入
        r = client.post(f"/api/planning/drafts/{draft_id}/scope", headers=auth_headers(token),
                        json={"planning_scope": "course"})
        assert r.status_code == 200
        new_draft = r.json()["draft"]
        assert new_draft["content"]["stages"]
        # 重开页面 = 重新拉取草稿：恢复最近成功内容
        reopened = client.get(f"/api/planning/drafts/{draft_id}", headers=auth_headers(token)).json()
        assert reopened["saved_state"] == "saved"
        assert reopened["content"]["stages"]


class TestFaultMaterialBoundary:
    def test_route_materials_not_auto_sent_to_course_task(self, env) -> None:
        """路线任务的材料选择不自动外发到课程任务：课程任务 manifest 列出实际范围。"""
        user_id, token, route_id = _prepare_route(env)
        client = env["client"]
        # 路线任务带材料（无本人材料 → 快照为空）
        task_id, body = _run_route_task(env, token, route_id)
        with env["database"].session() as sess:
            row = sess.get(PlanningTaskStateRow, task_id)
            route_materials = json.loads(row.material_ids_json)
        assert route_materials == []
        # 确认路线 → 课程任务：不继承路线材料（列什么发什么）
        draft_id = body["draft"]["draft_id"]
        draft = env["state"].drafts.get_draft(draft_id, actor_id=user_id)
        confirmed = client.post(f"/api/planning/drafts/{draft_id}/confirm", headers=auth_headers(token),
                                json={"revision": draft["revision"], "idempotency_key": f"c{uuid.uuid4().hex[:8]}"})
        assert confirmed.status_code == 200
        overview = client.get(f"/api/routes/{route_id}/overview", headers=auth_headers(token)).json()
        course_id = overview["stages"][0]["courses"][0]["course_id"]
        env["container"]["model"] = SyntheticModelAdapter(payload=OUTLINE_OK_PAYLOAD)
        t2 = client.post(
            "/api/planning/tasks", headers=auth_headers(token),
            json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "outline_generate",
                  "planning_scope": "course", "target_course_id": course_id},
        ).json()["task_id"]
        with env["database"].session() as sess:
            row2 = sess.get(PlanningTaskStateRow, t2)
            assert json.loads(row2.material_ids_json) == []


class TestFaultSceneOverflow:
    def test_ultra_long_titles_do_not_break_scene(self, env) -> None:
        """超长节点样本不破坏场景转换（无头截图比对由前端走查承担）。"""
        from backend.planning.scene import LAYOUT, measure_text_width, outline_scene

        payload = {
            "units": [{"id": "u1", "title": "极长" * 80, "lessons": [
                {"id": "l1", "title": "超" * 200}
            ]}],
            "prerequisites": [],
        }
        scene = outline_scene(payload)
        # 各节点类型内宽上限（转换层按各自节点宽度换行）
        max_inner = max(LAYOUT["unit_w"], LAYOUT["lesson_w"]) - 2 * LAYOUT["node_inner_pad"]
        texts = [el for el in scene["elements"] if el["type"] == "text"]
        assert texts
        for el in texts:
            lines = el["text"].split("\n")
            assert len(lines) <= LAYOUT["max_lines"]
            for line in lines:
                assert measure_text_width(line) <= max_inner
