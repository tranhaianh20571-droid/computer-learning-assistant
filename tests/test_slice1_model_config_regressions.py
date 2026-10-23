"""模型端点与加密故障回归：合成凭据、模拟 HTTP，无供应商或数据库请求。"""
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.slice1.adapters import build_adapter
from backend.slice1.adapters.anthropic import AnthropicAdapter
from backend.slice1.adapters.gemini import GeminiAdapter
from backend.slice1.capability_refresh import CapabilityRefresher
from backend.slice1.config_service import ConfigService
from backend.slice1.errors import Slice1Error
from backend.slice1.router import install_slice1_error_handlers
from backend.slice3_app import _configured_model_descriptor, _real_model_factory

FAKE_KEY = "synthetic-provider-key"
BASE = "https://api.example.test/proxy"
ENDPOINTS = [
    ("anthropic", suffix) for suffix in ("", "/", "/v1", "/v1/")
] + [
    ("gemini", suffix) for suffix in ("", "/", "/v1", "/v1/", "/v1beta", "/v1beta/")
]


def _url(adapter):
    if isinstance(adapter, GeminiAdapter):
        return adapter._model_url("generateContent")
    return adapter._messages_url()


def _expected(protocol):
    return BASE + ("/v1/messages" if protocol == "anthropic" else "/v1beta/models/synthetic:generateContent")


@pytest.mark.parametrize("protocol,suffix", ENDPOINTS)
def test_probe_and_configured_generation_use_same_normalized_endpoint(protocol, suffix, monkeypatch):
    # Factory registration must not start the asynchronous capability probe.
    monkeypatch.setattr(CapabilityRefresher, "register", lambda *args: None)
    view = dict(config_id="cfg_synthetic", config_version=1, protocol=protocol,
                endpoint=BASE + suffix, model_name="synthetic", capability_status={})
    runtime = _configured_model_descriptor(view, {"api_key": FAKE_KEY})
    requests = []

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        probe = build_adapter(protocol, endpoint=view["endpoint"], model_name="synthetic",
                              credentials={"api_key": FAKE_KEY}, client=client, allow_private=True)
        assert probe.probe("text").state == "available"
        # plain_text selects the non-streaming path of both adapters.
        runtime._client = client
        runtime.generate([{"role": "user", "content": "synthetic"}], plain_text=True)
        assert _url(probe) == _url(runtime) == _expected(protocol)
        assert requests == [httpx.URL(_expected(protocol)).path] * 2


@pytest.mark.parametrize("suffix", ["", "/", "/v1", "/v1/", "/v1beta", "/v1beta/"])
def test_environment_factory_uses_same_gemini_endpoint(suffix, monkeypatch):
    monkeypatch.setenv("LEARNING_REAL_MODEL_KEY", FAKE_KEY)
    monkeypatch.setenv("LEARNING_REAL_MODEL_ENDPOINT", BASE + suffix)
    monkeypatch.setenv("LEARNING_REAL_MODEL_NAME", "synthetic")
    monkeypatch.setenv("LEARNING_REAL_MODEL_PROTOCOL", "gemini")
    adapter = _real_model_factory()("synthetic-owner", "synthetic-task")
    assert _url(adapter) == _expected("gemini")


@pytest.mark.parametrize("operation", ["encrypt", "decrypt"])
def test_missing_encryption_key_is_controlled_and_retryable(operation, monkeypatch):
    monkeypatch.delenv("APP_ENCRYPTION_KEY", raising=False)
    svc = ConfigService(None)
    with pytest.raises(Slice1Error) as failure:
        if operation == "encrypt":
            svc._encrypt({"api_key": FAKE_KEY})
        else:
            svc._decrypt("synthetic-broken-ciphertext")
    assert failure.value.code == "credential_invalid"
    assert failure.value.status == 400
    assert "APP_ENCRYPTION_KEY" in failure.value.message
    assert "管理员" in failure.value.message
    # Lazy loading does not cache failures; a configured key permits retry.
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "synthetic-encryption-key")
    assert svc._decrypt(svc._encrypt({"api_key": FAKE_KEY})) == {"api_key": FAKE_KEY}


@pytest.mark.parametrize("broken", ["wrong-key", "invalid-ciphertext"])
def test_decrypt_failure_returns_safe_json_response(broken):
    svc = ConfigService(None, encryption_key="synthetic-original-key")
    ciphertext = svc._encrypt({"api_key": FAKE_KEY})
    other = ConfigService(None, encryption_key="synthetic-different-key")
    if broken == "invalid-ciphertext":
        ciphertext = "synthetic-broken-ciphertext"
    app = FastAPI()
    install_slice1_error_handlers(app)

    @app.get("/synthetic-credentials")
    def resolve():
        return other._decrypt(ciphertext)

    with TestClient(app) as client:
        response = client.get("/synthetic-credentials")
    assert response.status_code == 400
    assert response.json()["error_code"] == "credential_invalid"
    assert "重新填写" in response.json()["message"]
    assert FAKE_KEY not in response.text
    assert ciphertext not in response.text
    assert "synthetic-original-key" not in response.text
    assert "synthetic-different-key" not in response.text


@pytest.mark.parametrize("no_proxy,expected", [
    ("localhost,127.0.0.1", "localhost,127.0.0.1"),
    ("127.0.0.1,localhost,::1,[::1]", "127.0.0.1,localhost,::1"),
    ("localhost,[::1],[2001:db8::1]", "localhost,::1,2001:db8::1"),
])
def test_proxy_environment_cleanup_preserves_standard_proxies(monkeypatch, no_proxy, expected):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    runtime = root / "server/backend"
    monkeypatch.chdir(runtime)
    monkeypatch.syspath_prepend(str(runtime))
    from app.assembler import _prepare_http_proxy_environment

    standard = {"HTTP_PROXY": "http://proxy.example.test:8080",
                "HTTPS_PROXY": "http://proxy.example.test:8080",
                "ALL_PROXY": "http://proxy.example.test:8080",
                "NO_PROXY": no_proxy}
    for name, value in standard.items():
        # Clear lowercase before setting uppercase; Windows keys are case insensitive.
        monkeypatch.delenv(name.lower(), raising=False)
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("NODE_USE_ENV_PROXY", "1")
    _prepare_http_proxy_environment()
    import os
    assert "NODE_USE_ENV_PROXY" not in os.environ
    assert all(os.environ[name] == value for name, value in standard.items() if name != "NO_PROXY")
    assert os.environ["NO_PROXY"] == expected
    # Constructing the client exercises urllib proxy discovery without any request.
    with httpx.Client(timeout=2) as client:
        assert not client.is_closed


@pytest.mark.parametrize("value", ["[::1]:8080", "[not-an-ipv6-host]"])
def test_proxy_cleanup_keeps_non_host_bracketed_entries(value, monkeypatch):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    runtime = root / "server/backend"
    monkeypatch.chdir(runtime)
    monkeypatch.syspath_prepend(str(runtime))
    from app.assembler import _prepare_http_proxy_environment
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("NO_PROXY", value)
    _prepare_http_proxy_environment()
    import os
    assert os.environ["NO_PROXY"] == value
