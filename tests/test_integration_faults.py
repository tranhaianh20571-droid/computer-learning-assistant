"""集成级故障注入：HTTP 路径上的迟到 worker、观测降级、审计失败、泄漏扫描。"""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

from backend.logging_audit.server import AppState, start_in_thread  # noqa: E402
from backend.logging_audit.exporter import ObservabilityExporter  # noqa: E402
from backend.logging_audit.logger import MemoryLogStream, StructuredLogger  # noqa: E402
from backend.logging_audit.storage import SqliteAuditLog  # noqa: E402
from backend.logging_audit.audit import AuditWriteError  # noqa: E402

import http.client


class IntegrationFaultBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stream = MemoryLogStream()
        logger = StructuredLogger("api", "test", stream=stream)
        cls.state = AppState(db_path=":memory:", logger=logger)
        cls.httpd, cls.thread, _ = start_in_thread(port=0, state=cls.state)
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.state.close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        try:
            parsed = json.loads(data.decode() or "{}")
        except json.JSONDecodeError:
            parsed = {}
        return resp.status, parsed


class TestWorkerLateResultOverHTTP(IntegrationFaultBase):
    def test_old_generation_cannot_overwrite_current(self):
        task_id = f"task_late_{uuid.uuid4().hex[:6]}"
        self.request(
            "POST",
            "/tasks",
            {"task_id": task_id, "owner_user_id": "user_a", "subject_id": "s", "stage": "ocr", "generation": 3},
            headers={"X-Actor-Id": "user_a"},
        )
        # 当前 generation=3 成功
        self.request(
            "POST",
            "/tasks/events",
            {"task_id": task_id, "stage": "ocr", "status": "succeeded", "duration_ms": 10, "generation": 3},
            headers={"X-Actor-Id": "user_a"},
        )
        # 旧 worker generation=1 迟到
        self.request(
            "POST",
            "/tasks/events",
            {"task_id": task_id, "stage": "ocr", "status": "succeeded", "duration_ms": 999, "generation": 1},
            headers={"X-Actor-Id": "user_a"},
        )
        status, body = self.request("GET", f"/tasks/{task_id}/status", headers={"X-Actor-Id": "user_a"})
        self.assertEqual(status, 200)
        self.assertEqual(body["generation"], 3)
        self.assertEqual(body["status"], "succeeded")

        status, body = self.request(
            "GET", f"/tasks/{task_id}/events.json?after_sequence=0", headers={"X-Actor-Id": "user_a"}
        )
        last = body["events"][-1]
        self.assertEqual(last["status"], "stale_discarded")
        self.assertEqual(last["error_code"], "STALE_RESULT_DISCARDED")


class TestObservabilityOverHTTP(IntegrationFaultBase):
    def test_exporter_down_core_task_continues(self):
        stream = MemoryLogStream()
        logger = StructuredLogger("worker", "test", stream=stream)

        def dead(item):
            raise ConnectionError("langfuse down")

        exp = ObservabilityExporter("langfuse", logger=logger, sink=dead, failure_threshold=1)
        task_id = f"task_obs_{uuid.uuid4().hex[:6]}"
        self.request(
            "POST",
            "/tasks",
            {"task_id": task_id, "owner_user_id": "user_a", "subject_id": "s", "stage": "generate"},
            headers={"X-Actor-Id": "user_a"},
        )
        exp.enqueue({"trace": "t"})
        status, body = self.request(
            "POST",
            "/tasks/events",
            {"task_id": task_id, "stage": "generate", "status": "succeeded", "duration_ms": 20},
            headers={"X-Actor-Id": "user_a"},
        )
        self.assertEqual(status, 201)
        status, body = self.request("GET", f"/tasks/{task_id}/status", headers={"X-Actor-Id": "user_a"})
        self.assertEqual(body["status"], "succeeded")
        self.assertTrue(exp.stats.circuit_open or exp.stats.failures >= 1)
        events = [r["event"] for r in stream.records()]
        self.assertIn("observability.export.failed", events)


class TestAuditFailureOverHTTP(IntegrationFaultBase):
    def test_audit_write_failure_returns_503(self):
        self.state.audit.set_fail_write(True)
        try:
            status, body = self.request(
                "POST",
                "/audit",
                {"event": "auth.login.failed", "actor_id": "u", "result": "failed"},
                headers={"X-Actor-Id": "u"},
            )
            self.assertEqual(status, 503)
            self.assertEqual(body["error_code"], "AUDIT_WRITE_FAILED")
            self.assertGreaterEqual(self.state.audit.write_failures, 1)
        finally:
            self.state.audit.set_fail_write(False)

    def test_audit_sql_update_blocked(self):
        audit = self.state.audit
        with self.assertRaises(Exception):
            audit.sql_update_forbidden()


class TestLeakScanOverHTTP(IntegrationFaultBase):
    def test_full_chain_no_secrets_in_logs_or_audit(self):
        fake_key = "sk-test-fake-key-000000000000"
        fake_email = "synthetic.user@fake.test"
        fake_url = "https://vendor.example.test/v1?api_key=SECRET"
        task_id = f"task_leak_{uuid.uuid4().hex[:6]}"
        self.request(
            "POST",
            "/tasks",
            {
                "task_id": task_id,
                "owner_user_id": "user_a",
                "subject_id": "mat_1",
                "stage": "ocr",
                "template_params": {"note": f"check {fake_email}"},
            },
            headers={"X-Actor-Id": "user_a", "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"},
        )
        self.request(
            "POST",
            "/tasks/events",
            {
                "task_id": task_id,
                "stage": "ocr",
                "status": "failed",
                "error_code": "OCR_OUTPUT_INVALID",
                "duration_ms": 5,
            },
            headers={"X-Actor-Id": "user_a"},
        )
        self.request(
            "POST",
            "/audit",
            {
                "event": "config.changed",
                "actor_id": "user_a",
                "object_type": "service_config",
                "object_id": "cfg_1",
                "result": "success",
                "reason": f"leak {fake_key} in {fake_url}",
            },
            headers={"X-Actor-Id": "user_a"},
        )

        # 运行日志
        log_text = self.state.logger.stream.getvalue()
        self.assertNotIn(fake_key, log_text)
        self.assertNotIn(fake_email, log_text)
        self.assertNotIn("api_key=SECRET", log_text)

        # 任务事件
        status, body = self.request(
            "GET", f"/tasks/{task_id}/events.json?after_sequence=0", headers={"X-Actor-Id": "user_a"}
        )
        evt_text = json.dumps(body, ensure_ascii=False)
        self.assertNotIn(fake_key, evt_text)
        self.assertNotIn(fake_email, evt_text)

        # 审计
        status, body = self.request(
            "GET", "/audit?event=config.changed", headers={"X-Actor-Id": "admin", "X-Role": "admin"}
        )
        audit_text = json.dumps(body, ensure_ascii=False)
        self.assertNotIn(fake_key, audit_text)
        self.assertNotIn("api_key=SECRET", audit_text)


if __name__ == "__main__":
    unittest.main()
