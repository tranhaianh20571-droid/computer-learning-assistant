"""Offline strict reasoning profiles and real provider SDK wire contracts."""
from __future__ import annotations

import asyncio
import json
import socket

import httpx2 as httpx
import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from backend.agents import native
from backend.agents.contracts import AgentDeps
from backend.agents.native import ReasoningParameterError
from backend.planning.errors import PlanningError
from backend.slice1.adapters.base import AdapterBase
from backend.slice1.errors import Slice1Error
from backend.slice1.reasoning import (ReasoningSettings, ReasoningTestIn, describe_reasoning,
    model_settings_for, parse_settings, probe_saved_reasoning, validate_settings)
from backend.slice1.router import ConfigCreateIn, ConfigUpdateIn


def dns(host, port, *args):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]


@pytest.mark.parametrize("value", [
    {"mode": "effort"}, {"mode": "budget"}, {"mode": "default", "effort": "high"},
    {"mode": "effort", "effort": "high", "budget_tokens": 1024},
    {"mode": "auto", "budget_tokens": 1024}, {"mode": "off", "effort": "low"},
    {"mode": "budget", "budget_tokens": True}, {"mode": "budget", "budget_tokens": "1024"},
    {"mode": "budget", "budget_tokens": 0}, {"show_summary": 1}, {"show_summary": "true"},
    {"effort": None}, {"mode": "default", "arbitrary": 1}, {"mode": "effort", "effort": "high\n"},
    {"manual": "true"}, {"manual": 1}, {"manual": None},
    {"discovered": "true"}, {"discovered": None}, {"manual": True, "discovered": True},
])
def test_strict_settings_reject_ambiguous_or_coerced_inputs(value):
    with pytest.raises(Slice1Error):
        parse_settings(value)


def test_settings_roundtrip_and_strict_route_models():
    selected = {"mode": "budget", "budget_tokens": 2048, "show_summary": False}
    assert parse_settings(json.loads(json.dumps(selected))) == selected
    assert ReasoningSettings().model_dump(exclude_none=True) == {"mode": "default", "show_summary": True}
    for model, value in [(ConfigCreateIn, {"kind": "content_model", "protocol": "openai", "endpoint": "https://model.invalid", "reasoning_settings": None}),
                         (ConfigUpdateIn, {"reasoning_settings": None}), (ReasoningTestIn, {"config_version": True}),
                         (ReasoningTestIn, {"config_version": 1, "credentials": {"api_key": "synthetic"}})]:
        with pytest.raises(ValidationError):
            model.model_validate(value)


