"""Synthetic transport tests; DNS, sockets, credentials and provider payloads are fake."""
import asyncio
import gzip
import json
import socket
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from backend.search.contracts import AnySearchRequest, SearchProviderError
from backend.slice1.adapters.anysearch import ANYSEARCH_ENDPOINT, ANYSEARCH_SEARCH_URL, AnySearchAdapter


def payload(results=None):
    results = results if results is not None else [{"title": "Synthetic", "url": "https://example.org/x", "snippet": "Synthetic"}]
    return {"code": 0, "request_id": str(uuid4()), "message": "synthetic ignored field",
            "data": {"results": results, "metadata": {"total_results": len(results), "search_time_ms": 1}}}


def resolver(*_):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]


def adapter(handler=None, **kwargs):
    return AnySearchAdapter(api_key=SecretStr("synthetic-test-key"), resolver=resolver,
                            transport=httpx.MockTransport(handler or (lambda _: httpx.Response(200, json=payload()))), **kwargs)


def search(instance, *, request=None, outbound=None, wire=None, remaining=None):
    return asyncio.run(instance.search(request or AnySearchRequest(query="Synthetic topic"),
        check_outbound=outbound or (lambda *_: None), before_wire=wire or (lambda: None),
        remaining_seconds=remaining or (lambda: 5.0)))


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, *, delay=0, error=None):
        self.chunks, self.delay, self.error = chunks, delay, error
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk
        if self.error is not None:
            raise self.error

    async def aclose(self):
        self.closed = True


