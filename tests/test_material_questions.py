"""Synthetic evidence, layout, semantic roles, recovery and source consumption."""
# ruff: noqa: F811
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from backend.materials.document_pipeline import normalize_result, paddle_document_result
from backend.materials.question_extraction import (
    Extraction,
    Review,
    blocks_and_anchors,
    compile_questions,
    extract_questions,
    validate_extraction,
    validate_review,
    windows,
)
from backend.materials.study_sources import build_study_index
from pydantic_ai.exceptions import ModelRetry
from test_document_attachments import env, pdf  # noqa: F401
from test_source_imports import source_env  # noqa: F401


def state():
    result = {"pages": 2, "page_results": [
        {"page_no": 1, "blocks": [
            {"type": "title", "content": "Chapter 1: Synthetic chemistry"},
            {"type": "text", "content": "【例题1】Compare the two synthetic samples.\n"
             "(1) Sample X has mass 5 g.\n(2) Sample Y has mass 8 g."},
            {"type": "text", "content": "Worked result: sample Y is heavier."}]},
        {"page_no": 2, "blocks": [
            {"type": "text", "content": "Explain why the synthetic comparison holds."},
            {"type": "figure", "content": "", "source_bbox": [.1, .1, .9, .9]}]}]}
    result["study_index"] = build_study_index(result)
    return result


def window(source=None):
    blocks, anchors = blocks_and_anchors(source or state())
    return windows(blocks, anchors)[0]


def candidate(target=None):
    target = target or window()
    return Extraction.model_validate({"questions": [{
        "local_id": "q1", "anchor_ids": [target["anchors"][0]["id"]],
        "label": "Example 1", "kind": "example",
        "stem": [{"block_id": "p1/b1", "text": "Compare the two synthetic samples."}],
        "conditions": [], "options": [],
        "subquestions": [
            {"label": "1", "evidence": [{"block_id": "p1/b1", "text": "(1) Sample X has mass 5 g."}]},
            {"label": "2", "evidence": [{"block_id": "p1/b1", "text": "(2) Sample Y has mass 8 g."}]}],
        "answer": [{"block_id": "p1/b2", "text": "Worked result: sample Y is heavier."}],
        "figure_roles": [], "boundary": "complete", "needs_figure": False,
        "feynman_suitable": False, "explanation": "Synthetic independent two-part task."}],
        "exclusions": []})


def verdict(**changes):
    return Review.model_validate({"verdicts": [{"local_id": "q1", "complete": True,
        "answer_isolated": True, "single_question": True, "figures_complete": True,
        "feynman_suitable": False, "reason": "Both synthetic inputs are retained.", **changes}],
        "exclusions": []})


def test_layout_coordinates_reading_order_and_ids_survive():
    raw = {"prunedResult": {"width": 1000, "height": 500, "parsing_res_list": [
        {"block_label": "text", "block_content": "Synthetic text", "block_bbox": [10, 20, 300, 400]},
        {"block_label": "table", "block_content": "<table><tr><td>X</td></tr></table>",
         "block_bbox": [400, 30, 800, 450]}]}}
    normalized = normalize_result(paddle_document_result([raw]), expected_pages=1)
    first, second = normalized["page_results"][0]["blocks"]
    assert first["source_bbox"] == [.01, .04, .3, .8]
    assert second["source_bbox"] == [.4, .06, .8, .9]
    assert first["block_id"] == "p1/b0" and second["reading_order"] == 1
    assert "<table>" in second["content"]


def test_native_labels_fill_inventory_without_rewriting_ocr():
    source = state()
    source["native_pdf_blocks"] = [{"block_id": "native/p1/0", "page_no": 1,
        "type": "text", "text": "【例题1】", "source_kind": "native_pdf", "source_offset": None},
        {"block_id": "native/p2/0", "page_no": 2, "type": "text", "text": "【例题2】",
         "source_kind": "native_pdf", "source_offset": None}]
    before = deepcopy(source["study_index"])
    _, anchors = blocks_and_anchors(source)
    assert len(anchors) == 2 and anchors[-1]["source_kind"] == "native_pdf"
    assert source["study_index"] == before


