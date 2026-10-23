"""Synthetic original-example lifecycle, variant rejection and scope retrieval."""
# ruff: noqa: F811
import json
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from backend.agents.contracts import AgentDeps
from backend.materials.study_sources import (
    chapter_catalog,
    documents,
    freeze_sources,
    worked_examples,
)
from backend.planning.errors import PlanningError
from backend.planning.models import (
    ConversationRow,
    CourseRow,
    CourseUnitRow,
    LessonContentVersionRow,
    LessonRow,
    OutlineVersionRow,
)
from backend.teaching.examples import (
    assessment_example_issues,
    published_examples,
    remaining_candidates,
    review_variations,
    validate_teaching_examples,
    variation_items,
)
from backend.teaching.models import BoardSceneRow, TeachingStepRow
from backend.teaching.outputs import StepCandidate
from backend.teaching.scope_materials import LegacyScopeMaterialSearch as ScopeMaterialSearch
from backend.teaching.unit_scope import ScopeReference, validate_scope
from pydantic_ai.exceptions import ModelRetry
from sqlalchemy import select
from test_assessment_content_v2 import context_for, proposed
from test_course_library import confirm, database, draft, env  # noqa: F401
from test_study_sources import attachment
from test_teaching_assessments import LearningAdapter
from test_unit_scope import scope_context, scope_output


def original():
    return {"id": "doc/question", "prompt": "Compute the integral of x squared on [0, 1].",
            "original_allowed": True, "lesson_ids": ["lesson"]}


def test_bracketed_worked_examples_preserve_stems_and_hide_unlabelled_answers():
    from backend.materials.study_sources import build_study_index
    state = {"pages": 1, "page_results": [{"page_no": 1, "blocks": [
        {"type": "title", "content": "Synthetic chapter", "level": 1},
        {"type": "text", "content":
         "【例题1】解释为什么两种方法在给定条件下等价？\n解：Synthetic explanation.\n"
         "【例题2】计算给定表达式并说明依据。\n结果为四。\n"
         "[Example 3] Explain why the boundary condition matters?\nSolution: Synthetic answer."}]}]}
    examples = worked_examples(state, build_study_index(state)["sections"])
    assert len(examples) == 3
    assert examples[0]["prompt"] == "解释为什么两种方法在给定条件下等价？"
    assert examples[0]["feynman_suitable"]
    assert "结果" not in examples[1]["prompt"] and examples[1]["requires_context"]
    assert examples[2]["feynman_suitable"]


def test_unanswered_complete_example_is_usable_and_page_boundary_does_not_swallow_next_title():
    from backend.materials.study_sources import build_study_index
    state = {"pages": 2, "page_results": [
        {"page_no": 1, "blocks": [{"type": "title", "content": "Synthetic chapter", "level": 1},
            {"type": "text", "content": "【例题1】Explain why the stated assumptions are necessary?"}]},
        {"page_no": 2, "blocks": [{"type": "text", "content":
            "【例题2】计算给定表达式并说明依据。\n结果为四。"}]}]}
    examples = worked_examples(state, build_study_index(state)["sections"])
    assert len(examples) == 2 and examples[0]["end_page"] == 1
    assert examples[0]["feynman_suitable"] and not examples[0]["requires_context"]
    assert examples[1]["requires_context"]


def test_variation_review_sees_options_and_figure_without_private_answers():
    from backend.teaching.learning_outputs import AssessmentQuestion
    item = AssessmentQuestion(id="q1", kind="single", prompt="Which option matches the supplied values?",
        options=[{"id": "A", "text": "Synthetic option A"}, {"id": "B", "text": "Synthetic option B"}],
        lesson_ids=["lesson"], goal="Compare values", answer=["A"],
        rubric=["Synthetic private grading criterion"], explanation="Synthetic private solution",
        source_use="generated", example_reference_id="source",
        variation_reason="Reverse the given and unknown quantities.",
        figure={"kind": "data_table", "task": "Read the synthetic values",
                "table": {"columns": ["x"], "rows": [["2"]]}})
    review = variation_items([item], {"source": original()})[0]
    assert review["kind"] == "single" and len(review["options"]) == 2
    assert review["figure"]["table"]["rows"] == [["2"]]
    assert "private" not in json.dumps(review)
    assert "answer" not in review and "solution" not in review