def test_official_wire_pin_guards_and_default_body(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    records, sequence = [], []
    def handler(request):
        records.append(request)
        sequence.append("wire")
        assert request.url.host == "8.8.8.8"
        assert request.url.path == "/v1/search"
        assert request.headers["Host"] == "api.anysearch.com"
        assert request.extensions["sni_hostname"] == b"api.anysearch.com"
        assert request.headers["Authorization"] == "Bearer synthetic-test-key"
        assert json.loads(request.content) == {"query": "Synthetic topic", "max_results": 10, "format": "json"}
        return httpx.Response(200, json=payload())
    def outbound(body, url):
        sequence.append("guard")
        assert url == ANYSEARCH_SEARCH_URL and type(body) is bytes
    result = search(adapter(handler), outbound=outbound, wire=lambda: sequence.append("budget"))
    assert len(records) == 1 and result.code == 0
    assert sequence == ["guard", "guard", "budget", "wire"]


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "224.0.0.1", "ff0e::1", "not-an-ip"])
def test_dns_requires_every_answer_to_be_global(address):
    wires = []
    instance = AnySearchAdapter(api_key=SecretStr("synthetic"), transport=httpx.MockTransport(lambda request: wires.append(request)),
        resolver=lambda *_: resolver() + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    with pytest.raises(SearchProviderError) as caught:
        search(instance)
    assert caught.value.code == "SEARCH_REQUEST_REJECTED" and caught.value.unsent and not wires


def test_dns_failure_is_retryable_and_confirmed_unsent():
    def fail(*_):
        raise socket.gaierror("synthetic secret message")
    instance = AnySearchAdapter(api_key=SecretStr("synthetic"), resolver=fail, transport=httpx.MockTransport(lambda _: None))
    with pytest.raises(SearchProviderError) as caught:
        search(instance)
    assert caught.value.code == "SEARCH_CONNECTION_ERROR"
    assert caught.value.retryable and caught.value.unsent
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("endpoint", ["http://api.anysearch.com", "https://example.org", "https://api.anysearch.com/", "https://api.anysearch.com/v1/search"])
def test_custom_origin_is_rejected(endpoint):
    with pytest.raises(SearchProviderError) as caught:
        AnySearchAdapter(endpoint=endpoint, api_key=SecretStr("synthetic"))
    assert caught.value.code == "SEARCH_REQUEST_REJECTED"


def test_key_required_no_anonymous_or_plain_string_api_key():
    for kwargs in ({}, {"api_key": "synthetic"}, {"credentials": {"api_key": ""}}):
        with pytest.raises(SearchProviderError) as caught:
            AnySearchAdapter(**kwargs)
        assert caught.value.code == "SEARCH_CREDENTIAL_INVALID"


@pytest.mark.parametrize("status,code", [(400, "SEARCH_REQUEST_REJECTED"), (415, "SEARCH_REQUEST_REJECTED"),
    (401, "SEARCH_CREDENTIAL_INVALID"), (403, "SEARCH_CREDENTIAL_INVALID"), (402, "SEARCH_QUOTA_EXCEEDED"),
    (429, "SEARCH_RATE_LIMITED"), (500, "SEARCH_PROVIDER_UNAVAILABLE"), (503, "SEARCH_PROVIDER_UNAVAILABLE"),
    (302, "SEARCH_OUTPUT_INVALID")])
def test_safe_http_mapping_never_exposes_provider_message(status, code, caplog):
    records = []
    external_id = str(uuid4())
    def handler(request):
        records.append(request)
        return httpx.Response(status, json={"request_id": external_id, "message": "generated-api-key=synthetic-secret"},
                              headers={"Retry-After": "10", "Location": "https://example.org"})
    with pytest.raises(SearchProviderError) as caught:
        search(adapter(handler))
    error = caught.value
    assert error.code == code and not error.unsent
    assert error.retryable == (status == 429 or status >= 500)
    assert error.status == status and str(error.external_request_id) == external_id
    assert len(records) == 1
    assert (error.retry_after is not None) == (status == 429)
    assert "synthetic-secret" not in str(error) + repr(error) + caplog.text


@pytest.mark.parametrize("edit", [
    lambda p: p.update(code=True), lambda p: p.update(code="0"), lambda p: p.update(code=1),
    lambda p: p.update(request_id="untrusted-id"), lambda p: p.pop("data"),
    lambda p: p["data"]["metadata"].update(total_results=0),
    lambda p: p["data"]["metadata"].update(search_time_ms=True),
    lambda p: p["data"].update(results=["not-object"]),
    lambda p: p["data"].update(results=[{}] * 11),
])
def test_envelope_strictness(edit):
    data = payload()
    edit(data)
    with pytest.raises(SearchProviderError) as caught:
        search(adapter(lambda _: httpx.Response(200, json=data)))
    assert caught.value.code == "SEARCH_OUTPUT_INVALID"


@pytest.mark.parametrize("body", [b"{", b'[]', b'{"code":0,"code":0}', b'{"code":NaN}', b'"\xff"',
    b"[" * 40 + b"0" + b"]" * 40])
def test_invalid_json_and_duplicate_keys_rejected(body):
    with pytest.raises(SearchProviderError) as caught:
        search(adapter(lambda _: httpx.Response(200, content=body, headers={"Content-Type": "application/json"})))
    assert caught.value.code == "SEARCH_OUTPUT_INVALID"


def test_json_node_budget_before_schema_unknown_field_discard():
    data = payload()
    data["ignored"] = [0] * 20001
    with pytest.raises(SearchProviderError) as caught:
        search(adapter(lambda _: httpx.Response(200, json=data)))
    assert caught.value.code == "SEARCH_OUTPUT_INVALID"


def test_default_accepts_ten_results_and_request_timeout_stays_twenty_seconds():
    results = [{"title": "Synthetic", "url": f"https://example.org/{index}", "snippet": "Synthetic"}
               for index in range(10)]
    instance = adapter(lambda _: httpx.Response(200, json=payload(results)), timeout=120)
    assert AnySearchRequest(query="Synthetic topic").max_results == 10
    assert instance.timeout == 20.0
    assert len(search(instance, remaining=lambda: 120).data.results) == 10


@pytest.mark.parametrize("exception,code", [
    (httpx.ReadError("synthetic"), "SEARCH_CONNECTION_ERROR"),
    (httpx.ReadTimeout("synthetic"), "SEARCH_TIMEOUT"),
])
def test_stream_read_failure_is_retryable_without_adapter_replay(exception, code):
    wires = []
    stream = Chunks([b"{"], error=exception)
    def handler(request):
        wires.append(request)
        return httpx.Response(200, stream=stream, headers={"Content-Type": "application/json"})
    with pytest.raises(SearchProviderError) as caught:
        search(adapter(handler))
    assert caught.value.code == code and caught.value.retryable and not caught.value.unsent
    assert len(wires) == 1 and stream.closed


def test_provider_must_honor_requested_result_limit():
    with pytest.raises(SearchProviderError) as caught:
        search(adapter(lambda _: httpx.Response(200, json=payload([{}, {}]))), request=AnySearchRequest(query="topic", max_results=1))
    assert caught.value.code == "SEARCH_OUTPUT_INVALID"


def test_chunked_and_compressed_decompressed_size_limits_close_streams():
    for compressed in (False, True):
        data = b"x" * 2000
        wire = gzip.compress(data) if compressed else data
        stream = Chunks([wire[:10], wire[10:]])
        headers = {"Content-Type": "application/json"}
        if compressed:
            headers["Content-Encoding"] = "gzip"
        with pytest.raises(SearchProviderError) as caught:
            search(adapter(lambda _: httpx.Response(200, stream=stream, headers=headers), max_response_bytes=1000))
        assert caught.value.code == "SEARCH_RESPONSE_TOO_LARGE" and stream.closed


def test_valid_gzip_stream_and_truncated_gzip():
    wire = gzip.compress(json.dumps(payload()).encode())
    headers = {"Content-Type": "application/json", "Content-Encoding": "gzip"}
    assert search(adapter(lambda _: httpx.Response(200, stream=Chunks([wire[:5], wire[5:]]), headers=headers))).code == 0
    with pytest.raises(SearchProviderError) as caught:
        search(adapter(lambda _: httpx.Response(200, stream=Chunks([wire[:-5]]), headers=headers)))
    assert caught.value.code == "SEARCH_OUTPUT_INVALID"


@pytest.mark.parametrize("exception,code,retryable,unsent", [
    (httpx.ConnectError("synthetic"), "SEARCH_CONNECTION_ERROR", True, True),
    (httpx.ConnectTimeout("synthetic"), "SEARCH_TIMEOUT", True, False),
    (httpx.ReadError("synthetic"), "SEARCH_CONNECTION_ERROR", True, False),
    (httpx.WriteError("synthetic"), "SEARCH_CONNECTION_ERROR", True, False),
    (httpx.ReadTimeout("synthetic"), "SEARCH_TIMEOUT", True, False),
])
def test_no_transport_retries_and_safe_error_mapping(exception, code, retryable, unsent):
    wires = []
    def handler(request):
        wires.append(request)
        raise exception
    with pytest.raises(SearchProviderError) as caught:
        search(adapter(handler))
    assert len(wires) == 1
    assert (caught.value.code, caught.value.retryable, caught.value.unsent) == (code, retryable, unsent)


def test_total_timeout_includes_response_stream_and_dns():
    async def delayed_dns(*_):
        await asyncio.sleep(.1)
        return resolver()
    for instance in (adapter(lambda _: httpx.Response(200, stream=Chunks([b"{}"], delay=.1)), timeout=.02),
                     AnySearchAdapter(api_key=SecretStr("synthetic"), resolver=delayed_dns,
                         transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload())), timeout=.02)):
        with pytest.raises(SearchProviderError) as caught:
            search(instance)
        assert caught.value.code == "SEARCH_TIMEOUT" and caught.value.retryable


