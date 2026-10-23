"""Private adapter authentication, async result contract and media options."""
import importlib.util
from pathlib import Path
import threading
import urllib.error
import urllib.request
import json

import pytest


@pytest.fixture
def adapter(monkeypatch):
    spec = importlib.util.spec_from_file_location("source_video_adapter",
        Path(__file__).resolve().parents[1] / "server/source-worker/service.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "KEY", "synthetic-video-worker-key-0000000000")
    def extract(ident, body):
        with module.LOCK:
            module.JOBS[ident].update(status="ready", result={"segments": [
                {"start": 0, "end": 1, "text": "Synthetic video transcript"}], "frames": []})
    monkeypatch.setattr(module, "execute", extract)
    server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def post(path, payload, key=module.KEY):
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}" + path,
            json.dumps(payload).encode(), {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.headers["Cache-Control"] == "no-store"
            return response.status, json.loads(response.read())
    yield module, post
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_adapter_authentication_health_and_async_result(adapter):
    module, post = adapter
    with pytest.raises(urllib.error.HTTPError) as denied:
        post("/v1/health", {}, key="synthetic-wrong-key")
    assert denied.value.code == 401
    assert post("/v1/health", {})[1]["connection"] == "available"
    status, submitted = post("/v1/extract", {"url": "https://example.org/synthetic-video",
        "options": {"interval": 6, "max_frames": 12, "asr_model": "small"}})
    assert status == 202
    for _ in range(20):
        value = post("/v1/jobs/" + submitted["job_id"], {})[1]
        if value["status"] == "ready":
            break
    assert value["result"]["segments"][0]["text"] == "Synthetic video transcript"
    assert post("/v1/jobs/" + submitted["job_id"] + "/cancel", {})[1] == {"status": "failed"}


@pytest.mark.parametrize("payload", [
    {"url": "file:///synthetic", "options": {}},
    {"url": "https://example.org/synthetic", "local_path": "/synthetic", "options": {}},
    {"url": "https://example.org/synthetic", "options": {"max_frames": 601}},
    {"url": "https://example.org/synthetic", "options": {"asr_model": "../../synthetic"}},
])
def test_adapter_rejects_arbitrary_paths_and_unbounded_options(adapter, payload):
    with pytest.raises(urllib.error.HTTPError) as denied:
        adapter[1]("/v1/extract", payload)
    assert denied.value.code == 400
