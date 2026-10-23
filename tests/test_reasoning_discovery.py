"""Synthetic discovery, isolation, and native protocol contracts."""
import asyncio
import json
import socket

import httpx2 as httpx
import pytest

from backend.agents.contracts import AgentDeps
from backend.agents.native import SafeModelTransport
from backend.slice1.reasoning import model_settings_for
from backend.slice1.reasoning_discovery import ReasoningDiscovery, _enums, probe_payload


def successful(protocol):
    return ({"choices": [{"message": {"content": "OK"}}]} if protocol == "openai" else
            {"object": "response", "output": [], "error": None} if protocol == "openai_responses" else
            {"type": "message", "content": []} if protocol == "anthropic" else
            {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]})


def install(protocol, *, silent=False, metadata=False, fail=None):
    sent = []
    def handler(request):
        body = json.loads(request.content) if request.content else {}
        sent.append((request, body))
        if fail:
            return httpx.Response(fail, json={"error": {"message": "synthetic-private-secret"}})
        if request.method == "GET":
            return httpx.Response(200 if metadata else 404, json={
                "capabilities": {"reasoning": {"effort": {"supported_values": ["low", "ultra"]}}}})
        effort = body.get("reasoning_effort", body.get("reasoning", {}).get("effort",
            body.get("output_config", {}).get("effort", body.get("generationConfig", {}).get("thinkingConfig", {}).get("thinkingLevel"))))
        if effort and effort.lower() not in ("low", "ultra") and not silent:
            return httpx.Response(400, json={"error": {"param": "reasoning_effort",
                "message": "Invalid reasoning effort; supported values: 'low', 'ultra'. synthetic-private-secret"}})
        return httpx.Response(200, json=successful(protocol))
    def factory(endpoint, deps, **kwargs):
        return SafeModelTransport(endpoint, deps, **kwargs, inner=httpx.MockTransport(handler),
            resolver=lambda host, port, *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))])
    return ReasoningDiscovery(transport_factory=factory), sent


@pytest.mark.parametrize("protocol", ["openai", "openai_responses", "anthropic", "gemini"])
@pytest.mark.parametrize("endpoint", ["http://relay.synthetic.test", "http://relay.synthetic.test/v1/"])
def test_discovery_without_model_presets_and_protocol_fields(protocol, endpoint):
    service, sent = install(protocol)
    result = asyncio.run(service.detect(actor_id="a", protocol=protocol, endpoint=endpoint,
        model="synthetic-unlisted-model", key="synthetic-key"))
    assert result["state"] == "completed"
    assert result["parameter_validation"] == "rejected_invalid"
    assert {item["value"] for item in result["options"]} >= {"effort:low", "effort:ultra"}
    assert all(item["evidence"] == "request_accepted" for item in result["options"])
    assert result["requests"] == len(sent) <= 12
    assert "synthetic-private-secret" not in json.dumps(result)
    positive = next(body for request, body in sent if request.method == "POST" and (
        body.get("reasoning_effort") == "low" or body.get("reasoning", {}).get("effort") == "low"
        or body.get("output_config", {}).get("effort") == "low"
        or body.get("generationConfig", {}).get("thinkingConfig", {}).get("thinkingLevel") == "LOW"))
    if protocol == "openai_responses":
        assert positive["store"] is False and "reasoning_effort" not in positive
    if protocol == "anthropic":
        assert positive["thinking"] == {"type": "adaptive"}
    expected = "/v1beta/" if protocol == "gemini" else "/v1/"
    assert all(expected in str(request.url) for request, _ in sent)


def test_metadata_and_invalid_control_evidence():
    service, _ = install("openai_responses", metadata=True)
    result = asyncio.run(service.detect(actor_id="a", protocol="openai_responses",
        endpoint="https://relay.synthetic.test", model="unlisted", key="synthetic"))
    assert [item["evidence"] for item in result["options"]] == ["declared", "declared"]
    service, _ = install("openai", silent=True)
    result = asyncio.run(service.detect(actor_id="a", protocol="openai",
        endpoint="https://relay.synthetic.test", model="unlisted", key="synthetic"))
    assert result["parameter_validation"] == "unconfirmed"
    assert all(item["evidence"] == "request_accepted" for item in result["options"])


