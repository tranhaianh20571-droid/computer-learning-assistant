"""full-stack-fastapi-template 集成：模板 JWT 登录 + 切片 0 任务/租约/SSE。"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "server"
sys.path.insert(0, str(TPL / "backend"))

os.environ.setdefault("FASTAPI_ENV", "development")


@pytest.fixture(scope="module")
def client():
    # 延迟导入，确保 CWD/环境指向模板
    os.chdir(TPL / "backend")
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def _login(client) -> str:
    """用模板 FIRST_SUPERUSER 登录获取 JWT。"""
    from app.core.config import settings

    r = client.post(
        f"{settings.API_V1_STR}/login/access-token",
        data={"username": str(settings.FIRST_SUPERUSER), "password": settings.FIRST_SUPERUSER_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


class TestTemplateLearningAPI:
    def test_health(self, client):
        from app.core.config import settings

        r = client.get(f"{settings.API_V1_STR}/utils/health-check/")
        assert r.status_code == 200

    def test_login_and_create_task_with_lease(self, client):
        from app.core.config import settings

        token = _login(client)
        h = {"Authorization": f"Bearer {token}"}

        r = client.post(
            f"{settings.API_V1_STR}/learning/tasks",
            json={"idempotency_key": f"tpl-{uuid.uuid4().hex[:8]}", "kind": "generic", "subject_id": "mat_1"},
            headers=h,
        )
        assert r.status_code == 201, r.text
        tid = r.json()["task_id"]
        assert r.json().get("prompt_binding_id")

        # SSE
        r = client.get(f"{settings.API_V1_STR}/learning/tasks/{tid}/events", headers=h)
        assert r.status_code == 200
        assert "data: " in r.text

        # claim + result
        r = client.post(
            f"{settings.API_V1_STR}/learning/tasks/{tid}/claim",
            json={"lease_owner": "worker-1", "lease_seconds": 60},
        )
        assert r.status_code == 200, r.text
        lease = r.json()

        r = client.post(
            f"{settings.API_V1_STR}/learning/tasks/{tid}/result",
            json={
                "attempt_id": lease["attempt_id"],
                "lease_token": lease["lease_token"],
                "lease_owner": lease["lease_owner"],
                "generation": lease["generation"],
                "attempt": lease["attempt"],
                "stage": "ocr",
                "status": "succeeded",
                "duration_ms": 12,
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["accepted"] is True

        r = client.get(f"{settings.API_V1_STR}/learning/tasks/{tid}/status", headers=h)
        assert r.status_code == 200
        assert r.json()["status"] == "succeeded"

    def test_stale_lease_rejected(self, client):
        from app.core.config import settings

        token = _login(client)
        h = {"Authorization": f"Bearer {token}"}
        r = client.post(
            f"{settings.API_V1_STR}/learning/tasks",
            json={"idempotency_key": f"tpl-{uuid.uuid4().hex[:8]}", "kind": "generic", "subject_id": "s"},
            headers=h,
        )
        tid = r.json()["task_id"]
        old = client.post(
            f"{settings.API_V1_STR}/learning/tasks/{tid}/claim",
            json={"lease_owner": "w-old", "lease_seconds": 60},
        ).json()
        new = client.post(
            f"{settings.API_V1_STR}/learning/tasks/{tid}/claim",
            json={"lease_owner": "w-new", "lease_seconds": 60},
        ).json()

        r_old = client.post(
            f"{settings.API_V1_STR}/learning/tasks/{tid}/result",
            json={**old, "stage": "ocr", "status": "succeeded", "duration_ms": 1},
        )
        assert r_old.json()["accepted"] is False

        r_new = client.post(
            f"{settings.API_V1_STR}/learning/tasks/{tid}/result",
            json={**new, "stage": "ocr", "status": "succeeded", "duration_ms": 1},
        )
        assert r_new.json()["accepted"] is True

    def test_prompt_binding(self, client):
        from app.core.config import settings

        token = _login(client)
        h = {"Authorization": f"Bearer {token}"}
        r = client.post(
            f"{settings.API_V1_STR}/learning/tasks",
            json={"idempotency_key": f"tpl-{uuid.uuid4().hex[:8]}", "kind": "generic", "subject_id": "s"},
            headers=h,
        )
        tid = r.json()["task_id"]
        r = client.get(f"{settings.API_V1_STR}/learning/tasks/{tid}/prompt", headers=h)
        assert r.status_code == 200
        assert r.json()["template_sha256"]
        assert r.json()["source"] == "local_fallback"

    def test_unauthenticated_rejected(self, client):
        from app.core.config import settings

        r = client.get(f"{settings.API_V1_STR}/learning/tasks/unknown/status")
        assert r.status_code == 401

    def test_product_slice0_auth_task_sse(self, client):
        """产品前端使用的身份/审批/任务链路也必须挂在模板主应用上。"""
        from app.slice0_app import app as slice0_app

        admin_email = "synthetic.template.admin@example.com"
        admin_password = "synthetic-template-admin-password"
        bootstrap = slice0_app.state.slice0.auth.ensure_bootstrap_admin(admin_email, admin_password)
        if not bootstrap["created"]:
            # 测试库可能已有 bootstrap 管理员；重置为本夹具的合成凭据，避免依赖外部状态。
            from sqlalchemy import select
            from app.auth.models import UserRow

            with slice0_app.state.slice0.db.session() as session:
                existing = session.execute(select(UserRow).where(UserRow.is_admin.is_(True))).scalars().first()
                assert existing is not None
                existing.password_hash = slice0_app.state.slice0.auth._hash_password(admin_password)
                admin_email = existing.email

        suffix = uuid.uuid4().hex[:10]
        user_email = f"synthetic.slice0.{suffix}@example.com"
        user_password = "synthetic-template-user-password"

        r = client.post(
            "/api/auth/register",
            json={"email": user_email, "password": user_password, "display_name": "slice0"},
        )
        assert r.status_code == 201, r.text
        user_id = r.json()["user_id"]
        verify_token = r.json()["verify_token"]

        r = client.post("/api/auth/verify-email", json={"token": verify_token})
        assert r.status_code == 200
        assert r.json()["status"] == "pending_approval"

        admin = client.post(
            "/api/auth/login",
            json={"email": admin_email, "password": admin_password},
        )
        assert admin.status_code == 200, admin.text
        admin_headers = {"Authorization": f"Bearer {admin.json()['session_token']}"}
        r = client.post("/api/auth/admin/approve", json={"user_id": user_id}, headers=admin_headers)
        assert r.status_code == 200, r.text

        user = client.post(
            "/api/auth/login",
            json={"email": user_email, "password": user_password},
        )
        assert user.status_code == 200, user.text
        assert "session_token=" in user.headers.get("set-cookie", "")
        assert "HttpOnly" in user.headers.get("set-cookie", "")
        # 浏览器路径使用 HttpOnly cookie，不把会话令牌写入 localStorage 或请求正文。
        headers = {}
        r = client.post(
            "/api/tasks",
            json={"idempotency_key": f"template-slice0-{suffix}", "prompt_name": "lesson_step_v1"},
            headers=headers,
        )
        assert r.status_code == 201, r.text
        task_id = r.json()["task_id"]
        assert r.json()["prompt_binding_id"]

        r = client.get(f"/api/tasks/{task_id}/sse", headers=headers)
        assert r.status_code == 200
        assert "data: " in r.text
