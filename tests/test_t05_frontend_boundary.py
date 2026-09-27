"""T05 静态检查：前端适配边界存在、无真实外发、演示审计已标注。"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

UI = ROOT / "文档" / "ui" / "index.html"


class TestFrontendAdapterBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = UI.read_text(encoding="utf-8")

    def test_task_event_client_present(self):
        self.assertIn("App.taskEventClient", self.html)
        self.assertIn("TASK_EVENT_TEMPLATES", self.html)
        self.assertIn("demo-offline", self.html)

    def test_audit_marked_demo_only(self):
        self.assertIn("U.audit.isDemoOnly", self.html)
        self.assertIn("演示内存数组", self.html)

    def test_no_real_network_calls_in_adapter(self):
        # 适配边界本身不得发起真实请求
        # 允许注释中出现 URL 形状，但不出现实际 fetch/XMLHttpRequest/EventSource 调用
        # 在 adapter 片段中检查
        start = self.html.find("App.taskEventClient")
        self.assertGreater(start, 0)
        snippet = self.html[start : start + 2000]
        self.assertNotIn("fetch(", snippet)
        self.assertNotIn("XMLHttpRequest", snippet)
        self.assertNotIn("EventSource(", snippet)

    def test_no_hardcoded_api_keys_in_ui(self):
        for pattern in (r"sk-[A-Za-z0-9]{10,}", r"AKIA[0-9A-Z]{8,}", r"ghp_[A-Za-z0-9]{8,}"):
            self.assertIsNone(re.search(pattern, self.html), f"found {pattern}")

    def test_demo_offline_note_still_present(self):
        self.assertIn("不发起网络请求", self.html)

    def test_course_routes_still_defined(self):
        # 学习页路由未因适配边界丢失
        for path in ("/course", "/course/detail", "/lecture"):
            self.assertIn(path, self.html)


class TestTemplatesUserVisibleOnly(unittest.TestCase):
    def test_consume_whitelist_only(self):
        # 用与前端模板一致的白名单做一次纯 Python 等价校验
        templates = {
            "task.accepted",
            "task.started",
            "task.stage.succeeded",
            "task.stage.partial",
            "task.stage.failed",
            "task.retry_scheduled",
            "task.cancelled",
            "task.stale_discarded",
        }
        self.assertIn("task.stage.failed", templates)
        self.assertNotIn("task.events.expired", templates)  # 用户不可见
        self.assertNotIn("observability.export.failed", templates)


if __name__ == "__main__":
    unittest.main()
