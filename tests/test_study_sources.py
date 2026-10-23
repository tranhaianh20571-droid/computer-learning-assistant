"""Synthetic source indexes, frozen ownership and enforced original questions."""
import json
import uuid
from datetime import datetime, timezone
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import ModelRetry
from sqlalchemy import select

from backend.materials.study_sources import (build_study_index, chapter_catalog, documents,
    freeze_sources, source_candidates, verify_sources, validate_source_choices, attach_provenance)
from backend.materials.models import MaterialRow, DocumentAttachmentRow
from backend.planning.models import PlanningTaskStateRow, OutlineVersionRow, CourseUnitRow, LessonRow
from backend.planning.errors import PlanningError
from backend.teaching.learning_outputs import AssessmentQuestion, UnitAssessment
from backend.teaching.feynman_outputs import FeynmanQuestion
from test_course_library import database, env, draft, confirm  # noqa: F401


def content():
    return {"pages": 2, "page_results": [
        {"page_no": 1, "blocks": [
            {"type": "title", "content": "Chapter 1: Synthetic sampling", "level": 1},
            {"type": "text", "content": "A random sample describes a population.\n"
             "Why does random sampling reduce selection bias?\n"
             "Exercises\n1. Explain the difference between a sample and a population.\n"
             "2. Describe how random sampling supports an estimate.\n"
             "3. Explain why a sample estimate varies between repeated"}]},
        {"page_no": 2, "blocks": [
            {"type": "text", "content": "samples.\nAnswer: Synthetic sample variation.\n"
             "Question of Interest\nUse the following table to estimate the population mean.\n"
             "Read in and inspect the data\nsynthetic_data.csv"}]}],
        "chunks": [{"page_no": 1, "text": "Synthetic sampling.", "kind": "text"}]}


def attachment(db, owner, state=None):
    ident = "synthetic_" + uuid.uuid4().hex[:15]
    with db.session() as sess:
        sess.add(MaterialRow(material_id=ident, owner_user_id=owner, kind="pdf",
            filename_hash="a" * 64, source_bytes_sha256="b" * 64, status="parsed"))
        sess.flush()
        sess.add(DocumentAttachmentRow(material_id=ident, owner_user_id=owner,
            upload_key=ident, task_id="synthetic-task", config_id="synthetic-config",
            config_version=1, result_json=json.dumps(state or content())))
    return ident


def question(candidate, ident="one", use="original"):
    return AssessmentQuestion(id=ident, kind="short", prompt=candidate["prompt"], lesson_ids=["lesson"],
        goal="Synthetic sampling", answer=["Synthetic reference"], rubric=["Synthetic evidence"],
        explanation="Synthetic explanation", source_use=use, source_question_id=candidate["id"])


def test_index_cross_page_exact_offsets_and_no_answer_in_question():
    index = build_study_index(content())
    assert len(index["sections"]) == 1
    exercises = [q for q in index["questions"] if q["kind"] == "exercise"]
    assert len(exercises) == 3
    assert exercises[-1]["page_no"] == 1 and exercises[-1]["end_page"] == 2
    assert "samples." in exercises[-1]["prompt"]
    assert "Answer:" not in exercises[-1]["prompt"]
    assert exercises[-1]["answer_text"] == "Synthetic sample variation."
    assert next(q for q in index["questions"] if q["kind"] == "research")["requires_context"]
    assert next(q for q in index["questions"] if q["kind"] == "explain")["feynman_suitable"]


def test_index_never_extracts_generated_image_description():
    state = {"pages": 1, "page_results": [{"page_no": 1, "review_reasons": ["figure_without_ocr"],
        "blocks": [{"type": "figure", "content": "",
                    "image_understanding": "Why does this generated example look correct?"}]}]}
    assert build_study_index(state)["questions"] == []


