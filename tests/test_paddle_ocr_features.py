"""Synthetic feature options, provenance, geometry and resumable OCR snapshots."""
# ruff: noqa: F811
import json
from copy import deepcopy
from threading import Event

import pytest
from backend.materials.attachments import validate_document
from backend.materials.document_pipeline import (
    document_markdown,
    normalize_result,
    paddle_document_result,
)
from backend.materials.models import DocumentAttachmentRow
from backend.materials.study_sources import block_text, build_study_index
from backend.slice1.adapters.paddle_vl import (
    DOCUMENT_OPTIONS,
    LEGACY_DOCUMENT_OPTIONS,
    document_options,
)
from test_document_attachments import env, pdf  # noqa: F401
from test_paddle_document_parser import activate


def layout():
    return {"prunedResult": {"width": 1000, "height": 1000, "model_settings": {
        "use_doc_preprocessor": True, "use_seal_recognition": True, "use_ocr_for_image_block": True},
        "doc_preprocessor_res": {"angle": 0, "model_settings": {"use_doc_unwarping": True}},
        "parsing_res_list": [
            {"block_label": "doc_title", "block_content": "Synthetic source"},
            {"block_label": "image", "block_content": "【例题1】Explain the synthetic inputs?",
             "block_bbox": [100, 200, 800, 900]},
            {"block_label": "seal", "block_content": "【例题99】Synthetic seal",
             "block_bbox": [0, 0, 100, 100]}]}}


def test_image_ocr_and_seals_survive_but_have_separate_source_meanings():
    result = normalize_result(paddle_document_result([layout()], requested_options=DOCUMENT_OPTIONS))
    page = result["page_results"][0]
    figure, seal = page["blocks"][1:]
    assert figure["source_kind"] == "image_ocr" and "例题1" in figure["content"]
    assert figure["image_ocr"][0]["text"] == figure["content"]
    assert seal["source_kind"] == "seal" and "seal" in seal["content"]
    assert block_text(seal) == ""
    index = build_study_index(result)
    assert all("seal" not in q["prompt"] for q in index["questions"])
    assert any(c["kind"] == "image_ocr" for c in result["chunks"])
    assert any(c["kind"] == "seal" for c in result["chunks"])
    exported = document_markdown(result)
    assert "印章识别" in exported and "整页原图" in exported


def test_transformed_coordinates_are_preserved_without_becoming_original_crop_coordinates():
    page = normalize_result(paddle_document_result([layout()],
        requested_options=DOCUMENT_OPTIONS))["page_results"][0]
    figure = page["blocks"][1]
    assert figure["processed_bbox"] == [.1, .2, .8, .9]
    assert "source_bbox" not in figure and figure["coordinate_space"] == "processed"
    assert "processed_coordinates_unmapped" in page["review_reasons"]
    assert page["preprocessing"]["reported"]["use_doc_unwarping"] is True
    assert page["preprocessing"]["requested"] == DOCUMENT_OPTIONS


@pytest.mark.parametrize("reported,angle,processed", [(True, 0, True), (False, 0, False), (False, 90, True)])
def test_confirmed_unchanged_page_can_keep_original_coordinates(reported, angle, processed):
    item = layout()
    item["prunedResult"]["doc_preprocessor_res"].update(
        angle=angle, model_settings={"use_doc_unwarping": reported})
    figure = paddle_document_result([item], requested_options=DOCUMENT_OPTIONS)[
        "result"]["outputs"]["json"]["pages"][0]["blocks"][1]
    assert ("processed_bbox" in figure) is processed
    assert ("source_bbox" in figure) is not processed


def test_duplicate_image_text_requires_matching_location_and_keeps_raw_evidence():
    item = layout()
    rows = item["prunedResult"]["parsing_res_list"]
    rows.append({**rows[1], "block_label": "text"})
    result = normalize_result(paddle_document_result([item], requested_options=DOCUMENT_OPTIONS))
    figure = result["page_results"][0]["blocks"][1]
    assert figure["ocr_duplicate_of"] == 3 and figure["image_ocr"]
    assert sum("例题1" in c["text"] for c in result["chunks"]) == 1
    moved = deepcopy(item)
    moved["prunedResult"]["parsing_res_list"][-1]["block_bbox"] = [0, 0, 50, 50]
    assert "ocr_duplicate_of" not in paddle_document_result([moved], requested_options=DOCUMENT_OPTIONS)[
        "result"]["outputs"]["json"]["pages"][0]["blocks"][1]


def test_missing_report_is_unknown_not_a_claim_of_effective_feature_activation():
    item = layout()
    item["prunedResult"].pop("model_settings")
    item["prunedResult"].pop("doc_preprocessor_res")
    page = normalize_result(paddle_document_result([item],
        requested_options=DOCUMENT_OPTIONS))["page_results"][0]
    assert page["preprocessing"]["settings_confirmed"] is False
    assert page["preprocessing"]["coordinate_space"] == "processed"


