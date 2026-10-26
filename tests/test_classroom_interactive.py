"""Synthetic original documents and private classroom state; no real suppliers."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from pydantic_ai.exceptions import ModelRetry
from sqlalchemy import select

from backend.materials.models import MaterialRow
from backend.materials.storage import MaterialStorage
from backend.materials.study_sources import chapter_catalog, documents, freeze_sources
from backend.planning.models import ConversationRow, OutlineVersionRow
from backend.teaching.interactive import Cell, StateIn, bind_source, classroom_state, pdf_page_targets
from backend.teaching.models import BoardSceneRow, TeachingStepRow
from backend.teaching.outputs import BoardDeclaration, declaration_to_scene
from backend.teaching.errors import TeachingError
from test_teaching_assessments import database, env, lesson  # noqa: F401
from test_study_sources import attachment
from test_document_attachments import pdf


def ready(env):
    _, state, _, _ = env
    _, _, started = lesson(env)
    assert state.teaching.run_step_generation(started["task_id"])["status"] == "succeeded"
    ident = started["conversation_id"]
    with state.db.session() as sess:
        step = sess.scalar(select(TeachingStepRow).where(TeachingStepRow.conversation_id == ident))
        board = sess.get(BoardSceneRow, step.board_scene_id)
        scene = json.loads(board.scene_json)
        scene["presentation"]["blocks"] = [{"id": "example", "type": "code", "language": "python",
            "code": "value = 2\nprint(value * 3)", "interaction": True}]
        board.scene_json = json.dumps(scene)
    return ident


def body():
    return {"revision": 0, "playback_version": 0, "cells": [{"step_no": 1,
        "block_id": "example", "original_code": "value = 2\nprint(value * 3)",
        "code": "value = 10\nprint(value * 3)", "output": "30\n", "status": "success", "runs": 1}]}


def test_private_state_roundtrip_revision_and_exact_original(env):
    client, state, owner, headers = env
    ident = ready(env)
    url = f"/api/teaching/{ident}/interactive"
    assert client.get(url, headers=headers).json()["cells"] == []
    response = client.put(url, headers=headers, json=body())
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert client.get(url, headers=headers).json()["cells"][0]["output"] == "30\n"
    assert client.put(url, headers=headers, json=body()).status_code == 409
    invalid = body()
    invalid["revision"] = 1
    invalid["cells"][0]["original_code"] = "wrong source"
    assert client.put(url, headers=headers, json=invalid).status_code == 409
    assert classroom_state(state.teaching, ident, owner)["revision"] == 1


def test_deleted_cross_owner_and_playback_revision_are_rejected(env):
    client, state, owner, headers = env
    ident = ready(env)
    with pytest.raises(TeachingError):
        classroom_state(state.teaching, ident, "other-synthetic-owner")
    invalid = body()
    invalid["playback_version"] = 9
    assert client.put(f"/api/teaching/{ident}/interactive", headers=headers, json=invalid).status_code == 409
    with state.db.session() as sess:
        from backend.planning.models import utcnow
        sess.get(ConversationRow, ident).deleted_at = utcnow()
    assert client.get(f"/api/teaching/{ident}/interactive", headers=headers).status_code == 404


def test_concurrent_saves_do_not_overwrite_newer_revision(env):
    ident = ready(env)
    service, owner = env[1].teaching, env[2]
    def save(_):
        try:
            return classroom_state(service, ident, owner, StateIn.model_validate(body()))["revision"]
        except TeachingError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(map(str, pool.map(save, range(2)))) == ["1", "CHECKPOINT_STALE"]


def test_followup_code_is_bound_to_its_published_generation(env):
    client, state, _, headers = env
    ident = ready(env)
    value = body()
    cell = value["cells"][0]
    cell.update(step_no=0, generation=4)
    with state.db.session() as sess:
        sess.get(ConversationRow, ident).generation = 4
        sess.add(BoardSceneRow(scene_id="synthetic-followup-" + ident, conversation_id=ident,
            generation=4, step_no=0, scene_version_no=1, declaration_json="{}",
            scene_json=json.dumps({"presentation": {"blocks": [{"id": cell["block_id"], "type": "code",
                "code": cell["original_code"], "language": "python"}]}})))
    url = f"/api/teaching/{ident}/interactive"
    assert client.put(url, headers=headers, json=value).status_code == 200
    value["revision"] = 1
    cell["generation"] = 5
    assert client.put(url, headers=headers, json=value).status_code == 409


@pytest.mark.parametrize("invalid", ["duplicate", "unknown", "oversize", "extra"])
def test_state_rejects_invalid_cells(env, invalid):
    client, _, _, headers = env
    ident = ready(env)
    value = body()
    if invalid == "duplicate":
        value["cells"].append(deepcopy(value["cells"][0]))
    if invalid == "unknown":
        value["cells"][0]["block_id"] = "nonexistent"
    if invalid == "oversize":
        value["cells"][0]["output"] = "x" * 32769
    if invalid == "extra":
        value["cells"][0]["token"] = "synthetic-disallowed-field"
    assert client.put(f"/api/teaching/{ident}/interactive", headers=headers, json=value).status_code in (400, 409, 422)


def test_pdf_targets_from_original_and_unresolvable_focus(tmp_path):
    path = tmp_path / "synthetic.pdf"
    path.write_bytes(pdf(2))
    targets = pdf_page_targets(path, "synthetic-document", "a" * 64, 1,
        [{"type": "figure", "source_bbox": [.1, .2, .4, .5]},
         {"type": "figure", "source_bbox": [0, 0, float("nan"), 1]}])
    target = next(item for item in targets if item["precision"] == "text")
    assert "Synthetic database attachment 1" in target["text"]
    assert 0 <= target["bbox"][0] < target["bbox"][2] <= 1
    assert 0 <= target["bbox"][1] < target["bbox"][3] <= 1
    assert len([item for item in targets if item["precision"] == "ocr_region"]) == 1
    board = BoardDeclaration(title="Synthetic", source_focus={"material_id": "synthetic-document",
                                                              "target_id": target["target_id"]})
    bind_source(board, targets)
    assert declaration_to_scene(board)["pdf_focus"] == target
    with pytest.raises(ModelRetry):
        bind_source(board, [])


def test_original_download_requires_frozen_owner_hash_and_real_bytes(env, tmp_path):
    client, state, owner, headers = env
    ident = ready(env)
    payload = pdf()
    sha = hashlib.sha256(payload).hexdigest()
    material = attachment(state.db, owner)
    state.teaching.storage = MaterialStorage(tmp_path)
    state.teaching.storage.put(MaterialStorage.original_key(material), payload)
    with state.db.session() as sess:
        sess.get(MaterialRow, material).source_bytes_sha256 = sha
        conv = sess.get(ConversationRow, ident)
        docs = documents(sess, owner, [material])
        chapter = chapter_catalog(docs)[0]["id"]
        frozen = freeze_sources(sess, owner, [material], [(conv.lesson_id,
            {"source_section_ids": [chapter], "teaching_mode": "source_reading"})])
        assert frozen["reading_lessons"] == [conv.lesson_id]
        sess.get(OutlineVersionRow, conv.outline_version_id).source_snapshot_json = json.dumps(frozen)
    url = f"/api/teaching/{ident}/sources/{material}/{sha}/original"
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    assert response.content == payload and response.headers["cache-control"] == "no-store"
    download = client.get(url + "/download", headers=headers)
    assert download.content == payload
    assert download.headers["content-disposition"].startswith("attachment;")
    assert client.get(url.replace(sha, "f" * 64), headers=headers).status_code == 404
    state.teaching.storage.put(MaterialStorage.original_key(material), b"synthetic tampering")
    assert client.get(url, headers=headers).status_code == 409


def test_classroom_crop_route_binds_frozen_question_hash_and_rejects_answer_figures(env, tmp_path):
    from backend.materials.figure_crops import crop_key, png_crop
    from backend.materials.models import DocumentAttachmentRow
    from backend.teaching.interactive import question_targets
    from backend.teaching.assessment_sources import figure_catalog
    from test_material_question_consumption import bank_state
    from test_paddle_crops import picture
    from types import SimpleNamespace
    client, state, owner, headers = env
    ident = ready(env)
    data = bank_state(1)
    payload, crop = png_crop(picture())
    block = data["page_results"][0]["blocks"][2]
    block.pop("source_bbox")
    block.update(coordinate_space="processed", crop=crop, block_id="p1/b2")
    question = data["question_bank"]["questions"][0]
    question.update(needs_figure=True, figure_roles=[{"block_id": "p1/b2", "role": "input"}],
                    figure_crops={"p1/b2": crop["sha256"]}, review={"reason": "Synthetic reviewed inputs."})
    material = attachment(state.db, owner, data)
    original = pdf()
    sha = hashlib.sha256(original).hexdigest()
    state.teaching.storage = MaterialStorage(tmp_path)
    state.teaching.storage.put(MaterialStorage.original_key(material), original)
    state.teaching.storage.put(crop_key(material, 1, 2, crop), payload)
    with state.db.session() as session:
        session.get(MaterialRow, material).source_bytes_sha256 = sha
        conv = session.get(ConversationRow, ident)
        chapter = chapter_catalog(documents(session, owner, [material]))[0]["id"]
        frozen = freeze_sources(session, owner, [material],
            [(conv.lesson_id, {"source_section_ids": [chapter]})])
        session.get(OutlineVersionRow, conv.outline_version_id).source_snapshot_json = json.dumps(frozen)
        lesson_id = conv.lesson_id
        catalog = figure_catalog(session, owner, frozen, lesson_id)
    assert catalog["figures"][0]["crop_sha256"] == crop["sha256"]
    assert catalog["figures"][0]["content_status"] == "visual_reviewed"
    targets = question_targets(state.teaching, SimpleNamespace(owner_user_id=owner, lesson_id=lesson_id),
        {"source_snapshot": frozen}, [{"original_allowed": True, "required_figure_refs": [material + "/p1/b2"]}])
    assert targets[0]["precision"] == "provider_crop" and targets[0]["bbox"] is None
    url = f"/api/teaching/{ident}/sources/{material}/{sha}/figures/1/2/{crop['sha256']}"
    response = client.get(url, headers=headers)
    assert response.status_code == 200 and response.content == payload
    assert response.headers["cache-control"] == "no-store"
    assert client.get(url.replace(crop["sha256"], "c" * 64), headers=headers).status_code == 404
    # A mutated frozen source is refused before any crop can be served.
    with state.db.session() as session:
        row = session.get(DocumentAttachmentRow, material)
        changed = json.loads(row.result_json)
        changed["question_bank"]["questions"][0]["figure_roles"][0]["role"] = "mixed"
        row.result_json = json.dumps(changed)
    assert client.get(url, headers=headers).status_code in (404, 409)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotated_pdf_targets_stay_inside_visual_page(tmp_path, rotation):
    import io
    from reportlab.pdfgen import canvas
    output = io.BytesIO()
    doc = canvas.Canvas(output, pagesize=(600, 800))
    doc.setPageRotation(rotation)
    doc.drawString(30, 300, "Synthetic rotated region")
    doc.save()
    path = tmp_path / "synthetic.pdf"
    path.write_bytes(output.getvalue())
    targets = pdf_page_targets(path, "synthetic", "a" * 64, 1)
    region = next(item for item in targets if item["precision"] == "text")["bbox"]
    assert 0 <= region[0] < region[2] <= 1 and 0 <= region[1] < region[3] <= 1


def test_feedback_uses_saved_actual_code_and_output(env):
    client, state, owner, headers = env
    ident = ready(env)
    url = f"/api/teaching/{ident}/interactive"
    assert client.put(url, headers=headers, json=body()).status_code == 200
    response = client.post(url + "/feedback", headers=headers, json={
        "revision": 1, "step_no": 1, "block_id": "example", "action": "compare"})
    assert response.status_code == 200, response.text
    from backend.teaching.learning_models import LessonBlueprintRow
    with state.db.session() as sess:
        context = json.loads(sess.get(LessonBlueprintRow, ident).context_json)
        feedback = context["_code_feedback"]
    assert feedback["cell"]["output"] == "30\n"
    assert "value = 10" in feedback["cell"]["code"]
    assert feedback["generation"] == response.json()["generation"]
    repeated = client.post(url + "/feedback", headers=headers, json={
        "revision": 1, "step_no": 1, "block_id": "example", "action": "compare"})
    assert repeated.status_code == 200 and repeated.json()["deduped"]
    assert repeated.json()["generation"] == response.json()["generation"]


def test_feedback_generation_consumes_snapshot_and_disables_content_trace(env):
    from common_fixtures import SyntheticModelAdapter, synthetic_output_response
    class FeedbackAdapter(SyntheticModelAdapter):
        def __init__(self):
            super().__init__()
            self.seen = []

        def generate(self, messages, **options):
            self.seen.extend(messages)
            return synthetic_output_response({"alignment_version": 1,
                "answer": "Synthetic feedback describes the actual output.", "beats": []}, options)
    client, state, _, headers = env
    ident = ready(env)
    url = f"/api/teaching/{ident}/interactive"
    assert client.put(url, headers=headers, json=body()).status_code == 200
    adapter = FeedbackAdapter()
    state.teaching.model_adapter_factory = lambda owner, task: adapter
    response = client.post(url + "/feedback", headers=headers, json={
        "revision": 1, "step_no": 1, "block_id": "example", "action": "compare"})
    assert response.status_code == 200
    result = state.teaching.run_followup(response.json()["task_id"])
    assert result["status"] == "succeeded", result
    prompt = "\n".join(str(message["content"]) for message in adapter.seen)
    assert "value = 10" in prompt and "30" in prompt and "baseline_output" in prompt
    assert adapter.omit_prompt_trace is True


def test_source_reading_generation_binds_real_targets_to_scene(env, tmp_path):
    from test_teaching_assessments import LearningAdapter
    from common_fixtures import synthetic_output_response
    from backend.teaching.learning_models import LessonBlueprintRow
    from backend.teaching.pedagogy import lesson_context
    class ReadingAdapter(LearningAdapter):
        def generate(self, messages, **options):
            context = next((json.loads(message["content"]) for message in messages
                            if message["role"] == "user" and message["content"].startswith("{")), {})
            if "current_stage" not in context:
                return super().generate(messages, **options)
            if "current_reading_unit" in context:
                unit=context["current_reading_unit"]
                phase=context["current_stage"]
                return synthetic_output_response({"draft":{
                    "translation":"这段原文说明了当前资料中的基本关系。",
                    "explanation":"本段把原文中的基本关系连接起来，接下来据此理解方法。",
                    "covered_scope_capability_ids":phase["scope_capability_ids"],
                    "recap":"已讲清资料中的基本关系。"}},options)
            response = super().generate(messages, **options)
            payload = json.loads(response.get("tool_arguments") or response["content"])
            targets = context["context"]["pdf_targets"]
            target = next(item for item in targets if item["precision"] == "text")
            focus = {key: target[key] for key in ("material_id", "target_id")}
            payload["steps"][0]["board"]["source_focus"] = focus
            payload["steps"][0]["beats"][0]["source_focus"] = focus
            return synthetic_output_response(payload, options)
    _, state, owner, _ = env
    ident = ready(env)
    material = attachment(state.db, owner)
    payload = pdf(2)
    sha = hashlib.sha256(payload).hexdigest()
    state.teaching.storage = MaterialStorage(tmp_path)
    state.teaching.storage.put(MaterialStorage.original_key(material), payload)
    with state.db.session() as sess:
        sess.get(MaterialRow, material).source_bytes_sha256 = sha
        conv = sess.get(ConversationRow, ident)
        chapter = chapter_catalog(documents(sess, owner, [material]))[0]["id"]
        frozen = freeze_sources(sess, owner, [material], [(conv.lesson_id,
            {"source_section_ids": [chapter], "teaching_mode": "source_reading"})])
        sess.get(OutlineVersionRow, conv.outline_version_id).source_snapshot_json = json.dumps(frozen)
        sess.expunge(conv)
    context = lesson_context(state.teaching, conv)
    assert context["teaching_mode"] == "source_reading"
    with state.db.session() as sess:
        row = sess.get(LessonBlueprintRow, ident)
        plan = json.loads(row.plan_json)
        task_id = row.source_task_id
        row.context_json = json.dumps(context)
    from backend.teaching.learning_outputs import LessonBlueprint
    _, _, _, result = state.teaching._generate_one(conv, task_id,
        ReadingAdapter(), 2, prepared=(LessonBlueprint.model_validate(plan), context))
    candidate = result.payload.steps[0]
    scene = declaration_to_scene(candidate.board)
    from backend.teaching.board_alignment import scene_beats
    beats = scene_beats(candidate)
    assert scene["pdf_focus"]["sha256"] == sha and scene["pdf_focus"]["bbox"]
    assert beats[0]["pdf_focus"]["target_id"] == scene["pdf_focus"]["target_id"]
