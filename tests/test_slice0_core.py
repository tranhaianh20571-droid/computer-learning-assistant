"""切片 0：身份状态机、会话撤销、租约围栏、提示词绑定（T02/T03/T05）。"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

from backend.logging_audit.db import get_database_url  # noqa: E402

os.environ.setdefault("LEARNING_DATABASE_URL", get_database_url())

from backend.auth.service import AuthError, AuthService  # noqa: E402
from backend.db import Database  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.logging_audit.repositories import PgAuditLog, PgTaskEventStore  # noqa: E402
from backend.prompts.service import PromptService, PromptUnavailable  # noqa: E402
from backend.tasks.service import Lease, LeaseError, TaskService  # noqa: E402
from backend.slice0_app import Slice0State, create_slice0_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db


@pytest.fixture()
def svc(database: Database):
    audit = PgAuditLog(database)
    events = PgTaskEventStore(database)
    return {
        "db": database,
        "audit": audit,
        "auth": AuthService(database, audit),
        "tasks": TaskService(database, events),
        "prompts": PromptService(database),
    }


def _email(tag: str) -> str:
    return f"synthetic.{tag}.{uuid.uuid4().hex[:6]}@example.com"


def _pw() -> str:
    return "Synthetic-Passw0rd!"


class TestAccountLifecycle:
    def test_register_verify_approve_login(self, svc):
        auth = svc["auth"]
        email = _email("user")
        reg = auth.register(email, _pw(), "tester")
        assert reg["status"] == "pending_email"

        # 未验证不可登录
        with pytest.raises(AuthError) as ei:
            auth.login(email, _pw())
        assert ei.value.status == 401

        v = auth.verify_email(reg["verify_token"])
        assert v["status"] == "pending_approval"

        # 未审批不可登录
        with pytest.raises(AuthError):
            auth.login(email, _pw())

        # bootstrap admin
        admin = auth.ensure_bootstrap_admin(_email("admin"), _pw())
        assert admin["created"] in (True, False)
        admin_id = admin["user_id"]

        # 若 admin 是新建的则直接 approved
        ok = auth.approve(admin_id, reg["user_id"])
        assert ok["status"] == "approved"

        info, raw = auth.login(email, _pw())
        assert info.user_id == reg["user_id"]
        user = auth.resolve_session(raw)
        assert user.user_id == reg["user_id"]

        # 登出后会话失效
        auth.logout(raw)
        with pytest.raises(AuthError):
            auth.resolve_session(raw)

    def test_duplicate_email_rejected_without_leak(self, svc):
        auth = svc["auth"]
        email = _email("dup")
        auth.register(email, _pw())
        with pytest.raises(AuthError) as ei:
            auth.register(email, _pw())
        assert ei.value.code == "AUTH_REJECTED"

    def test_short_password_rejected(self, svc):
        with pytest.raises(AuthError):
            svc["auth"].register(_email("short"), "tooshort")

    def test_disable_revokes_sessions(self, svc):
        auth = svc["auth"]
        admin = auth.ensure_bootstrap_admin(_email("admin2"), _pw())
        admin_id = admin["user_id"]

        email = _email("dis")
        reg = auth.register(email, _pw())
        auth.verify_email(reg["verify_token"])
        auth.approve(admin_id, reg["user_id"])
        info, raw = auth.login(email, _pw())

        auth.disable(admin_id, reg["user_id"])
        with pytest.raises(AuthError):
            auth.resolve_session(raw)

    def test_password_reset_revokes_sessions(self, svc):
        auth = svc["auth"]
        admin = auth.ensure_bootstrap_admin(_email("admin3"), _pw())
        admin_id = admin["user_id"]
        email = _email("reset")
        reg = auth.register(email, _pw())
        auth.verify_email(reg["verify_token"])
        auth.approve(admin_id, reg["user_id"])
        info, raw = auth.login(email, _pw())

        req = auth.request_password_reset(email)
        assert req["reset_token"]
        auth.reset_password(req["reset_token"], "NewSynthetic-Passw0rd!")
        with pytest.raises(AuthError):
            auth.resolve_session(raw)

        # 新密码可登录
        info2, raw2 = auth.login(email, "NewSynthetic-Passw0rd!")
        assert info2.user_id == reg["user_id"]

    def test_unapproved_cannot_use_learning_api(self, svc):
        """未审批账户拿不到会话，自然无法调用任务 API。"""
        auth = svc["auth"]
        email = _email("pend")
        reg = auth.register(email, _pw())
        auth.verify_email(reg["verify_token"])
        with pytest.raises(AuthError):
            auth.login(email, _pw())


class TestLeaseFencing:
    def test_claim_and_conditional_submit(self, svc):
        tasks = svc["tasks"]
        created = tasks.create_task(
            owner_user_id="user_a",
            kind="generic",
            subject_id="s1",
            idempotency_key=f"idem-{uuid.uuid4().hex[:8]}",
        )
        assert created["status"] == "queued"

        lease = tasks.claim(created["task_id"], lease_owner="worker-1")
        assert lease.lease_token
        assert lease.attempt == 1

        result = tasks.submit_result(
            lease, stage="ocr", status="succeeded", duration_ms=10
        )
        assert result["accepted"] is True
        assert result["status"] == "succeeded"

    def test_idempotency_key_dedupes(self, svc):
        tasks = svc["tasks"]
        key = f"idem-{uuid.uuid4().hex[:8]}"
        a = tasks.create_task(owner_user_id="user_a", kind="generic", subject_id="s", idempotency_key=key)
        b = tasks.create_task(owner_user_id="user_a", kind="generic", subject_id="s", idempotency_key=key)
        assert b["deduped"] is True
        assert a["task_id"] == b["task_id"]

    def test_double_worker_only_one_wins(self, svc):
        tasks = svc["tasks"]
        created = tasks.create_task(
            owner_user_id="user_a", kind="generic", subject_id="s",
            idempotency_key=f"idem-{uuid.uuid4().hex[:8]}",
        )
        tid = created["task_id"]
        lease1 = tasks.claim(tid, lease_owner="worker-1", lease_seconds=60)
        # 第二个 worker 尝试领取同一任务（已 leased）——仍可抢占（模拟重试），
        # 但 lease1 的旧 token 将失效
        lease2 = tasks.claim(tid, lease_owner="worker-2", lease_seconds=60)

        # 旧 lease 提交被拒
        r1 = tasks.submit_result(lease1, stage="ocr", status="succeeded", duration_ms=1)
        assert r1["accepted"] is False
        assert r1["reason"] == "stale_discarded"

        # 新 lease 成功
        r2 = tasks.submit_result(lease2, stage="ocr", status="succeeded", duration_ms=1)
        assert r2["accepted"] is True

    def test_expired_lease_rejected(self, svc):
        tasks = svc["tasks"]
        created = tasks.create_task(
            owner_user_id="user_a", kind="generic", subject_id="s",
            idempotency_key=f"idem-{uuid.uuid4().hex[:8]}",
        )
        lease = tasks.claim(created["task_id"], lease_owner="w", lease_seconds=0)
        # 租约立即过期
        import time

        time.sleep(0.05)
        r = tasks.submit_result(lease, stage="save", status="succeeded")
        assert r["accepted"] is False

    def test_cross_account_task_denied(self, svc):
        tasks = svc["tasks"]
        created = tasks.create_task(
            owner_user_id="owner", kind="generic", subject_id="s",
            idempotency_key=f"idem-{uuid.uuid4().hex[:8]}",
        )
        from backend.logging_audit.task_events import AccessDeniedError

        with pytest.raises(AccessDeniedError):
            tasks.get_task(created["task_id"], actor_id="intruder")


class TestPromptBinding:
    def test_bind_and_resolve(self, svc):
        prompts = svc["prompts"]
        tasks = svc["tasks"]
        created = tasks.create_task(
            owner_user_id="user_a", kind="generic", subject_id="s",
            idempotency_key=f"idem-{uuid.uuid4().hex[:8]}",
        )
        binding = prompts.bind_for_task(created["task_id"], "lesson_step_v1", allow_fallback=True)
        assert binding["template_sha256"]
        resolved = prompts.resolve_for_task(created["task_id"])
        assert resolved["template_sha256"] == binding["template_sha256"]
        assert resolved["source"] == "local_fallback"

    def test_no_fallback_allowed_raises(self, svc):
        prompts = svc["prompts"]
        with pytest.raises(PromptUnavailable):
            prompts.bind_for_task("task_x", "lesson_step_v1", allow_fallback=False)

    def test_no_binding_resolves_unavailable(self, svc):
        with pytest.raises(PromptUnavailable):
            svc["prompts"].resolve_for_task("task_missing_binding")

    def test_missing_local_template(self, svc):
        with pytest.raises(PromptUnavailable):
            svc["prompts"].load_local_template("does_not_exist")


class TestSlice0HTTP:
    def test_full_chain_register_to_task(self, database: Database):
        audit = PgAuditLog(database)
        events = PgTaskEventStore(database)
        state = Slice0State(database, audit)
        state.events = events
        app = create_slice0_app(state)
        client = TestClient(app)

        # 始终确保有一个可用的测试管理员（已存在则重置密码与邮箱）
        admin_email = _email("httpadmin")
        admin_pw = _pw()
        admin = state.auth.ensure_bootstrap_admin(admin_email, admin_pw)
        admin_id = admin["user_id"]
        from backend.auth.models import UserRow

        with database.session() as sess:
            u = sess.get(UserRow, admin_id)
            u.email = admin_email
            u.password_hash = state.auth._hash_password(admin_pw)
            u.status = "approved"
            u.is_admin = True
            sess.commit()

        admin_login = client.post("/api/auth/login", json={"email": admin_email, "password": admin_pw})
        assert admin_login.status_code == 200
        admin_tok = admin_login.json()["session_token"]
        ah = {"Authorization": f"Bearer {admin_tok}"}

        email = _email("httpuser")
        r = client.post("/api/auth/register", json={"email": email, "password": _pw(), "display_name": "u"})
        assert r.status_code == 201
        uid = r.json()["user_id"]
        token = r.json()["verify_token"]
        r = client.post("/api/auth/verify-email", json={"token": token})
        assert r.json()["status"] == "pending_approval"

        r = client.post("/api/auth/admin/approve", json={"user_id": uid}, headers=ah)
        assert r.status_code == 200

        r = client.post("/api/auth/login", json={"email": email, "password": _pw()})
        assert r.status_code == 200
        utok = r.json()["session_token"]
        uh = {"Authorization": f"Bearer {utok}"}

        r = client.post(
            "/api/tasks",
            json={"idempotency_key": f"idem-{uuid.uuid4().hex[:8]}", "kind": "generic", "subject_id": "mat_1"},
            headers=uh,
        )
        assert r.status_code == 201
        tid = r.json()["task_id"]
        assert r.json().get("prompt_binding_id")

        r = client.get(f"/api/tasks/{tid}/sse", headers=uh)
        assert r.status_code == 200
        assert "data: " in r.text

        # 跨账户/管理员访问他人任务 → 403
        r = client.get(f"/api/tasks/{tid}/status", headers=ah)
        assert r.status_code == 403

        r = client.post("/api/auth/admin/disable", json={"user_id": uid}, headers=ah)
        assert r.status_code == 200
        r = client.get("/api/auth/me", headers=uh)
        assert r.status_code == 401
