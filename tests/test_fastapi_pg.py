"""FastAPI + PostgreSQL 集成测试（TestClient + 真实 PG）。"""

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

from backend.logging_audit.app import AppState, create_app  # noqa: E402
from backend.logging_audit.db import Database  # noqa: E402
from backend.logging_audit.logger import MemoryLogStream, StructuredLogger  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db


@pytest.fixture(scope="session")
def client(database: Database):
    stream = MemoryLogStream()
    logger = StructuredLogger("api", "test", stream=stream)
    state = AppState(db=database, logger=logger)
    app = create_app(state)
    with TestClient(app) as c:
        c.app_state = state  # type: ignore[attr-defined]
        c.log_stream = stream  # type: ignore[attr-defined]
        yield c


def _tid(prefix: str = "task") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class TestTaskLifecycle:
    def test_create_and_status(self, client: TestClient):
        tid = _tid()
        r = client.post(
            "/tasks",
            json={"task_id": tid, "owner_user_id": "user_a", "subject_id": "mat_1", "stage": "upload"},
            headers={"X-Actor-Id": "user_a"},
        )
        assert r.status_code == 201
        assert r.json()["sequence"] == 1

        r = client.get(f"/tasks/{tid}/status", headers={"X-Actor-Id": "user_a"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "started"
        assert body["owner_user_id"] == "user_a"
        assert body["revision"] == 1

    def test_cross_account_denied(self, client: TestClient):
        tid = _tid()
        client.post(
            "/tasks",
            json={"task_id": tid, "owner_user_id": "owner", "subject_id": "s", "stage": "upload"},
            headers={"X-Actor-Id": "owner"},
        )
        r = client.get(f"/tasks/{tid}/status", headers={"X-Actor-Id": "intruder"})
        assert r.status_code == 403
        assert r.json()["error_code"] == "ACCESS_DENIED"

    def test_traceparent_propagated(self, client: TestClient):
        tid = _tid()
        trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
        r = client.post(
            "/tasks",
            json={"task_id": tid, "owner_user_id": "user_a", "subject_id": "s", "stage": "ocr"},
            headers={"X-Actor-Id": "user_a", "traceparent": f"00-{trace_id}-00f067aa0ba902b7-01"},
        )
        assert r.status_code == 201
        assert r.headers.get("X-Trace-Id") == trace_id
        logs = client.log_stream.records()
        matched = [x for x in logs if x.get("task_id") == tid]
        assert matched and matched[0].get("trace_id") == trace_id

    def test_missing_traceparent_generates(self, client: TestClient):
        tid = _tid()
        r = client.post(
            "/tasks",
            json={"task_id": tid, "owner_user_id": "user_a", "subject_id": "s", "stage": "save"},
            headers={"X-Actor-Id": "user_a"},
        )
        assert r.status_code == 201
        assert r.headers.get("X-Trace-Id")
        assert r.headers["X-Trace-Id"] != "0" * 32


class TestSSE:
    def test_sse_full_and_gap(self, client: TestClient):
        tid = _tid("sse")
        client.post(
            "/tasks",
            json={"task_id": tid, "owner_user_id": "user_a", "subject_id": "s", "stage": "upload"},
            headers={"X-Actor-Id": "user_a"},
        )
        for i, stage in enumerate(("parse", "ocr", "index"), start=1):
            r = client.post(
                "/tasks/events",
                json={"task_id": tid, "stage": stage, "status": "succeeded", "duration_ms": i},
                headers={"X-Actor-Id": "user_a"},
            )
            assert r.status_code == 201

        r = client.get(f"/tasks/{tid}/events", headers={"X-Actor-Id": "user_a"})
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        text = r.text
        assert text.count("data: ") >= 4
        ids = [ln[4:].strip() for ln in text.splitlines() if ln.startswith("id: ")]
        assert len(ids) >= 2

        r2 = client.get(
            f"/tasks/{tid}/events",
            headers={"X-Actor-Id": "user_a", "Last-Event-ID": ids[1]},
        )
        assert r2.status_code == 200
        assert r2.text.count("data: ") == 2

    def test_sse_expired_410(self, client: TestClient):
        tid = _tid("exp")
        client.post(
            "/tasks",
            json={"task_id": tid, "owner_user_id": "user_a", "subject_id": "s", "stage": "upload"},
            headers={"X-Actor-Id": "user_a"},
        )
        r = client.get(
            f"/tasks/{tid}/events",
            headers={"X-Actor-Id": "user_a", "X-Last-Sequence": "999"},
        )
        assert r.status_code == 410
        assert r.json()["error_code"] == "EVENTS_EXPIRED"

    def test_sse_cross_account(self, client: TestClient):
        tid = _tid("x")
        client.post(
            "/tasks",
            json={"task_id": tid, "owner_user_id": "owner", "subject_id": "s", "stage": "upload"},
            headers={"X-Actor-Id": "owner"},
        )
        r = client.get(f"/tasks/{tid}/events", headers={"X-Actor-Id": "intruder"})
        assert r.status_code == 403

    def test_events_json_after_sequence(self, client: TestClient):
        tid = _tid("json")
        client.post(
            "/tasks",
            json={"task_id": tid, "owner_user_id": "user_a", "subject_id": "s", "stage": "upload"},
            headers={"X-Actor-Id": "user_a"},
        )
        for stage in ("parse", "ocr", "index"):
            client.post(
                "/tasks/events",
                json={"task_id": tid, "stage": stage, "status": "succeeded", "duration_ms": 1},
                headers={"X-Actor-Id": "user_a"},
            )
        r = client.get(f"/tasks/{tid}/events.json?after_sequence=2", headers={"X-Actor-Id": "user_a"})
        assert r.status_code == 200
        seqs = [e["sequence"] for e in r.json()["events"]]
        assert seqs == [3, 4]


class TestAudit:
    def test_append_admin_query_and_audit_query(self, client: TestClient):
        r = client.post(
            "/audit",
            json={"event": "auth.login.failed", "actor_id": "user_x", "result": "failed", "reason": "bad_password"},
            headers={"X-Actor-Id": "user_x", "X-Role": "app"},
        )
        assert r.status_code == 201

        r = client.get("/audit?event=auth.login.failed", headers={"X-Actor-Id": "admin", "X-Role": "admin"})
        assert r.status_code == 200
        assert len(r.json()["records"]) >= 1

        r = client.get("/audit?event=audit.query.executed", headers={"X-Actor-Id": "admin", "X-Role": "admin"})
        assert len(r.json()["records"]) >= 1

    def test_app_role_cannot_query(self, client: TestClient):
        r = client.get("/audit", headers={"X-Actor-Id": "user_x", "X-Role": "app"})
        assert r.status_code == 403

    def test_audit_write_failure_503(self, client: TestClient):
        client.app_state.audit.set_fail_write(True)  # type: ignore[attr-defined]
        try:
            r = client.post(
                "/audit",
                json={"event": "auth.login.failed", "actor_id": "u", "result": "failed"},
                headers={"X-Actor-Id": "u"},
            )
            assert r.status_code == 503
            assert r.json()["error_code"] == "AUDIT_WRITE_FAILED"
        finally:
            client.app_state.audit.set_fail_write(False)  # type: ignore[attr-defined]

    def test_no_secrets_in_audit(self, client: TestClient):
        fake_key = "sk-test-fake-key-000000000000"
        fake_email = "synthetic.user@fake.test"
        client.post(
            "/audit",
            json={
                "event": "config.changed",
                "actor_id": "user_a",
                "object_type": "service_config",
                "object_id": "cfg_1",
                "result": "success",
                "reason": f"leak {fake_key} at {fake_email}",
            },
            headers={"X-Actor-Id": "user_a"},
        )
        r = client.get("/audit?event=config.changed", headers={"X-Actor-Id": "admin", "X-Role": "admin"})
        text = r.text
        assert fake_key not in text
        assert fake_email not in text


class TestFaultInjection:
    def test_stale_generation_cannot_overwrite(self, client: TestClient):
        tid = _tid("stale")
        client.post(
            "/tasks",
            json={"task_id": tid, "owner_user_id": "user_a", "subject_id": "s", "stage": "ocr", "generation": 3},
            headers={"X-Actor-Id": "user_a"},
        )
        client.post(
            "/tasks/events",
            json={"task_id": tid, "stage": "ocr", "status": "succeeded", "duration_ms": 10, "generation": 3},
            headers={"X-Actor-Id": "user_a"},
        )
        client.post(
            "/tasks/events",
            json={"task_id": tid, "stage": "ocr", "status": "succeeded", "duration_ms": 999, "generation": 1},
            headers={"X-Actor-Id": "user_a"},
        )
        status = client.get(f"/tasks/{tid}/status", headers={"X-Actor-Id": "user_a"}).json()
        assert status["generation"] == 3
        assert status["status"] == "succeeded"
        events = client.get(f"/tasks/{tid}/events.json", headers={"X-Actor-Id": "user_a"}).json()["events"]
        last = events[-1]
        assert last["status"] == "stale_discarded"
        assert last["error_code"] == "STALE_RESULT_DISCARDED"

    def test_audit_sql_update_blocked(self, client: TestClient):
        from backend.logging_audit.audit import AuditImmutabilityError

        with pytest.raises(AuditImmutabilityError):
            client.app_state.audit.sql_update_forbidden()  # type: ignore[attr-defined]

    def test_leak_scan_full_chain(self, client: TestClient):
        fake_key = "sk-test-fake-key-000000000000"
        fake_email = "synthetic.user@fake.test"
        tid = _tid("leak")
        client.post(
            "/tasks",
            json={
                "task_id": tid,
                "owner_user_id": "user_a",
                "subject_id": "mat_1",
                "stage": "ocr",
                "template_params": {"note": f"see {fake_email}"},
            },
            headers={
                "X-Actor-Id": "user_a",
                "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            },
        )
        client.post(
            "/tasks/events",
            json={
                "task_id": tid,
                "stage": "ocr",
                "status": "failed",
                "error_code": "OCR_OUTPUT_INVALID",
                "duration_ms": 5,
            },
            headers={"X-Actor-Id": "user_a"},
        )
        log_text = client.log_stream.getvalue()  # type: ignore[attr-defined]
        assert fake_key not in log_text
        assert fake_email not in log_text

        evt_text = client.get(f"/tasks/{tid}/events.json", headers={"X-Actor-Id": "user_a"}).text
        assert fake_key not in evt_text
        assert fake_email not in evt_text