def test_native_pdf_resources_use_explicit_close(tmp_path):
    from backend.materials.question_extraction import add_native_pdf, render_pages
    path = tmp_path / "synthetic.pdf"
    path.write_bytes(pdf())
    source = {}
    calls = []
    add_native_pdf(source, path, lambda: calls.append("page"))
    assert "native_pdf_blocks" in source and calls == ["page"]
    assert render_pages(path, [1], lambda: None)[0][1].startswith(b"\xff\xd8")


def test_exact_evidence_retains_both_subparts_and_isolates_answer():
    target, source = window(), state()
    extracted = validate_extraction(candidate(target), target)
    reviewed = validate_review(verdict(), extracted)
    output = compile_questions(extracted, reviewed, target, source["study_index"]["sections"])[0]
    assert "5 g" in output["prompt"] and "8 g" in output["prompt"]
    assert "heavier" not in output["prompt"] and "heavier" in output["answer_text"]
    assert output["assessment_eligible"] and not output["feynman_suitable"]


@pytest.mark.parametrize("mutation", ["invented", "missing", "duplicate", "foreign_block"])
def test_fabricated_or_unaccounted_evidence_is_rejected(mutation):
    target = window()
    item = candidate(target)
    if mutation == "invented":
        item.questions[0].stem[0].text = "The original says mass 99 g."
    elif mutation == "missing":
        item.questions[0].anchor_ids = []
    elif mutation == "duplicate":
        item.questions[0].anchor_ids *= 2
    else:
        item.questions[0].stem[0].block_id = "foreign/p1/b0"
    with pytest.raises(ModelRetry):
        validate_extraction(item, target)


@pytest.mark.parametrize("field", ["complete", "answer_isolated", "single_question", "figures_complete"])
def test_independent_negative_verdict_cannot_be_overridden(field):
    target = window()
    output = compile_questions(candidate(target), verdict(**{field: False}),
        target, state()["study_index"]["sections"])[0]
    assert not output["assessment_eligible"]
    assert output["status"] == "needs_review"


def test_mixed_and_unseen_figures_cannot_become_originals():
    target = window()
    item = candidate(target)
    item.questions[0].needs_figure = True
    from backend.materials.question_extraction import FigureRole
    item.questions[0].figure_roles = [FigureRole(block_id="p2/b1", role="input")]
    assert not compile_questions(item, verdict(), target, state()["study_index"]["sections"])[0]["assessment_eligible"]
    target["visual_pages"] = [2]
    assert compile_questions(item, verdict(), target, state()["study_index"]["sections"])[0]["assessment_eligible"]
    item.questions[0].figure_roles[0].role = "mixed"
    assert not compile_questions(item, verdict(), target, state()["study_index"]["sections"])[0]["assessment_eligible"]


@pytest.mark.parametrize("second_role", ["input", "answer"])
def test_duplicate_or_conflicting_figure_roles_are_rejected(second_role):
    from backend.materials.question_extraction import FigureRole
    target, item = window(), candidate()
    item.questions[0].figure_roles = [
        FigureRole(block_id="p2/b1", role="input"),
        FigureRole(block_id="p2/b1", role=second_role)]
    with pytest.raises(ModelRetry, match="只能有一个角色"):
        validate_extraction(item, target)


def test_empty_diagram_anchor_remains_teaching_only():
    target = window()
    item = candidate(target)
    item.questions[0].stem = []
    item.questions[0].subquestions = []
    item.questions[0].kind = "worked_diagram"
    item.questions[0].boundary = "worked_only"
    output = compile_questions(item, verdict(complete=False), target, state()["study_index"]["sections"])[0]
    assert output["status"] == "teaching_only"
    assert output["anchor_ids"] and not output["assessment_eligible"]


