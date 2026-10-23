import pytest
from pydantic_ai.exceptions import ModelRetry
from sqlalchemy import select, func
from test_course_library import database, env, draft, confirm  # noqa: F401

from backend.teaching.learning_outputs import validate_new_blueprint
from backend.teaching.pedagogy import lightweight_plan
from backend.teaching.unit_scope import scope_capabilities
from backend.planning.models import ConversationRow, CourseRow, CourseUnitRow, LessonRow
from backend.tasks.service import LearningTaskRow
from backend.planning.models import utcnow
from datetime import timedelta


def requirements(point_count=3):
    scope = {"points": [{"point_id": f"p{number}", "lesson_id": "lesson_a",
        "label": f"Point {number}", "importance": "core",
        "capabilities": [f"Capability {number} definition", f"Capability {number} application"],
        "boundaries": [f"Boundary {number}"]} for number in range(point_count)]}
    values = scope_capabilities(scope, "a" * 64, "lesson_a")
    return scope, values


def test_scope_capability_ids_are_stable_and_bound_to_frozen_revision():
    scope, first = requirements()
    _, repeated = requirements()
    other_revision = scope_capabilities(scope, "b" * 64, "lesson_a")
    assert [item["capability_id"] for item in first] == [
        item["capability_id"] for item in repeated]
    assert set(item["capability_id"] for item in first).isdisjoint(
        item["capability_id"] for item in other_revision)
    assert all(item["point_id"].startswith("p") for item in first)


def test_scoped_fallback_blueprint_maps_all_capabilities_through_three_phases():
    _, capabilities = requirements()
    context = {"lesson_goal": "Compute and explain the synthetic methods",
        "scope_capabilities": capabilities}
    blueprint = validate_new_blueprint(context, lightweight_plan(context))
    schedule = [part for stage in blueprint.stages for part in stage.segments]
    assert {ident for goal in blueprint.goals for ident in goal.scope_capability_ids} == {
        item["capability_id"] for item in capabilities}
    for item in capabilities:
        phases = {stage.phase for stage in blueprint.stages if any(
            item["capability_id"] in segment.scope_capability_ids for segment in stage.segments)}
        assert {"intuition", "concept", "example"} <= phases


def test_scoped_blueprint_rejects_a_capability_missing_from_one_teaching_phase():
    _, capabilities = requirements(2)
    context = {"lesson_goal": "Synthetic goal", "scope_capabilities": capabilities}
    blueprint = lightweight_plan(context)
    concept = next(stage for stage in blueprint.stages if stage.phase == "concept")
    concept.segments[0].scope_capability_ids.remove(capabilities[0]["capability_id"])
    with pytest.raises(ModelRetry):
        validate_new_blueprint(context, blueprint)


def test_scoped_fallback_reorganizes_points_within_the_existing_lesson_capacity():
    _, capabilities = requirements(8)
    context = {"lesson_goal": "Synthetic goal", "scope_capabilities": capabilities}
    blueprint = validate_new_blueprint(context, lightweight_plan(context))
    assert len([segment for stage in blueprint.stages
        for segment in stage.segments]) <= 24


def test_scoped_blueprint_packs_many_points_without_dropping_capability_ids():
    _, capabilities = requirements(120)
    context = {"lesson_goal": "Synthetic full lesson", "scope_capabilities": capabilities}
    blueprint = validate_new_blueprint(context, lightweight_plan(context))
    assert len([segment for stage in blueprint.stages
        for segment in stage.segments]) <= 24
    scheduled = [segment for stage in blueprint.stages for segment in stage.segments]
    assert set().union(*(set(segment.scope_capability_ids) for segment in scheduled)) == {
        item["capability_id"] for item in capabilities}
    from backend.teaching.feynman import checkpoints
    boundaries = checkpoints(blueprint.model_dump_json())
    assert {ident for boundary in boundaries
        for ident in boundary["scope_capability_ids"]} == {
            item["capability_id"] for item in capabilities}


