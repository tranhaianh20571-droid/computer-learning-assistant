"""Synthetic official document wire, chart provenance and private image contracts."""
import io
import json
import socket

import httpx
import pytest
from PIL import Image

from test_document_attachments import env, pdf, create
from backend.materials.document_pipeline import (
    paddle_document_result, normalize_result, plan_batches, document_markdown)
from backend.materials.study_sources import build_study_index
from backend.materials.errors import MaterialsError
from backend.slice1.adapters.paddle_vl import PaddleVLAdapter
from backend.slice1.errors import AdapterError, Slice1Error
from backend.slice1.models import PADDLE_VL_ENDPOINT, PADDLE_VL_MODEL


def layout():
    return {"prunedResult": {"width": 1000, "height": 1000, "parsing_res_list": [
        {"block_label": "doc_title", "block_content": "Synthetic chapter"},
        {"block_label": "text", "block_content": "Synthetic ordinary source paragraph."},
        {"block_label": "chart", "block_content": "X | Y\n1 | 2",
         "block_bbox": [100, 100, 700, 700]},
        {"block_label": "image", "block_content": "", "block_bbox": [50, 50, 200, 200]},
        {"block_label": "display_formula", "block_content": r"x^2 + y^2"},
    ]}}


def activate(env):
    return env.configs.create(actor_id=env.owner, is_admin=False, kind="document_parser",
        protocol="paddleocr_vl", endpoint=PADDLE_VL_ENDPOINT, model_name=PADDLE_VL_MODEL,
        credentials={"api_key": "synthetic-paddle-key"})


def test_paddle_configuration_preserves_legacy_destination_and_owner(env):
    old = create(env)
    config = activate(env)
    assert env.service.config(env.owner)["config_id"] == config["config_id"]
    assert env.configs.get(env.config["config_id"], actor_id=env.owner, is_admin=False)["protocol"] == "somark"
    assert env.configs.resolve_document_credentials(config["config_id"],
        owner_user_id=env.owner, expected_version=1)["api_key"] == "synthetic-paddle-key"
    with pytest.raises(Slice1Error):
        env.configs.resolve_document_credentials(config["config_id"],
            owner_user_id="synthetic-other", expected_version=1)
    with pytest.raises(Slice1Error):
        env.configs.update(config["config_id"], actor_id=env.owner, is_admin=False, model_name="other")
    env.service.run(old["task_id"])
    assert env.service.view(old["material_id"], env.owner)["status"] == "parsed"


@pytest.mark.parametrize("endpoint,model", [
    ("https://synthetic.example/jobs", PADDLE_VL_MODEL), (PADDLE_VL_ENDPOINT, "other")])
def test_paddle_config_rejects_destination_changes(env, endpoint, model):
    with pytest.raises(Slice1Error):
        env.configs.create(actor_id=env.owner, is_admin=False, kind="document_parser",
            protocol="paddleocr_vl", endpoint=endpoint, model_name=model,
            credentials={"api_key": "synthetic-key"})


def test_charts_stay_auxiliary_and_cannot_enter_verbatim_source_index():
    state = normalize_result(paddle_document_result([layout()]), expected_pages=1)
    assert any(b.get("chart_conversion") for b in state["page_results"][0]["blocks"])
    chart = next(c for c in state["chunks"] if c["kind"] == "image_understanding")
    assert "非逐字原文" in chart["text"]
    assert not any("X" in c["text"] for c in state["chunks"] if c["kind"] == "text")
    index = build_study_index(state)
    assert "chart_conversion" not in json.dumps(index)
    from backend.materials.study_sources import block_text
    block = next(b for b in state["page_results"][0]["blocks"] if b.get("chart_conversion"))
    assert block_text(block) == ""
    assert "图表转换" in document_markdown(state)
    with pytest.raises(ValueError):
        normalize_result(paddle_document_result([layout()]), expected_pages=2)


def test_pdf_crops_are_local_owned_and_revoked_after_deletion(env):
    config = activate(env)
    env.adapter.check_document = lambda _: paddle_document_result([layout()])
    from backend.materials.attachments import validate_document
    item = env.service.create(env.owner, validate_document("synthetic.pdf", None, pdf()),
        config["config_id"], 1, "synthetic-paddle-upload")
    env.service.run(item["task_id"])
    assert env.service.view(item["material_id"], env.owner)["status"] == "parsed"
    crop = env.service.image(item["material_id"], env.owner, 1, 2)
    with Image.open(io.BytesIO(crop)) as image:
        assert image.width > 100 and image.height > 100
    md = env.service.markdown(item["material_id"], env.owner)
    assert "/images/1/2" in md and "非逐字原文" in md
    for owner, page, block in [("synthetic-other",1,2), (env.owner,1,-1), (env.owner,2,2)]:
        with pytest.raises(MaterialsError):
            env.service.image(item["material_id"], owner, page, block)
    env.service.remove(item["material_id"], env.owner)
    with pytest.raises(MaterialsError):
        env.service.image(item["material_id"], env.owner, 1, 2)