def test_sync_dns_timeout_does_not_wait_for_loop_shutdown():
    import threading
    from time import monotonic
    release = threading.Event()
    def blocked_dns(*_):
        release.wait(2)
        return resolver()
    instance = AnySearchAdapter(api_key=SecretStr("synthetic"), resolver=blocked_dns,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload())), timeout=.02)
    started = monotonic()
    try:
        with pytest.raises(SearchProviderError) as caught:
            search(instance)
        assert caught.value.code == "SEARCH_TIMEOUT"
        assert monotonic() - started < .5
    finally:
        release.set()


def test_deadline_exhausted_after_receipt_preserves_sent_uncertainty():
    remaining = iter([120.0] * 4 + [0.0])
    wires = []
    def handler(request):
        wires.append(request)
        return httpx.Response(200, json=payload())
    with pytest.raises(SearchProviderError) as caught:
        search(adapter(handler), remaining=lambda: next(remaining))
    assert caught.value.code == "SEARCH_TIMEOUT" and caught.value.retryable
    assert not caught.value.unsent and len(wires) == 1


def test_expired_business_window_makes_zero_wires():
    wires = []
    with pytest.raises(SearchProviderError) as caught:
        search(adapter(lambda request: wires.append(request)), remaining=lambda: 0)
    assert caught.value.code == "SEARCH_TIMEOUT" and not wires