def step(prompt=None, **example):
    prompt = prompt or original()["prompt"]
    return StepCandidate(title="Synthetic worked example", text="Synthetic explanation. " * 5,
        phase="example", board={"title": "Synthetic example", "format_version": 3,
            "blocks": [{"id": "problem", "type": "text", "text": prompt}]},
        examples=[{"prompt": prompt, "selection_reason": "Matches this stage's integration goal.",
                   **example}])


def test_teaching_prioritizes_exact_original_and_records_visible_problem():
    source = original()
    item = step(source_use="original", source_question_id=source["id"])
    validate_teaching_examples(item, [source], [], "example")
    with pytest.raises(ModelRetry, match="原题"):
        validate_teaching_examples(step(), [source], [], "example")
    item.board.blocks[0].text = "Only a solution; the problem has not been shown."
    with pytest.raises(ModelRetry, match="展示"):
        validate_teaching_examples(item, [source], [], "example")
    with pytest.raises(ModelRetry, match="应用短段"):
        validate_teaching_examples(step().model_copy(update={"examples": []}), [], [], "example")


def test_continuation_preserves_source_and_cannot_claim_an_unpublished_example():
    source = original()
    example = step(source_use="original", source_question_id=source["id"]).examples[0]
    previous = [{**example.model_dump(), "id": "published/0"}]
    continued = step(source_use="original", source_question_id=source["id"],
                     continues_example_id="published/0")
    validate_teaching_examples(continued, [source], previous, "example")
    continued.examples[0].continues_example_id = "unpublished/0"
    with pytest.raises(ModelRetry):
        validate_teaching_examples(continued, [source], previous, "example")


def test_remaining_originals_and_numeric_variants_are_checked():
    source = original()
    used = [{"id": "class/0", "prompt": source["prompt"], "source_question_id": source["id"]}]
    candidates = remaining_candidates([source, {**source, "id": "doc/other",
        "prompt": "Prove that an antiderivative is unique up to a constant."}], used)
    assert not candidates[0]["original_allowed"] and candidates[1]["original_allowed"]
    output = proposed(context_for(["lesson"]))
    question = output.questions[0]
    local = {"classroom_examples": used, "source_candidates": candidates}
    question.prompt = source["prompt"].replace("[0, 1]", "[0, 2]")
    question.example_reference_id = source["id"]
    question.variation_reason = "Changed the numeric bound."
    assert any("数值" in issue for issue in assessment_example_issues(output, local))
    question.prompt = "Infer an integrand from its antiderivative and justify the domain."
    assert not any("数值" in issue for issue in assessment_example_issues(output, local))
    question.example_reference_id = "forged/example"
    assert any("真实" in issue for issue in assessment_example_issues(output, local))


def test_adapted_classroom_example_cannot_only_replace_numeric_values():
    source = original()
    item = step(source["prompt"].replace("[0, 1]", "[0, 2]"), source_use="adapted",
        source_question_id=source["id"], variation_reason="Changed the bound.")
    with pytest.raises(ModelRetry, match="数值"):
        validate_teaching_examples(item, [{**source, "original_allowed": False}], [], "example")


def test_one_scope_point_has_one_completion_boundary_after_both_examples():
    from backend.teaching.feynman import checkpoints
    from backend.teaching.pedagogy import lightweight_plan
    plan = lightweight_plan({"lesson_goal": "Integrate.", "scope_capabilities": [{
        "point_id": "integral", "point_label": "Integral", "capability_id": "cap_integral"}]})
    points = checkpoints(plan.model_dump_json())
    assert len(points) == 1 and points[0]["scope_capability_ids"] == ["cap_integral"]
    assert points[0]["after_step_no"] == 6