@pytest.mark.parametrize("image_first", [True, False])
def test_overlapping_question_labels_have_one_anchor_with_both_locations(image_first):
    from backend.materials.question_extraction import blocks_and_anchors
    item = layout()
    image = item["prunedResult"]["parsing_res_list"][1]
    label = {"block_label": "text", "block_content": "【例题1】",
             "block_bbox": [100, 200, 800, 240]}
    item["prunedResult"]["parsing_res_list"] = [image, label] if image_first else [label, image]
    state = normalize_result(paddle_document_result([item], requested_options=DOCUMENT_OPTIONS))
    blocks, anchors = blocks_and_anchors(state)
    assert len(anchors) == 1 and len(anchors[0]["alternate_locations"]) == 1
    assert blocks[0]["coordinate_space"] == "processed" and blocks[0]["bbox"]
    item["prunedResult"]["parsing_res_list"][-1]["block_bbox"] = [0, 0, 50, 50]
    moved = normalize_result(paddle_document_result([item], requested_options=DOCUMENT_OPTIONS))
    assert len(blocks_and_anchors(moved)[1]) == 2


def test_transformed_figure_cannot_be_a_complete_original_even_with_model_approval():
    from backend.materials.question_extraction import FigureRole, compile_questions
    from test_material_questions import candidate, state, verdict, window
    target = window()
    next(b for b in target["source_blocks"] if b["block_id"] == "p2/b1")["coordinate_space"] = "processed"
    target["visual_pages"] = [2]
    question = candidate(target)
    question.questions[0].needs_figure = True
    question.questions[0].figure_roles = [FigureRole(block_id="p2/b1", role="input")]
    out = compile_questions(question, verdict(), target, state()["study_index"]["sections"])[0]
    assert not out["assessment_eligible"] and not out["figures_complete"]


@pytest.mark.parametrize("role,eligible", [("unknown", True), ("mixed", True), ("input", False)])
def test_self_contained_text_task_does_not_display_or_require_incidental_figures(role, eligible):
    from backend.materials.question_extraction import FigureRole, compile_questions
    from test_material_questions import candidate, state, verdict, window
    target = window()
    next(b for b in target["source_blocks"] if b["block_id"] == "p2/b1")["coordinate_space"] = "processed"
    question = candidate(target)
    question.questions[0].figure_roles = [FigureRole(block_id="p2/b1", role=role)]
    out = compile_questions(question, verdict(), target, state()["study_index"]["sections"])[0]
    assert out["assessment_eligible"] is eligible
    rejected = compile_questions(question, verdict(figures_complete=False), target,
        state()["study_index"]["sections"])[0]
    assert not rejected["assessment_eligible"]


def test_new_task_pins_all_features_before_worker_and_retry_preserves_snapshot(env):
    config = activate(env)
    item = env.service.create(env.owner, validate_document("synthetic.pdf", None, pdf()),
        config["config_id"], 1, "synthetic-feature-snapshot")
    with env.db.session() as session:
        row = session.get(DocumentAttachmentRow, item["material_id"])
        state = json.loads(row.result_json)
        assert state["ocr_options"] == DOCUMENT_OPTIONS
        state["ocr_options"]["useSealRecognition"] = False
        row.result_json = json.dumps(state)
    env.adapter.check_document = lambda _: {"status": "FAILED"}
    env.service.run(item["task_id"])
    retried = env.service.retry(item["material_id"], env.owner, config["config_id"], 1)
    env.adapter.check_document = lambda _: paddle_document_result([layout()], requested_options={
        **DOCUMENT_OPTIONS, "useSealRecognition": False})
    env.service.run(retried["task_id"])
    assert env.adapter.document_options["useSealRecognition"] is False
    assert env.service.view(item["material_id"], env.owner)["status"] == "parsed"


def test_started_legacy_job_keeps_original_options(env):
    state = {"pages": 1, "strategy": "direct", "batches": [
        {"start": 1, "end": 1, "status": "processing", "job_id": "synthetic-legacy-job"}],
        "page_results": [], "chunks": [], "outline": []}
    output = env.service.process_document(None, "pdf", state, adapter=env.adapter,
        protocol="paddleocr_vl", guard=lambda: None, checkpoint=lambda _: None, stop=Event())
    assert output["ocr_options"] == LEGACY_DOCUMENT_OPTIONS
    assert env.adapter.document_options == LEGACY_DOCUMENT_OPTIONS and not env.adapter.calls


def test_unmapped_figure_view_returns_full_original_page_with_owner_guard(env):
    import io

    from backend.materials.errors import MaterialsError
    from PIL import Image
    config = activate(env)
    env.adapter.check_document = lambda _: paddle_document_result([layout()], requested_options=DOCUMENT_OPTIONS)
    item = env.service.create(env.owner, validate_document("synthetic.pdf", None, pdf()),
        config["config_id"], 1, "synthetic-whole-page-view")
    env.service.run(item["task_id"])
    with Image.open(io.BytesIO(env.service.image(item["material_id"], env.owner, 1, 1))) as image:
        assert image.height > image.width > 1000
    with pytest.raises(MaterialsError):
        env.service.image(item["material_id"], "synthetic-other", 1, 1)


@pytest.mark.parametrize("value", [{}, {"useDocUnwarping": "true"}, {"unexpected": True}])
def test_invalid_feature_options_rejected(value):
    with pytest.raises(ValueError):
        document_options(value)