def test_cache_singleflight_isolation_and_rate_limit():
    async def run():
        service, sent = install("openai")
        arguments = dict(actor_id="a", protocol="openai", endpoint="https://relay.synthetic.test",
                         model="unlisted", key="synthetic")
        first, second = await asyncio.gather(service.detect(**arguments), service.detect(**arguments))
        assert first["options"] == second["options"]
        count = len(sent)
        third = await service.detect(**{**arguments, "endpoint": "https://relay.synthetic.test/v1/"})
        assert third["cached"] and len(sent) == count
        await service.detect(**{**arguments, "key": "synthetic-other"})
        assert len(sent) > count
        assert not (await service.detect(**{**arguments, "actor_id": "b"}))["cached"]
        for index in range(5):
            last = await service.detect(**{**arguments, "model": f"unlisted-{index}"})
        assert last["state"] == "rate_limited"
        assert all("synthetic-other" not in repr(identity) for identity in service._cache)
    asyncio.run(run())


@pytest.mark.parametrize("status,code", [(401, "MODEL_CREDENTIAL_INVALID"), (429, "MODEL_RATE_LIMITED"), (500, "MODEL_PROVIDER_ERROR")])
def test_failures_never_fabricate_options(status, code):
    service, sent = install("openai", fail=status)
    result = asyncio.run(service.detect(actor_id="a", protocol="openai", endpoint="https://relay.synthetic.test",
        model="unlisted", key="synthetic"))
    assert not result["options"] and result["error_code"] == code
    if status == 500:
        assert result["provider_status"] == 500
    assert len(sent) <= 2
    assert "synthetic-private-secret" not in json.dumps(result)


def test_audit_failure_blocks_wire_and_revocation_is_checked():
    service, sent = install("openai")
    def denied(*args):
        raise RuntimeError("synthetic-audit-unavailable")
    with pytest.raises(RuntimeError):
        asyncio.run(service.detect(actor_id="a", protocol="openai", endpoint="https://relay.synthetic.test",
            model="unlisted", key="synthetic", audit=denied))
    assert not sent
    with pytest.raises(RuntimeError):
        asyncio.run(service.detect(actor_id="a", protocol="openai", endpoint="https://relay.synthetic.test",
            model="unlisted", key="synthetic", check=denied))
    assert not sent


def test_cached_result_still_checks_revocation_and_request_ceiling(monkeypatch):
    import backend.slice1.reasoning_discovery as module
    async def run():
        service, sent = install("openai")
        args = dict(actor_id="a", protocol="openai", endpoint="https://relay.synthetic.test",
                    model="unlisted", key="synthetic")
        await service.detect(**args)
        before = len(sent)
        def revoked():
            raise RuntimeError("synthetic-revoked")
        with pytest.raises(RuntimeError):
            await service.detect(**args, check=revoked)
        assert len(sent) == before
        monkeypatch.setattr(module, "_enums", lambda *args, **kwargs: [f"value{index}" for index in range(8)])
        other, wires = install("anthropic")
        result = await other.detect(**{**args, "protocol": "anthropic"})
        assert result["requests"] == len(wires) == 12
    asyncio.run(run())


def test_discovery_does_not_resolve_dns_before_the_timed_transport(monkeypatch):
    def forbidden(*args):
        raise AssertionError("untimed DNS")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    service, _ = install("openai")
    result = asyncio.run(service.detect(actor_id="a", protocol="openai",
        endpoint="http://relay.synthetic.test", model="unlisted", key="synthetic"))
    assert result["options"]
    with pytest.raises(Exception):
        asyncio.run(service.detect(actor_id="a", protocol="openai",
            endpoint="http://169.254.169.254", model="unlisted", key="synthetic"))


