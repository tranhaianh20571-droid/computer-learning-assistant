"""T03 单元/集成测试：任务事件、序号、SSE 补取、所有权、迟到 worker。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

from backend.logging_audit.task_events import (  # noqa: E402
    AccessDeniedError,
    EventsExpiredError,
    HTTP_EVENTS_EXPIRED,
    TaskEventStore,
)
from backend.logging_audit.contracts import ContractError  # noqa: E402


class TestTaskEventStore(unittest.TestCase):
    def setUp(self):
        self.store = TaskEventStore(retention_seconds=3600)

    def test_create_and_sequence_monotonic(self):
        self.store.create_task("task_1", "user_a", "subject_1", "upload")
        self.store.record("task_1", stage="parse", status="succeeded", duration_ms=10)
        self.store.record("task_1", stage="ocr", status="started")
        events = self.store.get_events("task_1", actor_id="user_a")
        seqs = [e.sequence for e in events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))
        self.assertGreaterEqual(len(seqs), 3)

    def test_task_id_sequence_unique(self):
        self.store.create_task("task_2", "user_a", "s1", "upload")
        self.store.record("task_2", stage="parse", status="failed", error_code="PDF_ENCRYPTED")
        events = self.store.get_events("task_2")
        keys = [(e.task_id, e.sequence) for e in events]
        self.assertEqual(len(keys), len(set(keys)))

    def test_ownership_denied(self):
        self.store.create_task("task_3", "user_a", "s1", "upload")
        with self.assertRaises(AccessDeniedError):
            self.store.get_events("task_3", actor_id="user_b")
        with self.assertRaises(AccessDeniedError):
            self.store.get_state("task_3", actor_id="user_b")
        with self.assertRaises(AccessDeniedError):
            self.store.replay_sse("task_3", actor_id="user_b")

    def test_sse_replay_gap(self):
        self.store.create_task("task_4", "user_a", "s1", "upload")
        self.store.record("task_4", stage="parse", status="succeeded", duration_ms=1)
        self.store.record("task_4", stage="ocr", status="succeeded", duration_ms=2)
        events, next_seq = self.store.replay_sse(
            "task_4", actor_id="user_a", last_sequence=1
        )
        self.assertEqual([e.sequence for e in events], [2, 3])
        self.assertEqual(next_seq, 3)

    def test_sse_first_connect_full(self):
        self.store.create_task("task_5", "user_a", "s1", "upload")
        events, next_seq = self.store.replay_sse("task_5", actor_id="user_a")
        self.assertEqual(len(events), 1)
        self.assertEqual(next_seq, 1)

    def test_sse_expired_cursor(self):
        self.store.create_task("task_6", "user_a", "s1", "upload")
        with self.assertRaises(EventsExpiredError) as ctx:
            self.store.replay_sse("task_6", actor_id="user_a", last_sequence=99)
        self.assertEqual(ctx.exception.http_status, HTTP_EVENTS_EXPIRED)
        self.assertEqual(ctx.exception.error_code, "EVENTS_EXPIRED")

    def test_sse_last_event_id(self):
        self.store.create_task("task_7", "user_a", "s1", "upload")
        e1 = self.store.get_events("task_7")[0]
        self.store.record("task_7", stage="parse", status="succeeded", duration_ms=1)
        events, _ = self.store.replay_sse(
            "task_7", actor_id="user_a", last_event_id=e1.event_id
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].sequence, 2)

    def test_sse_unknown_event_id_expired(self):
        self.store.create_task("task_8", "user_a", "s1", "upload")
        with self.assertRaises(EventsExpiredError):
            self.store.replay_sse("task_8", actor_id="user_a", last_event_id="evt_missing")

    def test_retention_expired(self):
        self.store.create_task("task_9", "user_a", "s1", "upload")
        # 把时钟拨到保留期之后
        self.store.set_clock(lambda: 10**12)
        with self.assertRaises(EventsExpiredError) as ctx:
            self.store.replay_sse("task_9", actor_id="user_a", last_sequence=0)
        self.assertEqual(ctx.exception.reason, "retention_expired")

    def test_stale_generation_cannot_overwrite(self):
        self.store.create_task("task_10", "user_a", "s1", "upload", generation=2)
        # 迟到 worker（generation=1）只能写诊断
        event = self.store.record(
            "task_10",
            stage="ocr",
            status="succeeded",
            duration_ms=10,
            generation=1,
            diagnostics_only=False,
        )
        state = self.store.get_state("task_10")
        self.assertEqual(state.status, "started")  # 未被覆盖
        self.assertEqual(state.generation, 2)
        # 事件被标为 stale
        last = self.store.get_events("task_10")[-1]
        self.assertEqual(last.status, "stale_discarded")
        self.assertEqual(last.error_code, "STALE_RESULT_DISCARDED")

    def test_diagnostics_only_no_state_change(self):
        self.store.create_task("task_11", "user_a", "s1", "upload")
        self.store.record(
            "task_11",
            stage="save",
            status="stale_discarded",
            error_code="STALE_RESULT_DISCARDED",
            generation=1,
            diagnostics_only=True,
        )
        state = self.store.get_state("task_11")
        self.assertEqual(state.status, "started")

    def test_full_status_revision(self):
        self.store.create_task("task_12", "user_a", "s1", "upload")
        self.store.record("task_12", stage="parse", status="succeeded", duration_ms=1)
        status = self.store.full_status("task_12", actor_id="user_a")
        self.assertEqual(status["status"], "succeeded")
        self.assertGreaterEqual(status["revision"], 1)
        self.assertEqual(status["last_sequence"], 2)

    def test_reconnect_after_disconnect_sequential(self):
        """断开并重连后序号连续。"""
        self.store.create_task("task_13", "user_a", "s1", "upload")
        self.store.record("task_13", stage="parse", status="succeeded", duration_ms=1)
        self.store.record("task_13", stage="ocr", status="succeeded", duration_ms=2)
        self.store.record("task_13", stage="index", status="succeeded", duration_ms=3)
        # 第一次连到 seq=2 后断开（create=1, 三次 record=2/3/4）
        batch1, cursor = self.store.replay_sse("task_13", actor_id="user_a", last_sequence=2)
        self.assertEqual([e.sequence for e in batch1], [3, 4])
        # 重连用 last_sequence=cursor 继续
        batch2, cursor2 = self.store.replay_sse(
            "task_13", actor_id="user_a", last_sequence=cursor
        )
        self.assertEqual(batch2, [])
        self.assertEqual(cursor2, cursor)

    def test_duplicate_task_rejected(self):
        self.store.create_task("task_14", "user_a", "s1", "upload")
        with self.assertRaises(ContractError):
            self.store.create_task("task_14", "user_a", "s1", "upload")

    def test_retry_keeps_task_id_increments_attempt(self):
        self.store.create_task("task_15", "user_a", "s1", "ocr", attempt=1)
        self.store.record(
            "task_15", stage="ocr", status="failed", error_code="OCR_PAGE_TIMEOUT", attempt=1
        )
        self.store.record(
            "task_15", stage="ocr", status="retry_scheduled", error_code="OCR_PAGE_TIMEOUT", attempt=2
        )
        state = self.store.get_state("task_15")
        self.assertEqual(state.task_id, "task_15")
        self.assertEqual(state.attempt, 2)


if __name__ == "__main__":
    unittest.main()
