"""Synthetic independent scope contracts; no real materials or model responses."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest
from pydantic_ai.exceptions import ModelRetry
from sqlalchemy import select

from backend.teaching.unit_scope import (ScopeReference, UnitScope, UnitScopeRow, UnitScopeReview,
                                        ScopeCoveragePlan, validate_coverage_plan,
                                        validate_coverage_matches, merge_scope_review, validate_scope)
from backend.tasks.service import LearningTaskRow
from backend.planning.models import CourseRow, CourseUnitRow, LessonRow
from test_course_library import database, env, draft, confirm  # noqa: F401


def scope_context():
    return {"lessons": [{"lesson_id": "lesson_a", "title": "Synthetic sums", "goal": "Compute a sum"}],
            "source_sections": [{"id": "synthetic/section", "text": "Synthetic sums combine two numbers."}],
            "required_section_ids": ["synthetic/section"]}


def scope_output():
    return UnitScope(title="Synthetic scope", points=[{"point_id": "p1", "lesson_id": "lesson_a",
        "label": "Sum", "capabilities": ["Compute a sum and check the result."],
        "boundaries": ["Finite numbers only."], "importance": "normal",
        "source_refs": [{"section_id": "synthetic/section", "quote": "combine two numbers"}]}])


def test_independent_coverage_plan_and_matches_require_complete_owned_mapping():
    context, candidate = scope_context(), scope_output()
    plan = ScopeCoveragePlan(requirements=[{
        "requirement_id": "r1", "lesson_id": "lesson_a", "method_family": "Synthetic operation",
        "cases": ["Synthetic case"], "operations": ["Compute a synthetic result"],
        "goal_basis": context["lessons"][0]["goal"]}])
    assert validate_coverage_plan(plan, context) == plan
    review = UnitScopeReview(rationale="Synthetic audit", coverage_matches=[
        {"requirement_id": "r1", "point_ids": ["p1"]}])
    assert validate_coverage_matches(review, candidate, plan) == review
    review.coverage_matches = []
    with pytest.raises(ModelRetry):
        validate_coverage_matches(review, candidate, plan)
    review = UnitScopeReview(rationale="Synthetic audit", coverage_matches=[
        {"requirement_id": "r1", "point_ids": ["invented"]}])
    with pytest.raises(ModelRetry):
        validate_coverage_matches(review, candidate, plan)
    plan.requirements[0].goal_basis = "A different goal"
    with pytest.raises(ModelRetry):
        validate_coverage_plan(plan, context)


def test_coverage_matches_cannot_duplicate_requirements_or_cross_lessons():
    context, candidate = scope_context(), scope_output()
    plan = ScopeCoveragePlan(requirements=[{
        "requirement_id": "r1", "lesson_id": "lesson_a", "method_family": "Synthetic operation",
        "cases": ["Synthetic case"], "operations": ["Compute a synthetic result"],
        "goal_basis": context["lessons"][0]["goal"]}])
    match = {"requirement_id": "r1", "point_ids": ["p1"]}
    review = UnitScopeReview(rationale="Synthetic audit", coverage_matches=[match, match])
    with pytest.raises(ModelRetry):
        validate_coverage_matches(review, candidate, plan)
    review.coverage_matches.pop()
    candidate.points[0].lesson_id = "other_lesson"
    with pytest.raises(ModelRetry):
        validate_coverage_matches(review, candidate, plan)
    plan.requirements.append(deepcopy(plan.requirements[0]))
    with pytest.raises(ModelRetry):
        validate_coverage_plan(plan, context)


def test_scope_requires_real_sources_and_all_sections():
    context, output = scope_context(), scope_output()
    assert validate_scope(output, context) == output
    output.points[0].source_refs[0].quote = "An invented quotation"
    with pytest.raises(ModelRetry):
        validate_scope(output, context)
    output = scope_output()
    context["required_section_ids"].append("synthetic/missing")
    with pytest.raises(ModelRetry):
        validate_scope(output, context)
    context = scope_context()
    output.points[0].source_refs.clear()
    with pytest.raises(ModelRetry):
        validate_scope(output, context)


def test_scope_rejects_foreign_lessons_duplicate_ids_and_empty_capabilities():
    context, output = scope_context(), scope_output()
    output.points.append(deepcopy(output.points[0]))
    with pytest.raises(ModelRetry):
        validate_scope(output, context)
    output = scope_output()
    output.points[0].lesson_id = "someone_else"
    with pytest.raises(ModelRetry):
        validate_scope(output, context)
    output = scope_output()
    output.points[0].capabilities = [" "]
    with pytest.raises(ModelRetry):
        validate_scope(output, context)


def test_no_materials_requires_exact_outline_goal_not_a_blueprint():
    context, output = scope_context(), scope_output()
    context.update(source_sections=[], required_section_ids=[])
    output.points[0].source_refs = []
    output.points[0].goal_basis = context["lessons"][0]["goal"]
    assert validate_scope(output, context) == output
    output.points[0].basis = "goal"
    assert validate_scope(output, context) == output
    output.points[0].goal_basis = "A shallow classroom goal"
    with pytest.raises(ModelRetry):
        validate_scope(output, context)


def test_supplements_require_honest_goal_basis_without_fabricated_source():
    context, output = scope_context(), scope_output()
    supplement = deepcopy(output.points[0])
    supplement.point_id = "bridge"
    supplement.label = "Synthetic inverse method"
    supplement.basis = "supplement"
    supplement.source_refs = []
    supplement.goal_basis = context["lessons"][0]["goal"]
    supplement.supplement_reason = "The goal requires an inverse operation omitted by these notes."
    output.points.append(supplement)
    assert validate_scope(output, context) == output
    supplement.goal_basis = "An unrelated course goal"
    with pytest.raises(ModelRetry):
        validate_scope(output, context)
    supplement.goal_basis = context["lessons"][0]["goal"]
    supplement.supplement_reason = " "
    with pytest.raises(ModelRetry):
        validate_scope(output, context)
    supplement.supplement_reason = "Necessary inverse operation."
    supplement.source_refs = [ScopeReference(section_id="synthetic/section", quote="Invented inverse method.")]
    with pytest.raises(ModelRetry):
        validate_scope(output, context)


def test_supplements_do_not_replace_source_coverage_or_hide_inside_source_points():
    context, output = scope_context(), scope_output()
    point = output.points[0]
    point.basis, point.source_refs = "supplement", []
    point.goal_basis = context["lessons"][0]["goal"]
    point.supplement_reason = "Synthetic necessary bridge."
    with pytest.raises(ModelRetry, match="必需小节"):
        validate_scope(output, context)
    output = scope_output()
    output.points[0].supplement_reason = "A method absent from the reference."
    with pytest.raises(ModelRetry):
        validate_scope(output, context)


def test_independent_review_only_adds_and_revalidates_complete_scope():
    context, candidate = scope_context(), scope_output()
    context["scope_version"] = 3
    additions = []
    for index in range(5):
        point = deepcopy(candidate.points[0])
        point.point_id = f"bridge_{index}"
        point.basis, point.source_refs = "supplement", []
        point.goal_basis = context["lessons"][0]["goal"]
        point.supplement_reason = "A necessary synthetic method family omitted from the notes."
        additions.append(point)
    review = UnitScopeReview(additions=additions, rationale="Synthetic independent framework audit.")
    merged = merge_scope_review(review, candidate, context)
    assert merged.points[0] == candidate.points[0]
    assert len(merged.points) == 6 and len(candidate.points) == 1
    review.additions[0].point_id = candidate.points[0].point_id
    with pytest.raises(ModelRetry):
        merge_scope_review(review, candidate, context)
    review.additions[0].point_id = "bridge_0"
    review.additions[0].lesson_id = "foreign_lesson"
    with pytest.raises(ModelRetry):
        merge_scope_review(review, candidate, context)


def test_scope_api_is_read_only_owned_and_generation_is_idempotent(env, monkeypatch):
    client, state, owner, headers = env
    course, item = draft(env, {"title": "Synthetic scope course", "units": [
        {"title": "Synthetic scope unit", "lessons": [
            {"title": "Synthetic sums", "goal": "Compute a sum"}]}]}, kind="course_outline")
    assert confirm(env, item).status_code == 200
    with state.db.session() as sess:
        outline = sess.get(CourseRow, course).current_outline_version_id
        unit = sess.scalar(select(CourseUnitRow).where(CourseUnitRow.outline_version_id == outline))
        lesson = sess.scalar(select(LessonRow).where(LessonRow.unit_id == unit.unit_id))
        unit_id, lesson_id = unit.unit_id, lesson.lesson_id
    path = f"/api/courses/{course}/units/{unit_id}/scope"
    assert client.get(path, headers=headers).json()["status"] == "not_generated"
    monkeypatch.setattr(state.assessments.scopes, "kick", lambda _: None)
    first = client.post(path, headers=headers)
    assert first.status_code == 202, first.text
    again = client.post(path, headers=headers)
    assert again.json() == first.json()
    with state.db.session() as sess:
        row = sess.get(UnitScopeRow, first.json()["scope_id"])
        context = json.loads(row.context_json)
        assert context["scope_version"] == 9
        assert set(context) == {"scope_version", "course_title", "unit_title", "outline_version_id",
            "lessons", "source_snapshot", "source_sections", "required_section_ids"}
        assert context["lessons"] == [{"lesson_id": lesson_id, "title": "Synthetic sums", "goal": "Compute a sum"}]
        task = sess.get(LearningTaskRow, row.task_id)
        assert task.kind == "unit_scope_generate"
        assert not sess.scalar(select(UnitScopeRow).where(UnitScopeRow.course_id == course,
                                                         UnitScopeRow.owner_user_id != owner))
        from backend.agents.task_binding import _domain_binding
        scope, fence, category = _domain_binding(sess, task)
        assert scope["lesson_ids"] == [lesson_id]
        assert category == "generated_text" and fence[0] == row.scope_id
        binding = state.prompts.resolve_for_task(row.task_id, "unit_scope_v6")
        assert binding["prompt_name"] == "unit_scope_v6"
        assert state.prompts.resolve_for_task(row.task_id, "unit_scope_review_v5")["prompt_name"] == "unit_scope_review_v5"
        assert state.prompts.resolve_for_task(row.task_id, "unit_scope_coverage_v2")
    assert client.get(path, headers=headers).json()["scope"] is None
    assert client.get(path).status_code in (401, 403)
    state.assessments.scopes._run(first.json()["task_id"])
    value = client.get(path, headers=headers).json()
    assert value["status"] == "failed" and value["scope"] is None
    assert value["error_code"] == "MODEL_CAPABILITY_UNAVAILABLE"


@pytest.mark.parametrize("previous_prompt", ["unit_scope_v2", "unit_scope_v3", "unit_scope_v4",
                                           "unit_scope_v5", "unit_scope_v6"])
def test_scope_generation_publishes_frozen_result_and_goal_changes_invalidate(env, monkeypatch, previous_prompt):
    from common_fixtures import SyntheticModelAdapter, synthetic_output_response, make_user, auth_headers
    from backend.agents.task_binding import bind_task_deps
    from backend.materials.study_sources import digest
    from backend.planning.errors import PlanningError
    client, state, owner, headers = env
    course, item = draft(env, {"title": "Synthetic independent scope", "units": [
        {"title": "Synthetic scope unit", "lessons": [
            {"title": "Synthetic sum", "goal": "Compute a sum"}]}]}, kind="course_outline")
    assert confirm(env, item).status_code == 200
    with state.db.session() as sess:
        outline = sess.get(CourseRow, course).current_outline_version_id
        unit_id = sess.scalar(select(CourseUnitRow.unit_id).where(CourseUnitRow.outline_version_id == outline))
    path = f"/api/courses/{course}/units/{unit_id}/scope"
    inputs = []
    reject_review = []
    class ScopeAdapter(SyntheticModelAdapter):
        def generate(self, messages, **options):
            context = json.loads(next(message["content"] for message in reversed(messages)
                                      if message["role"] == "user"))
            if context.get("stage") == "scope_coverage_plan":
                assert "candidate" not in context
                return synthetic_output_response({"requirements": [
                    {"requirement_id": f"r{index}", "lesson_id": lesson["lesson_id"],
                     "method_family": "Synthetic sum", "cases": ["Finite inputs"],
                     "operations": ["Compute a sum"], "goal_basis": lesson["goal"]}
                    for index, lesson in enumerate(context["context"]["lessons"])]}, options)
            if "candidate" in context:
                if reject_review:
                    foreign = deepcopy(context["candidate"]["points"][0])
                    foreign.update(point_id="foreign", lesson_id="someone_else")
                    return synthetic_output_response({"additions": [foreign],
                        "rationale": "Synthetic invalid review."}, options)
                return synthetic_output_response({"additions": [],
                    "coverage_matches": [{"requirement_id": req["requirement_id"], "point_ids": ["p1"]}
                                         for req in context.get("coverage_plan", {}).get("requirements", [])],
                    "rationale": "The synthetic method and its finite-input conditions are complete."}, options)
            inputs.append(context)
            lesson = context["lessons"][0]
            return synthetic_output_response({"title": "Synthetic frozen scope", "points": [{
                "point_id": "p1", "lesson_id": lesson["lesson_id"], "label": "Synthetic sum",
                "capabilities": ["Compute sums and verify the result."], "boundaries": ["Finite inputs."],
                "importance": "normal", "source_refs": [], "goal_basis": lesson["goal"]}]}, options)
    def factory(actor, task_id):
        adapter = ScopeAdapter()
        adapter.agent_deps = bind_task_deps(state.db, adapter, actor_id=actor, task_id=task_id)
        return adapter
    state.teaching.model_adapter_factory = factory
    scopes = state.assessments.scopes
    monkeypatch.setattr(scopes, "kick", lambda _: None)
    first = client.post(path, headers=headers).json()
    from backend.prompts.service import PromptBindingRow
    # Simulate an already-bound task from the preceding prompt revision.
    with state.db.session() as sess:
        bound = sess.scalar(select(PromptBindingRow).where(
            PromptBindingRow.task_id == first["task_id"], PromptBindingRow.prompt_name == "unit_scope_v6"))
        old_template = state.prompts.load_local_template(previous_prompt)["template"]
        bound.prompt_name = previous_prompt
        bound.template_snapshot = old_template
        bound.template_sha256 = state.prompts._sha256(old_template)
        old_review = sess.scalar(select(PromptBindingRow).where(
            PromptBindingRow.task_id == first["task_id"],
            PromptBindingRow.prompt_name == "unit_scope_review_v5"))
        previous_review = {"unit_scope_v4": "unit_scope_review_v2",
                           "unit_scope_v5": "unit_scope_review_v3",
                           "unit_scope_v6": "unit_scope_review_v4"}.get(previous_prompt, "unit_scope_review_v1")
        review_template = state.prompts.load_local_template(previous_review)["template"]
        old_review.prompt_name = previous_review
        old_review.template_snapshot = review_template
        old_review.template_sha256 = state.prompts._sha256(review_template)
        if previous_prompt == "unit_scope_v6":
            old_plan = sess.scalar(select(PromptBindingRow).where(
                PromptBindingRow.task_id == first["task_id"],
                PromptBindingRow.prompt_name == "unit_scope_coverage_v2"))
            old_plan.prompt_name = "unit_scope_coverage_v1"
            old_plan.template_snapshot = state.prompts.load_local_template("unit_scope_coverage_v1")["template"]
            old_plan.template_sha256 = state.prompts._sha256(old_plan.template_snapshot)
    scopes._run(first["task_id"])
    result = client.get(path, headers=headers).json()
    assert result["status"] == "succeeded", result
    assert result["scope_hash"] == digest(result["scope"])
    assert inputs and "taught" not in json.dumps(inputs) and "blueprint" not in json.dumps(inputs)
    assert client.post(path, headers=headers).json() == first
    with state.db.session() as sess:
        if previous_prompt != "unit_scope_v6":
            assert not sess.scalar(select(PromptBindingRow.binding_id).where(
                PromptBindingRow.task_id == first["task_id"], PromptBindingRow.prompt_name == "unit_scope_v6"))
        assert not sess.scalar(select(PromptBindingRow.binding_id).where(
            PromptBindingRow.task_id == first["task_id"],
            PromptBindingRow.prompt_name == "unit_scope_review_v5"))
        if previous_prompt == "unit_scope_v6":
            assert not sess.scalar(select(PromptBindingRow.binding_id).where(
                PromptBindingRow.task_id == first["task_id"],
                PromptBindingRow.prompt_name == "unit_scope_coverage_v2"))
    assert len(inputs) == 1
    # A policy upgrade reuses a verified frozen candidate, but must run the
    # independent reviewer under a new task rather than directly publishing it.
    with state.db.session() as sess:
        seed = sess.get(UnitScopeRow, first["scope_id"])
        previous = json.loads(seed.context_json)
        previous["scope_version"] = int(previous_prompt.rsplit("v", 1)[1])
        seed.context_json = json.dumps(previous)
        seed.input_hash = digest(previous)
        sess.get(LearningTaskRow, seed.task_id).idempotency_key = "unit-scope:" + digest(
            [owner, unit_id, seed.input_hash])
    upgraded = client.post(path, headers=headers).json()
    assert upgraded["task_id"] != first["task_id"]
    scopes._run(upgraded["task_id"])
    upgraded_result = client.get(path, headers=headers).json()
    assert upgraded_result["status"] == "succeeded"
    assert upgraded_result["scope"]["review"]["seed_scope_hash"] == result["scope_hash"]
    assert upgraded_result["scope"]["coverage_plan"]["requirements"]
    assert upgraded_result["scope"]["coverage_matches"]
    assert len(inputs) == 1
    with state.db.session() as sess:
        from backend.agents.task_binding import AgentBudgetRow
        assert sess.get(AgentBudgetRow, first["task_id"]).logical_requests == (
            3 if previous_prompt == "unit_scope_v6" else 2)
        assert sess.get(AgentBudgetRow, upgraded["task_id"]).logical_requests == 2
    stranger, token = make_user(client, state.db)
    assert stranger != owner
    assert client.get(path, headers=auth_headers(token)).status_code == 404
    with state.db.session() as sess:
        lesson = sess.get(LessonRow, inputs[0]["lessons"][0]["lesson_id"])
        lesson.goal_ref = "Compute a product"
    assert client.get(path, headers=headers).json()["status"] == "not_generated"
    with state.db.session() as sess, pytest.raises(PlanningError):
        scopes._owned(sess, first["scope_id"], owner)
    second = client.post(path, headers=headers).json()
    assert second["task_id"] != first["task_id"]
    scopes._run(second["task_id"])
    assert client.get(path, headers=headers).json()["scope"]["points"][0]["goal_basis"] == "Compute a product"
    with state.db.session() as sess:
        sess.get(LessonRow, inputs[0]["lessons"][0]["lesson_id"]).goal_ref = "Compute an inverse"
    reject_review.append(True)
    rejected = client.post(path, headers=headers).json()
    scopes._run(rejected["task_id"])
    rejected_result = client.get(path, headers=headers).json()
    assert rejected_result["status"] == "failed" and rejected_result["scope"] is None
    assert rejected_result["scope_hash"] == ""
    with state.db.session() as sess:
        from backend.planning.deletion import delete_feature_data
        delete_feature_data(sess, "course", course)
        assert not sess.scalar(select(UnitScopeRow).where(UnitScopeRow.course_id == course))


def test_material_scope_freezes_full_text_and_rejects_deleted_or_changed_source(env, monkeypatch):
    import hashlib
    import uuid
    from backend.materials.models import MaterialRow, DocumentAttachmentRow
    from backend.materials.local_documents import parse_text
    from backend.materials.study_sources import chapter_catalog, documents, freeze_sources, digest
    from backend.planning.models import OutlineVersionRow
    from backend.planning.errors import PlanningError
    from backend.agents.task_binding import _domain_binding
    client, state, owner, headers = env
    course, item = draft(env, {"title": "Synthetic sourced course", "units": [
        {"title": "Synthetic sourced unit", "lessons": [
            {"title": "Synthetic sums", "goal": "Compute a sum"}]}]}, kind="course_outline")
    assert confirm(env, item).status_code == 200
    payload = b"# Synthetic chapter\nIntroductory method.\n\n## Synthetic sums\nCombine two finite inputs.\n"
    parsed = parse_text(payload, "md")
    material_id = "mat_" + uuid.uuid4().hex[:16]
    with state.db.session() as sess:
        outline_id = sess.get(CourseRow, course).current_outline_version_id
        unit = sess.scalar(select(CourseUnitRow).where(CourseUnitRow.outline_version_id == outline_id))
        lesson = sess.scalar(select(LessonRow).where(LessonRow.unit_id == unit.unit_id))
        unit_id, lesson_id = unit.unit_id, lesson.lesson_id
        sess.add(MaterialRow(material_id=material_id, owner_user_id=owner, kind="md",
            filename_hash="synthetic-filename-hash", source_bytes_sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload), status="parsed"))
        sess.flush()
        sess.add(DocumentAttachmentRow(material_id=material_id, owner_user_id=owner,
            upload_key=uuid.uuid4().hex, task_id="synthetic-local",
            result_json=json.dumps(parsed)))
        sess.flush()
        catalog = chapter_catalog(documents(sess, owner, [material_id]))
        snapshot = freeze_sources(sess, owner, [material_id], [
            (lesson_id, {"source_section_ids": [catalog[0]["id"]]})])
        sess.get(OutlineVersionRow, outline_id).source_snapshot_json = json.dumps(snapshot)
    scopes = state.assessments.scopes
    monkeypatch.setattr(scopes, "kick", lambda _: None)
    first = scopes.generate(course, unit_id, owner)
    from backend.materials import study_sources
    original_verify = study_sources.verify_sources
    verified = []
    def counted_verify(*args):
        verified.append(True)
        return original_verify(*args)
    monkeypatch.setattr(study_sources, "verify_sources", counted_verify)
    source_cache = {}
    with state.db.session() as sess:
        row = scopes._owned(sess, first["scope_id"], owner)
        context = json.loads(row.context_json)
        assert "Combine two finite inputs." in json.dumps(context)
        assert "Introductory method." in json.dumps(context)
        assert row.input_hash == digest(context)
        task = sess.get(LearningTaskRow, row.task_id)
        assert _domain_binding(sess, task)[0]["material_ids"] == [material_id]
        verified.clear()
        expected = _domain_binding(sess, task, source_cache=source_cache)
        assert _domain_binding(sess, task, source_cache=source_cache) == expected
        assert len(verified) == 1
        saved_source = sess.get(DocumentAttachmentRow, material_id)
        changed = deepcopy(parsed)
        changed["page_results"][-1]["blocks"][0]["content"] += " A changed source."
        saved_source.result_json = json.dumps(changed)
    with state.db.session() as sess, pytest.raises(PlanningError):
        _domain_binding(sess, sess.get(LearningTaskRow, first["task_id"]))
    with state.db.session() as sess, pytest.raises(PlanningError):
        _domain_binding(sess, sess.get(LearningTaskRow, first["task_id"]), source_cache=source_cache)
    with state.db.session() as sess:
        sess.get(DocumentAttachmentRow, material_id).result_json = json.dumps(parsed)
        sess.get(MaterialRow, material_id).deleted_at = datetime.now(timezone.utc)
    with state.db.session() as sess, pytest.raises(PlanningError):
        scopes._owned(sess, first["scope_id"], owner)
    with state.db.session() as sess, pytest.raises(PlanningError):
        _domain_binding(sess, sess.get(LearningTaskRow, first["task_id"]), source_cache=source_cache)
