"""集成测试：真实 HTTP/SSE、SQLite 约束、审计触发器、trace 链路。"""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

from backend.logging_audit.server import AppState, start_in_thread  # noqa: E402
from backend.logging_audit.storage import SqliteAuditLog, SqliteTaskEventStore  # noqa: E402
from backend.logging_audit.audit import AuditImmutabilityError, AuditWriteError, AuditAccessDeniedError  # noqa: E402
from backend.logging_audit.task_events import AccessDeniedError, EventsExpiredError  # noqa: E402


class HttpTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.thread, cls.state = start_in_thread(port=0)
        cls.port = cls.httpd.server_address[1]
        cls.base = f"127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.state.close()

    def request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        try:
            parsed = json.loads(data.decode("utf-8")) if data else {}
        except json.JSONDecodeError:
            parsed = {"_raw": data.decode("utf-8", errors="replace")}
        return resp.status, dict(resp.getheaders()), parsed

    def sse(self, path: str, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"Accept": "text/event-stream"}
        if headers:
            hdrs.update(headers)
        conn.request("GET", path, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        return resp.status, raw


class TestTaskLifecycleOverHTTP(HttpTestBase):
    def test_create_and_status(self):
        status, _, body = self.request(
            "POST",
            "/tasks",
            {"task_id": "task_http_1", "owner_user_id": "user_a", "subject_id": "mat_1", "stage": "upload"},
            headers={"X-Actor-Id": "user_a"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["sequence"], 1)

        status, _, body = self.request("GET", "/tasks/task_http_1/status", headers={"X-Actor-Id": "user_a"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "started")
        self.assertEqual(body["owner_user_id"], "user_a")

    def test_cross_account_denied(self):
        self.request(
            "POST",
            "/tasks",
            {"task_id": "task_http_2", "owner_user_id": "owner", "subject_id": "s", "stage": "upload"},
            headers={"X-Actor-Id": "owner"},
        )
        status, _, body = self.request("GET", "/tasks/task_http_2/status", headers={"X-Actor-Id": "intruder"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error_code"], "ACCESS_DENIED")

    def test_traceparent_propagated(self):
        trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
        status, _, _ = self.request(
            "POST",
            "/tasks",
            {"task_id": "task_http_3", "owner_user_id": "user_a", "subject_id": "s", "stage": "ocr"},
            headers={
                "X-Actor-Id": "user_a",
                "traceparent": f"00-{trace_id}-00f067aa0ba902b7-01",
            },
        )
        self.assertEqual(status, 201)
        # 服务端日志应含同一 trace_id
        logs = self.state.logger.stream.records()
        matched = [r for r in logs if r.get("task_id") == "task_http_3"]
        self.assertTrue(matched)
        self.assertEqual(matched[0].get("trace_id"), trace_id)

    def test_missing_traceparent_generates_trace(self):
        status, _, _ = self.request(
            "POST",
            "/tasks",
            {"task_id": "task_http_4", "owner_user_id": "user_a", "subject_id": "s", "stage": "save"},
            headers={"X-Actor-Id": "user_a"},
        )
        self.assertEqual(status, 201)
        logs = self.state.logger.stream.records()
        matched = [r for r in logs if r.get("task_id") == "task_http_4"]
        self.assertTrue(matched)
        self.assertTrue(matched[0].get("trace_id"))
        self.assertNotEqual(matched[0]["trace_id"], "0" * 32)


class TestSSEOverHTTP(HttpTestBase):
    def setUp(self):
        self.task_id = f"task_sse_{uuid.uuid4().hex[:8]}"
        self.request(
            "POST",
            "/tasks",
            {"task_id": self.task_id, "owner_user_id": "user_a", "subject_id": "s", "stage": "upload"},
            headers={"X-Actor-Id": "user_a"},
        )
        for i, stage in enumerate(("parse", "ocr", "index"), start=1):
            self.request(
                "POST",
                "/tasks/events",
                {"task_id": self.task_id, "stage": stage, "status": "succeeded", "duration_ms": i},
                headers={"X-Actor-Id": "user_a"},
            )

    def test_sse_full_stream(self):
        status, raw = self.sse(f"/tasks/{self.task_id}/events", headers={"X-Actor-Id": "user_a"})
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", "text/event-stream")
        self.assertIn("id: evt_", raw)
        self.assertIn("task", raw)
        # 至少 4 条（accepted + 3 stage）
        self.assertGreaterEqual(raw.count("data: "), 4)

    def test_sse_gap_resume_with_last_event_id(self):
        # 先拿全量，记录第 2 个 event id
        status, raw = self.sse(f"/tasks/{self.task_id}/events", headers={"X-Actor-Id": "user_a"})
        self.assertEqual(status, 200)
        ids = []
        for line in raw.splitlines():
            if line.startswith("id: "):
                ids.append(line[4:].strip())
        self.assertGreaterEqual(len(ids), 2)
        last_id = ids[1]

        status, raw = self.sse(
            f"/tasks/{self.task_id}/events",
            headers={"X-Actor-Id": "user_a", "Last-Event-ID": last_id},
        )
        self.assertEqual(status, 200)
        # 共 4 条事件；补取第 2 条之后应只剩 2 条
        self.assertEqual(raw.count("data: "), 2)

    def test_sse_expired_410(self):
        status, _, body = self.request(
            "GET", f"/tasks/{self.task_id}/events.json?after_sequence=0",
            headers={"X-Actor-Id": "user_a"},
        )
        self.assertEqual(status, 200)

        # 游标超前 → 410
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(
            "GET",
            f"/tasks/{self.task_id}/events",
            headers={"X-Actor-Id": "user_a", "X-Last-Sequence": "999"},
        )
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        conn.close()
        self.assertEqual(resp.status, 410)
        self.assertEqual(body["error_code"], "EVENTS_EXPIRED")

    def test_sse_cross_account_rejected(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", f"/tasks/{self.task_id}/events", headers={"X-Actor-Id": "intruder"})
        resp = conn.getresponse()
        conn.close()
        self.assertEqual(resp.status, 403)

    def test_events_json_after_sequence(self):
        status, _, body = self.request(
            "GET",
            f"/tasks/{self.task_id}/events.json?after_sequence=2",
            headers={"X-Actor-Id": "user_a"},
        )
        self.assertEqual(status, 200)
        seqs = [e["sequence"] for e in body["events"]]
        self.assertEqual(seqs, [3, 4])


class TestAuditOverHTTP(HttpTestBase):
    def test_append_and_admin_query(self):
        status, _, body = self.request(
            "POST",
            "/audit",
            {
                "event": "auth.login.failed",
                "actor_id": "user_x",
                "result": "failed",
                "reason": "bad_password",
            },
            headers={"X-Actor-Id": "user_x", "X-Role": "app"},
        )
        self.assertEqual(status, 201)

        status, _, body = self.request(
            "GET",
            "/audit?event=auth.login.failed",
            headers={"X-Actor-Id": "admin_1", "X-Role": "admin"},
        )
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(body["records"]), 1)
        # 查询留痕
        status, _, body = self.request(
            "GET",
            "/audit?event=audit.query.executed",
            headers={"X-Actor-Id": "admin_1", "X-Role": "admin"},
        )
        self.assertGreaterEqual(len(body["records"]), 1)

    def test_app_role_cannot_query(self):
        status, _, body = self.request(
            "GET", "/audit", headers={"X-Actor-Id": "user_x", "X-Role": "app"}
        )
        self.assertEqual(status, 403)

    def test_audit_no_secrets(self):
        self.request(
            "POST",
            "/audit",
            {
                "event": "config.changed",
                "actor_id": "user_a",
                "object_type": "service_config",
                "object_id": "cfg_1",
                "result": "success",
                "reason": "see sk-test-fake-key-000000000000 at synthetic.user@fake.test",
            },
            headers={"X-Actor-Id": "user_a"},
        )
        status, _, body = self.request(
            "GET", "/audit?event=config.changed", headers={"X-Actor-Id": "admin", "X-Role": "admin"}
        )
        text = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("sk-test-fake-key-000000000000", text)
        self.assertNotIn("synthetic.user@fake.test", text)


class TestSqliteConstraints(unittest.TestCase):
    def test_task_sequence_unique(self):
        store = SqliteTaskEventStore()
        store.create_task("t1", "u", "s", "upload")
        store.record("t1", stage="parse", status="succeeded", duration_ms=1)
        events = store.get_events("t1")
        seqs = [e.sequence for e in events]
        self.assertEqual(len(seqs), len(set(seqs)))

    def test_audit_triggers_block_update_delete(self):
        audit = SqliteAuditLog()
        audit.append("access.denied", actor_id="u", result="denied", reason="x")
        with self.assertRaises(AuditImmutabilityError):
            audit.sql_update_forbidden()

    def test_audit_write_failure_injected(self):
        audit = SqliteAuditLog()
        audit.set_fail_write(True)
        with self.assertRaises(AuditWriteError):
            audit.append("auth.login.failed", actor_id="u", result="failed")

    def test_stale_generation_persisted(self):
        store = SqliteTaskEventStore()
        store.create_task("t2", "u", "s", "ocr", generation=3)
        store.record("t2", stage="ocr", status="succeeded", duration_ms=1, generation=3)
        store.record("t2", stage="ocr", status="succeeded", duration_ms=9, generation=1)
        state = store.get_state("t2")
        self.assertEqual(state.generation, 3)
        self.assertEqual(state.status, "succeeded")
        last = store.get_events("t2")[-1]
        self.assertEqual(last.status, "stale_discarded")

    def test_file_persistence_across_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "audit.db"
            a1 = SqliteAuditLog(db_path=db)
            a1.append("connector.bound", actor_id="u", object_id="c1", result="success")
            a1.close()
            a2 = SqliteAuditLog(db_path=db)
            self.assertEqual(len(a2), 1)
            a2.close()


if __name__ == "__main__":
    unittest.main()
