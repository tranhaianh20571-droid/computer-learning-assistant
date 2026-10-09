"""访客模式（auth/guest.py）：无登录态时以访客 Cookie 主体继续使用。

契约：
- /api/auth/guest 签发 HttpOnly 访客 Cookie，同一 Cookie 映射稳定 owner；
- 访客可创建并读取自己的资源，不同访客之间、访客与登录用户之间互相隔离；
- /api/auth/me 语义不变：访客仍 401（前端据此展示登录入口）；
- 无效 Bearer 仍 401；过期/失效的 session Cookie 回落为访客而不是阻断。
全部合成数据。
"""

from __future__ import annotations

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

from common_fixtures import auth_headers, make_user  # noqa: E402


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db


@pytest.fixture()
def app(database: Database):
    state = Slice3State(database, PgAuditLog(database))
    application = create_slice3_app(state=state)
    application.state.slice0 = Slice0State(database, PgAuditLog(database))
    return application


def _create_task(client: TestClient, title: str, **kw) -> str:
    """建路线壳 + route_generate 规划任务，返回 task_id（归属当前主体）。"""
    r = client.post("/api/planning/routes", json={"title": title, "scenario": "self_study"}, **kw)
    assert r.status_code == 201, r.text
    route_id = r.json()["route_id"]
    r = client.post(
        "/api/planning/tasks",
        json={
            "idempotency_key": f"g-{uuid.uuid4().hex[:8]}",
            "task_kind": "route_generate",
            "planning_scope": "auto",
            "target_route_id": route_id,
        },
        **kw,
    )
    assert r.status_code == 201, r.text
    return r.json()["task_id"]


def test_guest_bootstrap_sets_stable_cookie(app) -> None:
    client = TestClient(app)
    r1 = client.get("/api/auth/guest")
    assert r1.status_code == 200, r1.text
    body = r1.json()
    assert body["is_guest"] is True and body["is_admin"] is False
    assert body["user_id"].startswith("guest_")
    set_cookie = r1.headers.get("set-cookie", "")
    assert "guest_id=" in set_cookie and "httponly" in set_cookie.lower()

    # 带着 Cookie 再次引导：身份不变，也不重复下发 Cookie
    r2 = client.get("/api/auth/guest")
    assert r2.json()["user_id"] == body["user_id"]
    assert "guest_id=" not in r2.headers.get("set-cookie", "")


def test_me_still_401_for_guest(app) -> None:
    client = TestClient(app)
    client.get("/api/auth/guest")
    assert client.get("/api/auth/me").status_code == 401


def test_guest_can_create_and_read_own_route(app) -> None:
    client = TestClient(app)
    client.get("/api/auth/guest")
    task_id = _create_task(client, "访客的路线")
    r = client.get(f"/api/planning/tasks/{task_id}")
    assert r.status_code == 200, r.text


def test_guests_are_isolated(app) -> None:
    a, b = TestClient(app), TestClient(app)
    a.get("/api/auth/guest")
    b.get("/api/auth/guest")
    task_id = _create_task(a, "A 的路线")
    assert b.get(f"/api/planning/tasks/{task_id}").status_code in (403, 404)


def test_guest_and_user_are_isolated(app, database) -> None:
    guest = TestClient(app)
    guest.get("/api/auth/guest")
    task_id = _create_task(guest, "访客私有路线")

    user = TestClient(app)
    _, token = make_user(user, database)
    assert user.get(f"/api/planning/tasks/{task_id}", headers=auth_headers(token)).status_code in (403, 404)


def test_logged_in_user_resolves_as_user(app, database) -> None:
    client = TestClient(app)
    user_id, token = make_user(client, database)
    r = client.get("/api/auth/guest", headers=auth_headers(token))
    assert r.status_code == 200
    assert r.json()["user_id"] == user_id and r.json()["is_guest"] is False


def test_invalid_bearer_still_rejected(app) -> None:
    client = TestClient(app)
    r = client.get("/api/planning/courses", headers=auth_headers("not-a-real-token"))
    assert r.status_code == 401


def test_stale_session_cookie_falls_back_to_guest(app) -> None:
    client = TestClient(app)
    client.cookies.set("session_token", "stale-or-revoked")
    r = client.get("/api/planning/courses")
    assert r.status_code == 200, r.text
    assert r.json()["data"] == []
