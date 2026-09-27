"""切片 3 T08 测试：即时文字对话端到端（generation、快照、预算、external、边界）。"""

from __future__ import annotations

import json
import uuid

import pytest

from slice3_common import (
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


class ChatModelAdapter(SyntheticModelAdapter):
    """合成聊天适配器：纯文字输出。"""

    def generate(self, messages, **options):
        self.generate_calls.append({"options": options, "messages": [m["role"] for m in messages]})
        return {"content": f"已收到（合成回答 {len(self.generate_calls)}）"}


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


class TestChatFlow:
    def test_send_and_history(self, env) -> None:
        user_id, token = make_user(env["client"], env["database"])
        client = env["client"]
        env["container"]["model"] = ChatModelAdapter()
        env["container"]["search"] = SyntheticSearchAdapter()

        conv = client.post(
            "/api/chat", headers=auth_headers(token),
            json={"material_ids": [], "scenario": "self_study"},
        ).json()
        assert conv["generation"] == 0
        conv_id = conv["conversation_id"]

        r1 = client.post(
            f"/api/chat/{conv_id}/messages", headers=auth_headers(token),
            json={"content": "什么是事务？", "use_search": False,
                  "idempotency_key": f"m{uuid.uuid4().hex[:8]}"},
        )
        assert r1.status_code == 200, r1.text
        assert r1.json()["generation"] == 1
        assert r1.json()["message"]["content"].startswith("已收到")

        r2 = client.post(
            f"/api/chat/{conv_id}/messages", headers=auth_headers(token),
            json={"content": "那隔离级别呢？", "use_search": False,
                  "idempotency_key": f"m{uuid.uuid4().hex[:8]}"},
        )
        assert r2.json()["generation"] == 2

        # 历史完整：旧 generation 消息保留、不回填
        got = client.get(f"/api/chat/{conv_id}", headers=auth_headers(token)).json()
        assert got["generation"] == 2
        assert [m["generation"] for m in got["messages"]] == [1, 1, 2, 2]

    def test_send_idempotent_same_key(self, env) -> None:
        user_id, token = make_user(env["client"], env["database"])
        client = env["client"]
        env["container"]["model"] = ChatModelAdapter()
        conv_id = client.post("/api/chat", headers=auth_headers(token), json={}).json()["conversation_id"]
        idem = f"m{uuid.uuid4().hex[:8]}"
        r1 = client.post(f"/api/chat/{conv_id}/messages", headers=auth_headers(token),
                         json={"content": "hi", "idempotency_key": idem})
        r2 = client.post(f"/api/chat/{conv_id}/messages", headers=auth_headers(token),
                         json={"content": "hi", "idempotency_key": idem})
        # 同幂等键：不产生新任务/新消息（任务侧幂等 + generation 不重复递增）
        assert r1.status_code == 200 and r2.status_code == 200
        got = client.get(f"/api/chat/{conv_id}", headers=auth_headers(token)).json()
        user_msgs = [m for m in got["messages"] if m["role"] == "user"]
        assert len(user_msgs) == 1

    def test_material_snapshot_and_manifest_no_body(self, env) -> None:
        user_id, token = make_user(env["client"], env["database"])
        client = env["client"]
        env["container"]["model"] = ChatModelAdapter()
        conv = client.post(
            "/api/chat", headers=auth_headers(token),
            json={"material_ids": ["mat_synthetic"], "scenario": "self_study"},
        ).json()
        # 非本人材料被过滤：快照为空
        assert conv["material_ids"] == []
        conv_id = conv["conversation_id"]
        r = client.post(
            f"/api/chat/{conv_id}/messages", headers=auth_headers(token),
            json={"content": "讲解一下", "idempotency_key": f"m{uuid.uuid4().hex[:8]}"},
        )
        assert r.status_code == 200
        manifest_id = r.json()["manifest_id"]
        from sqlalchemy import select

        from backend.planning.models import ContextManifestRow

        with env["database"].session() as sess:
            row = sess.execute(
                select(ContextManifestRow).where(ContextManifestRow.manifest_id == manifest_id)
            ).scalars().one()
            blob = row.budget_json + row.retrieval_refs_json + row.history_range_json + row.truncation_reasons_json
        # manifest 不含消息正文
        assert "讲解一下" not in blob
        assert row.summary_version == "slice3-v1"
        # 历史范围已记录
        assert isinstance(json.loads(row.history_range_json), dict)

    def test_search_failure_wait_then_offline_resume(self, env) -> None:
        user_id, token = make_user(env["client"], env["database"])
        client = env["client"]
        env["container"]["model"] = ChatModelAdapter()
        env["container"]["search"] = SyntheticSearchAdapter(fail="timeout")
        conv_id = client.post("/api/chat", headers=auth_headers(token), json={}).json()["conversation_id"]

        r = client.post(
            f"/api/chat/{conv_id}/messages", headers=auth_headers(token),
            json={"content": "联网查一下", "use_search": True,
                  "idempotency_key": f"m{uuid.uuid4().hex[:8]}"},
        )
        assert r.status_code == 409
        assert r.json()["error_code"] == "AWAITING_SEARCH_DECISION"

        # 会话处于等待态：当前 generation 只有 user 消息
        got = client.get(f"/api/chat/{conv_id}", headers=auth_headers(token)).json()
        assert got["wait_state"] == "awaiting_search_decision"

        # 决策：离线继续 → 生成答案，generation 不再递增
        r2 = client.post(
            f"/api/chat/{conv_id}/resume", headers=auth_headers(token),
            json={"decision": "offline_continue"},
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["message"]["content"].startswith("已收到")
        got2 = client.get(f"/api/chat/{conv_id}", headers=auth_headers(token)).json()
        assert got2["generation"] == 1
        assert got2["wait_state"] is None

    def test_search_retry_after_recovery(self, env) -> None:
        user_id, token = make_user(env["client"], env["database"])
        client = env["client"]
        env["container"]["model"] = ChatModelAdapter()
        search = SyntheticSearchAdapter(fail="timeout")
        env["container"]["search"] = search
        conv_id = client.post("/api/chat", headers=auth_headers(token), json={}).json()["conversation_id"]
        client.post(
            f"/api/chat/{conv_id}/messages", headers=auth_headers(token),
            json={"content": "联网查一下", "use_search": True,
                  "idempotency_key": f"m{uuid.uuid4().hex[:8]}"},
        )
        search.fail = None
        r = client.post(
            f"/api/chat/{conv_id}/resume", headers=auth_headers(token),
            json={"decision": "retry"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["external_result_count"] == 1

    def test_refresh_restores_saved_without_resend(self, env) -> None:
        user_id, token = make_user(env["client"], env["database"])
        client = env["client"]
        env["container"]["model"] = ChatModelAdapter()
        conv_id = client.post("/api/chat", headers=auth_headers(token), json={}).json()["conversation_id"]
        client.post(f"/api/chat/{conv_id}/messages", headers=auth_headers(token),
                    json={"content": "第一条", "idempotency_key": f"m{uuid.uuid4().hex[:8]}"})
        # 刷新 = GET；只读，不重发
        before = client.get(f"/api/chat/{conv_id}", headers=auth_headers(token)).json()
        after = client.get(f"/api/chat/{conv_id}", headers=auth_headers(token)).json()
        assert before == after
        model_adapter = env["container"]["model"]
        assert len(model_adapter.generate_calls) == 1

    def test_no_model_adapter_reports_capability_error(self, env) -> None:
        user_id, token = make_user(env["client"], env["database"])
        client = env["client"]
        env["container"]["model"] = None
        conv_id = client.post("/api/chat", headers=auth_headers(token), json={}).json()["conversation_id"]
        r = client.post(f"/api/chat/{conv_id}/messages", headers=auth_headers(token),
                        json={"content": "hi", "idempotency_key": f"m{uuid.uuid4().hex[:8]}"})
        assert r.status_code == 200
        assert r.json()["error_code"] == "MODEL_CAPABILITY_UNAVAILABLE"
        assert r.json()["message"] is None


class TestChatBoundaries:
    def test_chat_module_has_no_board_tts_paths(self) -> None:
        """边界回归：即时对话模块不导入、不调用板书/TTS/播放路径（AST 扫描）。"""
        import ast
        import inspect

        import backend.planning.chat as chat_module

        source = inspect.getsource(chat_module)
        tree = ast.parse(source)
        forbidden_names = ("tts", "board", "audio", "play", "speech")

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not any(f in alias.name.lower() for f in forbidden_names), alias.name
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not any(f in module.lower() for f in forbidden_names), module
            elif isinstance(node, ast.Attribute):
                assert not any(f in node.attr.lower() for f in forbidden_names), node.attr

    def test_message_length_bounds(self, env) -> None:
        user_id, token = make_user(env["client"], env["database"])
        client = env["client"]
        conv_id = client.post("/api/chat", headers=auth_headers(token), json={}).json()["conversation_id"]
        r = client.post(f"/api/chat/{conv_id}/messages", headers=auth_headers(token),
                        json={"content": "x" * 4001, "idempotency_key": f"m{uuid.uuid4().hex[:8]}"})
        assert r.status_code == 422  # pydantic 长度校验

    def test_cross_user_conversation_denied(self, env) -> None:
        user_id, token = make_user(env["client"], env["database"])
        client = env["client"]
        conv_id = client.post("/api/chat", headers=auth_headers(token), json={}).json()["conversation_id"]
        _, other_token = make_user(client, env["database"])
        r = client.get(f"/api/chat/{conv_id}", headers=auth_headers(other_token))
        assert r.status_code == 404
