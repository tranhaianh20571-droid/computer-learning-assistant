"""切片 1：MiniMax TTS 与管理员 OCR/搜索适配器（T04，AC-04）。"""

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

os.environ.setdefault(
    "LEARNING_DATABASE_URL",
    "postgresql+psycopg://learning_assistant:learning_dev_pw@127.0.0.1:5432/learning_assistant",
)

from backend.logging_audit.db import Database  # noqa: E402
from backend.slice1.adapters.minimax import MiniMaxTTSAdapter  # noqa: E402
from backend.slice1.adapters.paddleocr import PaddleOCRAdapter  # noqa: E402
from backend.slice1.adapters.tavily_hikari import TavilyHikariAdapter  # noqa: E402
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


class TestTavilyHikariAdapter:
    def test_search_normalizes_results(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"title": "T", "url": "https://example.test/a", "content": "snippet"},
                        {"title": "T2", "url": "https://example.test/b", "content": "snippet2"},
                    ]
                },
            )

        adapter = TavilyHikariAdapter(
            endpoint="http://127.0.0.1:8787",
            credentials={"api_key": FAKE_KEY},
            client=_client(handler),
        )
        result = adapter.search("test query")
        assert result["result_count"] == 2
        assert result["results"][0]["source"] == "external_supplement"
        assert result["results"][0]["fetched_at"].endswith("Z")
        assert result["query_id"].startswith("qry_")

    def test_timeout_is_20_seconds(self):
        adapter = TavilyHikariAdapter(endpoint="http://127.0.0.1:8787")
        assert adapter.timeout == 20.0

    def test_failure_maps_to_controlled_error(self):
        from backend.slice1.adapters.base import AdapterError

        adapter = TavilyHikariAdapter(
            endpoint="http://127.0.0.1:8787",
            credentials={"api_key": FAKE_KEY},
            client=_client(lambda r: httpx.Response(503, json={"error": "down"})),
        )
        with pytest.raises(AdapterError) as exc:
            adapter.search("q")
        assert exc.value.code == "capability_test_failed"


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
            kind="search",
            protocol="tavily_hikari",
            endpoint="https://search.example.test",
            model_name="",
            credentials={"api_key": FAKE_KEY},
            owner_scope="admin",
        )
        user_list = svc.list(actor_id=owner, is_admin=False)
        assert all(c["kind"] == "content_model" for c in user_list)