@pytest.mark.parametrize("where", ["outbound", "wire"])
def test_guard_failures_propagate_and_prevent_wire(where):
    class ControlError(RuntimeError):
        pass
    error = ControlError("synthetic controlled auth failure")
    wires = []
    def fail(*_):
        raise error
    with pytest.raises(ControlError) as caught:
        search(adapter(lambda request: wires.append(request)), **{where: fail})
    assert caught.value is error and not wires


def test_cancellation_propagates():
    def fail(*_):
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        search(adapter(), wire=fail)


def test_per_call_owned_clients_close_once_in_same_loop(monkeypatch):
    closed, loops = [], []
    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            loops.append(asyncio.get_running_loop())
            return httpx.Response(200, json=payload())
        async def aclose(self):
            closed.append(asyncio.get_running_loop())
    def make_transport(**kwargs):
        assert kwargs == {"retries": 0, "trust_env": False}
        return Transport()
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", make_transport)
    instance = AnySearchAdapter(api_key=SecretStr("synthetic"), resolver=resolver)
    search(instance)
    search(instance)
    assert len(closed) == 2 and closed == loops and closed[0] is not closed[1]
    instance.close()
    with pytest.raises(SearchProviderError) as caught:
        search(instance)
    assert caught.value.code == "SEARCH_CONFIG_UNAVAILABLE"


def test_borrowed_client_is_not_closed():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload()))) as client:
            instance = AnySearchAdapter(api_key=SecretStr("synthetic"), client=client, resolver=resolver)
            await instance.search(AnySearchRequest(query="topic"), check_outbound=lambda *_: None,
                                  before_wire=lambda: None, remaining_seconds=lambda: 5)
            await instance.aclose()
            assert not client.is_closed
    asyncio.run(run())


def test_probe_fixed_nonprivate_query_limit_and_guards():
    records, calls = [], []
    def handler(request):
        records.append(json.loads(request.content))
        return httpx.Response(200, json=payload())
    instance = adapter(handler, probe_guard=lambda: calls.append(True))
    assert instance.probe("text").state == "available"
    assert records == [{"query": "public learning resources", "max_results": 1, "format": "json"}]
    assert len(calls) >= 2
    assert instance.probe("image").state == "unavailable"
    assert len(records) == 1


def test_probe_after_receipt_guard_and_control_errors_propagate():
    received = False
    error = SearchProviderError("SEARCH_CONFIG_UNAVAILABLE")
    def handler(_):
        nonlocal received
        received = True
        return httpx.Response(200, json=payload())
    def guard():
        if received:
            raise error
    instance = adapter(handler, probe_guard=guard)
    with pytest.raises(SearchProviderError) as caught:
        instance.probe("text")
    assert caught.value is error


def test_probe_running_loop_rejected_without_unawaited_coroutine():
    async def run():
        with pytest.raises(RuntimeError, match="worker thread"):
            adapter().probe("text")
    asyncio.run(run())
