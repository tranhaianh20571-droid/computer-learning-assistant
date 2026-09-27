"""切片 0 故障注入与安全回归（T09 / AC-10～AC-12）。"""

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

from backend.auth.models import UserRow  # noqa: E402
from backend.db import Database  # noqa: E402
from backend.logging_audit.exporter import ObservabilityExporter  # noqa: E402
from backend.logging_audit.logger import MemoryLogStream, StructuredLogger  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.logging_audit.repositories import PgAuditLog, PgTaskEventStore  # noqa: E402
from backend.prompts.service import PromptService  # noqa: E402
from backend.slice0_app import Slice0State, create_slice0_app  # noqa: E402
from backend.tasks.service import TaskService  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db


@pytest.fixture()
def env(database: Database):
    stream = MemoryLogStream()
    logger = StructuredLogger("worker", "test", stream=stream)
    audit = PgAuditLog(database)
    events = PgTaskEventStore(database)
    auth_state = Slice0State(database, audit)
    auth_state.events = events
    return {
        "db": database,
        "audit": audit,
        "events": events,
        "tasks": TaskService(database, events),
        "prompts": PromptService(database),
        "state": auth_state,
        "logger": logger,
        "stream": stream,
    }


class TestAC10_LateWorker:
    def test_expired_lease_only_writes_stale_discarded(self, env):
        tasks = env["tasks"]
        created = tasks.create_task(
            owner_user_id="user_a", kind="generic", subject_id="s",
            idempotency_key=f"idem-{uuid.uuid4().hex[:8]}",
        )
        tid = created["task_id"]
        lease_old = tasks.claim(tid, lease_owner="worker-old", lease_seconds=0)
        lease_new = tasks.claim(tid, lease_owner="worker-new", lease_seconds=60)

        # 迟到 worker（旧 lease）提交
        r_old = tasks.submit_result(lease_old, stage="ocr", status="succeeded", duration_ms=999)
        assert r_old["accepted"] is False
        assert r_old["reason"] == "stale_discarded"

        # 当前结果仍属新 lease
        r_new = tasks.submit_result(lease_new, stage="ocr", status="succeeded", duration_ms=10)
        assert r_new["accepted"] is True

        status = tasks.get_task(tid, actor_id="user_a")
        assert status["status"] == "succeeded"
        assert status["revision"] >= 1

        events = env["events"].get_events(tid)
        last = events[-1]
        # 若旧提交写入了诊断，应为 stale_discarded
        stale = [e for e in events if e.status == "stale_discarded"]
        assert len(stale) == 1
        assert stale[0].error_code == "STALE_RESULT_DISCARDED"


class TestAC11_ObservabilityDown:
    def test_langfuse_down_core_task_continues(self, env):
        def dead(item):
            raise ConnectionError("langfuse unreachable")

        exp = ObservabilityExporter("langfuse", logger=env["logger"], sink=dead, failure_threshold=1)
        created = env["tasks"].create_task(
            owner_user_id="user_a", kind="generic", subject_id="s",
            idempotency_key=f"idem-{uuid.uuid4().hex[:8]}",
        )
        lease = env["tasks"].claim(created["task_id"], lease_owner="w")
        exp.enqueue({"trace": "t"})
        result = env["tasks"].submit_result(lease, stage="generate", status="succeeded", duration_ms=5)
        assert result["accepted"] is True
        assert exp.stats.circuit_open or exp.stats.failures >= 1
        assert exp.stats.dropped >= 1
        events = [r["event"] for r in env["stream"].records()]
        assert "observability.export.failed" in events

    def test_audit_write_failure_visible(self, env):
        env["audit"].set_fail_write(True)
        try:
            with pytest.raises(Exception):
                env["audit"].append("access.denied", actor_id="u", result="denied", reason="x")
            assert env["audit"].write_failures >= 1
        finally:
            env["audit"].set_fail_write(False)


class TestAC12_E2EHttp:
    def test_register_verify_approve_login_task_sse(self, database: Database):
        """真实 HTTP 端到端（与 Playwright 同一路径，服务端证据）。"""
        from backend.auth.service import AuthService

        audit = PgAuditLog(database)
        events = PgTaskEventStore(database)
        state = Slice0State(database, audit)
        state.events = events
        app = create_slice0_app(state)
        client = TestClient(app)

        admin_email = f"synthetic.acc12.admin.{uuid.uuid4().hex[:6]}@example.com"
        admin = state.auth.ensure_bootstrap_admin(admin_email, "Synthetic-Admin-Passw0rd!")
        with database.session() as sess:
            u = sess.get(UserRow, admin["user_id"])
            u.email = admin_email
            u.password_hash = state.auth._hash_password("Synthetic-Admin-Passw0rd!")
            u.status = "approved"
            u.is_admin = True
            sess.commit()

        ah = {
            "Authorization": "Bearer "
            + client.post("/api/auth/login", json={"email": admin_email, "password": "Synthetic-Admin-Passw0rd!"})
            .json()["session_token"]
        }

        email = f"synthetic.acc12.user.{uuid.uuid4().hex[:6]}@example.com"
        pw = "Synthetic-User-Passw0rd!"
        r = client.post("/api/auth/register", json={"email": email, "password": pw, "display_name": "u"})
        assert r.status_code == 201
        uid, token = r.json()["user_id"], r.json()["verify_token"]
        assert client.post("/api/auth/verify-email", json={"token": token}).json()["status"] == "pending_approval"
        assert client.post("/api/auth/admin/approve", json={"user_id": uid}, headers=ah).status_code == 200

        login = client.post("/api/auth/login", json={"email": email, "password": pw})
        assert login.status_code == 200
        uh = {"Authorization": f"Bearer {login.json()['session_token']}"}

        r = client.post(
            "/api/tasks",
            json={"idempotency_key": f"idem-{uuid.uuid4().hex[:8]}", "kind": "generic", "subject_id": "mat_1"},
            headers=uh,
        )
        assert r.status_code == 201
        tid = r.json()["task_id"]
        assert r.json().get("prompt_binding_id")

        sse = client.get(f"/api/tasks/{tid}/sse", headers=uh)
        assert sse.status_code == 200
        assert "data: " in sse.text

        # 第二账户交叉访问
        other = f"synthetic.acc12.other.{uuid.uuid4().hex[:6]}@example.com"
        client.post("/api/auth/register", json={"email": other, "password": pw})
        # 未验证无法登录 → 用 admin 访问他人任务
        assert client.get(f"/api/tasks/{tid}/status", headers=ah).status_code == 403

        # 停用后旧会话失效
        assert client.post("/api/auth/admin/disable", json={"user_id": uid}, headers=ah).status_code == 200
        assert client.get("/api/auth/me", headers=uh).status_code == 401


class TestSecretScan:
    def test_no_sensitive_in_runtime_logs_and_audit(self, env):
        fake_key = "sk-test-fake-key-000000000000"
        fake_email = "synthetic.leak@example.com"
        env["audit"].append(
            "config.changed",
            actor_id="user_a",
            object_type="service_config",
            object_id="cfg_1",
            result="success",
            reason=f"rotated {fake_key} for {fake_email}",
        )
        for rec in env["audit"].list_all_for_test():
            text = rec.reason or ""
            assert fake_key not in text
            assert fake_email not in text or "example.com" not in text
