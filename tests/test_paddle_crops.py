"""Provider crops, frozen originals and actual image delivery; synthetic sources."""
# ruff: noqa: F811
import io
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from backend.materials.attachments import validate_document
from backend.materials.document_pipeline import normalize_result, paddle_document_result
from backend.materials.errors import MaterialsError
from backend.materials.figure_crops import (
    crop_key,
    load_crop,
    matched_crop,
    persist_crops,
    png_crop,
    visual_crops,
)
from backend.materials.models import DocumentAttachmentRow
from backend.materials.question_extraction import (
    Extraction,
    FigureRole,
    compile_questions,
    extract_questions,
    validate_extraction,
)
from backend.materials.storage import StorageError
from backend.slice1.adapters.paddle_vl import DOCUMENT_OPTIONS, PaddleVLAdapter
from backend.slice1.errors import AdapterError
from backend.slice1.models import PADDLE_VL_ENDPOINT
from backend.teaching.assessment_sources import public_figure_source
from PIL import Image
from pydantic_ai.exceptions import ModelRetry
from test_document_attachments import env, pdf  # noqa: F401
from test_material_questions import candidate, state, verdict, window
from test_paddle_document_parser import activate, public_dns  # noqa: F401


def picture():
    output = io.BytesIO()
    Image.new("RGB", (120, 80), "#286c45").save(output, "PNG")
    return output.getvalue()


def layout():
    return {"prunedResult": {"width": 1000, "height": 1000,
        "doc_preprocessor_res": {"angle": 0, "model_settings": {"use_doc_unwarping": True}},
        "parsing_res_list": [
            {"block_label": "text", "block_content": "Synthetic source text."},
            {"block_label": "image", "block_content": "S Q",
             "block_bbox": [100, 200, 800, 900]}]},
        "markdown": {"images": {
            "imgs/img_in_image_box_100_200_800_900.jpg":
                "https://pplines-online.bj.bcebos.com/synthetic-crop?signature=synthetic-private"}}}


def test_exact_unique_crop_mapping_rejects_guessing_order_and_path_traversal():
    images = layout()["markdown"]["images"]
    assert matched_crop(images, [100, 200, 800, 900])
    assert matched_crop(images, [101, 200, 800, 900]) is None
    assert matched_crop({"../imgs/img_in_image_box_100_200_800_900.jpg": "private"},
                        [100, 200, 800, 900]) is None
    images["imgs/img_in_image_box_100_200_800_900.png"] = "duplicate"
    assert matched_crop(images, [100, 200, 800, 900]) is None


def test_crop_download_uses_pinned_object_storage_without_token_cookie_or_redirect(public_dns):
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, content=picture())
    with httpx.Client(transport=httpx.MockTransport(respond),
                      headers={"Cookie": "synthetic-private"}) as client:
        adapter = PaddleVLAdapter(endpoint=PADDLE_VL_ENDPOINT,
            credentials={"api_key": "synthetic-secret"}, client=client)
        assert adapter.download_crop(next(iter(layout()["markdown"]["images"].values()))) == picture()
    assert seen[0].headers.get("authorization") is None and seen[0].headers.get("cookie") is None
    assert seen[0].headers["host"] == "pplines-online.bj.bcebos.com"
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(302,
            headers={"location": "https://untrusted.synthetic/crop"}))) as client:
        adapter = PaddleVLAdapter(endpoint=PADDLE_VL_ENDPOINT, client=client)
        with pytest.raises(AdapterError):
            adapter.download_crop("https://pplines-online.bj.bcebos.com/synthetic")


def test_pipeline_persists_pixels_and_hash_not_signed_url_and_owner_can_read(env):
    config = activate(env)
    env.adapter.check_document = lambda _: paddle_document_result([layout()], requested_options=DOCUMENT_OPTIONS)
    calls = []
    env.adapter.download_crop = lambda url: (calls.append(url), picture())[1]
    item = env.service.create(env.owner, validate_document("synthetic.pdf", None, pdf()),
        config["config_id"], 1, "synthetic-crops")
    env.service.run(item["task_id"])
    assert env.service.view(item["material_id"], env.owner)["status"] == "parsed"
    with env.db.session() as session:
        row = session.get(DocumentAttachmentRow, item["material_id"])
        assert "signature" not in row.result_json and "bcebos.com" not in row.result_json
        saved = json.loads(row.result_json)
    crop = saved["page_results"][0]["blocks"][1]["crop"]
    assert crop["width"] == 120 and crop["height"] == 80 and len(calls) == 1
    data = env.service.image(item["material_id"], env.owner, 1, 1, crop["sha256"])
    assert data == picture()
    for owner, expected in [("synthetic-other", crop["sha256"]), (env.owner, "a" * 64)]:
        with pytest.raises(MaterialsError):
            env.service.image(item["material_id"], owner, 1, 1, expected)
    key = crop_key(item["material_id"], 1, 1, crop)
    env.storage.put(key, b"synthetic tampered bytes")
    with pytest.raises(MaterialsError):
        env.service.image(item["material_id"], env.owner, 1, 1, crop["sha256"])
    env.service.remove(item["material_id"], env.owner)
    with pytest.raises(MaterialsError):
        env.service.image(item["material_id"], env.owner, 1, 1)


