"""v2 UI 支撑端点：课程列表（/api/planning/courses）与管理员待审批列表（/api/auth/admin/pending）。

全部合成数据：待审批用户与课程壳均由测试创建，邮箱为合成值；
邮箱只出现在管理员接口响应中（审批需要），不写入任何日志。
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

PASSWORD = "synthetic-password-12345"


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db


@pytest.fixture()
def env(database: Database):
    state = Slice3State(database, PgAuditLog(database))
    app = create_slice3_app(state=state)
    app.state.slice0 = Slice0State(database, PgAuditLog(database))
    return {"client": TestClient(app), "database": database}


def _make_admin(client: TestClient, database: Database) -> str:
    user_id, token = make_user(client, database)
    from backend.auth.models import UserRow

    with database.session() as sess:
        sess.get(UserRow, user_id).is_admin = True
    return token


def _register_pending(client: TestClient) -> str:
    email = f"pending.{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    vtoken = r.json().get("verify_token")
    if vtoken:
        rv = client.post("/api/auth/verify-email", json={"token": vtoken})
        assert rv.status_code == 200, rv.text
    return email


class TestCourseList:
    def test_owned_shell_summary(self, env) -> None:
        client = env["client"]
        _, token = make_user(client, env["database"])
        r = client.post(
            "/api/planning/courses", headers=auth_headers(token),
            json={"title": "分布式锁入门", "scenario": "self_study"},
        )
        assert r.status_code == 201, r.text
        course_id = r.json()["course_id"]

        r = client.get("/api/planning/courses", headers=auth_headers(token))
        assert r.status_code == 200, r.text
        item = next(c for c in r.json()["data"] if c["course_id"] == course_id)
        assert item["title"] == "分布式锁入门"
        assert item["outline_confirmed"] is False
        assert item["unit_count"] == 0
        assert item["lesson_total"] == 0 and item["lesson_started"] == 0
        assert item["next_lesson_id"] is None

    def test_isolated_by_owner(self, env) -> None:
        client = env["client"]
        _, t1 = make_user(client, env["database"])
        _, t2 = make_user(client, env["database"])
        r = client.post(
            "/api/planning/courses", headers=auth_headers(t1),
            json={"title": "A 独有的课程"},
        )
        assert r.status_code == 201, r.text
        data = client.get("/api/planning/courses", headers=auth_headers(t2)).json()["data"]
        assert all(c["title"] != "A 独有的课程" for c in data)

    def test_requires_login(self, env) -> None:
        r = env["client"].get("/api/planning/courses")
        assert r.status_code == 401, r.text


class TestAdminPending:
    def test_lists_pending_and_requires_admin(self, env) -> None:
        client = env["client"]
        admin_token = _make_admin(client, env["database"])
        _, regular_token = make_user(client, env["database"])
        email = _register_pending(client)

        r = client.get("/api/auth/admin/pending", headers=auth_headers(admin_token))
        assert r.status_code == 200, r.text
        item = next(u for u in r.json()["data"] if u["email"] == email)
        assert item["user_id"]

        # 普通用户 403；匿名 401
        r = client.get("/api/auth/admin/pending", headers=auth_headers(regular_token))
        assert r.status_code == 403, r.text
        r = client.get("/api/auth/admin/pending")
        assert r.status_code == 401, r.text

    def test_approved_user_leaves_pending(self, env) -> None:
        client = env["client"]
        admin_token = _make_admin(client, env["database"])
        email = _register_pending(client)
        user_id = next(
            u["user_id"] for u in client.get(
                "/api/auth/admin/pending", headers=auth_headers(admin_token),
            ).json()["data"] if u["email"] == email
        )
        r = client.post(
            "/api/auth/admin/approve", headers=auth_headers(admin_token),
            json={"user_id": user_id},
        )
        assert r.status_code == 200, r.text
        emails = [u["email"] for u in client.get(
            "/api/auth/admin/pending", headers=auth_headers(admin_token),
        ).json()["data"]]
        assert email not in emails