def test_cross_page_continuation_is_located_and_review_must_cover_all():
    target = window()
    item = candidate(target)
    from backend.materials.question_extraction import Evidence
    item.questions[0].conditions = [Evidence(block_id="p2/b0",
        text="Explain why the synthetic comparison holds.")]
    output = compile_questions(item, verdict(), target, state()["study_index"]["sections"])[0]
    assert output["end_page"] == 2 and output["section_id"]
    with pytest.raises(ModelRetry):
        validate_review(Review(verdicts=[]), item)


def test_long_parent_keeps_anchor_and_only_reviewed_independent_parts_are_split():
    source = state()
    source["page_results"][0]["blocks"][1]["content"] += "\n" + "x" * 900 + "\n" + "y" * 900
    source["study_index"] = build_study_index(source)
    target = window(source)
    item = candidate(target)
    from backend.materials.question_extraction import Evidence, Subquestion
    item.questions[0].subquestions = [
        Subquestion(label="a", evidence=[Evidence(block_id="p1/b1", text="x" * 900)]),
        Subquestion(label="b", evidence=[Evidence(block_id="p1/b1", text="y" * 900)])]
    extracted = validate_extraction(item, target)
    plain = compile_questions(extracted, verdict(), target, source["study_index"]["sections"])
    assert len(plain) == 1 and not plain[0]["assessment_eligible"]
    split = compile_questions(extracted, verdict(independent_subquestions=True),
        target, source["study_index"]["sections"])
    assert len(split) == 3 and split[0]["anchor_ids"] and not split[0]["assessment_eligible"]
    assert all(q["parent_id"] == split[0]["id"] and q["assessment_eligible"] for q in split[1:])


def test_model_notes_cannot_be_used_as_original_transcription():
    target = window()
    target["source_blocks"].append({"block_id": "description", "page_no": 1,
        "type": "text", "source_kind": "image_understanding", "text": "", "source_offset": None})
    item = candidate(target)
    item.questions[0].stem[0].block_id = "description"
    with pytest.raises(ModelRetry):
        validate_extraction(item, target)


def test_resume_reuses_extraction_after_review_failure_and_keeps_attempts():
    source, calls, snapshots = state(), [], []
    def invoke(cls, prompt, payload, images, validator, name):
        calls.append(name)
        if cls is Extraction:
            return validator(candidate(payload))
        if len(calls) == 2:
            raise RuntimeError("synthetic reviewer interruption")
        return validator(verdict())
    run = lambda: extract_questions(source, invoke=invoke, prompts=["extract", "review"],
        checkpoint=lambda value: snapshots.append(deepcopy(value)), guard=lambda: None)
    with pytest.raises(RuntimeError):
        run()
    failed = next(iter(source["question_bank"]["windows"].values()))
    assert failed["status"] == "failed" and failed["inventory"][0]["status"] == "failed"
    run()
    assert calls == ["material_questions_extract", "material_questions_review", "material_questions_review"]
    saved = next(iter(source["question_bank"]["windows"].values()))
    assert saved["attempts"] == 2 and source["question_bank"]["counts"]["anchors"] == 1
    run()
    assert len(calls) == 3
    with pytest.raises(ValueError, match="snapshot_changed"):
        extract_questions(source, invoke=invoke, prompts=["changed", "review"],
            checkpoint=lambda _: None, guard=lambda: None)


def test_failed_window_preserves_inventory_for_unprocessed_pages():
    source = state()
    source["page_results"].append({"page_no": 5, "blocks": [
        {"type": "text", "content": "【例题2】Explain the second synthetic observation."}]})
    source["study_index"] = build_study_index(source)
    def unavailable(*args):
        raise RuntimeError("Synthetic provider unavailable.")
    with pytest.raises(RuntimeError):
        extract_questions(source, invoke=unavailable, prompts=["a", "b"],
            checkpoint=lambda _: None, guard=lambda: None, max_pages=1)
    bank = source["question_bank"]
    assert bank["status"] == "failed"
    assert len(bank["inventory"]) == len(bank["anchors"]) == 2
    assert bank["counts"]["statuses"] == {"failed": 1, "pending": 1}
    assert bank["counts"]["completed_windows"] == 0