@pytest.mark.parametrize("actual_crop,seen,reviewed,role,expected", [
    (True, True, True, "input", True), (True, False, True, "input", False),
    (True, True, False, "input", False), (False, True, True, "input", False),
    (True, True, True, "mixed", False),
])
def test_processed_original_requires_actual_crop_input_and_independent_review(
        actual_crop, seen, reviewed, role, expected):
    target = window()
    block = next(b for b in target["source_blocks"] if b["block_id"] == "p2/b1")
    block["coordinate_space"] = "processed"
    if actual_crop:
        block["crop"] = png_crop(picture())[1]
    target["visual_pages"] = [2]
    target["visual_figures"] = ["p2/b1"] if seen else []
    question = candidate(target)
    question.questions[0].needs_figure = True
    question.questions[0].figure_roles = [FigureRole(block_id="p2/b1", role=role)]
    result = compile_questions(question, verdict(figures_complete=reviewed), target,
                               state()["study_index"]["sections"])[0]
    assert result["assessment_eligible"] is expected


def test_unclear_ocr_escalates_to_page_and_crop_for_both_roles(env, monkeypatch):
    source = state()
    block = source["page_results"][1]["blocks"][1]
    data, crop = png_crop(picture())
    block.update(crop=crop, coordinate_space="processed")
    env.storage.put(crop_key("synthetic-material", 2, 1, crop), data)
    monkeypatch.setattr("backend.materials.question_extraction.render_pages",
        lambda path, pages, guard: [(n, b"synthetic-original-page") for n in pages])
    calls = []
    def invoke(cls, prompt, payload, images, validator, name):
        calls.append((cls.__name__, deepcopy(images), payload.get("visual_figures")))
        if cls is Extraction:
            output = candidate(payload)
            output.visual_review_pages = [2]
            output.questions[0].needs_figure = True
            output.questions[0].figure_roles = [FigureRole(block_id="p2/b1", role="input")]
            return validator(output)
        return validator(verdict())
    bank = extract_questions(source, invoke=invoke, prompts=["extract", "review"],
        checkpoint=lambda _: None, guard=lambda: None, source="synthetic.pdf", visual=True,
        crop_loader=lambda target, guard: visual_crops(env.storage, "synthetic-material", target, guard))
    assert [v[0] for v in calls] == ["Extraction", "Extraction", "Review"]
    assert not calls[0][1]
    assert all(("p2/b1", data) in v[1] and v[2] == ["p2/b1"] for v in calls[1:])
    assert bank["questions"][0]["assessment_eligible"]
    assert bank["questions"][0]["figure_crops"]["p2/b1"] == crop["sha256"]
    assert load_crop(env.storage, "synthetic-material", 2, 1, crop) == data


def test_visual_request_cannot_reference_another_page():
    output = candidate()
    output.visual_review_pages = [999]
    with pytest.raises(ModelRetry):
        validate_extraction(output, window())


def test_unclear_text_without_authorized_visual_confirmation_cannot_be_original():
    output = candidate()
    output.visual_review_pages = [1]
    target = window()
    result = compile_questions(output, verdict(), target, state()["study_index"]["sections"])[0]
    assert not result["assessment_eligible"] and result["status"] == "needs_review"


def test_invalid_image_is_not_published_as_an_available_crop(env):
    result = normalize_result(paddle_document_result([layout()], requested_options=DOCUMENT_OPTIONS))
    adapter = SimpleNamespace(download_crop=lambda _: b"synthetic-not-an-image")
    persist_crops(result, env.storage, "synthetic-material", adapter, lambda: None)
    figure = result["page_results"][0]["blocks"][1]
    assert "crop" not in figure and "provider_crop_url" not in figure
    assert figure["crop_status"] == "unavailable"
    with pytest.raises(StorageError):
        crop_key("../foreign", 1, 1, {"sha256": "a" * 64})


def test_assessment_image_source_never_exposes_review_notes_or_answers():
    source = public_figure_source({"material_id": "synthetic", "block_no": 2, "page_no": 1,
        "crop_sha256": "a" * 64, "description": "Synthetic secret answer", "caption": "Private source text",
        "question_id": "private", "content_status": "visual_reviewed"})
    assert source == {"material_id": "synthetic", "block_no": 2, "page_no": 1, "crop_sha256": "a" * 64}


def test_bound_multimodal_role_receives_real_png_bytes_for_extraction_and_review(env, monkeypatch):
    from backend.materials.question_extraction import Review
    from pydantic_ai import BinaryContent
    from test_material_questions import content_model
    config = activate(env)
    semantic, visual = content_model(env), content_model(env)
    env.adapter.check_document = lambda _: paddle_document_result([layout()], requested_options=DOCUMENT_OPTIONS)
    env.adapter.download_crop = lambda _: picture()
    targets = {name: {"config_id": model["config_id"], "config_version": 1}
               for name, model in (("semantic", semantic), ("visual", visual))}
    item = env.service.create(env.owner, validate_document("synthetic.pdf", None, pdf()),
        config["config_id"], 1, "synthetic-multimodal-crop", extraction=targets)
    seen = []
    def fake(descriptor, cls, messages, **kwargs):
        deps = kwargs["deps"]
        deps.check()
        deps.budget.consume("logical")
        deps.budget.consume("wire")
        history = kwargs["message_history"]
        if history:
            content = history[0].parts[0].content
            binaries = [part for part in content if isinstance(part, BinaryContent)]
            assert any(part.data == picture() and part.media_type == "image/png" for part in binaries)
            assert any(part.media_type == "image/jpeg" for part in binaries)
            assert "Source figure p1/b1" in content
            assert descriptor.config_id == visual["config_id"]
        seen.append((cls.__name__, bool(history)))
        output = Extraction(questions=[], visual_review_pages=[1]) if cls is Extraction else Review(verdicts=[])
        return SimpleNamespace(payload=kwargs["output_validator"](None, output))
    monkeypatch.setattr("backend.agents.runner.run_structured_agent", fake)
    env.service.run(item["task_id"])
    assert env.service.view(item["material_id"], env.owner)["status"] == "parsed"
    assert seen == [("Extraction", False), ("Extraction", True), ("Review", True)]
