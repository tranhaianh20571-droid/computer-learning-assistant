"""切片 3 测试公共辅助：合成适配器、夹具构造（全部合成数据，无真实凭据）。"""

from __future__ import annotations

import io
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

from backend.logging_audit.db import get_database_url  # noqa: E402

os.environ.setdefault("LEARNING_DATABASE_URL", get_database_url())

FAKE_PASSWORD = "synthetic-password-12345"

ROUTE_OK_PAYLOAD = {
    "stages": [
        {"title": "阶段一：基础", "courses": [
            {"title": "数据库基础", "goal": "掌握关系模型"},
            {"title": "SQL 实践", "goal": "能写复杂查询"},
        ]},
        {"title": "阶段二：进阶", "courses": [
            {"title": "事务与并发", "goal": "理解隔离级别"},
            {"title": "索引与优化", "goal": "会读执行计划"},
        ]},
    ],
    "prerequisites": [{"from_index": 0, "to_index": 2}, {"from_index": 1, "to_index": 3}],
}

OUTLINE_OK_PAYLOAD = {
    "units": [
        {"title": "单元一：入门", "lessons": [
            {"title": "第 1 讲：环境准备", "goal": "装好环境"},
            {"title": "第 2 讲：第一个查询", "goal": "能执行 SELECT"},
        ]},
        {"title": "单元二：深入", "lessons": [
            {"title": "第 3 讲：事务", "goal": "理解 ACID"},
            {"title": "第 4 讲：索引", "goal": "会用索引"},
        ]},
    ],
    "prerequisites": [{"from_index": 0, "to_index": 2}],
}


def capability_status(**overrides) -> dict:
    base = {"text": "available", "image": "available", "tool_call": "available",
            "json_schema": "available", "streaming": "available", "cancel": "available"}
    base.update(overrides)
    return base


class SyntheticModelAdapter:
    """合成内容模型适配器：按 capability_status 决定探测，按脚本决定输出。"""

    kind = "content_model"
    protocol = "openai"

    def __init__(self, *, status: dict | None = None, payload: dict | None = None,
                 scripted: list | None = None, raise_on_generate: Exception | None = None) -> None:
        self.capability_status = status or capability_status()
        self.payload = payload if payload is not None else ROUTE_OK_PAYLOAD
        self.scripted = list(scripted or [])
        self.raise_on_generate = raise_on_generate
        self.generate_calls: list[dict] = []

    def probe(self, capability: str):
        return SimpleNamespace(capability=capability,
                               state=self.capability_status.get(capability, "unavailable"))

    def generate(self, messages, **options):
        self.generate_calls.append(options)
        if self.raise_on_generate is not None:
            raise self.raise_on_generate
        if self.scripted:
            action = self.scripted.pop(0)
            if isinstance(action, Exception):
                raise action
            return action
        # 默认按模式给出可解析输出
        if "tools" in options:
            import json

            return {"content": "", "tool_arguments": json.dumps(self.payload, ensure_ascii=False)}
        if "response_format" in options:
            import json

            return {"content": json.dumps(self.payload, ensure_ascii=False)}
        import json

        return {"content": f"```json\n{json.dumps(self.payload, ensure_ascii=False)}\n```"}


class BrokenJsonThenGoodAdapter(SyntheticModelAdapter):
    """native/tool 模式输出坏内容、prompted 模式输出好内容 → 降级链测试。"""

    def generate(self, messages, **options):
        self.generate_calls.append(options)
        import json

        if "response_format" in options or "tools" in options:
            return {"content": "抱歉，我无法按该格式输出。"}
        return {"content": f"```json\n{json.dumps(self.payload, ensure_ascii=False)}\n```"}


class AllUnavailableAdapter(SyntheticModelAdapter):
    def __init__(self) -> None:
        super().__init__(status=capability_status(text="unavailable", json_schema="unavailable",
                                                  tool_call="unavailable"))


class SyntheticSearchAdapter:
    """合成搜索适配器：ok / timeout / error 三态。"""

    kind = "search"
    protocol = "tavily_hikari"

    def __init__(self, *, fail: str | None = None) -> None:
        self.fail = fail  # None | "timeout" | "error"
        self.calls = 0

    def search(self, query, **kwargs):
        self.calls += 1
        if self.fail == "timeout":
            exc = Exception("timeout")
            exc.code = "timeout"
            raise exc
        if self.fail == "error":
            exc = Exception("connection_error")
            exc.code = "connection_error"
            raise exc
        return {
            "query_id": f"qry_{uuid.uuid4().hex[:8]}",
            "results": [
                {"title": "合成搜索结果", "url": "https://example.com/synthetic",
                 "content": "synthetic snippet", "fetched_at": "2026-09-27T00:00:00Z"}
            ],
        }


def make_user(client, database) -> str:
    """注册 + 置 approved + 登录，返回 (user_id, token)。"""
    from backend.auth.models import UserRow

    email = f"synthetic.{uuid.uuid4().hex[:8]}@example.com"
    auth = client.app.state.slice0.auth
    registered = auth.register(email, FAKE_PASSWORD, display_name="synth3")
    user_id = registered["user_id"]
    with database.session() as sess:
        row = sess.get(UserRow, user_id)
        row.status = "approved"
        row.is_admin = False
    _, token = auth.login(email, FAKE_PASSWORD)
    return user_id, token


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def txt_bytes(text: str = "神经网络 卷积 conv2d_block_7 v1.2.3") -> bytes:
    return text.encode("utf-8")