def test_rejected_exclusion_is_preserved_for_followup():
    source = state()
    def invoke(cls, prompt, payload, images, validator, name):
        if cls is Extraction:
            return validator(Extraction(questions=[], exclusions=[{
                "anchor_id": payload["anchors"][0]["id"], "reason": "Synthetic suspected heading."}]))
        return validator(Review(verdicts=[], exclusions=[{
            "anchor_id": payload["candidate"]["exclusions"][0]["anchor_id"],
            "accepted": False, "reason": "Synthetic task exists and needs review."}]))
    bank = extract_questions(source, invoke=invoke, prompts=["a", "b"], checkpoint=lambda _: None,
                             guard=lambda: None)
    assert bank["inventory"][0]["status"] == "needs_review"


def test_unrelated_page_figure_does_not_trigger_visual_model(monkeypatch):
    source, calls = state(), []
    def invoke(cls, prompt, payload, images, validator, name):
        calls.append((cls.__name__, bool(images)))
        return validator(candidate(payload) if cls is Extraction else verdict())
    def forbidden_render(*_):
        raise AssertionError("Synthetic text task does not need page images.")
    monkeypatch.setattr("backend.materials.question_extraction.render_pages", forbidden_render)
    bank = extract_questions(source, invoke=invoke, prompts=["a", "b"], checkpoint=lambda _: None,
        guard=lambda: None, source="unused-synthetic.pdf", visual=True)
    assert calls == [("Extraction", False), ("Review", False)]
    assert next(iter(bank["windows"].values()))["visual_required"] is False


def test_native_only_anchor_triggers_visual_even_without_ocr_figure_blocks(monkeypatch):
    source, calls = state(), []
    source["page_results"][0]["blocks"][1]["content"] = source["page_results"][0]["blocks"][1][
        "content"].replace("【例题1】", "")
    source["page_results"][1]["blocks"].pop()
    source["native_pdf_blocks"] = [{"block_id": "native/p1/0", "page_no": 1,
        "type": "text", "text": "【例题1】", "source_kind": "native_pdf", "source_offset": None}]
    source["study_index"] = build_study_index(source)
    def render(path, numbers, guard):
        assert numbers == [1]
        return [(1, b"synthetic rendered source page")]
    def invoke(cls, prompt, payload, images, validator, name):
        calls.append((cls.__name__, bool(images)))
        return validator(candidate(payload) if cls is Extraction else verdict())
    monkeypatch.setattr("backend.materials.question_extraction.render_pages", render)
    bank = extract_questions(source, invoke=invoke, prompts=["a", "b"], checkpoint=lambda _: None,
        guard=lambda: None, source="synthetic.pdf", visual=True)
    assert calls == [("Extraction", False), ("Extraction", True), ("Review", True)]
    assert next(iter(bank["windows"].values()))["visual_pages"] == [1]


def test_needed_figure_escalates_after_semantic_extraction(monkeypatch):
    source, calls = state(), []
    def invoke(cls, prompt, payload, images, validator, name):
        calls.append((cls.__name__, bool(images)))
        if cls is Review:
            return validator(verdict())
        item = candidate(payload)
        item.questions[0].needs_figure = True
        from backend.materials.question_extraction import FigureRole
        item.questions[0].figure_roles = [FigureRole(block_id="p2/b1", role="input")]
        return validator(item)
    monkeypatch.setattr("backend.materials.question_extraction.render_pages",
                        lambda *_: [(2, b"synthetic-real-image-input")])
    bank = extract_questions(source, invoke=invoke, prompts=["a", "b"], checkpoint=lambda _: None,
        guard=lambda: None, source="unused-synthetic.pdf", visual=True)
    assert calls == [("Extraction", False), ("Extraction", True), ("Review", True)]
    assert bank["questions"][0]["assessment_eligible"]


