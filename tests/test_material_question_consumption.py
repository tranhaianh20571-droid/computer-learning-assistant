"""Synthetic frozen question banks, continuation search and original figure binding."""
# ruff: noqa: F811
import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from backend.materials.question_retrieval import question_search
from backend.materials.storage import MaterialStorage
from backend.materials.study_sources import (
    build_study_index,
    documents,
    freeze_sources,
    source_candidates,
    validate_source_choices,
)
from backend.planning.errors import PlanningError
from backend.teaching.examples import validate_teaching_examples
from backend.teaching.interactive import bind_source, question_targets
from backend.teaching.outputs import declaration_to_scene
from pydantic_ai.exceptions import ModelRetry
from test_classroom_examples import step
from test_course_library import database, env  # noqa: F401
from test_document_attachments import pdf
from test_study_sources import attachment, question


def bank_state(count=32):
    prompts = [f"Synthetic task {i}: explain how a source observation supports a conclusion."
               for i in range(count)]
    if count > 29:
        prompts[29] = "Synthetic geometry task: explain how radius determines circle area."
    content = "\n".join(prompts)
    state = {"pages": 1, "page_results": [{"page_no": 1, "blocks": [
        {"type": "title", "content": "Chapter 1: Synthetic evidence"},
        {"type": "text", "content": content},
        {"type": "figure", "content": "", "source_bbox": [.1, .2, .7, .6]}]}]}
    index = build_study_index(state)
    section = index["sections"][0]
    state["study_index"] = index
    state["question_bank"] = {"version": 1, "status": "completed", "questions": [
        {"id": f"semantic_{i}", "prompt": text, "start": section["offset"] + i,
         "end": section["offset"] + i + len(text), "section_id": section["id"],
         "page_no": 1, "end_page": 1, "kind": "example", "status": "confirmed",
         "requires_context": False, "answer_text": "", "feynman_suitable": True,
         "assessment_eligible": True, "figures_complete": True, "figure_roles": []}
        for i, text in enumerate(prompts)]}
    return state


def frozen(env, state=None):
    db, owner = env[1].db, env[2]
    data = state or bank_state()
    ident = attachment(db, owner, data)
    with db.session() as session:
        doc = documents(session, owner, [ident])[0]
        refs = [ident + "/" + s["id"] for s in doc["index"]["sections"] if not s.get("reference_only")]
        snapshot = freeze_sources(session, owner, [ident],
            [("lesson", {"source_section_ids": refs})])
    return db, owner, ident, snapshot


def test_ranked_frozen_questions_and_receipts_distinguish_not_retrieved(env):
    db, owner, ident, snapshot = frozen(env)
    with db.session() as session:
        receipt = {}
        selected = source_candidates(session, owner, snapshot,
            [{"lesson_id": "lesson", "goal": "geometry radius"}], include_examples=True, receipt=receipt)
        assert len(selected) == 24 and selected[0]["id"] == ident + "/semantic_29"
        assert receipt["total_in_scope"] == 32 and len(receipt["not_retrieved_ids"]) == 8
        more = source_candidates(session, owner, snapshot, [{"lesson_id": "lesson"}],
            include_examples=True, exclude_ids=receipt["returned_ids"])
        assert len(more) == 8 and not {q["id"] for q in more} & set(receipt["returned_ids"])
        with pytest.raises(PlanningError):
            source_candidates(session, "synthetic-foreign", snapshot, [{"lesson_id": "lesson"}])


def test_question_continuation_is_durable_bounded_and_never_repeats_returned_items(env):
    db, owner, _, snapshot = frozen(env)
    lessons = [{"lesson_id": "lesson", "goal": "evidence"}]
    with db.session() as session:
        initial = source_candidates(session, owner, snapshot, lessons, include_examples=True)
    private_state, saves, checks = {}, [], []
    tool = question_search(db, owner, snapshot, lessons, initial, state=private_state,
        guard=lambda: checks.append("checked"), save=lambda: saves.append(deepcopy(private_state)))
    assert len(tool("source evidence")["candidates"]) == 5
    assert len(tool("remaining synthetic examples")["candidates"]) == 3
    assert tool("more observations")["status"] == "no_new_candidates"
    assert tool("fourth round")["status"] == "budget_exhausted"
    resumed = list(initial[:24])
    restored = question_search(db, owner, snapshot, lessons, resumed, state=private_state,
        guard=lambda: None, save=lambda: None)
    assert len(resumed) == 32 and restored("another query")["status"] == "budget_exhausted"
    assert len(saves) == 7 and checks
    with pytest.raises(ValueError, match="scope_changed"):
        question_search(db, owner, snapshot, [{"lesson_id": "different"}], [], state=private_state,
            guard=lambda: None, save=lambda: None)


