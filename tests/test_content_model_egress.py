"""Offline nonpublic model endpoints and saved reasoning in capability probes."""
import json
import socket
from types import SimpleNamespace

import httpx
import pytest

from backend.slice1.adapters import CapabilityTestService, build_adapter
from backend.slice1.adapters.base import AdapterError, validate_endpoint
from backend.slice1.errors import Slice1Error


def resolve_to(monkeypatch, address):
    monkeypatch.setattr("backend.slice1.adapters.base.socket.getaddrinfo",
        lambda host, port, *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))])


@pytest.mark.parametrize("protocol", ["openai", "anthropic", "gemini"])
@pytest.mark.parametrize("address", ["198.18.0.23", "127.0.0.1", "10.0.0.1", "::1", "fc00::1"])
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_content_model_factory_accepts_nonpublic_dns(monkeypatch, protocol, address, scheme):
    resolve_to(monkeypatch, address)
    adapter = build_adapter(protocol, endpoint=f"{scheme}://model.synthetic.test/v1", model_name="synthetic")
    assert adapter.protocol == protocol
    adapter.close()


@pytest.mark.parametrize("protocol", ["openai", "anthropic", "gemini"])
@pytest.mark.parametrize("address", ["169.254.169.254", "::ffff:169.254.169.254"])
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_metadata_dns_aliases_remain_blocked(monkeypatch, protocol, address, scheme):
    resolve_to(monkeypatch, address)
    with pytest.raises(Slice1Error) as caught:
        build_adapter(protocol, endpoint=f"{scheme}://model.synthetic.test/v1")
    assert caught.value.code == "invalid_field"


@pytest.mark.parametrize("protocol", ["openai", "anthropic", "gemini"])
@pytest.mark.parametrize("endpoint", ["ftp://127.0.0.1/v1", "https://user:synthetic@model.synthetic.test/v1",
    "https://model.synthetic.test/v1?key=synthetic", "https://metadata.google.internal/v1"])
def test_content_model_factory_keeps_scheme_and_url_constraints(protocol, endpoint):
    with pytest.raises(Slice1Error):
        build_adapter(protocol, endpoint=endpoint)


def test_media_accepts_proxy_dns_while_search_factory_stays_restricted(monkeypatch):
    resolve_to(monkeypatch, "198.18.0.23")
    validate_endpoint("https://media.synthetic.test/image")
    build_adapter("openai_image", endpoint="https://images.synthetic.test/v1").close()
    with pytest.raises(Slice1Error):
        build_adapter("anysearch", endpoint="https://api.anysearch.com", allow_private=True)


def test_media_still_rejects_loopback_dns_and_literal_proxy_address(monkeypatch):
    resolve_to(monkeypatch, "127.0.0.1")
    with pytest.raises(AdapterError):
        validate_endpoint("https://media.synthetic.test/image")
    with pytest.raises(Slice1Error):
        build_adapter("openai_image", endpoint="https://images.synthetic.test/v1")
    with pytest.raises(AdapterError):
        validate_endpoint("https://198.18.0.23/image")


@pytest.mark.parametrize("protocol,settings,expected", [
    ("openai", {"mode": "effort", "effort": "low", "manual": True},
        {"reasoning_effort": "low", "max_tokens": 4096}),
    ("openai", {"mode": "off", "manual": True},
        {"reasoning_effort": "none", "max_tokens": 4096}),
    ("openai", {"mode": "default"}, {}),
    ("anthropic", {"mode": "effort", "effort": "high", "manual": True},
        {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}, "max_tokens": 4096}),
    ("anthropic", {"mode": "budget", "budget_tokens": 2048, "manual": True},
        {"thinking": {"type": "enabled", "budget_tokens": 2048}, "max_tokens": 6144}),
    ("anthropic", {"mode": "auto", "manual": True},
        {"thinking": {"type": "adaptive"}, "max_tokens": 4096}),
    ("anthropic", {"mode": "off", "manual": True},
        {"thinking": {"type": "disabled"}, "max_tokens": 4096}),
    ("anthropic", {"mode": "default"}, {}),
    ("gemini", {"mode": "effort", "effort": "low", "manual": True},
        {"generationConfig": {"thinkingConfig": {"thinkingLevel": "LOW", "includeThoughts": False}}}),
    ("gemini", {"mode": "budget", "budget_tokens": 1024, "manual": True},
        {"generationConfig": {"thinkingConfig": {"thinkingBudget": 1024, "includeThoughts": False}}}),
    ("gemini", {"mode": "auto", "manual": True},
        {"generationConfig": {"thinkingConfig": {"thinkingBudget": -1, "includeThoughts": False}}}),
    ("gemini", {"mode": "off", "manual": True},
        {"generationConfig": {"thinkingConfig": {"thinkingBudget": 0, "includeThoughts": False}}}),
    ("gemini", {"mode": "default"}, {}),
])
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_capability_service_sends_saved_settings_in_all_probes(monkeypatch, protocol, settings, expected, scheme):
    resolve_to(monkeypatch, "198.18.0.23")
    calls = []
    settings = {**settings, "show_summary": False}
    view = {"config_id": "synthetic-config", "kind": "content_model", "protocol": protocol,
        "endpoint": f"{scheme}://model.synthetic.test/v1", "model_name": "synthetic-unknown-model",
        "config_version": 1, "reasoning_settings": settings, "capability_status": {}}

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if payload.get("stream") or ":streamGenerateContent" in request.url.path:
            return httpx.Response(200, text='data: {"ok":true}\n\n',
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"ok": True})

    def factory(protocol, **kwargs):
        return build_adapter(protocol, client=httpx.Client(transport=httpx.MockTransport(handler)), **kwargs)

    configs = SimpleNamespace(get=lambda *args, **kwargs: view,
        resolve_credentials=lambda *args: {"api_key": "synthetic-key"},
        set_capability_status=lambda ident, status, **kwargs: {"capability_status": status})
    result = CapabilityTestService(configs, adapter_factory=factory).run("synthetic-config",
        actor_id="synthetic-owner", is_admin=False)
    assert len(calls) == 6
    assert all(item["state"] == "available" for item in result["capability_status"].values())
    for payload in calls:
        if settings["mode"] == "default":
            assert not ({"reasoning_effort", "thinking", "output_config"} & payload.keys())
            assert "thinkingConfig" not in payload.get("generationConfig", {})
        for key, value in expected.items():
            if key == "generationConfig":
                assert payload[key]["thinkingConfig"] == value["thinkingConfig"]
                assert payload[key]["maxOutputTokens"] == max(4096, settings.get("budget_tokens", 0) + 4096)
            else:
                assert payload[key] == value
    if protocol == "gemini":
        schema = next(payload for payload in calls if "response_schema" in payload.get("generationConfig", {}))
        assert schema["generationConfig"]["response_schema"]["properties"] == {"ok": {"type": "boolean"}}


def test_probe_parameter_rejection_has_no_silent_default_retry(monkeypatch):
    resolve_to(monkeypatch, "198.18.0.23")
    calls = []

    def reject(request):
        calls.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "unsupported reasoning_effort"}})

    with httpx.Client(transport=httpx.MockTransport(reject)) as client:
        adapter = build_adapter("openai", endpoint="https://model.synthetic.test/v1", model_name="unknown",
            reasoning_settings={"mode": "effort", "effort": "low", "manual": True, "show_summary": False},
            client=client)
        result = adapter.probe("text")
    assert result.state == "unavailable" and result.detail == "unsupported"
    assert len(calls) == 1 and calls[0]["reasoning_effort"] == "low"