def content_model(env):
    return env.configs.create(actor_id=env.owner, is_admin=False, kind="content_model",
        protocol="openai", endpoint="https://synthetic.example/v1", model_name="synthetic-model",
        credentials={"api_key": "synthetic-content-key"})


def test_attachment_semantic_roles_are_opt_in_and_pinned(env, monkeypatch):
    from backend.materials.attachments import validate_document
    from backend.materials.models import DocumentAttachmentRow
    model = content_model(env)
    target = {"semantic": {"config_id": model["config_id"], "config_version": 1}}
    item = env.service.create(env.owner, validate_document("synthetic.pdf", None, pdf()),
        env.config["config_id"], 1, "semantic-upload", extraction=target)
    def fake(adapter, cls, messages, **kwargs):
        kwargs["deps"].check()
        kwargs["deps"].budget.consume("logical")
        assert adapter.timeout == kwargs["timeout_seconds"] == 300
        assert adapter.separate_connect_timeout
        kwargs["deps"].budget.consume("wire")
        output = Extraction(questions=[], exclusions=[]) if cls is Extraction else Review(verdicts=[])
        return SimpleNamespace(payload=kwargs["output_validator"](None, output))
    monkeypatch.setattr("backend.agents.runner.run_structured_agent", fake)
    env.service.run(item["task_id"])
    assert env.service.view(item["material_id"], env.owner)["status"] == "parsed", env.service.view(item["material_id"], env.owner)
    with env.db.session() as sess:
        data = json.loads(sess.get(DocumentAttachmentRow, item["material_id"]).result_json)
    assert data["question_bank"]["status"] == "completed"
    assert data["extraction_binding"]["targets"]["semantic"]["config_id"] == model["config_id"]
    from backend.agents.task_binding import AgentBudgetRow
    with env.db.session() as sess:
        assert sess.get(AgentBudgetRow, item["task_id"]).logical_requests == 2


def test_semantic_retry_keeps_completed_ocr_and_cumulative_budget(env, monkeypatch):
    from backend.agents.task_binding import AgentBudgetRow
    from backend.materials.attachments import validate_document
    from backend.materials.models import DocumentAttachmentRow
    model = content_model(env)
    item = env.service.create(env.owner, validate_document("synthetic.pdf", None, pdf()),
        env.config["config_id"], 1, "semantic-retry",
        extraction={"semantic": {"config_id": model["config_id"], "config_version": 1}})
    calls = []
    def fake(adapter, cls, messages, **kwargs):
        deps = kwargs["deps"]
        deps.check()
        deps.budget.consume("logical")
        deps.budget.consume("wire")
        calls.append(cls)
        if calls == [Extraction, Review]:
            raise RuntimeError("synthetic interruption before review result")
        value = Extraction(questions=[], exclusions=[]) if cls is Extraction else Review(verdicts=[])
        return SimpleNamespace(payload=kwargs["output_validator"](None, value))
    monkeypatch.setattr("backend.agents.runner.run_structured_agent", fake)
    env.service.run(item["task_id"])
    assert env.service.view(item["material_id"], env.owner)["status"] == "failed"
    retry = env.service.retry(item["material_id"], env.owner, env.config["config_id"], 1)
    env.service.run(retry["task_id"])
    assert env.service.view(item["material_id"], env.owner)["status"] == "parsed"
    assert calls == [Extraction, Review, Review]
    assert len(env.adapter.calls) == 1
    with env.db.session() as session:
        assert session.get(AgentBudgetRow, retry["task_id"]).logical_requests == 3
        bank = json.loads(session.get(DocumentAttachmentRow, item["material_id"]).result_json)["question_bank"]
        assert next(iter(bank["windows"].values()))["attempts"] == 2


