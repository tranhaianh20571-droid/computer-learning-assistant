"""Synthetic async video wire contracts; never call paid providers."""
import json
import socket

import httpx
import pytest

from backend.slice1.adapters import build_adapter
from backend.slice1.adapters.video import MiniMaxVideoAdapter, SeedanceVideoAdapter
from backend.slice1.errors import AdapterError, Slice1Error


@pytest.fixture(autouse=True)
def synthetic_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])


@pytest.mark.parametrize("protocol,cls,base,create,query,resolution", [
    ("minimax_video", MiniMaxVideoAdapter, "https://video.example.test/gateway/v2", "/gateway/v2/video_generation", "/gateway/v2/query/video_generation/job-1", "768P"),
    ("seedance", SeedanceVideoAdapter, "https://video.example.test/api/v3", "/api/v3/contents/generations/tasks", "/api/v3/contents/generations/tasks/job-1", "720p"),
])
def test_custom_base_auth_reference_image_and_result(protocol, cls, base, create, query, resolution):
    seen = []
    def respond(request):
        seen.append(request)
        assert request.headers["authorization"] == "Bearer synthetic-video-key"
        if request.method == "POST":
            assert request.url.path == create
            payload = json.loads(request.content)
            assert payload["model"] == "synthetic-model"
            assert payload["content"] == [{"type": "text", "text": "Synthetic wave"},
                {"type": "image_url", "image_url": {"url": "https://assets.example.test/frame.png"}, "role": "first_frame"}]
            assert payload["duration"] == 5 and payload["resolution"] == resolution
            return httpx.Response(200, json={"task_id" if protocol == "minimax_video" else "id": "job-1"})
        assert request.url.path == query
        task = {"id": "job-1", "status": "succeeded", "content": {
            "url" if protocol == "minimax_video" else "video_url": "https://assets.example.test/video.mp4?signature=synthetic"}}
        return httpx.Response(200, json={"task": task} if protocol == "minimax_video" else task)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        adapter = build_adapter(protocol, endpoint=base, model_name="synthetic-model",
            credentials={"api_key": "synthetic-video-key"}, client=client)
        assert isinstance(adapter, cls)
        assert adapter.probe_all()["video_generate"]["state"] == "unknown" and not seen
        job = adapter.submit("Synthetic wave", first_frame_url="https://assets.example.test/frame.png", resolution=resolution)
        result = adapter.query(job.job_id)
        assert result.status == "succeeded" and "signature" in result.result_url
        assert "signature" not in repr(result) and len(seen) == 2


@pytest.mark.parametrize("state", ["queued", "running", "failed", "cancelled", "expired"])
@pytest.mark.parametrize("cls", [MiniMaxVideoAdapter, SeedanceVideoAdapter])
def test_statuses_do_not_leak_provider_message(cls, state):
    task = {"id": "job-1", "status": state, "error": {"message": "private-provider-message"}}
    if state != "failed":
        task.pop("error")
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200,
            json={"task": task} if cls is MiniMaxVideoAdapter else task))) as client:
        adapter = cls(endpoint="https://video.example.test", model_name="synthetic-model",
            credentials={"api_key": "synthetic-video-key"}, client=client)
        result = adapter.query("job-1")
        assert result.status == state and result.result_url is None
        assert "private-provider-message" not in repr(result)


@pytest.mark.parametrize("url", ["http://video.example.test", "https://name:password@video.example.test",
    "https://video.example.test?key=synthetic", "https://video.example.test#fragment", "https://video.example.test\\bad"])
def test_unsafe_urls_rejected(url):
    with pytest.raises((AdapterError, Slice1Error)):
        build_adapter("seedance", endpoint=url, model_name="synthetic-model", credentials={"api_key": "synthetic-video-key"})


def test_private_and_unresolved_dns_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises((AdapterError, Slice1Error)):
        build_adapter("minimax_video", endpoint="https://video.example.test", model_name="MiniMax-H3", credentials={"api_key": "synthetic-video-key"})
    def fail(*a, **k):
        raise socket.gaierror()
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises((AdapterError, Slice1Error)):
        build_adapter("minimax_video", endpoint="https://video.example.test", model_name="MiniMax-H3", credentials={"api_key": "synthetic-video-key"})


@pytest.mark.parametrize("state,action,expected", [("queued", "cancelled", True), ("queued", "deleted", False), ("running", None, False), ("succeeded", None, False)])
def test_minimax_only_queued_cancel_counts(state, action, expected):
    seen = []
    def respond(request):
        seen.append(request.method)
        if request.method == "GET":
            task = {"id": "job-1", "status": state, "content": {"url": "https://assets.example.test/video.mp4"}}
            return httpx.Response(200, json={"task": task})
        return httpx.Response(200, json={"action": action, "status": action})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        adapter = MiniMaxVideoAdapter(endpoint="https://video.example.test", model_name="MiniMax-H3", credentials={"api_key": "synthetic-video-key"}, client=client)
        assert adapter.cancel("job-1") is expected
        assert seen == (["GET", "DELETE"] if state == "queued" else ["GET"])


@pytest.mark.parametrize("payload,status,code", [({}, 401, "credential_invalid"), ({}, 429, "rate_limited"),
    ({"error": {"message": "private-provider-message"}}, 200, "capability_test_failed"),
    ({"id": "../escape"}, 200, "capability_test_failed"), ({}, 302, "capability_test_failed")])
def test_bad_response_never_retries_submit(payload, status, code):
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(status, json=payload)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        adapter = SeedanceVideoAdapter(endpoint="https://video.example.test", model_name="synthetic-model", credentials={"api_key": "synthetic-video-key"}, client=client)
        with pytest.raises(AdapterError) as exc:
            adapter.submit("Synthetic wave")
        assert exc.value.code == code and "private-provider-message" not in str(exc.value)
        assert len(seen) == 1


def test_request_timeout_does_not_retry_or_report_success():
    seen = []
    def respond(request):
        seen.append(request)
        raise httpx.ReadTimeout("private-provider-message", request=request)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        adapter = SeedanceVideoAdapter(endpoint="https://video.example.test", model_name="synthetic-model", credentials={"api_key": "synthetic-video-key"}, client=client)
        assert adapter.cancel("job-1") is False and not seen
        with pytest.raises(AdapterError) as exc:
            adapter.submit("Synthetic wave")
        assert exc.value.code == "timeout" and len(seen) == 1


def test_invalid_model_parameters_and_path_never_open_network():
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("must not send"))) as client:
        adapter = MiniMaxVideoAdapter(endpoint="https://video.example.test", model_name="MiniMax-H3-Max", credentials={"api_key": "synthetic-video-key"}, client=client)
        for options in ({"duration": 4}, {"resolution": "2K"}, {"duration": True}):
            with pytest.raises(AdapterError):
                adapter.submit("Synthetic wave", **options)
        with pytest.raises(AdapterError):
            adapter.query("../escape")
