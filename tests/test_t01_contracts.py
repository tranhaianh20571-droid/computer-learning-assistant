"""T01 单元测试：字段允许名单、错误码、事件注册表、脱敏与序列化。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

from backend.logging_audit.contracts import (  # noqa: E402
    ALLOWED_FIELDS,
    ERROR_CODES,
    EVENT_REGISTRY,
    FORBIDDEN_FIELD_NAMES,
    REQUIRED_COMMON_FIELDS,
    TaskStage,
    build_registry_document,
    validate_error_code,
    validate_event_name,
    validate_field_name,
    validate_stage,
    ContractError,
)
from backend.logging_audit.sanitize import (  # noqa: E402
    dumps_line,
    map_vendor_error,
    redact_sensitive_text,
    sanitize_record,
    sanitize_string,
    strip_control_chars,
)


class TestFieldAllowlist(unittest.TestCase):
    def test_required_common_fields_present(self):
        self.assertIn("schema_version", REQUIRED_COMMON_FIELDS)
        self.assertIn("timestamp", REQUIRED_COMMON_FIELDS)
        self.assertIn("level", REQUIRED_COMMON_FIELDS)
        self.assertIn("event", REQUIRED_COMMON_FIELDS)
        self.assertIn("service", REQUIRED_COMMON_FIELDS)
        self.assertIn("environment", REQUIRED_COMMON_FIELDS)

    def test_unknown_field_rejected(self):
        with self.assertRaises(ContractError) as ctx:
            validate_field_name("user_question")
        self.assertEqual(ctx.exception.code, "UNKNOWN_FIELD")

    def test_forbidden_field_rejected(self):
        for name in ("password", "api_key", "email", "token", "prompt", "url"):
            with self.assertRaises(ContractError) as ctx:
                validate_field_name(name)
            self.assertEqual(ctx.exception.code, "FORBIDDEN_FIELD")

    def test_allowed_fields_do_not_overlap_forbidden(self):
        overlap = ALLOWED_FIELDS & FORBIDDEN_FIELD_NAMES
        self.assertEqual(overlap, frozenset())

    def test_sanitize_rejects_unknown(self):
        with self.assertRaises(ContractError):
            sanitize_record({"schema_version": 1, "secret_payload": "x"})


class TestErrorCodes(unittest.TestCase):
    def test_known_codes(self):
        for code in (
            "OCR_PAGE_TIMEOUT",
            "MODEL_TIMEOUT",
            "CHECKPOINT_SAVE_FAILED",
            "STALE_RESULT_DISCARDED",
            "UPLOAD_TYPE_REJECTED",
            "CLEANUP_OVERDUE",
        ):
            spec = validate_error_code(code)
            self.assertIsInstance(spec.stage, TaskStage)

    def test_unknown_code_rejected(self):
        with self.assertRaises(ContractError):
            validate_error_code("NOT_A_REAL_CODE")

    def test_all_codes_have_stage_and_alert(self):
        for code, spec in ERROR_CODES.items():
            self.assertTrue(code)
            self.assertIsInstance(spec.stage, TaskStage)
            self.assertTrue(spec.description)

    def test_vendor_error_mapping(self):
        self.assertEqual(map_vendor_error("Timeout while waiting"), "MODEL_TIMEOUT")
        self.assertEqual(map_vendor_error("invalid_api_key provided"), "MODEL_NOT_CONFIGURED")
        self.assertEqual(map_vendor_error("something else"), "INTERNAL_ERROR")


class TestEventRegistry(unittest.TestCase):
    def test_registered_events(self):
        for name in (
            "task.accepted",
            "task.stage.failed",
            "task.stale_discarded",
            "auth.login.failed",
            "admin.approval.changed",
            "access.denied",
            "resource.delete.requested",
            "cleanup.completed",
            "observability.export.failed",
            "audit.query.executed",
        ):
            spec = validate_event_name(name)
            self.assertTrue(spec.required_fields)

    def test_unknown_event_rejected(self):
        with self.assertRaises(ContractError):
            validate_event_name("made.up.event")

    def test_events_required_subset_of_allowed(self):
        for name, spec in EVENT_REGISTRY.items():
            for f in spec.required_fields | spec.optional_fields:
                self.assertIn(f, ALLOWED_FIELDS, f"{name}.{f} 不在允许名单")


class TestSanitize(unittest.TestCase):
    def test_control_chars_removed(self):
        self.assertEqual(strip_control_chars("a\x00b\nc\r\nd"), "ab c  d")

    def test_log_injection_newline_removed(self):
        out = sanitize_string('evil\n{"level":"INFO"}', "message")
        self.assertNotIn("\n", out)

    def test_email_redacted(self):
        out = redact_sensitive_text("contact me at user@example.com please")
        self.assertNotIn("user@example.com", out)
        self.assertIn("[REDACTED]", out)

    def test_api_key_redacted(self):
        out = redact_sensitive_text("key sk-abc1234567890xyz used")
        self.assertNotIn("sk-abc1234567890xyz", out)

    def test_url_with_query_redacted(self):
        out = redact_sensitive_text("see https://api.example.com/v1?q=secret&page=2")
        self.assertNotIn("q=secret", out)

    def test_length_limited(self):
        out = sanitize_string("x" * 500, "message")
        self.assertLessEqual(len(out), 200)

    def test_duration_ms_non_negative(self):
        with self.assertRaises(ContractError):
            sanitize_record({"duration_ms": -1})
        rec = sanitize_record({"duration_ms": 0})
        self.assertEqual(rec["duration_ms"], 0)

    def test_dumps_line_is_single_json(self):
        line = dumps_line(
            {
                "schema_version": 1,
                "timestamp": "2026-09-26T00:00:00.000Z",
                "level": "ERROR",
                "event": "task.stage.failed",
                "service": "worker",
                "environment": "test",
                "task_id": "task_7f3a",
                "attempt": 2,
                "stage": "ocr",
                "status": "failed",
                "error_code": "OCR_PAGE_TIMEOUT",
                "duration_ms": 180003,
            }
        )
        self.assertNotIn("\n", line)
        obj = json.loads(line)
        self.assertEqual(obj["error_code"], "OCR_PAGE_TIMEOUT")
        self.assertEqual(obj["schema_version"], 1)


class TestSamplesNoLeak(unittest.TestCase):
    """用合成数据生成上传失败和审批失败样例，确认不含正文/密钥/邮箱/完整 URL。"""

    def test_upload_failure_sample(self):
        sample = {
            "schema_version": 1,
            "timestamp": "2026-09-26T08:12:30.000Z",
            "level": "ERROR",
            "event": "task.stage.failed",
            "service": "worker",
            "environment": "test",
            "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
            "span_id": "00f067aa0ba902b7",
            "task_id": "task_7f3a",
            "attempt": 2,
            "actor_id": "user_4c21",
            "resource_type": "material",
            "resource_id": "material_81b0",
            "stage": "upload",
            "status": "failed",
            "error_code": "UPLOAD_TYPE_REJECTED",
            "duration_ms": 12,
            "reason": "mime_mismatch",
        }
        line = dumps_line(sample)
        for banned in ("password", "api_key", "@example.com", "http://", "prompt"):
            self.assertNotIn(banned, line.lower() if banned != "@example.com" else line)

    def test_approval_failure_sample(self):
        sample = {
            "schema_version": 1,
            "timestamp": "2026-09-26T08:13:00.000Z",
            "level": "WARN",
            "event": "admin.approval.changed",
            "service": "api",
            "environment": "test",
            "trace_id": "a" * 32,
            "admin_id": "admin_0001",
            "target_actor_id": "user_9aa1",
            "object_type": "user",
            "object_id": "user_9aa1",
            "result": "failed",
            "reason": "already_disabled",
        }
        line = dumps_line(sample)
        obj = json.loads(line)
        self.assertEqual(obj["event"], "admin.approval.changed")
        self.assertNotIn("email", obj)
        self.assertNotIn("password", obj)


class TestRegistryDocument(unittest.TestCase):
    def test_build_registry_document(self):
        doc = build_registry_document()
        self.assertEqual(doc["schema_version"], 1)
        self.assertIn("task.accepted", doc["events"])
        self.assertIn("OCR_PAGE_TIMEOUT", doc["error_codes"])
        self.assertIn("upload", doc["stages"])


if __name__ == "__main__":
    unittest.main()
