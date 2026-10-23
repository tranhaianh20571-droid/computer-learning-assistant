"""切片 1：MiniMax TTS 与管理员 OCR 与个人搜索适配器（T04，AC-04）。"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

from backend.logging_audit.db import get_database_url  # noqa: E402

os.environ.setdefault("LEARNING_DATABASE_URL", get_database_url())

from backend.logging_audit.db import Database  # noqa: E402
from backend.slice1.adapters.minimax import MiniMaxTTSAdapter  # noqa: E402
from backend.slice1.adapters.paddleocr import PaddleOCRAdapter  # noqa: E402
from backend.slice1.adapters import build_adapter  # noqa: E402
from backend.slice1.adapters.anysearch import AnySearchAdapter  # noqa: E402
from backend.slice1.config_service import ConfigService  # noqa: E402
from backend.slice1.errors import Slice1Error  # noqa: E402

FAKE_KEY = "sk-test-fake-key-000000000000"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5)


class TestMiniMaxTTS:
    def test_build_request_fixes_subtitle_and_model(self):
        adapter = MiniMaxTTSAdapter(
            endpoint="https://api.minimax.example.test",
            model_name="speech-2.8-hd",
            credentials={"api_key": FAKE_KEY},
            voice_id="voice_a",
        )
        body = adapter.build_request("你好")
        assert body["model"] == "speech-2.8-hd"
        assert body["voice_setting"]["voice_id"] == "voice_a"
        assert body["subtitle_enable"] is True
        assert body["subtitle_type"] == "sentence"
        assert body["stream"] is False
        assert body["audio_setting"]["format"] == "mp3"

    def test_synthesize_returns_hex_and_hash(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"audio": "deadbeef"}, "trace_id": "vt-1"})

        adapter = MiniMaxTTSAdapter(
            endpoint="https://api.minimax.example.test",
            credentials={"api_key": FAKE_KEY},
            voice_id="voice_a",
            client=_client(handler),
        )
        result = adapter.synthesize("测试文本")
        assert result["audio_hex"] == "deadbeef"
        assert result["subtitle_enable"] is True
        assert result["vendor_trace_id"] == "vt-1"
        assert len(result["text_sha256"]) == 64
        assert "测试文本" not in json.dumps(result, ensure_ascii=False)

    def test_multiple_voices_supported(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return httpx.Response(200, json={"data": {"audio": "aa", "voice_id": body["voice_setting"]["voice_id"]}})

        adapter = MiniMaxTTSAdapter(
            endpoint="https://api.minimax.example.test",
            credentials={"api_key": FAKE_KEY},
            voice_id="voice_a",
            client=_client(handler),
        )
        for voice in ("voice_a", "voice_b", "voice_c"):
            result = adapter.synthesize("hi", voice_id=voice)
            assert result["voice_id"] == voice

    def test_custom_endpoint_region(self):
        adapter = MiniMaxTTSAdapter(
            endpoint="https://api.minimax.io",
            credentials={"api_key": FAKE_KEY},
            voice_id="v",
        )
        assert adapter._tts_url() == "https://api.minimax.io/v1/t2a_v2"


class TestPaddleOCRAdapter:
    def test_submit_query_fetch_contract(self):
        calls: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, str(request.url)))
            if request.method == "POST":
                return httpx.Response(200, json={"job_id": "job_123"})
            if str(request.url).endswith("/result"):
                return httpx.Response(200, json={"pages": [{"page_no": 1, "regions": []}]})
            return httpx.Response(200, json={"status": "running"})

        adapter = PaddleOCRAdapter(
            endpoint="https://ocr.example.test/api",
            credentials={"api_key": FAKE_KEY},
            client=_client(handler),
        )
        submitted = adapter.submit(material_id="mat_1", page_no=1)
        assert submitted["external_request_id"] == "job_123"
        assert adapter.query("job_123")["status"] == "running"
        result = adapter.fetch_result("job_123")
        assert "pages" in result["result"]

    def test_probe_uses_synthetic_material(self):
        bodies: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"job_id": "job_x"})

        adapter = PaddleOCRAdapter(
            endpoint="https://ocr.example.test/api",
            credentials={"api_key": FAKE_KEY},
            client=_client(handler),
        )
        adapter.probe_all()
        assert bodies and bodies[0]["material_id"] == "synthetic_probe"


class TestPersonalSearchProviderRegistration:
    def test_legacy_search_provider_is_retired(self):
        with pytest.raises(Slice1Error) as failure:
            build_adapter("tavily_hikari", endpoint="http://127.0.0.1:8787", credentials={"api_key": FAKE_KEY})
        assert failure.value.code == "protocol_mismatch"

    def test_anysearch_timeout_is_20_seconds(self):
        adapter = AnySearchAdapter(credentials={"api_key": FAKE_KEY})
        try:
            assert adapter.timeout == 20.0
        finally:
            adapter.close()

    def test_search_cannot_be_an_administrator_configuration(self):
        db = Database()
        db.create_all()
        svc = ConfigService(db, encryption_key="k")
        with pytest.raises(Slice1Error) as failure:
            svc.create(actor_id="admin_1", is_admin=True, kind="search", protocol="anysearch",
                endpoint="https://api.anysearch.com", model_name="", credentials={"api_key": FAKE_KEY},
                owner_scope="admin")
        assert failure.value.status == 422


class TestAdminCloudConfigPermissions:
    def test_user_cannot_read_or_modify_admin_config(self):
        db = Database()
        db.create_all()
        svc = ConfigService(db, encryption_key="k")
        admin_cfg = svc.create(
            actor_id="admin_1",
            is_admin=True,
            kind="ocr",
            protocol="paddleocr",
            endpoint="https://ocr.example.test/api",
            model_name="PaddleOCR-VL-1.6",
            credentials={"api_key": FAKE_KEY},
            owner_scope="admin",
        )
        with pytest.raises(Slice1Error):
            svc.update(admin_cfg["config_id"], actor_id="user_1", is_admin=False, endpoint="https://evil.test")
        with pytest.raises(Slice1Error):
            svc.deactivate(admin_cfg["config_id"], actor_id="user_1", is_admin=False)
        assert svc.get(admin_cfg["config_id"], actor_id="admin_1", is_admin=True)["kind"] == "ocr"

    def test_user_configs_do_not_include_admin(self):
        db = Database()
        db.create_all()
        owner = f"user_{uuid.uuid4().hex[:8]}"
        svc = ConfigService(db, encryption_key="k")
        svc.create(
            actor_id=owner,
            is_admin=False,
            kind="content_model",
            protocol="openai",
            endpoint="https://api.example.test/v1",
            model_name="m",
            credentials={"api_key": FAKE_KEY},
        )
        svc.create(
            actor_id="admin_1",
            is_admin=True,
            kind="ocr",
            protocol="paddleocr",
            endpoint="https://ocr.example.test",
            model_name="",
            credentials={"api_key": FAKE_KEY},
            owner_scope="admin",
        )
        user_list = svc.list(actor_id=owner, is_admin=False)
        assert all(c["kind"] == "content_model" for c in user_list)
