"""切片 1：三类模型适配器与能力探测（T03，AC-02/AC-03）。

全部使用 httpx MockTransport，不调用真实供应商。
"""

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
from backend.slice1.adapters import CapabilityTestService, build_adapter  # noqa: E402
from backend.slice1.adapters.anthropic import AnthropicAdapter  # noqa: E402
from backend.slice1.adapters.base import AdapterError, validate_endpoint  # noqa: E402
from backend.slice1.adapters.gemini import GeminiAdapter  # noqa: E402
from backend.slice1.adapters.openai import OpenAIAdapter  # noqa: E402
from backend.slice1.config_service import ConfigService  # noqa: E402

FAKE_KEY = "sk-test-fake-key-000000000000"


def _record_handler(record: list, responses: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        record.append({"url": str(request.url), "body": body})
        payload = body
        if payload.get("stream"):
            return httpx.Response(200, text="data: {\"ok\": true}\n\n", headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=responses.get("body", {"ok": True}))

    return handler


def _openai(record=None, responses=None) -> OpenAIAdapter:
    transport = httpx.MockTransport(_record_handler(record if record is not None else [], responses or {}))
    client = httpx.Client(transport=transport, timeout=5)
    return OpenAIAdapter(
        endpoint="https://api.example.test/v1",
        model_name="gpt-test",
        credentials={"api_key": FAKE_KEY},
        client=client,
    )


def _anthropic(record=None) -> AnthropicAdapter:
    transport = httpx.MockTransport(_record_handler(record if record is not None else [], {}))
    client = httpx.Client(transport=transport, timeout=5)
    return AnthropicAdapter(
        endpoint="https://api.anthropic.test",
        model_name="claude-test",
        credentials={"api_key": FAKE_KEY},
        client=client,
    )


def _gemini(record=None) -> GeminiAdapter:
    transport = httpx.MockTransport(_record_handler(record if record is not None else [], {}))
    client = httpx.Client(transport=transport, timeout=5)
    return GeminiAdapter(
        endpoint="https://generativelanguage.example.test",
        model_name="gemini-test",
        credentials={"api_key": FAKE_KEY},
        client=client,
    )


class TestOpenAIAdapter:
    def test_probes_all_capabilities_available(self):
        record: list = []
        adapter = _openai(record)
        status = adapter.probe_all()
        assert set(status) == {"text", "image", "tool_call", "json_schema", "streaming", "cancel"}
        assert all(v["state"] == "available" for v in status.values())
        # 只发非私人样例
        joined = json.dumps(record, ensure_ascii=False)
        assert "你好，请回复 OK" in joined
        assert FAKE_KEY not in joined  # 凭据在 header 中，不应出现在 body

    def test_json_schema_unsupported_marked_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if "response_format" in body:
                return httpx.Response(400, json={"error": {"message": "json_schema is not supported"}})
            return httpx.Response(200, json={"ok": True})

        client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
        adapter = OpenAIAdapter(
            endpoint="https://api.example.test/v1",
            model_name="m",
            credentials={"api_key": FAKE_KEY},
            client=client,
        )
        status = adapter.probe_all()
        assert status["json_schema"]["state"] == "unavailable"
        assert status["text"]["state"] == "available"

    def test_auth_failure_maps_to_credential_invalid(self):
        client = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "bad key"})),
            timeout=5,
        )
        adapter = OpenAIAdapter(
            endpoint="https://api.example.test/v1",
            model_name="m",
            credentials={"api_key": FAKE_KEY},
            client=client,
        )
        status = adapter.probe_all()
        assert status["text"]["state"] == "unavailable"
        assert status["text"]["detail"] == "credential_invalid"

    def test_timeout_maps_to_controlled_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out")

        client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
        adapter = OpenAIAdapter(
            endpoint="https://api.example.test/v1",
            model_name="m",
            credentials={"api_key": FAKE_KEY},
            client=client,
        )
        status = adapter.probe_all()
        assert status["text"]["state"] == "unavailable"
        assert status["text"]["detail"] == "timeout"


class TestAnthropicAdapter:
    def test_probe_uses_native_message_format(self):
        record: list = []
        adapter = _anthropic(record)
        status = adapter.probe_all()
        assert all(v["state"] == "available" for v in status.values())
        urls = [r["url"] for r in record]
        assert all("/v1/messages" in u for u in urls)
        tool_probe = [r for r in record if "tools" in r["body"]]
        assert tool_probe and "input_schema" in json.dumps(tool_probe[0]["body"])


class TestGeminiAdapter:
    def test_probe_uses_generate_content(self):
        record: list = []
        adapter = _gemini(record)
        status = adapter.probe_all()
        assert all(v["state"] == "available" for v in status.values())
        assert any("generateContent" in r["url"] for r in record)
        assert any("streamGenerateContent" in r["url"] for r in record)
        # API key 走查询参数，不写入 body
        assert any("key=" in r["url"] for r in record)


class TestEndpointValidation:
    def test_https_required_for_public_model(self):
        with pytest.raises(AdapterError):
            validate_endpoint("http://api.example.test/v1", require_https=True)

    def test_metadata_address_rejected(self):
        with pytest.raises(AdapterError):
            validate_endpoint("http://169.254.169.254/latest/meta-data")

    def test_private_address_rejected_for_public(self):
        with pytest.raises(AdapterError):
            validate_endpoint("https://127.0.0.1:8080/v1")

    def test_private_allowed_for_local_tts(self):
        adapter = build_adapter("minimax", endpoint="http://127.0.0.1:9999", model_name="speech-2.8-hd")
        assert adapter.protocol == "minimax"


class TestCapabilityTestService:
    def test_probe_writes_back_status(self):
        db = Database()
        db.create_all()
        owner = f"user_{uuid.uuid4().hex[:8]}"
        svc = ConfigService(db, encryption_key="k")
        view = svc.create(
            actor_id=owner,
            is_admin=False,
            kind="content_model",
            protocol="openai",
            endpoint="https://api.example.test/v1",
            model_name="m",
            credentials={"api_key": FAKE_KEY},
        )

        class FakeAdapter:
            capabilities = ("text", "json_schema")

            def probe_all(self):
                return {"text": {"state": "available", "detail": ""}, "json_schema": {"state": "unavailable", "detail": "unsupported"}}

            def close(self):
                pass

        tester = CapabilityTestService(svc, adapter_factory=lambda *a, **k: FakeAdapter())
        result = tester.run(view["config_id"], actor_id=owner, is_admin=False)
        assert result["capability_status"]["json_schema"]["state"] == "unavailable"
        persisted = svc.get(view["config_id"], actor_id=owner, is_admin=False)
        assert persisted["capability_status"]["text"]["state"] == "available"
