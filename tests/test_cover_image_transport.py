"""Synthetic-only image wire contract and transport hardening."""
import base64
import io
import json
import httpx
import pytest
from PIL import Image
from backend.slice1.adapters.imagegen import ImageGenerationAdapter
from backend.slice1.adapters.base import AdapterError


def adapter(handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ImageGenerationAdapter(endpoint="https://image.example.test/v1", client=client,
        resolver=lambda *args: [(None, None, None, None, ("93.184.216.34", 443))], **kwargs)


def test_final_gate_runs_before_wire_and_output_is_bounded(monkeypatch):
    monkeypatch.setattr("backend.slice1.adapters.imagegen.validate_endpoint", lambda endpoint: None)
    order = []
    sample = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(sample, format="PNG")
    def respond(request):
        order.append("wire")
        body = json.loads(request.content)
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "image.example.test"
        assert request.extensions["sni_hostname"] == b"image.example.test"
        assert request.url.path == "/v1/images/generations"
        assert body["response_format"] == "b64_json" and body["n"] == 1
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(sample.getvalue()).decode()}]})
    instance = adapter(respond)
    instance.authorize_request = lambda: order.append("gate")
    image = instance.generate_image("synthetic topic")
    assert order == ["gate", "gate", "wire", "gate"] and Image.open(io.BytesIO(image)).size == (960, 720)


def test_denied_gate_never_opens_wire(monkeypatch):
    monkeypatch.setattr("backend.slice1.adapters.imagegen.validate_endpoint", lambda endpoint: None)
    calls = []
    instance = adapter(lambda request: calls.append(request))
    def deny():
        raise AdapterError("disclosure_revoked", status=403)
    instance.authorize_request = deny
    with pytest.raises(AdapterError, match="disclosure_revoked"):
        instance.generate_image("synthetic topic")
    assert not calls


@pytest.mark.parametrize("status,code", [(302, "invalid_response"), (401, "credential_invalid"),
    (429, "rate_limited"), (503, "provider_unavailable")])
def test_status_classification_and_no_redirect(monkeypatch, status, code):
    monkeypatch.setattr("backend.slice1.adapters.imagegen.validate_endpoint", lambda endpoint: None)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers={"location": "https://other.example.test/steal"})
    instance = adapter(respond)
    with pytest.raises(AdapterError) as caught:
        instance.generate_image("synthetic topic")
    assert caught.value.code == code and len(calls) == 1


@pytest.mark.parametrize("body", [{"data": [{"url": "https://other.example.test/image.png"}]},
    {"task_id": "vendor-async"}, {"data": "invalid"}, []])
def test_no_url_or_async_job_response_fetching(monkeypatch, body):
    monkeypatch.setattr("backend.slice1.adapters.imagegen.validate_endpoint", lambda endpoint: None)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=body)
    instance = adapter(respond)
    with pytest.raises(AdapterError) as caught:
        instance.generate_image("synthetic topic")
    assert caught.value.code == "invalid_response" and len(calls) == 1


def test_response_size_bound_is_enforced_during_stream(monkeypatch):
    monkeypatch.setattr("backend.slice1.adapters.imagegen.validate_endpoint", lambda endpoint: None)
    instance = adapter(lambda request: httpx.Response(200, content=b"x" * 200), max_response_bytes=100)
    with pytest.raises(AdapterError) as caught:
        instance.generate_image("synthetic topic")
    assert caught.value.code == "response_too_large"


@pytest.mark.parametrize("endpoint", ["http://example.test/v1", "https://127.0.0.1/v1", "https://169.254.169.254/v1"])
def test_private_or_insecure_endpoint_never_sends(endpoint):
    calls = []
    instance = adapter(lambda request: calls.append(request))
    instance.endpoint = endpoint
    with pytest.raises(AdapterError) as caught:
        instance.generate_image("synthetic topic")
    assert caught.value.code == "invalid_endpoint" and not calls


@pytest.mark.parametrize("address", ["127.0.0.1", "10.2.3.4", "169.254.169.254", "::1"])
def test_rebound_private_address_is_rejected(monkeypatch, address):
    monkeypatch.setattr("backend.slice1.adapters.imagegen.validate_endpoint", lambda endpoint: None)
    calls = []
    instance = adapter(lambda request: calls.append(request))
    instance.resolver = lambda *args: [(None, None, None, None, (address, 443))]
    with pytest.raises(AdapterError) as caught:
        instance.generate_image("synthetic topic")
    assert caught.value.code == "invalid_endpoint" and not calls


def test_dns_change_never_causes_hostname_connection(monkeypatch):
    monkeypatch.setattr("backend.slice1.adapters.imagegen.validate_endpoint", lambda endpoint: None)
    addresses = ["93.184.216.34", "127.0.0.1"]
    resolutions = []
    wires = []
    def resolver(*args):
        resolutions.append(args)
        return [(None, None, None, None, (addresses.pop(0), 443))]
    instance = adapter(lambda request: wires.append(request) or httpx.Response(200, json={}))
    instance.resolver = resolver
    with pytest.raises(AdapterError):
        instance.generate_image("synthetic topic")
    assert len(resolutions) == len(wires) == 1
    assert wires[0].url.host == "93.184.216.34" and addresses == ["127.0.0.1"]


def test_revoke_during_dns_prevents_wire(monkeypatch):
    monkeypatch.setattr("backend.slice1.adapters.imagegen.validate_endpoint", lambda endpoint: None)
    wires, revoked = [], []
    instance = adapter(lambda request: wires.append(request))
    def resolver(*args):
        revoked.append(True)
        return [(None, None, None, None, ("93.184.216.34", 443))]
    def gate():
        if revoked:
            raise AdapterError("disclosure_revoked", status=403)
    instance.resolver, instance.authorize_request = resolver, gate
    with pytest.raises(AdapterError, match="disclosure_revoked"):
        instance.generate_image("synthetic topic")
    assert not wires


def test_midstream_revoke_closes_response_and_aborts(monkeypatch):
    monkeypatch.setattr("backend.slice1.adapters.imagegen.validate_endpoint", lambda endpoint: None)
    revoked, closed, chunks = [], [], []
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            chunks.append(1)
            yield b'{"data":'
            revoked.append(True)
            chunks.append(2)
            yield b'[]}'
            chunks.append(3)
        def close(self):
            closed.append(True)
    instance = adapter(lambda request: httpx.Response(200, stream=Stream()))
    def gate():
        if revoked:
            raise AdapterError("disclosure_revoked", status=403)
    instance.authorize_request = gate
    with pytest.raises(AdapterError, match="disclosure_revoked"):
        instance.generate_image("synthetic topic")
    assert closed and chunks == [1, 2]


def test_default_client_ignores_environment_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://synthetic-proxy.invalid:9")
    options = []
    class Client:
        def __init__(self, **kwargs):
            options.append(kwargs)
    monkeypatch.setattr("backend.slice1.adapters.imagegen.httpx.Client", Client)
    instance = ImageGenerationAdapter(endpoint="https://image.example.test/v1")
    instance._client_or_new()
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False