def test_index_stable_and_namespace_materials(env):
    db = env[1].db
    ident = attachment(db, env[2])
    with db.session() as sess:
        docs = documents(sess, env[2], [ident])
        chapter = chapter_catalog(docs)[0]
        frozen = freeze_sources(sess, env[2], [ident],
            [("lesson", {"source_section_ids": [chapter["id"]]})])
        candidates = source_candidates(sess, env[2], frozen, [{"lesson_id": "lesson"}])
        assert candidates and all(c["id"].startswith(ident + "/") for c in candidates)
        assert verify_sources(sess, env[2], frozen)[0]["index"] == docs[0]["index"]
        assert source_candidates(sess, env[2], frozen, [{"lesson_id": "other"}]) == []
        with pytest.raises(PlanningError):
            documents(sess, "other-owner", [ident])
    with db.session() as sess:
        row = sess.get(DocumentAttachmentRow, ident)
        row.result_json = json.dumps({"pages": 1, "chunks": []})
    with db.session() as sess, pytest.raises(PlanningError):
        verify_sources(sess, env[2], frozen)


def test_original_priority_cannot_be_bypassed_or_fabricated():
    candidates = [{"id": "doc/q1", "prompt": "Why does a random sample reduce bias?",
        "original_allowed": True, "lesson_ids": ["lesson"], "material_id": "doc",
        "content_hash": "synthetic", "chapter": "Sampling", "page_no": 2, "end_page": 2}]
    q = question(candidates[0])
    validate_source_choices([q], candidates)
    assert attach_provenance(q, candidates)["kind"] == "original"
    q.prompt += " Modified"
    with pytest.raises(ModelRetry):
        validate_source_choices([q], candidates)
    q = question(candidates[0], use="generated")
    q.source_question_id = ""
    with pytest.raises(ModelRetry):
        validate_source_choices([q], candidates)
    reject = SimpleNamespace(question_id="doc/q1", reason="not_taught")
    validate_source_choices([q], candidates, [reject])
    q.source_question_id = "forged"
    with pytest.raises(ModelRetry):
        validate_source_choices([q], candidates)


def test_contents_are_reference_only_and_inline_questions_are_found():
    state = {"pages": 2, "page_results": [
        {"page_no": 1, "blocks": [{"type": "title", "content": "Table of Contents"},
          {"type": "text", "content": "Chapter 1: Sampling 1\nChapter 2: Regression 12"}]},
        {"page_no": 2, "blocks": [{"type": "title", "content": "Chapter 1: Sampling"},
          {"type": "text", "content": "A natural question is: How does a random sample reduce bias???"}]}]}
    index = build_study_index(state)
    assert len(index["sections"]) == 2
    assert index["sections"][0]["reference_only"]
    assert index["questions"][0]["prompt"] == "How does a random sample reduce bias???"
    assert index["questions"][0]["page_no"] == 2


def test_feynman_keeps_original_and_rejects_missing_context():
    c = {"id": "doc/q", "prompt": "Why does a random sample reduce bias?",
         "original_allowed": True, "lesson_ids": ["lesson"]}
    q = FeynmanQuestion(question=c["prompt"], criteria=["Synthetic criterion"],
        source_question_id=c["id"], source_use="original")
    validate_source_choices([q], [c], feynman=True)
    c["original_allowed"] = False
    with pytest.raises(ModelRetry):
        validate_source_choices([q], [c], feynman=True)