def test_independent_semantic_review_rejects_cosmetic_story_change():
    from common_fixtures import synthetic_output_response

    class RejectingAdapter(LearningAdapter):
        def generate(self, messages, **options):
            data = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "user"))
            return synthetic_output_response({"verdicts": [{"question_id": item["question_id"],
                "acceptable": False, "reason": "Only the story changed; the same solution steps remain."}
                for item in data["items"]]}, options)

    items = [{"question_id": "q1", "prompt": "A renamed story.",
              "reference": original(), "source_use": "adapted"}]
    with pytest.raises(ModelRetry, match="独立审查"):
        review_variations(RejectingAdapter(), AgentDeps(), items, {"goal": "Integrate."})
    accepted = review_variations(LearningAdapter(), AgentDeps(), items, {"goal": "Integrate."})
    assert accepted[0]["acceptable"] is True


def test_worked_examples_preserve_problem_without_solution_or_snapshot_hash_changes():
    state = {"pages": 1, "page_results": [{"page_no": 1, "blocks": [
        {"type": "title", "content": "Synthetic chapter", "level": 1},
        {"type": "text", "content": "例1 计算函数 x^2 在区间 [0,1] 的定积分。\n解：先求原函数。\n"
         "例2 证明连续函数满足所给条件。\n解：使用反证法。"}]}]}
    from backend.materials.study_sources import build_study_index
    index = build_study_index(state)
    before = deepcopy(index)
    examples = worked_examples(state, index["sections"])
    assert len(examples) == 2 and index == before
    assert "解：" not in examples[0]["prompt"] and examples[0]["answer_text"] == "先求原函数。"


def test_only_published_owned_matching_revision_examples_count(env):
    _, state, owner, _ = env
    course, item = draft(env, {"title": "Synthetic calculus", "units": [{"title": "Integration",
        "lessons": [{"title": "Integral", "goal": "Compute an integral"}]}]}, kind="course_outline")
    assert confirm(env, item).status_code == 200
    with state.db.session() as sess:
        outline = sess.get(CourseRow, course).current_outline_version_id
        unit = sess.scalar(select(CourseUnitRow).where(CourseUnitRow.outline_version_id == outline))
        lesson = sess.scalar(select(LessonRow).where(LessonRow.unit_id == unit.unit_id))
        version = sess.scalar(select(LessonContentVersionRow).where(
            LessonContentVersionRow.lesson_id == lesson.lesson_id))
        if version is None:
            version = LessonContentVersionRow(content_version_id="version_" + uuid.uuid4().hex,
                lesson_id=lesson.lesson_id, goal_ref=lesson.goal_ref)
            sess.add(version)
            sess.flush()
        lesson_id, content_id = lesson.lesson_id, version.content_version_id
        conv_id = "synthetic_examples_" + uuid.uuid4().hex[:12]
        sess.add(ConversationRow(conversation_id=conv_id, owner_user_id=owner, course_id=course,
            outline_version_id=outline, lesson_id=lesson_id, lesson_content_version_id=content_id,
            kind="teaching", generation=1))
        sess.flush()
        scene_id, step_id = "scene_" + uuid.uuid4().hex, "step_" + uuid.uuid4().hex
        sess.add(BoardSceneRow(scene_id=scene_id, conversation_id=conv_id, generation=1, step_no=1,
            scene_json=json.dumps({"examples": [step().examples[0].model_dump()]})))
        sess.add(TeachingStepRow(step_id=step_id, conversation_id=conv_id, generation=1, step_no=1,
            text="Synthetic published explanation.", status="generating", board_scene_id=scene_id))
        sess.flush()
        def read(actor=owner, revision=content_id):
            return published_examples(sess, actor, course, outline, lesson_id, revision)
        assert not read()
        sess.get(TeachingStepRow, step_id).status = "ready"
        sess.flush()
        assert len(read()) == 1
        sess.get(ConversationRow, conv_id).generation = 2
        sess.flush()
        assert len(read()) == 1  # A follow-up turn does not erase the frozen teaching generation.
        assert not read(actor="another-owner") and not read(revision="another-revision")
        context = {"lessons": [{"lesson_id": lesson_id, "content_version_id": content_id,
                               "source_candidates": [original()]}]}
        state.assessments._attach_examples(sess, context, sess.get(CourseRow, course), owner)
        assert len(context["classroom_examples"]) == 1
        sess.get(ConversationRow, conv_id).deleted_at = datetime.now(timezone.utc)
        sess.flush()
        assert not read()