def test_scoped_assessment_must_reference_every_frozen_point():
    from backend.teaching.assessment import question_issues
    from backend.teaching.assessment_validation import quotas_for
    from backend.teaching.learning_outputs import LessonAssessment

    context = {"assessment_version": 2, "scope_hash": "a" * 64, "unit_title": "Synthetic unit",
        "lessons": [{"lesson_id": "lesson", "scope_points": [
            {"id": "p1", "label": "Synthetic point one"},
            {"id": "p2", "label": "Synthetic point two"}],
            "knowledge_points": [{"id": "p1"}, {"id": "p2"}]}]}
    context["lesson_quotas"] = quotas_for(context["lessons"])
    quota = context["lesson_quotas"][0]
    output = LessonAssessment(title="Synthetic scope assessment",
        lesson_quotas=context["lesson_quotas"], questions=[{
            "id": f"q{index}", "kind": "short", "prompt": f"Synthetic question {index}",
            "primary_lesson_id": "lesson", "lesson_ids": ["lesson"], "goal": "Apply frozen scope",
            "answer": ["Synthetic answer"], "rubric": ["Correct method"],
            "explanation": "Synthetic explanation", "knowledge_point_ids": ["p1", "p2"]}
            for index in range(quota["target_questions"])])
    for question in output.questions:
        question.knowledge_point_ids = ["p1", "p2"]
    assert not question_issues(output, {"lesson"}, context)
    for question in output.questions:
        question.knowledge_point_ids = ["p1"]
    assert any("全部知识点" in issue for issue in question_issues(output, {"lesson"}, context))


def test_legacy_blueprint_contract_remains_valid_without_scope():
    plan = lightweight_plan({"lesson_goal": "Legacy goal"})
    validate_new_blueprint({"lesson_goal": "Legacy goal"}, plan)
    assert not any(goal.scope_capability_ids for goal in plan.goals)


def test_pending_scope_has_a_wait_state_and_reentry_reuses_the_session(env, monkeypatch):
    _, state, owner, _ = env
    course, editable = draft(env, {"title": "Synthetic pending scope", "units": [
        {"title": "Synthetic unit", "lessons": [{"title": "Synthetic lesson", "goal": "Explain lookup"}]}
    ]}, kind="course_outline")
    assert confirm(env, editable).status_code == 200
    scopes = state.assessments.scopes
    monkeypatch.setattr(scopes, "kick", lambda _: None)
    with state.db.session() as sess:
        outline = sess.get(CourseRow, course).current_outline_version_id
        unit = sess.scalar(select(CourseUnitRow).where(CourseUnitRow.outline_version_id == outline))
        lesson = sess.scalar(select(LessonRow).where(LessonRow.unit_id == unit.unit_id))
        lesson_id, unit_id = lesson.lesson_id, unit.unit_id
    first = state.teaching.start_lesson(course, lesson_id, actor_id=owner)
    for _ in range(3):
        assert state.teaching.start_lesson(course, lesson_id, actor_id=owner)["conversation_id"] == first["conversation_id"]
    view = state.teaching.get_workspace(first["conversation_id"], actor_id=owner)
    assert view["scope_preparation"] == {"status": "queued", "error_code": None}
    assert view["needs_upgrade"] is False and view["authorization_required"] is False
    assert view["steps"] == []
    with state.db.session() as sess:
        assert sess.scalar(select(func.count()).select_from(ConversationRow).where(
            ConversationRow.owner_user_id == owner, ConversationRow.kind == "teaching")) == 1
    scoped = scopes.get(course, unit_id, owner)
    task = state.tasks.get_task(scoped["task_id"], actor_id=owner)
    state.tasks.fail_queued(task["task_id"], expected_generation=task["generation"],
        expected_revision=task["revision"], error_code="MODEL_PROVIDER_ERROR")
    failed = state.teaching.get_workspace(first["conversation_id"], actor_id=owner)
    assert failed["scope_preparation"] == {"status": "failed", "error_code": "MODEL_PROVIDER_ERROR"}
    assert failed["needs_upgrade"] is False
    recovered = state.teaching.start_lesson(course, lesson_id, actor_id=owner)
    assert recovered["conversation_id"] == first["conversation_id"]
    assert scopes.get(course, unit_id, owner)["status"] == "queued"
    calls = []
    monkeypatch.setattr(scopes, "kick", calls.append)
    lease = state.tasks.claim(scoped["task_id"], lease_owner="synthetic-previous-process", lease_seconds=60)
    scopes.resume(course, unit_id, owner)
    assert calls == []
    with state.db.session() as sess:
        sess.get(LearningTaskRow, lease.task_id).lease_until = utcnow() - timedelta(seconds=1)
    scopes.resume(course, unit_id, owner)
    assert calls == [lease.task_id]
