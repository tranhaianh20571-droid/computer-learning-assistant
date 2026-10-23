"""Synthetic graphics, private projection and deterministic assessment boundaries."""
from copy import deepcopy
import json

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select

from backend.teaching.graphics import (
    CircuitFigure, CircuitDrawing, PlotDrawing, PlotFigure, DrawingAnswer,
    expression_tree, evaluate, circuit_table, drawing_grade, validate_drawing_answer,
)
from backend.teaching.board_blocks import BLOCK_ADAPTER
from backend.teaching.board_alignment import board_targets
from backend.teaching.learning_outputs import AssessmentQuestion
from backend.teaching.assessment import objective_grades
from backend.teaching.assessment_contract import AssessmentFigure
from backend.teaching.learning_models import UnitAssessmentRow
from backend.planning.models import CourseRow, CourseUnitRow, LessonRow
from test_course_library import database, env, draft, confirm  # noqa: F401
from test_teaching_assessments import LearningAdapter, poll
from test_assessment_content_v2 import context_for
from common_fixtures import synthetic_output_response, synthetic_output_schema, make_user, auth_headers


def circuit():
    return {"kind": "circuit", "inputs": ["A", "B", "C"], "outputs": ["Y"],
            "gates": [{"id": "n", "type": "NOT", "x": 150, "y": 100},
                      {"id": "a", "type": "AND", "x": 300, "y": 30},
                      {"id": "o", "type": "OR", "x": 480, "y": 80}],
            "wires": [{"source": "B", "target": "n", "port": 0},
                      {"source": "A", "target": "a", "port": 0},
                      {"source": "n", "target": "a", "port": 1},
                      {"source": "a", "target": "o", "port": 0},
                      {"source": "C", "target": "o", "port": 1},
                      {"source": "o", "target": "Y", "port": 0}]}


def question():
    return {"id": "drawing", "kind": "drawing", "prompt": "Synthetic: construct a logic circuit.",
            "points": 20, "goal": "Synthetic Boolean function", "lesson_ids": ["lesson"],
            "answer": ["Synthetic reference"], "rubric": ["Equivalent Boolean outputs"],
            "explanation": "Synthetic explanation.", "drawing": {
                "kind": "circuit", "inputs": ["A", "B", "C"], "outputs": ["Y"],
                "allowed_gates": ["NOT", "AND", "OR"]}, "solution": circuit()}


@pytest.mark.parametrize("source", [
    "__import__('os')", "x.__class__", "x[0]", "f(x)", "x=1", "x^x",
    "2^100000", "2^(2^10)", "sqrt(x,y)", "True", "[1,2]", "1e309", "x;1",
    "("*40+"x"+")"*40, "sin("*20+"x"+")"*20,
])
def test_expression_rejects_executable_or_expensive_sources(source):
    # Redundant parentheses are harmless; AST depth is what matters.
    if source.startswith("("*40):
        assert evaluate(expression_tree(source), {"x": 2}) == 2
        return
    with pytest.raises(ValueError):
        expression_tree(source)


def test_numeric_domain_failures_are_gaps_and_powers_match():
    assert evaluate(expression_tree("x^3-3*x"), {"x": -1}) == 2
    assert evaluate(expression_tree("sqrt(x)"), {"x": -1}) is None
    assert evaluate(expression_tree("1/x"), {"x": 0}) is None
    assert evaluate(expression_tree("exp(x)"), {"x": 1e4}) is None
    assert evaluate(expression_tree("x^(-2)"), {"x": 2}) == .25


def test_typed_figures_and_narration_targets():
    block = BLOCK_ADAPTER.validate_python({"id": "curve", "type": "figure", "graphic": {
        "kind": "plot", "curves": [{"expression": "x^3-3*x"}], "points": [{"x": -1, "y": 2}]}})
    assert "figure" in board_targets(block)
    surface = BLOCK_ADAPTER.validate_python({"id": "surface", "type": "figure", "graphic": {
        "kind": "surface", "expression": "x^2-y^2"}})
    assert surface.graphic.kind == "surface"
    with pytest.raises(ValidationError):
        AssessmentFigure(kind="generated_figure", task="Synthetic read graph", graphic=surface.graphic.model_dump())
    with pytest.raises(ValidationError):
        BLOCK_ADAPTER.validate_python({"id": "empty", "type": "figure", "graphic": {"kind": "plot"}})


@pytest.mark.parametrize("change", ["nan", "unknown", "multi", "port", "duplicate", "over_budget"])
def test_bad_circuit_shapes_rejected(change):
    value = circuit()
    if change == "nan":
        value["gates"][0]["x"] = float("nan")
    elif change == "unknown":
        value["wires"][0]["source"] = "missing"
    elif change == "multi":
        value["wires"].append(value["wires"][0])
    elif change == "port":
        value["wires"][0]["port"] = 1
    elif change == "duplicate":
        value["gates"][0]["id"] = "A"
    else:
        value["inputs"] = list("ABCDEFG")
    with pytest.raises(ValidationError):
        CircuitFigure.model_validate(value)