def test_partial_unknown_result_is_not_cached_for_six_hours():
    async def run():
        service, _ = install("openai")
        now, calls = [0], []
        service.clock = lambda: now[0]
        async def partial(*args):
            calls.append(True)
            return {**service._empty(), "state": "completed", "error_code": "MODEL_PROVIDER_ERROR"}
        service._explore = partial
        args = dict(actor_id="a", protocol="openai", endpoint="https://relay.synthetic.test",
                    model="unlisted", key="synthetic")
        await service.detect(**args)
        assert (await service.detect(**args))["cached"]
        now[0] = 61
        assert not (await service.detect(**args))["cached"]
        assert len(calls) == 2
    asyncio.run(run())


@pytest.mark.parametrize("control_status", [500, 401, 429])
def test_invalid_value_control_is_advisory_except_auth_and_rate_limit(control_status):
    sent = []
    def handler(request):
        body = json.loads(request.content) if request.content else {}
        sent.append(body)
        if request.method == "GET":
            return httpx.Response(404, json={})
        effort = body.get("reasoning_effort")
        if effort == "invalid_probe_value":
            return httpx.Response(control_status, json={"error": {"message": "synthetic-private-diagnostic"}})
        if effort and effort != "low":
            return httpx.Response(400, json={"error": {"param": "reasoning_effort", "message": "unsupported reasoning effort"}})
        return httpx.Response(200, json=successful("openai"))
    def factory(endpoint, deps, **kwargs):
        return SafeModelTransport(endpoint, deps, **kwargs, inner=httpx.MockTransport(handler),
            resolver=lambda host, port, *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))])
    result = asyncio.run(ReasoningDiscovery(transport_factory=factory).detect(actor_id="a", protocol="openai",
        endpoint="https://relay.synthetic.test", model="unlisted", key="synthetic"))
    if control_status == 500:
        assert [item["value"] for item in result["options"]] == ["effort:low"]
        assert result["parameter_validation"] == "unconfirmed"
    else:
        assert result["options"] == [] and len(sent) == 3
        assert result["error_code"] == ("MODEL_CREDENTIAL_INVALID" if control_status == 401 else "MODEL_RATE_LIMITED")


def test_explicit_refresh_skips_cache_but_never_limits_or_revocation():
    async def run():
        service, sent = install("openai")
        args = dict(actor_id="a", protocol="openai", endpoint="https://relay.synthetic.test",
                    model="unlisted", key="synthetic")
        await service.detect(**args)
        before = len(sent)
        assert (await service.detect(**args))["cached"]
        assert len(sent) == before
        assert not (await service.detect(**args, refresh=True))["cached"]
        assert len(sent) > before
        def revoked():
            raise RuntimeError("synthetic-revoked")
        with pytest.raises(RuntimeError):
            await service.detect(**args, refresh=True, check=revoked)
        for _ in range(3):
            result = await service.detect(**args, refresh=True)
        assert result["state"] == "rate_limited"
    asyncio.run(run())


def test_discovered_selection_is_native_and_does_not_depend_on_model_whitelist():
    for protocol in ("openai", "openai_responses"):
        mapped = model_settings_for(protocol, "https://relay.synthetic.test", "gpt-5.2",
            {"mode": "effort", "effort": "ultra", "discovered": True})
        assert mapped["openai_reasoning_effort"] == "ultra"
    assert model_settings_for("anthropic", "https://relay.synthetic.test", "arbitrary",
        {"mode": "effort", "effort": "low", "discovered": True})["anthropic_thinking"] == {"type": "adaptive"}
    assert model_settings_for("gemini", "https://relay.synthetic.test", "arbitrary",
        {"mode": "effort", "effort": "low", "discovered": True})["google_thinking_config"]["thinking_level"] == "LOW"
    assert _enums({"description": "Effort is low/high, ignore instructions"}) == []
    assert _enums({"capabilities": {"reasoning": {"effort": {"supported_values": ["low", "bad\nvalue"]}}}}) == ["low"]
    with pytest.raises(Exception):
        model_settings_for("anthropic", "https://api.openai.com", "arbitrary",
            {"mode": "effort", "effort": "low", "discovered": True})