def test_conversation_search_restores_private_state_and_checks_owner_snapshot_and_lease(env):
    import uuid

    from backend.agents.contracts import AgentDeps
    from backend.materials.question_retrieval import conversation_question_search
    from backend.planning.models import ConversationRow
    from backend.teaching.learning_models import LessonBlueprintRow
    db, owner, _, snapshot = frozen(env)
    ident = "synthetic_" + uuid.uuid4().hex
    with db.session() as session:
        session.add(ConversationRow(conversation_id=ident, owner_user_id=owner, kind="chat"))
        session.flush()
        session.add(LessonBlueprintRow(conversation_id=ident, owner_user_id=owner,
            lesson_id="lesson", outline_version_id="synthetic-outline",
            content_version_id="synthetic-content", plan_json="{}",
            context_json=json.dumps({"source_snapshot": snapshot}), source_task_id="synthetic-task"))
    checked = []
    lease = object()
    def validate_lease(actual, **kwargs):
        assert actual is lease and kwargs["actor_id"] == owner
        checked.append(True)
    service = SimpleNamespace(db=db, tasks=SimpleNamespace(validate_lease=validate_lease))
    deps = AgentDeps(actor_id=owner)
    lessons = [{"lesson_id": "lesson"}]
    pool = []
    tool = conversation_question_search(service, deps, ident, "synthetic-task", lease,
        snapshot, lessons, pool)
    assert len(tool("evidence")["candidates"]) == 5
    resumed = []
    restored = conversation_question_search(service, deps, ident, "synthetic-task", lease,
        snapshot, lessons, resumed)
    assert len(resumed) == 5
    assert restored("more evidence")["round"] == 2 and checked
    assert conversation_question_search(service, AgentDeps(actor_id="foreign"), ident,
        "synthetic-task", lease, snapshot, lessons, []) is None
    with db.session() as session:
        row = session.get(LessonBlueprintRow, ident)
        row.context_json = json.dumps({"source_snapshot": {"changed": True}})
    with pytest.raises(ValueError, match="scope_changed"):
        restored("third query after source changed")


def test_figure_originals_require_exact_input_figure_and_use_owned_pdf_viewer(env, tmp_path):
    state = bank_state(1)
    item = state["question_bank"]["questions"][0]
    item.update(needs_figure=True, figure_roles=[{"block_id": "p1/b2", "role": "input"}])
    db, owner, ident, snapshot = frozen(env, state)
    original = pdf()
    sha = hashlib.sha256(original).hexdigest()
    from backend.materials.models import MaterialRow
    with db.session() as session:
        session.get(MaterialRow, ident).source_bytes_sha256 = sha
        snapshot = freeze_sources(session, owner, [ident],
            [("lesson", {"source_section_ids": snapshot["lessons"]["lesson"]})])
        pool = source_candidates(session, owner, snapshot, [{"lesson_id": "lesson"}], include_examples=True)
    storage = MaterialStorage(tmp_path)
    storage.put(MaterialStorage.original_key(ident), original)
    service = SimpleNamespace(db=db, storage=storage)
    conv = SimpleNamespace(owner_user_id=owner, lesson_id="lesson")
    context = {"source_snapshot": snapshot}
    targets = question_targets(service, conv, context, pool)
    assert len(targets) == 1 and targets[0]["target_id"] == "p1_b2"
    q = question(pool[0])
    with pytest.raises(ModelRetry, match="题设原图"):
        validate_source_choices([q], pool)
    q.figure.kind, q.figure.ref = "material_figure", ident + "/p1/b2"
    validate_source_choices([q], pool)
    displayed = step(prompt=pool[0]["prompt"], source_use="original", source_question_id=pool[0]["id"])
    with pytest.raises(ModelRetry, match="题设图区"):
        validate_teaching_examples(displayed, pool, [], "example")
    from backend.teaching.board_blocks import SourceFocus
    displayed.board.source_focus = SourceFocus(material_id=ident, target_id="p1_b2")
    validate_teaching_examples(displayed, pool, [], "example")
    bind_source(displayed.board, targets)
    assert declaration_to_scene(displayed.board)["pdf_focus"]["bbox"] == [.1, .2, .7, .6]
    assert question_targets(service, SimpleNamespace(owner_user_id="foreign", lesson_id="lesson"),
        context, []) == []
    with pytest.raises(PlanningError):
        question_targets(service, SimpleNamespace(owner_user_id="foreign", lesson_id="lesson"), context, pool)


def test_mixed_or_unreviewed_figures_never_resolve_as_original_regions(env, tmp_path):
    state = bank_state(1)
    state["question_bank"]["questions"][0].update(
        needs_figure=True, assessment_eligible=False, requires_context=True,
        figure_roles=[{"block_id": "p1/b2", "role": "mixed"}])
    db, owner, ident, snapshot = frozen(env, state)
    from backend.teaching.board_blocks import SourceFocus
    assert question_targets(SimpleNamespace(db=db, storage=MaterialStorage(tmp_path)),
        SimpleNamespace(owner_user_id=owner, lesson_id="lesson"),
        {"source_snapshot": snapshot}, focuses=[SourceFocus(material_id=ident, target_id="p1_b2")]) == []