def test_circuit_complete_truth_table_and_equivalent_layout():
    value = CircuitFigure.model_validate(circuit())
    assert circuit_table(value) == [(False,), (True,), (False,), (True,),
                                    (True,), (True,), (False,), (True,)]
    answer = circuit()
    answer["gates"][0]["x"] = 500
    answer["gates"][0]["y"] = 600
    swapped = [w for w in answer["wires"] if w["target"] == "a"]
    for wire in swapped:
        wire["port"] = 1 - wire["port"]
    assert drawing_grade(question(), answer)["earned"] == 20


def test_incomplete_and_looped_answers_can_submit_but_cannot_score_as_complete():
    spec = CircuitDrawing.model_validate(question()["drawing"])
    missing = circuit()
    missing["wires"].pop()
    assert validate_drawing_answer(missing, spec)
    assert drawing_grade(question(), missing)["earned"] == 0
    looping = circuit()
    looping["wires"][0]["source"] = "a"
    assert drawing_grade(question(), looping)["earned"] == 0
    for answer in (missing, looping):
        invalid = question()
        invalid["solution"] = answer
        with pytest.raises(ValidationError):
            AssessmentQuestion.model_validate(invalid)


def test_fixed_terminals_allowed_gates_and_empty_answers():
    spec = CircuitDrawing.model_validate(question()["drawing"])
    wrong = circuit()
    wrong["inputs"] = ["C", "B", "A"]
    with pytest.raises(ValueError):
        validate_drawing_answer(wrong, spec)
    wrong = circuit()
    wrong["gates"][0]["type"] = "BUFFER"
    with pytest.raises(ValueError):
        validate_drawing_answer(wrong, spec)
    empty = {"kind": "circuit", "inputs": spec.inputs, "outputs": spec.outputs}
    with pytest.raises(ValueError):
        validate_drawing_answer(empty, spec)


def test_plot_grading_expression_sketch_points_and_extra_geometry():
    value = question()
    value["drawing"] = {"kind": "plot", "bounds": {"x": [-2, 2], "y": [-4, 4]}}
    value["solution"] = {"kind": "plot", "bounds": value["drawing"]["bounds"],
                         "curves": [{"expression": "x^3-3*x"}], "points": [{"x": -1, "y": 2}]}
    q = AssessmentQuestion.model_validate(value).model_dump()
    correct = {"kind": "plot", "curves": [{"expression": "x*x*x-3*x"}], "points": [{"x": -1, "y": 2}]}
    assert drawing_grade(q, correct)["earned"] == 20
    wrong = deepcopy(correct)
    wrong["curves"][0]["expression"] = "x^2"
    assert drawing_grade(q, wrong)["earned"] < 20
    extra = deepcopy(correct)
    extra["points"].append({"x": 0, "y": 0})
    assert drawing_grade(q, extra)["earned"] < 20
    sketch = {"kind": "plot", "points": correct["points"], "strokes": [[
        {"x": x / 50, "y": (x / 50)**3 - 3 * x / 50} for x in range(-100, 101)]]}
    assert drawing_grade(q, sketch)["earned"] == 20
    short = deepcopy(sketch)
    short["strokes"][0] = short["strokes"][0][90:110]
    assert drawing_grade(q, short)["earned"] < 20
    forbidden = PlotDrawing(kind="plot", allow_expression=False)
    with pytest.raises(ValueError):
        validate_drawing_answer(correct, forbidden)
    with pytest.raises(ValidationError):
        TypeAdapter(DrawingAnswer).validate_python({"kind": "plot", "strokes": [[{"x": 0, "y": 0}]*301]})


def test_ordinary_questions_cannot_hide_drawing_references():
    value = question()
    value["kind"] = "short"
    with pytest.raises(ValidationError):
        AssessmentQuestion.model_validate(value)
    assert objective_grades([question()], {"drawing": circuit()})[0]["earned"] == 20


def test_generated_figure_is_typed_and_requires_reading_task():
    figure = {"kind": "generated_figure", "task": "Read synthetic curve",
              "graphic": {"kind": "plot", "curves": [{"expression": "sin(x)"}]}}
    assert AssessmentFigure.model_validate(figure).graphic.kind == "plot"
    for changed in [figure | {"task": ""}, figure | {"graphic": None},
                    figure | {"graphic": {"kind": "surface", "expression": "x*y"}}]:
        with pytest.raises(ValidationError):
            AssessmentFigure.model_validate(changed)