@pytest.mark.parametrize("change", ["delete", "revoke", "config", "task"])
def test_semantic_publication_rechecks_owner_config_grant_and_task(env, change):
    from backend.materials.attachments import validate_document
    from backend.materials.errors import MaterialsError
    from backend.materials.models import DocumentAttachmentRow, MaterialRow, utcnow
    from backend.slice1.errors import Slice1Error
    from backend.slice1.models import DisclosureGrantRow, ServiceConfigRow
    from backend.tasks.service import LeaseError
    model = content_model(env)
    item = env.service.create(env.owner, validate_document("synthetic.pdf", None, pdf()),
        env.config["config_id"], 1, "semantic-fence",
        extraction={"semantic": {"config_id": model["config_id"], "config_version": 1}})
    lease = env.tasks.claim(item["task_id"], lease_owner="synthetic-test", lease_seconds=180)
    with env.db.session() as session:
        row = session.get(DocumentAttachmentRow, item["material_id"])
        binding = json.loads(row.result_json)["extraction_binding"]
        if change == "delete":
            session.get(MaterialRow, item["material_id"]).deleted_at = utcnow()
        elif change == "revoke":
            session.get(DisclosureGrantRow, binding["targets"]["semantic"]["grant_id"]).revoked_at = utcnow()
        elif change == "config":
            session.get(ServiceConfigRow, model["config_id"]).is_active = False
        else:
            row.task_id = "synthetic-replacement-task"
    with env.db.session() as session, pytest.raises((Slice1Error, MaterialsError, LeaseError)):
        material = session.get(MaterialRow, item["material_id"])
        row = session.get(DocumentAttachmentRow, item["material_id"])
        if change == "delete":
            env.service._owned(session, item["material_id"], env.owner)
        else:
            env.service._publish_guard(session, material, row, lease.task_id, env.owner)


def test_unified_import_discloses_semantic_targets_and_checks_snapshot(source_env, tmp_path):
    from backend.materials.source_router import ImportItemIn
    from backend.slice1.errors import Slice1Error
    from backend.slice1.models import PADDLE_VL_ENDPOINT, PADDLE_VL_MODEL
    source_env.configs.create(actor_id=source_env.owner, is_admin=False, kind="document_parser",
        protocol="paddleocr_vl", endpoint=PADDLE_VL_ENDPOINT, model_name=PADDLE_VL_MODEL,
        credentials={"api_key": "synthetic-ocr-key"})
    model = content_model(source_env)
    batch = source_env.service.create(source_env.owner, "synthetic-semantic-batch", [
        ImportItemIn(client_id="file", name="synthetic.pdf", mime="application/pdf")])
    path = tmp_path / "synthetic.pdf"
    path.write_bytes(pdf())
    source_env.service.upload(source_env.owner, batch["batch_id"], batch["items"][0]["item_id"], path)
    prepared = source_env.service.prepare(source_env.owner, batch["batch_id"])
    assert any(d["purpose"] == "semantic" and d["config_id"] == model["config_id"]
               for d in prepared["destinations"])
    with pytest.raises(Slice1Error):
        source_env.service.commit(source_env.owner, batch["batch_id"], "stale-manifest", True)
    committed = source_env.service.commit(source_env.owner, batch["batch_id"], prepared["manifest_hash"], True)
    from backend.materials.models import DocumentAttachmentRow
    with source_env.db.session() as session:
        row = session.get(DocumentAttachmentRow, committed["items"][0]["material_id"])
        assert json.loads(row.result_json)["extraction_binding"]["targets"]["semantic"]["config_id"] == model["config_id"]


def test_foreign_semantic_destination_rejected_before_calls(env):
    import uuid

    from backend.materials.attachments import validate_document
    from backend.slice1.errors import Slice1Error
    model = env.configs.create(actor_id="synthetic-other-" + uuid.uuid4().hex, is_admin=False, kind="content_model",
        protocol="openai", endpoint="https://synthetic.example/v1", model_name="synthetic-model",
        credentials={"api_key": "synthetic-content-key"})
    with pytest.raises(Slice1Error):
        env.service.create(env.owner, validate_document("synthetic.pdf", None, pdf()),
            env.config["config_id"], 1, "foreign-semantic",
            extraction={"semantic": {"config_id": model["config_id"], "config_version": 1}})