def test_describe_is_local_and_does_not_guess_unknown_model(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("describe attempted DNS")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    known = describe_reasoning("openai", "https://relay.invalid/v1", "gpt-5.2")
    assert known["source"] == "rule" and known["endpoint_scope"] == "compatible_unverified"
    assert known["effort_values"] == ["none", "low", "medium", "high", "xhigh"]
    unknown = describe_reasoning("openai", "https://relay.invalid/v1", "synthetic-next-model")
    assert unknown["source"] == "unknown" and unknown["supported_modes"] == ["default"]
    assert unknown["manual_controls"]["effort_values"] == ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
    assert describe_reasoning("openai", "https://api.anthropic.com", "gpt-5.2")["source"] == "unknown"
    assert describe_reasoning("openai", "http://relay.invalid", "gpt-5.2")["source"] == "rule"
    for endpoint in ("ftp://relay.invalid", "https://user:synthetic@relay.invalid", "https://relay.invalid?q=x", "https://relay.invalid/#x"):
        with pytest.raises(Slice1Error):
            describe_reasoning("openai", endpoint, "gpt-5.2")


@pytest.mark.parametrize("model,mode,field", [
    ("claude-sonnet-4-6", "effort", "anthropic_effort"),
    ("claude-sonnet-4-20250514", "budget", "max_tokens"),
])
def test_anthropic_modes_are_exclusive_and_budget_reserves_answer(model, mode, field):
    value = {"mode": mode, "effort": "high"} if mode == "effort" else {"mode": mode, "budget_tokens": 2048}
    settings = model_settings_for("anthropic", "https://relay.invalid", model, value)
    assert field in settings
    if mode == "effort":
        assert settings["anthropic_thinking"] == {"type": "adaptive"}
        assert "budget_tokens" not in settings["anthropic_thinking"]
    else:
        assert settings["max_tokens"] > settings["anthropic_thinking"]["budget_tokens"]
        assert "anthropic_effort" not in settings
    with pytest.raises(Slice1Error):
        validate_settings({"mode": "budget", "budget_tokens": 512}, protocol="anthropic", endpoint="https://relay.invalid", model_name=model)


def test_model_specific_off_and_budget_rules():
    with pytest.raises(Slice1Error):
        validate_settings({"mode": "off"}, protocol="gemini", endpoint="https://relay.invalid", model_name="gemini-2.5-pro")
    with pytest.raises(Slice1Error):
        validate_settings({"mode": "effort", "effort": "medium"}, protocol="openai", endpoint="https://api.deepseek.com", model_name="deepseek-v4-flash")
    assert model_settings_for("openai", "https://relay.invalid", "synthetic", {}) == {}
    assert model_settings_for("openai", "https://relay.invalid", "gpt-5.2", {}) == {}
    assert model_settings_for("anthropic", "https://relay.invalid", "claude-sonnet-4-6", {}) == {}


CASES = [
    ("openai", "https://relay.invalid/v1", "synthetic-new", {"discovered": True, "mode": "effort", "effort": "low"}, {"reasoning_effort": "low"}),
    ("anthropic", "https://relay.invalid/v1", "synthetic-new", {"discovered": True, "mode": "effort", "effort": "low"}, {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}),
    ("gemini", "https://relay.invalid/v1beta", "synthetic-new", {"discovered": True, "mode": "effort", "effort": "low", "show_summary": False}, {"generationConfig": {"thinkingConfig": {"thinking_level": "LOW", "include_thoughts": False}}}),
    ("openai", "https://relay.invalid/v1", "gpt-5.2", {"mode": "effort", "effort": "high"}, {"reasoning_effort": "high"}),
    ("openai", "https://relay.invalid/v1", "gpt-5.2", {"mode": "off"}, {"reasoning_effort": "none"}),
    ("openai", "https://api.deepseek.com/v1", "deepseek-v4-flash", {"mode": "effort", "effort": "max"}, {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}),
    ("openai", "https://api.deepseek.com/v1", "deepseek-v4-flash", {"mode": "off"}, {"thinking": {"type": "disabled"}}),
    ("anthropic", "https://relay.invalid/v1", "claude-sonnet-4-6", {"mode": "effort", "effort": "high"}, {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}),
    ("anthropic", "https://relay.invalid/v1", "claude-sonnet-4-20250514", {"mode": "budget", "budget_tokens": 2048}, {"thinking": {"type": "enabled", "budget_tokens": 2048}, "max_tokens": 6144}),
    ("anthropic", "https://relay.invalid/v1", "claude-sonnet-4-6", {"mode": "auto"}, {"thinking": {"type": "adaptive"}}),
    ("gemini", "https://relay.invalid/v1beta", "gemini-2.5-flash", {"mode": "budget", "budget_tokens": 2048}, {"generationConfig": {"thinkingConfig": {"thinking_budget": 2048, "include_thoughts": True}}}),
    ("gemini", "https://relay.invalid/v1beta", "gemini-2.5-flash", {"mode": "auto", "show_summary": False}, {"generationConfig": {"thinkingConfig": {"thinking_budget": -1, "include_thoughts": False}}}),
    ("gemini", "https://relay.invalid/v1beta", "gemini-2.5-flash", {"mode": "off"}, {"generationConfig": {"thinkingConfig": {"thinking_budget": 0, "include_thoughts": True}}}),
    ("gemini", "https://relay.invalid/v1beta", "gemini-3-pro-preview", {"mode": "effort", "effort": "high"}, {"generationConfig": {"thinkingConfig": {"thinking_level": "HIGH", "include_thoughts": True}}}),
    ("openai", "https://relay.invalid/v1", "synthetic-unlisted", {"manual": True, "mode": "effort", "effort": "max"}, {"reasoning_effort": "max"}),
    ("openai", "https://relay.invalid/v1", "synthetic-unlisted", {"manual": True, "mode": "off"}, {"reasoning_effort": "none"}),
    ("anthropic", "https://relay.invalid/v1", "synthetic-unlisted", {"manual": True, "mode": "effort", "effort": "high"}, {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}),
    ("anthropic", "https://relay.invalid/v1", "synthetic-unlisted", {"manual": True, "mode": "budget", "budget_tokens": 2048}, {"thinking": {"type": "enabled", "budget_tokens": 2048}, "max_tokens": 6144}),
    ("anthropic", "https://relay.invalid/v1", "synthetic-unlisted", {"manual": True, "mode": "auto"}, {"thinking": {"type": "adaptive"}}),
    ("anthropic", "https://relay.invalid/v1", "synthetic-unlisted", {"manual": True, "mode": "off"}, {"thinking": {"type": "disabled"}}),
    ("gemini", "https://relay.invalid/v1beta", "synthetic-unlisted", {"manual": True, "mode": "effort", "effort": "medium", "show_summary": False}, {"generationConfig": {"thinkingConfig": {"thinking_level": "MEDIUM", "include_thoughts": False}}}),
    ("gemini", "https://relay.invalid/v1beta", "synthetic-unlisted", {"manual": True, "mode": "budget", "budget_tokens": 2048}, {"generationConfig": {"thinkingConfig": {"thinking_budget": 2048, "include_thoughts": True}}}),
    ("gemini", "https://relay.invalid/v1beta", "synthetic-unlisted", {"manual": True, "mode": "auto"}, {"generationConfig": {"thinkingConfig": {"thinking_budget": -1, "include_thoughts": True}}}),
    ("gemini", "https://relay.invalid/v1beta", "synthetic-unlisted", {"manual": True, "mode": "off"}, {"generationConfig": {"thinkingConfig": {"thinking_budget": 0, "include_thoughts": True}}}),
]


@pytest.mark.parametrize("protocol", ["openai", "anthropic", "gemini"])
def test_unlisted_model_requires_explicit_manual_and_default_has_no_strength(protocol):
    selected = {"mode": "effort", "effort": "high"}
    target = dict(protocol=protocol, endpoint="https://relay.invalid", model_name="synthetic-unlisted")
    with pytest.raises(Slice1Error):
        validate_settings(selected, **target)
    assert validate_settings({**selected, "manual": True}, **target)["manual"] is True
    assert model_settings_for(protocol, target["endpoint"], target["model_name"]) == {}
    caps = describe_reasoning(**target)
    assert caps["source"] == "unknown" and caps["supported_modes"] == ["default"]
    assert caps["manual_controls"]["supported_modes"][0] == "default"


@pytest.mark.parametrize("protocol,model", [("openai", "gpt-5.2"), ("anthropic", "claude-sonnet-4-6"), ("gemini", "gemini-2.5-pro")])
def test_manual_cannot_override_known_profiles_or_wrong_provider(protocol, model):
    with pytest.raises(Slice1Error):
        validate_settings({"manual": True}, protocol=protocol, endpoint="https://relay.invalid", model_name=model)
    wrong = "https://api.openai.com" if protocol != "openai" else "https://api.anthropic.com"
    assert "manual_controls" not in describe_reasoning(protocol, wrong, "synthetic-unlisted")
    with pytest.raises(Slice1Error):
        validate_settings({"manual": True, "mode": "effort", "effort": "high"}, protocol=protocol, endpoint=wrong, model_name="synthetic-unlisted")


@pytest.mark.parametrize("protocol,value", [
    ("openai", {"mode": "budget", "budget_tokens": 2048}),
    ("openai", {"mode": "auto"}),
    ("openai", {"mode": "effort", "effort": "invented"}),
    ("anthropic", {"mode": "effort", "effort": "xhigh"}),
    ("anthropic", {"mode": "budget", "budget_tokens": 512}),
    ("anthropic", {"mode": "budget", "budget_tokens": 32001}),
    ("gemini", {"mode": "effort", "effort": "max"}),
    ("gemini", {"mode": "budget", "budget_tokens": 127}),
    ("gemini", {"mode": "budget", "budget_tokens": 32769}),
])
def test_manual_protocol_values_remain_bounded(protocol, value):
    with pytest.raises(Slice1Error):
        validate_settings({**value, "manual": True}, protocol=protocol, endpoint="https://relay.invalid", model_name="synthetic-unlisted")


def install_wire(monkeypatch, sent, status=200, failure=None):
    real = native.SafeModelTransport
    def factory(endpoint, deps, *, max_bytes):
        def handler(request):
            sent.append(json.loads(request.content))
            if status != 200:
                return httpx.Response(status, json={"error": failure or {"code": "synthetic_error", "message": "synthetic-private-diagnostic"}})
            body = sent[-1]
            if "generationConfig" in body or "contents" in body:
                reply = {"candidates": [{"content": {"role": "model", "parts": [{"text": "OK"}]}, "finishReason": "STOP"}]}
            elif request.url.path.endswith("/messages"):
                reply = {"id": "synthetic", "type": "message", "role": "assistant", "model": body["model"],
                    "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}
            else:
                reply = {"id": "synthetic", "object": "chat.completion", "created": 1, "model": body["model"], "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]}
            return httpx.Response(200, json=reply)
        return real(endpoint, deps, max_bytes=max_bytes, resolver=dns, inner=httpx.MockTransport(handler))
    monkeypatch.setattr(native, "SafeModelTransport", factory)


def contains(actual, expected):
    for key, value in expected.items():
        if isinstance(value, dict):
            contains(actual[key], value)
        else:
            assert actual[key] == value


@pytest.mark.parametrize("protocol,endpoint,model,value,expected", CASES)
def test_real_sdk_wire_contains_selected_protocol_parameters(monkeypatch, protocol, endpoint, model, value, expected):
    sent = []
    install_wire(monkeypatch, sent)
    source = AdapterBase(endpoint=endpoint, model_name=model, credentials={"api_key": "synthetic-key"}, reasoning_settings=value)
    source.protocol = protocol
    deps = AgentDeps()
    async def request():
        async with native.native_model_session(source, deps) as sdk_model:
            await sdk_model.request([ModelRequest(parts=[UserPromptPart("synthetic")])], None, ModelRequestParameters())
    asyncio.run(request())
    assert len(sent) == deps.budget.wire_requests == 1
    contains(sent[0], expected)
    if protocol == "anthropic" and value["mode"] == "budget":
        assert "output_config" not in sent[0]
    if protocol == "gemini":
        thinking = sent[0]["generationConfig"]["thinkingConfig"]
        assert not ("thinking_budget" in thinking and "thinking_level" in thinking)


class SavedConfigs:
    def __init__(self):
        self.result = None
    def reserve_reasoning_probe(self, *args, **kwargs):
        return {"endpoint": "https://relay.invalid/v1", "model_name": "gpt-5.2", "protocol": "openai",
                "reasoning_settings": {"mode": "effort", "effort": "high", "show_summary": True}}
    def resolve_reasoning_credentials(self, *args, **kwargs):
        return {"api_key": "synthetic-key"}
    def check_reasoning_probe(self, *args, **kwargs):
        return None
    def finish_reasoning_probe(self, *args, result, **kwargs):
        self.result = result
        return result


@pytest.mark.parametrize("status,failure,evidence,code", [
    (200, None, "request_accepted", None),
    (400, {"code": "unsupported_parameter", "param": "reasoning_effort"}, "unsupported", "MODEL_CAPABILITY_UNAVAILABLE"),
    (400, {"code": "unsupported_parameter", "param": "temperature"}, "unknown", "MODEL_PROVIDER_ERROR"),
    (401, None, "unknown", "MODEL_CREDENTIAL_INVALID"),
    (429, None, "unknown", "MODEL_RATE_LIMITED"),
    (503, None, "unknown", "MODEL_PROVIDER_ERROR"),
])
def test_explicit_probe_uses_sdk_wire_and_preserves_evidence_boundary(monkeypatch, status, failure, evidence, code):
    sent = []
    install_wire(monkeypatch, sent, status, failure)
    result = asyncio.run(probe_saved_reasoning(SavedConfigs(), "cfg_synthetic", actor_id="owner_synthetic", is_admin=False, config_version=1))
    assert result == {"evidence": evidence, "error_code": code}
    assert len(sent) == 1 and sent[0]["reasoning_effort"] == "high"
    assert "synthetic-private-diagnostic" not in str(result)


def test_reasoning_parameter_error_never_selects_output_fallback():
    assert not issubclass(ReasoningParameterError, native.ModelCapabilityError)


@pytest.mark.parametrize("protocol,failure", [
    ("openai", {"code": "unsupported_parameter", "param": "reasoning_effort", "message": "synthetic-private-diagnostic"}),
    ("anthropic", {"type": "invalid_request_error", "message": "thinking: adaptive is not supported; synthetic-private-diagnostic"}),
    ("gemini", {"code": 400, "status": "INVALID_ARGUMENT", "message": 'Unknown name "thinkingLevel" at generationConfig.thinkingConfig; synthetic-private-diagnostic'}),
])
def test_manual_probe_protocol_rejections_are_controlled_without_retry(monkeypatch, protocol, failure):
    sent = []
    install_wire(monkeypatch, sent, 400, failure)
    class ManualConfigs(SavedConfigs):
        def reserve_reasoning_probe(self, *args, **kwargs):
            return {"endpoint": "https://relay.invalid/v1", "model_name": "synthetic-unlisted", "protocol": protocol,
                "reasoning_settings": {"manual": True, "mode": "effort", "effort": "high", "show_summary": True}}
    result = asyncio.run(probe_saved_reasoning(ManualConfigs(), "cfg_synthetic", actor_id="owner_synthetic", is_admin=False, config_version=1))
    assert result == {"evidence": "unsupported", "error_code": "MODEL_CAPABILITY_UNAVAILABLE"}
    assert len(sent) == 1
    assert "synthetic-private-diagnostic" not in str(result)


@pytest.mark.parametrize("detail,path", [
    ({"type": "invalid_request_error", "message": "invalid max_tokens"}, "/v1/messages"),
    ({"status": "INVALID_ARGUMENT", "message": "invalid contents"}, "/v1/models/synthetic:generateContent"),
    ({"status": "INVALID_ARGUMENT", "message": "thinkingLevel is HIGH"}, "/v1/models/synthetic:generateContent"),
    ({"status": "INVALID_ARGUMENT", "message": 'Unknown name "thinkingLevel"'}, "/v1/chat/completions"),
    ({"type": "invalid_request_error", "message": "thinking unsupported" + "x" * 2048}, "/v1/messages"),
])
def test_other_native_errors_are_not_reasoning_support_evidence(detail, path):
    assert native._native_reasoning_rejection(detail, path) is False
