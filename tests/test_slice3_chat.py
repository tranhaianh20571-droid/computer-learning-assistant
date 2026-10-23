"""切片 3 T08 测试：即时文字对话端到端（generation、快照、预算、external、边界）。"""

from __future__ import annotations

import json
import uuid

import pytest

from common_fixtures import (
    SyntheticSearchAdapter,
    auth_headers,
    make_user,
)
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from fastapi.testclient import TestClient  # noqa: E402

from backend.logging_audit.db import Database  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.logging_audit.repositories import PgAuditLog  # noqa: E402
from backend.slice0_app import Slice0State  # noqa: E402
from backend.slice3_app import Slice3State, create_slice3_app  # noqa: E402


class ChatModelAdapter(FunctionModel):
    """Native synthetic model retaining request observations for API assertions."""

    def __init__(self):
        self.generate_calls = []
        super().__init__(self._answer)

    def _answer(self, messages, info):
        self.generate_calls.append({"messages": messages, "instructions": info.instructions})
        return ModelResponse(parts=[TextPart(f"已收到（合成回答 {len(self.generate_calls)}）")])


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
        native_call = env["container"]["model"].generate_calls[-1]
        assert "什么是事务？" in str(native_call["messages"])
        assert "已收到（合成回答 1）" in str(native_call["messages"])
        assert native_call["instructions"]

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

    def test_search_failure_automatically_continues_offline(self, env):
        _, token = make_user(env["client"], env["database"])
        client = env["client"]
        model = env["container"]["model"] = ChatModelAdapter()
        env["container"]["search"] = SyntheticSearchAdapter(fail="timeout")
        conv_id = client.post("/api/chat", headers=auth_headers(token), json={}).json()["conversation_id"]
        result = client.post(f"/api/chat/{conv_id}/messages", headers=auth_headers(token),
            json={"content": "讲解事务概念", "idempotency_key": uuid.uuid4().hex})
        assert result.status_code == 200, result.text
        assert result.json()["external_result_count"] == 0
        assert len(model.generate_calls) == 1
        got = client.get(f"/api/chat/{conv_id}", headers=auth_headers(token)).json()
        assert got["wait_state"] is None
        assert [m["role"] for m in got["messages"]] == ["user", "assistant"]
        assert client.post(f"/api/chat/{conv_id}/resume", headers=auth_headers(token),
            json={"decision": "retry"}).status_code == 409

    def test_optional_search_evidence_is_consumed_and_only_refs_persist(self, env):
        _, token = make_user(env["client"], env["database"])
        client = env["client"]
        model = env["container"]["model"] = ChatModelAdapter()
        env["container"]["search"] = SyntheticSearchAdapter()
        conv_id = client.post("/api/chat", headers=auth_headers(token), json={}).json()["conversation_id"]
        result = client.post(f"/api/chat/{conv_id}/messages", headers=auth_headers(token),
            json={"content": "事务原理", "idempotency_key": uuid.uuid4().hex})
        assert result.status_code == 200, result.text
        assert result.json()["external_result_count"] == 1
        call = model.generate_calls[-1]
        assert "external_results" in str(call["messages"])
        assert "untrusted" in call["instructions"]
        from backend.planning.models import ContextManifestRow
        with env["database"].session() as sess:
            manifest = sess.get(ContextManifestRow, result.json()["manifest_id"])
            assert manifest.conversation_id == conv_id
            refs = json.loads(manifest.retrieval_refs_json)
            external = [r for r in refs if r.get("source") == "external"]
            assert len(external) == 1
            assert "excerpt" not in external[0] and "url" not in external[0]

    def test_latest_question_calls_model_after_search_failure(self, env):
        _, token = make_user(env["client"], env["database"])
        client = env["client"]
        model = env["container"]["model"] = ChatModelAdapter()
        env["container"]["search"] = SyntheticSearchAdapter(fail="timeout")
        conv_id = client.post("/api/chat", headers=auth_headers(token), json={}).json()["conversation_id"]
        result = client.post(f"/api/chat/{conv_id}/messages", headers=auth_headers(token),
            json={"content": "今天的最新发布是什么？", "idempotency_key": uuid.uuid4().hex})
        assert result.status_code == 200, result.text
        assert result.json()["message"]["content"].startswith("已收到")
        assert len(model.generate_calls) == 1
        assert "do not claim current facts were verified" in model.generate_calls[0]["instructions"]

    def test_explicit_search_disabled_does_not_create_adapter(self, env):
        _, token = make_user(env["client"], env["database"])
        client = env["client"]
        env["container"]["model"] = ChatModelAdapter()
        def forbidden(*args):
            raise AssertionError("disabled search touched factory")
        env["state"].chat.search_adapter_factory = forbidden
        conv_id = client.post("/api/chat", headers=auth_headers(token), json={}).json()["conversation_id"]
        result = client.post(f"/api/chat/{conv_id}/messages", headers=auth_headers(token),
            json={"content": "事务", "use_search": False, "idempotency_key": uuid.uuid4().hex})
        assert result.status_code == 200, result.text

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


class StreamingChatAdapter(FunctionModel):
    """Synthetic native streaming model with fixed ordered text fragments."""

    CHUNKS = ["TCP ", "三次握手", "的目的是建立可靠连接。"]

    def __init__(self):
        super().__init__(self._answer, stream_function=self._stream)

    def _answer(self, messages, info):
        return ModelResponse(parts=[TextPart("".join(self.CHUNKS))])

    async def _stream(self, messages, info):
        for chunk in self.CHUNKS:
            yield chunk