@pytest.mark.parametrize("message,format_sent,capability_error", [
    ("Synthetic response_format type is unavailable now.", True, True),
    ("Synthetic response_format type is unavailable now.", False, False),
    ("Synthetic upstream model is unavailable.", True, False),
])
def test_named_output_protocol_error_uses_only_same_model_fallback(message, format_sent, capability_error):
    import asyncio
    import socket

    import httpx2
    from backend.agents.contracts import AgentDeps
    from backend.agents.native import ModelCapabilityError, SafeModelTransport
    from backend.planning.errors import PlanningError
    inner = httpx2.MockTransport(lambda _: httpx2.Response(400,
        json={"error": {"code": "invalid_request_error", "message": message}}))
    transport = SafeModelTransport("https://synthetic.example/v1", AgentDeps(), inner=inner,
        resolver=lambda *_: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", 443))])
    payload = {"response_format": {"type": "json_schema"}} if format_sent else {}
    request = httpx2.Request("POST", "https://synthetic.example/v1/chat/completions",
                             content=json.dumps(payload).encode())
    with pytest.raises(ModelCapabilityError if capability_error else PlanningError):
        asyncio.run(transport.handle_async_request(request))


def test_thinking_tool_choice_rejection_is_a_protocol_error():
    import asyncio
    import socket

    import httpx2
    from backend.agents.contracts import AgentDeps
    from backend.agents.native import ModelCapabilityError, SafeModelTransport
    transport = SafeModelTransport("https://synthetic.example/v1", AgentDeps(),
        inner=httpx2.MockTransport(lambda _: httpx2.Response(400, json={"error": {
            "code": "invalid_request_error", "message": "Thinking mode does not support this tool_choice"}})),
        resolver=lambda *_: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", 443))])
    with pytest.raises(ModelCapabilityError):
        asyncio.run(transport.handle_async_request(httpx2.Request(
            "POST", "https://synthetic.example/v1/chat/completions",
            content=json.dumps({"tool_choice": "required"}).encode())))


def test_compressed_provider_error_can_select_same_model_output_fallback():
    import asyncio
    import gzip
    import socket

    import httpx2
    from backend.agents.contracts import AgentDeps
    from backend.agents.native import ModelCapabilityError, SafeModelTransport
    class Compressed(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield gzip.compress(json.dumps({"error": {"code": "unsupported_response_format"}}).encode())
    transport = SafeModelTransport("https://synthetic.example/v1", AgentDeps(),
        inner=httpx2.MockTransport(lambda _: httpx2.Response(400,
            headers={"content-encoding": "gzip"}, stream=Compressed())),
        resolver=lambda *_: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", 443))])
    with pytest.raises(ModelCapabilityError):
        asyncio.run(transport.handle_async_request(httpx2.Request(
            "POST", "https://synthetic.example/v1/chat/completions", content=b"{}")))


def test_payment_required_is_quota_and_never_selects_another_output_mode():
    import asyncio
    import socket

    import httpx2
    from backend.agents.contracts import AgentDeps
    from backend.agents.limits import allows_content_fallback
    from backend.agents.native import SafeModelTransport
    from backend.planning.errors import PlanningError
    transport = SafeModelTransport("https://synthetic.example/v1", AgentDeps(),
        inner=httpx2.MockTransport(lambda _: httpx2.Response(402, content=b"synthetic private balance detail")),
        resolver=lambda *_: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", 443))])
    with pytest.raises(PlanningError) as caught:
        asyncio.run(transport.handle_async_request(httpx2.Request(
            "POST", "https://synthetic.example/v1/chat/completions", content=b"{}")))
    assert caught.value.code == "QUOTA_EXCEEDED"
    assert not allows_content_fallback(caught.value)
