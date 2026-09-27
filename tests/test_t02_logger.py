"""T02 单元测试：结构化日志、traceparent、导出降级。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

from backend.logging_audit.logger import MemoryLogStream, StructuredLogger  # noqa: E402
from backend.logging_audit.trace import (  # noqa: E402
    TraceContext,
    ensure_trace,
    parse_traceparent,
    trace_context,
)
from backend.logging_audit.exporter import ObservabilityExporter  # noqa: E402
from backend.logging_audit.contracts import ContractError  # noqa: E402


class TestTraceparent(unittest.TestCase):
    def test_valid_traceparent(self):
        header = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        ctx = parse_traceparent(header)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertTrue(ctx.sampled)

    def test_invalid_traceparent_returns_none(self):
        self.assertIsNone(parse_traceparent("garbage"))
        self.assertIsNone(parse_traceparent(None))
        self.assertIsNone(parse_traceparent("00-" + "0" * 32 + "-" + "0" * 16 + "-01"))
        self.assertIsNone(parse_traceparent("ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"))

    def test_ensure_trace_generates_when_missing(self):
        ctx = ensure_trace(None)
        self.assertEqual(len(ctx.trace_id), 32)
        self.assertEqual(len(ctx.span_id), 16)
        self.assertNotEqual(ctx.trace_id, "0" * 32)

    def test_ensure_trace_reuses_parent_trace(self):
        header = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        ctx = ensure_trace(header)
        self.assertEqual(ctx.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertNotEqual(ctx.span_id, "00f067aa0ba902b7")

    def test_child_span_keeps_trace(self):
        parent = TraceContext(trace_id="a" * 32, span_id="b" * 16)
        with trace_context(parent):
            child = ensure_trace(parent.as_traceparent())
            self.assertEqual(child.trace_id, parent.trace_id)


class TestStructuredLogger(unittest.TestCase):
    def setUp(self):
        self.stream = MemoryLogStream()
        self.logger = StructuredLogger("worker", "test", stream=self.stream)
        self.logger.set_clock(lambda: "2026-09-26T08:12:30.000Z")

    def test_required_fields_present(self):
        self.logger.log(
            "task.stage.succeeded",
            level="INFO",
            task_id="task_7f3a",
            stage="ocr",
            status="succeeded",
            duration_ms=100,
        )
        rec = self.stream.records()[0]
        for key in (
            "schema_version",
            "timestamp",
            "level",
            "event",
            "service",
            "environment",
            "task_id",
            "stage",
            "status",
            "duration_ms",
        ):
            self.assertIn(key, rec)
        self.assertEqual(rec["timestamp"], "2026-09-26T08:12:30.000Z")

    def test_trace_injected(self):
        ctx = TraceContext(trace_id="c" * 32, span_id="d" * 16)
        with trace_context(ctx):
            self.logger.log("task.accepted", task_id="task_1", attempt=1, stage="upload")
        rec = self.stream.records()[0]
        self.assertEqual(rec["trace_id"], "c" * 32)
        self.assertEqual(rec["span_id"], "d" * 16)

    def test_missing_required_rejected(self):
        with self.assertRaises(ContractError):
            self.logger.log("task.stage.failed", level="ERROR", task_id="t1")

    def test_unknown_event_rejected(self):
        with self.assertRaises(ContractError):
            self.logger.log("not.an.event")

    def test_exception_mapped_not_raw(self):
        self.logger.log_exception(
            "task.stage.failed",
            RuntimeError("Timeout while calling vendor"),
            task_id="task_x",
            stage="generate",
            status="failed",
            duration_ms=50,
        )
        rec = self.stream.records()[0]
        self.assertEqual(rec["error_code"], "MODEL_TIMEOUT")
        self.assertEqual(rec["reason"], "RuntimeError")
        self.assertNotIn("vendor", json.dumps(rec))

    def test_no_sensitive_in_output(self):
        self.logger.log(
            "task.stage.failed",
            level="ERROR",
            task_id="task_1",
            stage="ocr",
            status="failed",
            error_code="OCR_PAGE_TIMEOUT",
            duration_ms=10,
            reason="mail user@fake.test leaked in vendor error",
        )
        text = self.stream.getvalue()
        self.assertNotIn("user@fake.test", text)


class TestExporterDegradation(unittest.TestCase):
    def test_export_failure_degrades(self):
        stream = MemoryLogStream()
        logger = StructuredLogger("api", "test", stream=stream)

        def bad_sink(item):
            raise RuntimeError("otel down")

        exp = ObservabilityExporter("otel", logger=logger, sink=bad_sink, failure_threshold=2)
        ok1 = exp.enqueue({"span": 1})
        ok2 = exp.enqueue({"span": 2})
        self.assertFalse(ok1)
        self.assertFalse(ok2)
        self.assertTrue(exp.stats.circuit_open)
        self.assertGreaterEqual(exp.stats.dropped, 2)
        # 业务不抛异常，指标可见
        self.assertGreaterEqual(exp.stats.failures, 2)

    def test_queue_full_drops(self):
        exp = ObservabilityExporter("otel", max_queue=1, sink=lambda i: None)
        self.assertTrue(exp.enqueue({"a": 1}))
        # 队列已满且 sink 成功后队列被保留… drain 后再测
        exp.drain()
        # 填满
        def slow_ok(i):
            pass

        exp2 = ObservabilityExporter("otel", max_queue=1, sink=lambda i: None)
        exp2.enqueue({"a": 1})
        # 直接制造队列满：sink 成功时事件仍入队后已处理；改为失败阈值高时看 dropped
        # 使用一个不消费的 sink
        held = []

        def hold(i):
            held.append(i)

        exp3 = ObservabilityExporter("otel", max_queue=1, sink=hold)
        # sink 成功会 exported++，队列仍在内部；再入队应 queue_full
        exp3.enqueue({"a": 1})
        # 由于 sink 成功后事件仍在 _queue（本实现保留直到 drain），第二次应满
        result = exp3.enqueue({"a": 2})
        self.assertFalse(result)
        self.assertGreaterEqual(exp3.stats.dropped, 1)

    def test_core_business_not_blocked(self):
        """导出失败不影响业务返回。"""
        results = []

        def business(exp: ObservabilityExporter):
            exp.enqueue({"trace": "x"})
            return "ok"

        exp = ObservabilityExporter("otel", sink=lambda i: (_ for _ in ()).throw(IOError("down")))
        self.assertEqual(business(exp), "ok")
        self.assertEqual(business(exp), "ok")


if __name__ == "__main__":
    unittest.main()