class TestStreamingChat:
    @pytest.fixture()
    def streaming_env(self, env):
        env["container"]["model"] = StreamingChatAdapter()
        return env

    def test_stream_deltas_persist_full_answer(self, streaming_env) -> None:
        """delta 按序到达；done 后完整回答落库（generation 语义不变）。"""
        client = streaming_env["client"]
        user_id, token = make_user(client, streaming_env["database"])
        conv_id = client.post("/api/chat", headers=auth_headers(token), json={}).json()["conversation_id"]
        r = client.post(
            f"/api/chat/{conv_id}/messages/stream", headers=auth_headers(token),
            json={"content": "什么是三次握手？", "use_search": False,
                  "idempotency_key": f"m{uuid.uuid4().hex[:8]}"},
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse(r.text)
        types = [e["type"] for e in events]
        assert types[0] == "meta" and types[-1] == "done"
        deltas = [e["text"] for e in events if e["type"] == "delta"]
        assert deltas == StreamingChatAdapter.CHUNKS  # 按序逐段
        got = client.get(f"/api/chat/{conv_id}", headers=auth_headers(token)).json()
        assert got["generation"] == 1
        assistant = [m for m in got["messages"] if m["role"] == "assistant"]
        assert len(assistant) == 1
        assert assistant[0]["content"] == "".join(deltas)
        assert assistant[0]["status"] == "completed"

    def test_stream_search_failure_finishes_without_learner_wait(self, streaming_env):
        client = streaming_env["client"]
        _, token = make_user(client, streaming_env["database"])
        streaming_env["container"]["search"] = SyntheticSearchAdapter(fail="timeout")
        conv_id = client.post("/api/chat", headers=auth_headers(token), json={}).json()["conversation_id"]
        result = client.post(f"/api/chat/{conv_id}/messages/stream", headers=auth_headers(token),
            json={"content": "事务概念", "idempotency_key": uuid.uuid4().hex})
        events = _parse_sse(result.text)
        assert events[-1]["type"] == "done", events
        assert not any(e["type"] == "wait" for e in events)
        got = client.get(f"/api/chat/{conv_id}", headers=auth_headers(token)).json()
        assert got["wait_state"] is None
        assert got["messages"][-1]["status"] == "completed"

    def test_stream_error_event_on_no_adapter(self, streaming_env) -> None:
        client = streaming_env["client"]
        user_id, token = make_user(client, streaming_env["database"])
        streaming_env["container"]["model"] = None
        conv_id = client.post("/api/chat", headers=auth_headers(token), json={}).json()["conversation_id"]
        r = client.post(
            f"/api/chat/{conv_id}/messages/stream", headers=auth_headers(token),
            json={"content": "hi", "idempotency_key": f"m{uuid.uuid4().hex[:8]}"},
        )
        events = _parse_sse(r.text)
        assert events[-1]["type"] == "error"
        assert events[-1]["error_code"] == "MODEL_CAPABILITY_UNAVAILABLE"

    def test_stream_cross_user_denied(self, streaming_env) -> None:
        user_id, token = make_user(streaming_env["client"], streaming_env["database"])
        _, other = make_user(streaming_env["client"], streaming_env["database"])
        conv_id = streaming_env["client"].post(
            "/api/chat", headers=auth_headers(token), json={}).json()["conversation_id"]
        r = streaming_env["client"].post(
            f"/api/chat/{conv_id}/messages/stream", headers=auth_headers(other),
            json={"content": "x", "idempotency_key": f"m{uuid.uuid4().hex[:8]}"})
        assert r.status_code == 404

    def test_client_disconnect_saves_interrupted(self, streaming_env) -> None:
        """注入：客户端中断流 → 已产出文本以 interrupted 状态保留。"""
        user_id, token = make_user(streaming_env["client"], streaming_env["database"])
        client = streaming_env["client"]
        streaming_env["container"]["model"] = StreamingChatAdapter()
        conv_id = client.post("/api/chat", headers=auth_headers(token), json={}).json()["conversation_id"]
        # 直接取服务层生成器，消费 2 段后模拟客户端断开
        meta, gen = streaming_env["state"].chat.stream_answer(
            conv_id, actor_id=user_id, content="讲讲三次握手",
            use_search=False, idempotency_key=f"m{uuid.uuid4().hex[:8]}")
        got = []
        for evt in gen:
            got.append(evt)
            if evt["type"] == "delta" and len([x for x in got if x["type"] == "delta"]) >= 2:
                break
        gen.close()  # 触发 GeneratorExit → interrupted 落库
        got_conv = client.get(f"/api/chat/{conv_id}", headers=auth_headers(token)).json()
        assistant = [m for m in got_conv["messages"] if m["role"] == "assistant"]
        assert len(assistant) == 1
        assert assistant[0]["status"] == "interrupted"
        assert assistant[0]["content"] == "TCP 三次握手"
        # 刷新恢复：已保存的 interrupted 回答仍在历史里
        again = client.get(f"/api/chat/{conv_id}", headers=auth_headers(token)).json()
        assert [m["content"] for m in again["messages"]].count("TCP 三次握手") == 1


def _parse_sse(text: str) -> list[dict]:
    """解析 SSE 帧：事件名取自 event: 行并合并进 data（type 字段）。"""
    events = []
    for frame in text.split("\n\n"):
        lines = frame.split("\n")
        ev_name = next((l[7:] for l in lines if l.startswith("event: ")), None)
        data_line = next((l for l in lines if l.startswith("data: ")), None)
        if not data_line:
            continue
        try:
            data = json.loads(data_line[6:])
        except ValueError:
            continue
        if ev_name:
            data.setdefault("type", ev_name)
        events.append(data)
    return events