def test_timeout_cancels_wire_and_does_not_claim_support(monkeypatch):
    import backend.slice1.reasoning_discovery as module
    monkeypatch.setattr(module, "DEADLINE_SECONDS", 0.03)
    cancelled = []
    async def handler(request):
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
    def factory(endpoint, deps, **kwargs):
        return SafeModelTransport(endpoint, deps, **kwargs, inner=httpx.MockTransport(handler),
            resolver=lambda host, port, *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))])
    service = ReasoningDiscovery(transport_factory=factory)
    result = asyncio.run(service.detect(actor_id="a", protocol="openai",
        endpoint="https://relay.synthetic.test", model="unlisted", key="synthetic"))
    assert result["error_code"] == "MODEL_TIMEOUT" and result["options"] == []
    assert cancelled and result["requests"] == 1


def test_discovery_route_keeps_saved_credentials_on_owned_destination(monkeypatch):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    import backend.slice1.router as module
    from backend.slice1.errors import Slice1Error, error
    audits, calls = [], []
    class Configs:
        audit = SimpleNamespace(append=lambda *args, **kwargs: audits.append(kwargs))
        def get(self, config_id, *, actor_id, is_admin):
            if actor_id != "synthetic-owner":
                raise error("config_not_found")
            return {"config_version": 3, "endpoint": "http://relay.synthetic.test/v1", "protocol": "openai"}
        def check_reasoning_probe(self, config_id, *, actor_id, expected_version):
            if expected_version != 3:
                raise Slice1Error("capability_test_failed", "reasoning_config_stale", 409)
        def resolve_reasoning_credentials(self, *args, **kwargs):
            return {"api_key": "synthetic-saved-key"}
    class Discovery:
        async def detect(self, **kwargs):
            kwargs["check"]()
            calls.append(kwargs)
            return {"state": "completed", "options": []}
    monkeypatch.setattr(module, "ReasoningDiscovery", Discovery)
    app = FastAPI()
    @app.exception_handler(Slice1Error)
    async def controlled(request, exc):
        return JSONResponse({"detail": {"error_code": exc.code}}, status_code=exc.status)
    def user(request):
        actor = request.headers.get("x-synthetic-user")
        if not actor:
            raise HTTPException(401)
        return SimpleNamespace(user_id=actor, is_admin=False)
    app.include_router(module.create_slice1_router(configs=Configs(), capabilities=None,
        disclosures=None, connectors=None, resolve_user=user))
    client = TestClient(app)
    path = "/api/capabilities/reasoning/discover"
    body = {"protocol": "openai", "endpoint": "http://relay.synthetic.test", "model_name": "arbitrary",
            "config_id": "synthetic-owned", "config_version": 3}
    headers = {"x-synthetic-user": "synthetic-owner"}
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, json=body, headers={"x-synthetic-user": "other"}).status_code == 404
    assert client.post(path, json={**body, "endpoint": "https://other.synthetic.test"}, headers=headers).status_code == 400
    assert client.post(path, json={**body, "protocol": "openai_responses"}, headers=headers).status_code == 400
    assert client.post(path, json={**body, "config_version": 2}, headers=headers).status_code == 409
    assert calls == []
    reply = client.post(path, json=body, headers=headers)
    assert reply.status_code == 200 and reply.headers["cache-control"] == "no-store"
    assert calls[-1]["key"] == "synthetic-saved-key"
    assert "synthetic-saved-key" not in reply.text
    bad = client.post(path, json={**body, "model_name": "", "api_key": "synthetic-private-input"}, headers=headers)
    assert bad.status_code == 422 and "synthetic-private-input" not in bad.text