def test_reference_cannot_be_empty_offscreen_or_precompleted_in_starter():
    value = question()
    value["drawing"]["starter"] = circuit()
    with pytest.raises(ValidationError):
        AssessmentQuestion.model_validate(value)
    value = question()
    value["drawing"] = {"kind": "plot", "bounds": {"x": [-2, 2], "y": [-4, 4]}}
    value["solution"] = {"kind": "plot", "bounds": value["drawing"]["bounds"],
                         "curves": [{"expression": "1000"}], "points": []}
    with pytest.raises(ValidationError):
        AssessmentQuestion.model_validate(value)
    value["solution"]["curves"] = [{"expression": "x"}]
    value["solution"]["points"] = [{"x": 90, "y": 0}]
    with pytest.raises(ValidationError):
        AssessmentQuestion.model_validate(value)


def test_drawing_generation_projection_submit_grading_and_owner_fences(env, monkeypatch):
    client, state, owner, headers = env
    course, item = draft(env, {"title": "Synthetic graphics", "units": [{"title": "Synthetic logic", "lessons": [
        {"title": "Synthetic Boolean logic", "goal": "Construct a circuit"}]}]}, kind="course_outline")
    assert confirm(env, item).status_code == 200
    with state.db.session() as session:
        outline = session.get(CourseRow, course).current_outline_version_id
        unit = session.scalar(select(CourseUnitRow).where(CourseUnitRow.outline_version_id == outline))
        unit_id = unit.unit_id
        lesson_id = session.scalar(select(LessonRow.lesson_id).where(LessonRow.unit_id == unit_id))
    context = context_for([lesson_id])
    monkeypatch.setattr(state.assessments, "_context", lambda *_: deepcopy(context))

    class GraphicAdapter(LearningAdapter):
        grade_calls = 0

        def generate(self, messages, **options):
            schema = synthetic_output_schema(options)
            if "questions" in schema.get("properties", {}):
                q = question() | {"lesson_ids": [lesson_id], "primary_lesson_id": lesson_id}
                payload = {"title": "Synthetic graphics", "assessment_version": 2,
                           "lesson_quotas": context["lesson_quotas"],
                           "questions": [q | {"id": f"q{i}", "prompt": f"Synthetic circuit task {i}"} for i in range(5)]}
                return synthetic_output_response(payload, options)
            if "grades" in schema.get("properties", {}):
                self.grade_calls += 1
                raise AssertionError("Deterministic drawings must not be sent to model grading")
            return super().generate(messages, **options)

    adapter = GraphicAdapter()
    state.teaching.model_adapter_factory = lambda *_: adapter
    result = client.post(f"/api/courses/{course}/units/{unit_id}/assessment", headers=headers,
                         json={"idempotency_key": "synthetic-graphics"})
    assert result.status_code == 202, result.text
    ident = result.json()["assessment_id"]
    view = poll(client, f"/api/assessments/{ident}", headers)
    assert view["status"] == "succeeded", view
    assert len(view["questions"]) == 5
    for q in view["questions"]:
        assert q["drawing"]["kind"] == "circuit"
        assert not {"solution", "answer", "rubric", "explanation"} & q.keys()
    _, other_token = make_user(client, state.db)
    other = auth_headers(other_token)
    assert client.get(f"/api/assessments/{ident}", headers=other).status_code == 404
    answers = {q["id"]: circuit() for q in view["questions"]}
    bad = deepcopy(answers)
    bad[next(iter(bad))]["inputs"] = ["C", "B", "A"]
    assert client.post(f"/api/assessments/{ident}/attempts", headers=headers,
                       json={"idempotency_key": "synthetic-attempt", "answers": bad}).status_code == 400
    state.teaching.model_adapter_factory = None
    response = client.post(f"/api/assessments/{ident}/attempts", headers=headers,
                           json={"idempotency_key": "synthetic-attempt", "answers": answers})
    assert response.status_code == 202, response.text
    replay = client.post(f"/api/assessments/{ident}/attempts", headers=headers,
                        json={"idempotency_key": "synthetic-attempt", "answers": answers})
    assert replay.json()["attempt_id"] == response.json()["attempt_id"]
    result = poll(client, f"/api/assessment-attempts/{response.json()['attempt_id']}", headers)
    assert result["status"] == "succeeded", result
    assert result["feedback"]["earned"] == 100
    assert result["feedback"]["grades"][0]["drawing_solution"]["kind"] == "circuit"
    assert adapter.grade_calls == 0
    assert client.get(f"/api/assessment-attempts/{response.json()['attempt_id']}", headers=other).status_code == 404
    with state.db.session() as session:
        row = session.get(UnitAssessmentRow, ident)
        assert json.loads(row.questions_json)["questions"][0]["solution"]["gates"]
    assert client.delete(f"/api/planning/courses/{course}", headers=headers).status_code in (200, 204)
    assert client.get(f"/api/assessments/{ident}", headers=headers).status_code == 404