def test_confirm_freezes_sections_and_rejects_forged_refs(env):
    client, state, owner, headers = env
    ident = attachment(state.db, owner)
    with state.db.session() as sess:
        chapter = chapter_catalog(documents(sess, owner, [ident]))[0]
    payload = {"title": "Synthetic sampling", "units": [{"title": "Sampling", "lessons": [
        {"title": "Random sampling", "goal": "Explain random sampling",
         "source_section_ids": [chapter["id"]]}]}]}
    course, item = draft(env, payload, kind="course_outline")
    task = state.tasks.create_task(owner_user_id=owner, kind="outline_generate", subject_id=course,
                                   idempotency_key=uuid.uuid4().hex)
    with state.db.session() as sess:
        sess.add(PlanningTaskStateRow(task_id=task["task_id"], owner_user_id=owner,
            target_kind="course", target_id=course, draft_id=item["draft_id"],
            material_ids_json=json.dumps([ident])))
    result = confirm(env, item)
    assert result.status_code == 200, result.text
    with state.db.session() as sess:
        version = sess.get(OutlineVersionRow, result.json()["version_id"])
        frozen = json.loads(version.source_snapshot_json)
        lesson = sess.scalar(select(LessonRow).where(LessonRow.outline_version_id == version.outline_version_id))
        assert frozen["material_ids"] == [ident]
        assert frozen["lessons"][lesson.lesson_id] == [chapter["id"]]
        from backend.teaching.pedagogy import lesson_context
        conv = SimpleNamespace(course_id=course, lesson_id=lesson.lesson_id,
            outline_version_id=version.outline_version_id, owner_user_id=owner)
        context = lesson_context(state.teaching, conv)
        assert context["source_sections"][0]["id"] == chapter["id"]
    with state.db.session() as sess:
        row = sess.get(PlanningTaskStateRow, task["task_id"])
        row.material_ids_json = "[]"
    assert lesson_context(state.teaching, conv)["material_ids"] == [ident]
    with state.db.session() as sess:
        sess.get(MaterialRow, ident).deleted_at = datetime.now(timezone.utc)
    with pytest.raises(PlanningError):
        lesson_context(state.teaching, conv)


def test_confirm_rejects_forged_chapter_atomically(env):
    _, state, owner, _ = env
    ident = attachment(state.db, owner)
    payload = {"title": "Synthetic invalid source", "units": [{"title": "Sampling", "lessons": [
        {"title": "Sampling", "goal": "Explain sampling", "source_section_ids": ["other/forged"]}]}]}
    course, item = draft(env, payload, kind="course_outline")
    task = state.tasks.create_task(owner_user_id=owner, kind="outline_generate", subject_id=course,
                                   idempotency_key=uuid.uuid4().hex)
    with state.db.session() as sess:
        sess.add(PlanningTaskStateRow(task_id=task["task_id"], owner_user_id=owner,
            target_kind="course", target_id=course, draft_id=item["draft_id"],
            material_ids_json=json.dumps([ident])))
    assert confirm(env, item).status_code == 400
    with state.db.session() as sess:
        assert sess.scalar(select(OutlineVersionRow).where(OutlineVersionRow.course_id == course)) is None


@pytest.mark.parametrize("embedded", [True, False])
def test_route_sources_inherit_to_embedded_or_later_course_outline(env, embedded):
    from backend.planning.models import RouteVersionRow, RouteCourseNodeRow
    _, state, owner, _ = env
    ident = attachment(state.db, owner)
    with state.db.session() as sess:
        chapter = chapter_catalog(documents(sess, owner, [ident]))[0]
    course_data = {"title": "Synthetic sampling route course", "goal": "Explain sampling"}
    if embedded:
        course_data["units"] = [{"title": "Sampling", "lessons": [
            {"title": "Random sample", "goal": "Explain sampling", "source_section_ids": [chapter["id"]]}]}]
    rid, item = draft(env, {"title": "Synthetic source route",
        "stages": [{"title": "Sampling", "courses": [course_data]}]})
    task = state.tasks.create_task(owner_user_id=owner, kind="route_generate", subject_id=rid,
                                   idempotency_key=uuid.uuid4().hex)
    with state.db.session() as sess:
        sess.add(PlanningTaskStateRow(task_id=task["task_id"], owner_user_id=owner,
            target_kind="route", target_id=rid, draft_id=item["draft_id"],
            material_ids_json=json.dumps([ident])))
    receipt = confirm(env, item)
    assert receipt.status_code == 200, receipt.text
    with state.db.session() as sess:
        version = sess.get(RouteVersionRow, receipt.json()["version_id"])
        node = sess.scalar(select(RouteCourseNodeRow).where(
            RouteCourseNodeRow.route_version_id == version.route_version_id))
        if embedded:
            snapshot = json.loads(sess.get(OutlineVersionRow, node.outline_version_id).source_snapshot_json)
            assert snapshot["material_ids"] == [ident]
            assert next(iter(snapshot["lessons"].values())) == [chapter["id"]]
            return
        course_id = node.course_id
    created = state.planning.create_planning_task(owner_user_id=owner, task_kind="outline_generate",
        scenario="self_study", planning_scope="course", target_id=course_id,
        idempotency_key=uuid.uuid4().hex)
    with state.db.session() as sess:
        current = sess.get(PlanningTaskStateRow, created["task_id"])
        assert json.loads(current.material_ids_json) == [ident]