def test_scope_retrieval_uses_real_selected_uploads_and_accepts_only_returned_quotes(env):
    _, state, owner, _ = env
    payload = {"pages": 1, "page_results": [{"page_no": 1, "blocks": [
        {"type": "title", "content": "Synthetic calculus", "level": 1},
        {"type": "text", "content": "The antiderivative method computes an integral using endpoint values."}]}]}
    ident = attachment(state.db, owner, payload)
    foreign = attachment(state.db, "another-owner", payload)
    with state.db.session() as sess:
        catalog = chapter_catalog(documents(sess, owner, [ident]))
        snapshot = freeze_sources(sess, owner, [ident], [("lesson_a",
            {"source_section_ids": [catalog[0]["id"]]})])
    context = {**scope_context(), "source_snapshot": snapshot}
    checked = []
    search = ScopeMaterialSearch(state.teaching, owner, "synthetic-scope-search", context,
                                 AgentDeps(check_state=lambda: checked.append(True)))
    result = json.loads(search.search_materials("antiderivative integral"))
    assert result["items"] and len(checked) >= 2
    assert all(source["material_id"] == ident for source in context["retrieved_source_sections"])
    assert foreign not in json.dumps(context)
    output = scope_output()
    output.points[0].source_refs.append(ScopeReference(section_id=catalog[0]["id"], quote="endpoint values"))
    validate_scope(output, context)
    output.points[0].source_refs[-1].quote = "Invented quotation."
    with pytest.raises(ModelRetry):
        validate_scope(output, context)
    assert search.receipts[0]["result_count"] > 0
    with state.db.session() as sess:
        from backend.materials.models import MaterialRow
        sess.get(MaterialRow, ident).owner_user_id = "another-owner"
    with pytest.raises(PlanningError):
        search.search_materials("integral")


def test_native_assessment_retries_rejected_variants_before_checkpointing():
    from backend.teaching.assessment import AssessmentService
    from common_fixtures import synthetic_output_response, synthetic_output_schema

    context = context_for(["lesson"])
    example = {**step().examples[0].model_dump(), "id": "class/0"}
    context["example_policy_version"] = 1
    context["classroom_examples"] = [example]
    context["lessons"][0]["classroom_examples"] = [example]
    checkpoint = {}

    def checkpointing(*_, update=None, **__):
        if update:
            update(checkpoint)
        return deepcopy(checkpoint)

    service = SimpleNamespace(_checkpoint=checkpointing, prompts=SimpleNamespace(
        resolve_for_task=lambda *_: {"template_snapshot": "Synthetic assessment generation."}))

    class RetryingAdapter(LearningAdapter):
        def __init__(self):
            super().__init__()
            self.reviews = 0
            self.generations = 0

        def generate(self, messages, **options):
            props = synthetic_output_schema(options).get("properties", {})
            if "questions" in props:
                self.generations += 1
            if "verdicts" in props:
                data = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "user"))
                self.reviews += 1
                return synthetic_output_response({"verdicts": [{
                    "question_id": item["question_id"], "acceptable": self.reviews > 1,
                    "reason": "Synthetic first proposal only renames the story." if self.reviews == 1
                              else "Synthetic repaired task changes the reasoning direction."}
                    for item in data["items"]]}, options)
            return super().generate(messages, **options)

    adapter = RetryingAdapter()
    result = AssessmentService._compute_v2(service, "synthetic-task", adapter, context, None, None)
    assert len(result["questions"]) == 5
    assert adapter.reviews >= 2 and adapter.generations >= 2
    assert "lesson" in checkpoint["_batches"]