def test_paddle_smaller_batch_limit_is_used(env, monkeypatch):
    activate(env)
    validated = type("SyntheticFile", (), {"page_count": 2, "size_bytes": 46_000_000, "kind": "pdf"})()
    assert env.service.preflight(validated, env.owner)["strategy"] == "batched"
    from backend.materials import document_pipeline as pipeline
    monkeypatch.setattr(pipeline, "pdf_batch_bytes", lambda source, start, end: b"x" * (30 if start == end else 60))
    state = plan_batches(b"synthetic", kind="pdf", pages=2, size=60, target_bytes=45, provider_bytes=50)
    assert [(b["start"], b["end"]) for b in state["batches"]] == [(1,1),(2,2)]
    with pytest.raises(ValueError, match="single_page"):
        plan_batches(b"synthetic", kind="pdf", pages=1, size=60, target_bytes=20, provider_bytes=25)


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8",443))])


@pytest.mark.parametrize("result_host", ["paddleocr-store-1.bj.bcebos.com", "paddleocr-store.bj.bcebos.com"])
def test_official_wire_enables_charts_and_download_never_gets_credentials(public_dns, result_host):
    seen = []
    def respond(request):
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"data": {"jobId": "synthetic-job"}},
                headers={"set-cookie": "synthetic_session=private; Path=/"})
        if request.headers.get("host") == "paddleocr.aistudio-app.com":
            return httpx.Response(200, json={"data": {"state": "done", "resultUrl": {
                "jsonUrl": f"https://{result_host}/synthetic.jsonl"}}})
        return httpx.Response(200, text=json.dumps({"result": {"layoutParsingResults": [layout()]}}))
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        adapter = PaddleVLAdapter(endpoint=PADDLE_VL_ENDPOINT, model_name=PADDLE_VL_MODEL,
            credentials={"api_key": "synthetic-secret"}, client=client)
        assert adapter.submit_document(b"%PDF-synthetic", "pdf") == "synthetic-job"
        output = adapter.check_document("synthetic-job")
    assert b'"useChartRecognition": true' in seen[0].content
    for feature in ("useDocOrientationClassify", "useDocUnwarping", "useSealRecognition",
                    "useOcrForImageBlock", "useLayoutDetection"):
        assert ('"' + feature + '": true').encode() in seen[0].content
    assert PADDLE_VL_MODEL.encode() in seen[0].content
    assert seen[0].headers["authorization"] == "bearer synthetic-secret"
    assert seen[-1].headers.get("authorization") is None
    assert seen[-1].headers.get("cookie") is None
    assert seen[-1].url.host == "8.8.8.8"
    assert seen[-1].extensions["sni_hostname"] == result_host
    assert normalize_result(output, expected_pages=1)["pages"] == 1


@pytest.mark.parametrize("status,code", [(401,"credential_invalid"),(429,"rate_limited"),(500,"capability_test_failed")])
def test_wire_errors_are_controlled(public_dns, status, code):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status, text="synthetic-private"))) as client:
        adapter = PaddleVLAdapter(endpoint=PADDLE_VL_ENDPOINT, client=client)
        with pytest.raises(AdapterError) as caught:
            adapter.submit_document(b"synthetic", "pdf")
        assert caught.value.code == code and "private" not in str(caught.value)


def test_probe_is_nonmutating_authenticated_404(public_dns):
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(404)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        adapter = PaddleVLAdapter(endpoint=PADDLE_VL_ENDPOINT, client=client)
        assert adapter.probe("connection").state == "available"
    assert len(seen) == 1 and seen[0].method == "GET"


def test_provider_failed_job_can_be_explicitly_resubmitted(env):
    config = activate(env)
    env.adapter.check_document = lambda _: {"status": "FAILED"}
    from backend.materials.attachments import validate_document
    item = env.service.create(env.owner, validate_document("synthetic.pdf", None, pdf()),
        config["config_id"], 1, "synthetic-paddle-failure")
    env.service.run(item["task_id"])
    assert env.service.view(item["material_id"], env.owner)["status"] == "failed"
    retry = env.service.retry(item["material_id"], env.owner, config["config_id"], 1)
    env.adapter.check_document = lambda _: paddle_document_result([layout()])
    env.service.run(retry["task_id"])
    assert len(env.adapter.calls) == 3  # Failed primary, explicit retry, then one missing-chart crop pass.
    assert env.service.view(item["material_id"], env.owner)["status"] == "parsed"


@pytest.mark.parametrize("url", ["http://paddleocr-store-1.bj.bcebos.com/a",
    "https://127.0.0.1/a", "https://synthetic.example/a",
    "https://paddleocr-store.gz.bcebos.com/a",
    "https://paddleocr-store.bj.bcebos.com.evil.example/a",
    "https://paddleocr-store.bj.bcebos.com:8443/a",
    "https://paddleocr-store-1.bj.bcebos.com.evil.example/a"])
def test_result_destination_is_restricted(public_dns, url):
    adapter = PaddleVLAdapter(endpoint=PADDLE_VL_ENDPOINT)
    with pytest.raises(AdapterError):
        adapter._wire("GET", url, authenticated=False)
    adapter.close()


def test_private_dns_and_oversized_response_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a: [(2,1,6,"",("127.0.0.1",443))])
    adapter = PaddleVLAdapter(endpoint=PADDLE_VL_ENDPOINT)
    with pytest.raises(AdapterError, match="invalid_endpoint"):
        adapter.submit_document(b"synthetic", "pdf")
    adapter.close()
