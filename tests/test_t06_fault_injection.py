"""T06 故障注入：worker 迟到、观测降级、审计写入失败、跨账户、敏感字段泄漏扫描。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

from backend.logging_audit.audit import AuditLog, AuditWriteError  # noqa: E402
from backend.logging_audit.exporter import ObservabilityExporter  # noqa: E402
from backend.logging_audit.logger import MemoryLogStream, StructuredLogger  # noqa: E402
from backend.logging_audit.task_events import (  # noqa: E402
    AccessDeniedError,
    EventsExpiredError,
    TaskEventStore,
)
from backend.logging_audit.trace import TraceContext, trace_context  # noqa: E402
from backend.logging_audit.sanitize import dumps_line  # noqa: E402

# 合成泄漏样本（禁止真实密钥/邮箱）
FAKE_API_KEY = "sk-test-fake-key-000000000000"
FAKE_EMAIL = "synthetic.user@fake.test"
FAKE_URL = "https://vendor.example.test/v1/ocr?api_key=REDACT&file=secret.pdf"
FAKE_TEXT = "这是教材正文段落，不得出现在日志中。"


class TestWorkerStaleResult(unittest.TestCase):
    """AC-08：worker 租约过期、旧 generation 迟到。"""

    def test_old_worker_cannot_overwrite(self):
        store = TaskEventStore()
        store.create_task("task_stale", "user_a", "sub_1", "ocr", generation=3, attempt=2)
        # 当前 generation=3 正常推进
        store.record("task_stale", stage="ocr", status="succeeded", duration_ms=50, generation=3)

        # 旧 worker（generation=1）迟到返回
        event = store.record(
            "task_stale",
            stage="ocr",
            status="succeeded",
            duration_ms=999,
            generation=1,
        )
        state = store.get_state("task_stale")
        self.assertEqual(state.generation, 3)
        self.assertEqual(state.status, "succeeded")
        rev_before_stale = state.revision
        last = store.get_events("task_stale")[-1]
        self.assertEqual(last.status, "stale_discarded")
        self.assertEqual(last.error_code, "STALE_RESULT_DISCARDED")
        # 诊断写入不推进业务 revision
        self.assertEqual(store.get_state("task_stale").revision, rev_before_stale)

    def test_stale_only_diagnostics(self):
        store = TaskEventStore()
        store.create_task("task_stale2", "user_a", "sub_1", "save", generation=2)
        rev_before = store.get_state("task_stale2").revision
        store.record(
            "task_stale2",
            stage="save",
            status="stale_discarded",
            error_code="STALE_RESULT_DISCARDED",
            generation=1,
            diagnostics_only=True,
        )
        state = store.get_state("task_stale2")
        self.assertEqual(state.revision, rev_before)
        self.assertEqual(state.status, "started")


class TestObservabilityDegradation(unittest.TestCase):
    """AC-09：Langfuse/OTel 不可用时核心任务继续。"""

    def test_exporter_down_business_continues(self):
        stream = MemoryLogStream()
        logger = StructuredLogger("worker", "test", stream=stream)
        store = TaskEventStore()

        def dead_sink(item):
            raise ConnectionError("langfuse unreachable")

        exp = ObservabilityExporter(
            "langfuse", logger=logger, sink=dead_sink, failure_threshold=1
        )

        # 核心任务路径
        store.create_task("task_obs", "user_a", "s1", "generate")
        exp.enqueue({"trace": "t1"})
        result = store.record(
            "task_obs", stage="generate", status="succeeded", duration_ms=30
        )
        self.assertIsNotNone(result)
        self.assertEqual(store.get_state("task_obs").status, "succeeded")
        self.assertTrue(exp.stats.circuit_open or exp.stats.failures >= 1)
        # 本地指标可见
        self.assertGreaterEqual(exp.stats.dropped, 1)

    def test_degraded_log_emitted(self):
        stream = MemoryLogStream()
        logger = StructuredLogger("api", "test", stream=stream)
        exp = ObservabilityExporter(
            "otel", logger=logger, sink=lambda i: (_ for _ in ()).throw(IOError("x")),
            failure_threshold=1,
        )
        exp.enqueue({"a": 1})
        events = [r["event"] for r in stream.records()]
        self.assertIn("observability.export.failed", events)


class TestAuditWriteFailure(unittest.TestCase):
    """AC-09：审计写入失败触发告警/阻断策略，不静默丢失。"""

    def test_write_failure_visible(self):
        audit = AuditLog()
        audit.set_fail_write(True)
        with self.assertRaises(AuditWriteError):
            audit.append("access.denied", actor_id="u", result="denied", reason="x")
        self.assertEqual(audit.write_failures, 1)
        # 恢复后可继续写
        audit.set_fail_write(False)
        rec = audit.append("access.denied", actor_id="u", result="denied", reason="x")
        self.assertIsNotNone(rec)

    def test_no_silent_loss(self):
        audit = AuditLog(fail_write=True)
        errors = 0
        for _ in range(5):
            try:
                audit.append("auth.login.failed", actor_id="u", result="failed")
            except AuditWriteError:
                errors += 1
        self.assertEqual(errors, 5)
        self.assertEqual(audit.write_failures, 5)
        self.assertEqual(len(audit), 0)


class TestCrossAccountAccess(unittest.TestCase):
    """AC-04/AC-06：跨账户任务 ID 被拒绝。"""

    def test_cross_account_rejected(self):
        store = TaskEventStore()
        store.create_task("task_x", "owner_user", "s1", "upload")
        for fn in (
            lambda: store.get_state("task_x", actor_id="intruder"),
            lambda: store.get_events("task_x", actor_id="intruder"),
            lambda: store.replay_sse("task_x", actor_id="intruder"),
            lambda: store.full_status("task_x", actor_id="intruder"),
        ):
            with self.assertRaises(AccessDeniedError):
                fn()

    def test_sse_replay_requires_ownership(self):
        store = TaskEventStore()
        store.create_task("task_y", "owner_user", "s1", "upload")
        events, _ = store.replay_sse("task_y", actor_id="owner_user")
        self.assertEqual(len(events), 1)


class TestSensitiveLeakScan(unittest.TestCase):
    """AC-02：日志、任务事件、审计均不出现禁止字段。"""

    def test_logger_never_emits_secrets(self):
        stream = MemoryLogStream()
        logger = StructuredLogger("worker", "test", stream=stream)
        with trace_context(TraceContext(trace_id="a" * 32, span_id="b" * 16)):
            logger.log(
                "task.stage.failed",
                level="ERROR",
                task_id="task_leak",
                attempt=1,
                stage="ocr",
                status="failed",
                error_code="OCR_OUTPUT_INVALID",
                duration_ms=10,
                reason=f"vendor said {FAKE_API_KEY} for {FAKE_EMAIL}",
            )
        text = stream.getvalue()
        self.assertNotIn(FAKE_API_KEY, text)
        self.assertNotIn(FAKE_EMAIL, text)
        self.assertNotIn(FAKE_TEXT, text)

    def test_audit_record_json_clean(self):
        audit = AuditLog()
        rec = audit.append(
            "config.changed",
            actor_id="user_a",
            object_type="service_config",
            object_id="cfg_1",
            result="success",
            reason=f"rotated because {FAKE_API_KEY} leaked in {FAKE_URL}",
        )
        line = dumps_line(rec.to_record())
        self.assertNotIn(FAKE_API_KEY, line)
        self.assertNotIn("api_key=REDACT", line)  # URL with query must be redacted
        self.assertNotIn(FAKE_EMAIL, line)

    def test_task_event_template_params_safe(self):
        store = TaskEventStore()
        store.create_task(
            "task_tpl",
            "user_a",
            "s1",
            "generate",
            template_id="lesson_step_v1",
            template_params={"step_no": 1, "note": f"see {FAKE_EMAIL}"},
        )
        events = store.get_events("task_tpl")
        payload = json.dumps([e.to_record() for e in events], ensure_ascii=False)
        self.assertNotIn(FAKE_EMAIL, payload)
        self.assertNotIn(FAKE_API_KEY, payload)

    def test_url_with_query_not_in_logs(self):
        stream = MemoryLogStream()
        logger = StructuredLogger("api", "test", stream=stream)
        logger.log(
            "task.stage.failed",
            level="ERROR",
            task_id="t",
            stage="search",
            status="failed",
            error_code="SEARCH_TIMEOUT",
            duration_ms=5,
            reason=f"fetch {FAKE_URL} failed",
        )
        text = stream.getvalue()
        self.assertNotIn("api_key=REDACT", text)
        self.assertNotIn("secret.pdf", text)


class TestSSEExpiredAndResume(unittest.TestCase):
    """AC-04：SSE 断线、Last-Event-ID 补取、事件过期。"""

    def test_disconnect_resume_sequence_continuous(self):
        store = TaskEventStore()
        store.create_task("task_sse", "user_a", "s1", "upload")
        for stage in ("parse", "ocr", "index", "retrieve"):
            store.record("task_sse", stage=stage, status="succeeded", duration_ms=1)

        # 断线在 seq=2
        gap, cursor = store.replay_sse("task_sse", actor_id="user_a", last_sequence=2)
        self.assertEqual([e.sequence for e in gap], [3, 4, 5])
        # 再断再连
        gap2, cursor2 = store.replay_sse("task_sse", actor_id="user_a", last_sequence=cursor)
        self.assertEqual(gap2, [])

    def test_expired_returns_410(self):
        store = TaskEventStore()
        store.create_task("task_exp", "user_a", "s1", "upload")
        with self.assertRaises(EventsExpiredError) as ctx:
            store.replay_sse("task_exp", actor_id="user_a", last_sequence=999)
        self.assertEqual(ctx.exception.http_status, 410)


if __name__ == "__main__":
    unittest.main()