def test_source_example_to_remaining_original_assessment_pipeline(env):
    from backend.teaching.learning_models import UnitAssessmentRow
    from backend.teaching.unit_scope import UnitScopeRow
    from test_teaching_assessments import finish, poll
    _, state, owner, headers = env
    client = env[0]
    course, item = draft(env, {"title": "Synthetic integration", "units": [{"title": "Synthetic unit",
        "lessons": [{"title": "Synthetic integral", "goal": "Compute an integral by antiderivatives"}]}]},
        kind="course_outline")
    assert confirm(env, item).status_code == 200
    material = attachment(state.db, owner, {"pages": 1, "page_results": [{"page_no": 1, "blocks": [
        {"type": "title", "content": "Synthetic integral chapter", "level": 1},
        {"type": "text", "content": "Antiderivatives compute definite integrals.\n"
         "Example 1: Compute the integral of x squared on [0, 1].\nSolution: Synthetic first solution.\n"
         "Example 2: Compute the integral of a constant on [0, 2].\nSolution: Synthetic second solution.\n"
         "Example 3: Compute the integral of x cubed on [0, 3].\nSolution: Synthetic third solution."}]}]})
    with state.db.session() as sess:
        outline = sess.get(CourseRow, course).current_outline_version_id
        unit = sess.scalar(select(CourseUnitRow).where(CourseUnitRow.outline_version_id == outline))
        lesson = sess.scalar(select(LessonRow).where(LessonRow.unit_id == unit.unit_id))
        unit_id, lesson_id = unit.unit_id, lesson.lesson_id
        section = chapter_catalog(documents(sess, owner, [material]))[0]
        snapshot = freeze_sources(sess, owner, [material],
            [(lesson_id, {"source_section_ids": [section["id"]]})])
        sess.get(OutlineVersionRow, outline).source_snapshot_json = json.dumps(snapshot)
    state.teaching.model_adapter_factory = lambda *_: LearningAdapter()
    state.assessments.scopes.kick = lambda task: state.assessments.scopes._run(task)
    start = client.post(f"/api/courses/{course}/lessons/{lesson_id}/start", headers=headers)
    assert start.status_code == 201, start.text
    finish(env, start.json())
    with state.db.session() as sess:
        conv = sess.get(ConversationRow, start.json()["conversation_id"])
        examples = published_examples(sess, owner, course, outline, lesson_id, conv.lesson_content_version_id)
        assert len(examples) == 2 and all(e["source_use"] == "original" for e in examples)
        used = {e["source_question_id"] for e in examples}
        scope = sess.scalar(select(UnitScopeRow).where(UnitScopeRow.unit_id == unit_id))
        assert json.loads(scope.scope_json)["material_evidence"]["rounds_used"] == 1
    created = client.post(f"/api/courses/{course}/units/{unit_id}/assessment", headers=headers,
        json={"idempotency_key": "synthetic-example-pipeline"})
    assert created.status_code == 202, created.text
    quiz = poll(client, f"/api/assessments/{created.json()['assessment_id']}", headers)
    assert len(quiz["questions"]) == 5 and all("answer" not in q for q in quiz["questions"])
    with state.db.session() as sess:
        saved = json.loads(sess.get(UnitAssessmentRow, created.json()["assessment_id"]).questions_json)
        originals = [q for q in saved["questions"] if q["source_use"] == "original"]
        assert len(originals) == 1 and originals[0]["source_question_id"] not in used
        assert saved["example_reviews"]