def test_assessment_source_compute_hides_answers_and_retains_provenance(env):
    from backend.teaching.learning_models import UnitAssessmentRow
    from backend.planning.models import CourseRow
    from pydantic_ai.models.test import TestModel
    _, state, owner, _ = env
    ident = attachment(state.db, owner)
    payload = {"title": "Synthetic source assessment", "units": [
        {"title": "Sampling", "lessons": [{"title": "Sampling", "goal": "Explain sampling"}]}]}
    course, item = draft(env, payload, kind="course_outline")
    receipt = confirm(env, item)
    assert receipt.status_code == 200
    with state.db.session() as sess:
        outline = sess.get(OutlineVersionRow, receipt.json()["version_id"])
        unit = sess.scalar(select(CourseUnitRow).where(CourseUnitRow.outline_version_id == outline.outline_version_id))
        lesson = sess.scalar(select(LessonRow).where(LessonRow.unit_id == unit.unit_id))
        chapter = chapter_catalog(documents(sess, owner, [ident]))[0]
        frozen = freeze_sources(sess, owner, [ident],
            [(lesson.lesson_id, {"source_section_ids": [chapter["id"]]})])
        candidates = source_candidates(sess, owner, frozen, [{"lesson_id": lesson.lesson_id}])
        selected = [c for c in candidates if c["original_allowed"] and c["kind"] == "exercise"][:3]
        output = UnitAssessment(title="Synthetic original assessment", questions=[
            question(c, str(index)).model_copy(update={"lesson_ids": [lesson.lesson_id]})
            for index, c in enumerate(selected)])
        context = {"lessons": [{"lesson_id": lesson.lesson_id, "taught": [{"text": "Synthetic sampling scope"}]}],
                   "source_snapshot": frozen, "source_candidates": candidates}
        unit_id, outline_id = unit.unit_id, outline.outline_version_id
    task = state.tasks.create_task(owner_user_id=owner, kind="unit_assessment_generate",
        subject_id=course, idempotency_key=uuid.uuid4().hex)
    assessment_id = "synthetic_assessment_" + uuid.uuid4().hex[:12]
    with state.db.session() as sess:
        sess.add(UnitAssessmentRow(assessment_id=assessment_id, owner_user_id=owner,
            course_id=course, unit_id=unit_id, outline_version_id=outline_id,
            task_id=task["task_id"], context_json=json.dumps(context)))
    state.teaching.model_adapter_factory = lambda *_: TestModel(custom_output_args=output.model_dump())
    state.assessments._bind(task["task_id"], "unit_assessment_v1")
    result = state.assessments._compute(task["task_id"])
    assert all(q["source"]["kind"] == "original" for q in result["questions"])
    assert [q["prompt"] for q in result["questions"]] == [c["prompt"] for c in selected]
    assert result["questions"][-1]["answer"] == ["Synthetic sample variation."]
    assert result["questions"][-1]["source"]["answer_basis"] == "material"
    with state.db.session() as sess:
        sess.get(UnitAssessmentRow, assessment_id).questions_json = json.dumps(result)
        from backend.tasks.service import LearningTaskRow
        sess.get(LearningTaskRow, task["task_id"]).status = "succeeded"
    view = state.assessments.get(assessment_id, owner)
    assert all(q["source"]["kind"] == "original" for q in view["questions"])
    assert all(not {"answer", "rubric", "explanation"} & q.keys() for q in view["questions"])
