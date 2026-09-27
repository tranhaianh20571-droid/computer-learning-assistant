"""切片 1 前端静态回归：能力配置页面存在、只显示掩码、无真实密钥、无控制台泄漏。"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

SRC = ROOT / "frontend" / "src"


class TestCapabilityConfigPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (SRC / "App.tsx").read_text(encoding="utf-8")
        cls.page = (SRC / "CapabilityConfig.tsx").read_text(encoding="utf-8")
        cls.api = (SRC / "api.ts").read_text(encoding="utf-8")

    def test_route_and_nav_present(self):
        self.assertIn('"/capabilities"', self.app)
        self.assertIn("CapabilityConfig", self.app)

    def test_capability_states_rendered(self):
        for cap in ("text", "image", "tool_call", "json_schema", "streaming", "cancel"):
            self.assertIn(cap, self.page)

    def test_only_mask_displayed(self):
        self.assertIn("credential_mask", self.page)
        self.assertNotIn("encrypted_credentials", self.page)
        self.assertNotIn("api_key", self.page.split("credentials: { api_key")[0])

    def test_admin_section_gated(self):
        self.assertIn("isAdmin && (", self.page)
        self.assertIn("管理员云能力", self.page)

    def test_connector_pairing_and_revoke(self):
        self.assertIn("createPairing", self.page)
        self.assertIn("revokeConnector", self.page)
        self.assertIn("revoked", self.page)

    def test_disclosure_records_queryable(self):
        self.assertIn("listDisclosures", self.page)
        self.assertIn("revokeDisclosure", self.page)

    def test_no_hardcoded_keys_or_console_dumps(self):
        blob = self.app + self.page + self.api
        for pattern in (r"sk-[A-Za-z0-9]{12,}", r"AKIA[0-9A-Z]{8,}", r"ghp_[A-Za-z0-9]{8,}"):
            self.assertIsNone(re.search(pattern, blob), f"found {pattern}")
        self.assertNotIn("console.log", blob)
        self.assertNotIn("console.error", blob)

    def test_api_endpoints_declared(self):
        for path in (
            "/api/capabilities/configs",
            "/api/disclosures",
            "/api/connectors/pairing",
            "/api/connectors",
        ):
            self.assertIn(path, self.api)


if __name__ == "__main__":
    unittest.main()
