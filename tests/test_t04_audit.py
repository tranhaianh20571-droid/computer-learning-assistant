"""T04 单元/集成测试：追加式审计、不可变、查询权限、查询审计。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

from backend.logging_audit.audit import (  # noqa: E402
    AuditAccessDeniedError,
    AuditImmutabilityError,
    AuditLog,
    AuditWriteError,
)
from backend.logging_audit.sanitize import dumps_line  # noqa: E402


class TestAuditAppend(unittest.TestCase):
    def setUp(self):
        self.audit = AuditLog()

    def test_append_success_and_failure(self):
        self.audit.append(
            "auth.login.failed",
            actor_id="user_a",
            result="failed",
            reason="bad_password",
            trace_id="a" * 32,
        )
        self.audit.append(
            "admin.approval.changed",
            actor_id="admin_1",
            admin_id="admin_1",
            target_actor_id="user_b",
            object_type="user",
            object_id="user_b",
            result="success",
        )
        records = self.audit.list_all_for_test()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].event, "auth.login.failed")
        self.assertTrue(records[0].content_hash)

    def test_no_sensitive_fields_in_output(self):
        self.audit.append(
            "config.changed",
            actor_id="user_a",
            object_type="service_config",
            object_id="config_7e52",
            result="success",
            reason="rotate",
        )
        for rec in self.audit.list_all_for_test():
            line = dumps_line(rec.to_record())
            for banned in ("password", "api_key", "token", "cookie", "@"):
                self.assertNotIn(banned, line if banned != "@" else line)
            self.assertNotIn("email", rec.to_record())

    def test_immutable(self):
        self.audit.append("access.denied", actor_id="user_x", resource_type="material",
                          resource_id="material_1", result="denied", reason="not_owner")
        with self.assertRaises(AuditImmutabilityError):
            self.audit.update()
        with self.assertRaises(AuditImmutabilityError):
            self.audit.delete()
        self.assertEqual(len(self.audit), 1)


class TestAuditQueryPermissions(unittest.TestCase):
    def setUp(self):
        self.audit = AuditLog()
        self.audit.append(
            "resource.delete.requested",
            actor_id="user_a",
            object_type="material",
            object_id="material_9",
            result="success",
        )

    def test_app_role_cannot_query(self):
        with self.assertRaises(AuditAccessDeniedError):
            self.audit.query(actor_id="user_a", role="app")

    def test_admin_can_query(self):
        results = self.audit.query(actor_id="admin_1", role="admin")
        self.assertGreaterEqual(len(results), 1)

    def test_query_writes_audit(self):
        before = len(self.audit)
        self.audit.query(actor_id="admin_1", role="admin", event="resource.delete.requested")
        events = [r.event for r in self.audit.list_all_for_test()]
        self.assertIn("audit.query.executed", events)
        self.assertGreater(len(self.audit), before)

    def test_query_failure_attempt_recorded(self):
        # 写入失败注入下查询仍要能暴露 write_failures
        self.audit.set_fail_write(True)
        with self.assertRaises(AuditWriteError):
            self.audit.append("access.denied", actor_id="u", result="denied", reason="x")
        self.assertEqual(self.audit.write_failures, 1)

    def test_access_denied_event(self):
        self.audit.append(
            "access.denied",
            actor_id="user_b",
            resource_type="task",
            resource_id="task_1",
            result="denied",
            reason="not_owner",
            trace_id="b" * 32,
        )
        denied = [r for r in self.audit.list_all_for_test() if r.event == "access.denied"]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0].result, "denied")


class TestAuditCoverage(unittest.TestCase):
    """敏感操作覆盖：登录失败、审批、配置、外发、连接器、删除、清理、恢复。"""

    def test_all_categories(self):
        audit = AuditLog()
        samples = [
            ("auth.login.failed", {"actor_id": "u1", "result": "failed"}),
            ("auth.session.revoked", {"actor_id": "u1", "result": "success"}),
            ("admin.approval.changed", {
                "actor_id": "admin_1", "admin_id": "admin_1",
                "target_actor_id": "u2", "object_type": "user", "object_id": "u2",
                "result": "success",
            }),
            ("config.changed", {
                "actor_id": "u1", "object_type": "service_config",
                "object_id": "cfg_1", "result": "success",
            }),
            ("external.disclosure.confirmed", {
                "actor_id": "u1", "capability": "admin_ocr", "result": "success",
            }),
            ("connector.bound", {"actor_id": "u1", "object_id": "conn_1", "result": "success"}),
            ("connector.revoked", {"actor_id": "u1", "object_id": "conn_1", "result": "success"}),
            ("access.denied", {
                "actor_id": "u3", "resource_type": "material",
                "resource_id": "m1", "result": "denied", "reason": "not_owner",
            }),
            ("resource.delete.requested", {
                "actor_id": "u1", "object_type": "material",
                "object_id": "m1", "result": "success",
            }),
            ("cleanup.completed", {"actor_id": "system", "cleanup_stage": "online", "result": "success"}),
            ("restore.locked", {"actor_id": "system", "result": "locked"}),
        ]
        for event, kwargs in samples:
            rec = audit.append(event, **kwargs)
            self.assertEqual(rec.event, event)
        self.assertEqual(len(audit), len(samples))


if __name__ == "__main__":
    unittest.main()
