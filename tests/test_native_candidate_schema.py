"""Offline strict generation schemas and real SDK retry contracts."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from typing import Annotated, Literal

import pytest
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, RootModel, ValidationError, field_validator, model_validator
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from backend.agents.contracts import AgentDeps, GenerationInput
from backend.agents.runner import execute_agent
from backend.agents.schema import generation_model
from backend.planning.errors import PlanningError
from backend.planning.outputs import (
    ChatAnswer, IntakeQuestionSet, LearnerProfile, OutlineCandidate, RouteCandidate,
    validate_route_candidate,
)
from backend.teaching.learning_outputs import (
    AssessmentFeedback, LessonBlueprint, UnitAssessment, validate_blueprint,
)
from backend.teaching.outputs import FollowupCandidate, LessonPlanCandidate, StepCandidate


@pytest.fixture(name="native_synthetic_model_session")
def genuine_sdk_models_only():
    yield


class Nested(BaseModel):
    value: Annotated[int, Field(ge=1, le=5)] = 2

    @field_validator("value")
    @classmethod
    def reject_three(cls, value):
        if value == 3:
            raise ValueError("synthetic rejected value")
        return value

    @property
    def doubled(self):
        return self.value * 2


class Cat(BaseModel):
    kind: Literal["cat"]
    lives: int = Field(ge=1, le=9)


class Dog(BaseModel):
    kind: Literal["dog"]
    trained: bool = False


class RichCandidate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    items: list[Nested] = Field(default_factory=list, max_length=2)
    lookup: dict[str, Nested] = Field(default_factory=dict)
    pair: tuple[Nested, Nested] | None = None
    union: Nested | str | None = None
    pet: Annotated[Cat | Dog, Field(discriminator="kind")]
    name: str = Field(default="default", alias="public_name",
                      validation_alias=AliasChoices("public_name", "name"), serialization_alias="label",
                      min_length=1, max_length=12)

    @model_validator(mode="after")
    def names(self):
        if self.name == "forbidden":
            raise ValueError("synthetic model constraint")
        return self


class Recursive(BaseModel):
    value: int
    children: list[Recursive] = Field(default_factory=list)


class Left(BaseModel):
    right: Right | None = None


class Right(BaseModel):
    left: Left | None = None


Left.model_rebuild()
Right.model_rebuild()


def rich_payload():
    return {"items": [{"value": 2}], "lookup": {"a": {"value": 4}},
            "pair": [{"value": 1}, {"value": 2}], "union": {"value": 5},
            "pet": {"kind": "cat", "lives": 9}, "public_name": "synthetic"}


def test_cached_subclasses_preserve_fields_aliases_defaults_validators_and_properties():
    original_fields = RichCandidate.model_fields.copy()
    original_schema = deepcopy(RichCandidate.model_json_schema())
    strict = generation_model(RichCandidate)
    result = strict.model_validate(rich_payload())
    assert strict is generation_model(RichCandidate) and issubclass(strict, RichCandidate)
    assert isinstance(result, RichCandidate) and isinstance(result.items[0], Nested)
    assert result.items[0].doubled == 4 and result.model_dump(by_alias=True)["label"] == "synthetic"
    first, second = strict(pet={"kind": "dog"}), strict(pet={"kind": "dog"})
    first.items.append(generation_model(Nested)(value=2))
    assert second.items == [] and second.name == "default" and second.pet.trained is False
    assert RichCandidate.model_fields == original_fields
    assert all(RichCandidate.model_fields[name] is field for name, field in original_fields.items())
    assert RichCandidate.model_json_schema() == original_schema
    assert RichCandidate.model_config.get("extra") is None
    assert strict.model_json_schema()["properties"]["pet"]["discriminator"]["propertyName"] == "kind"


@pytest.mark.parametrize("path", [(), ("items", 0), ("lookup", "a"), ("pair", 1), ("union",), ("pet",)])
def test_all_nested_container_variants_reject_extra_without_changing_original(path):
    payload = rich_payload()
    target = payload
    for key in path:
        target = target[key]
    target["unexpected"] = True
    assert isinstance(RichCandidate.model_validate(payload), RichCandidate)
    with pytest.raises(ValidationError) as exc:
        generation_model(RichCandidate).model_validate(payload)
    assert any(item["type"] == "extra_forbidden" for item in exc.value.errors())


@pytest.mark.parametrize("mutate", [
    lambda data: data["items"][0].update(value=0),
    lambda data: data["items"][0].update(value=3),
    lambda data: data.update(items=[{"value": 1}] * 3),
    lambda data: data.update(public_name=""),
    lambda data: data.update(public_name="forbidden"),
    lambda data: data["pet"].update(kind="unknown"),
    lambda data: data["pet"].update(lives=10),
])
def test_field_and_inherited_model_validators_are_preserved(mutate):
    payload = rich_payload()
    mutate(payload)
    with pytest.raises(ValidationError):
        RichCandidate.model_validate(payload)
    with pytest.raises(ValidationError):
        generation_model(RichCandidate).model_validate(payload)


def test_recursive_and_mutually_recursive_graphs_are_strict_and_cached():
    recursive = generation_model(Recursive)
    assert isinstance(recursive.model_validate({"value": 1, "children": [{"value": 2}]}).children[0], recursive)
    with pytest.raises(ValidationError):
        recursive.model_validate({"value": 1, "children": [{"value": 2, "unexpected": 1}]})
    left = generation_model(Left)
    result = left.model_validate({"right": {"left": {"right": None}}})
    assert isinstance(result.right, generation_model(Right)) and isinstance(result.right.left, left)
    with pytest.raises(ValidationError):
        left.model_validate({"right": {"left": {"unexpected": 1}}})
    assert left.model_json_schema()


def test_concurrent_schema_cache_is_stable():
    with ThreadPoolExecutor(max_workers=4) as pool:
        variants = list(pool.map(generation_model, [RichCandidate] * 16))
    assert all(variant is variants[0] for variant in variants)


def test_root_container_preserves_root_semantics_and_strict_nested_models():
    original = RootModel[list[Nested]]
    strict = generation_model(original)
    assert isinstance(strict.model_validate([{"value": 2}]), original)
    with pytest.raises(ValidationError):
        strict.model_validate([{"value": 2, "unexpected": 1}])
    assert original.model_validate([{"value": 2, "unexpected": 1}]).root[0].value == 2


CANDIDATES = [RouteCandidate, OutlineCandidate, ChatAnswer, IntakeQuestionSet,
    LearnerProfile, LessonBlueprint, LessonPlanCandidate, StepCandidate,
    FollowupCandidate, UnitAssessment, AssessmentFeedback]


@pytest.mark.parametrize("candidate", CANDIDATES)
def test_every_actual_candidate_schema_and_nested_object_is_strict(candidate):
    original = deepcopy(candidate.model_json_schema())
    strict = generation_model(candidate)
    assert issubclass(strict, candidate)
    schema = strict.model_json_schema()
    def inspect(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False
            for value in node.values():
                inspect(value)
        elif isinstance(node, list):
            for item in node:
                inspect(item)
    inspect(schema)
    assert candidate.model_json_schema() == original


def route_payload(count=1):
    return {"stages": [{"title": "synthetic", "courses": [
        {"title": f"course-{number}"} for number in range(count)]}]}


def step_payload():
    return {"title": "synthetic", "board": {"title": "board", "nodes": [{"id": "n", "label": "node"}]},
            "beats": [{"id": "beat", "text": "x" * 80, "blocks": []}]}


def test_step_before_and_after_validators_preserve_derived_text_and_nested_types():
    strict = generation_model(StepCandidate)
    step = strict.model_validate(step_payload())
    assert step.text == "x" * 80 and step.beats[0].text == step.text
    assert isinstance(step, StepCandidate)
    bad = step_payload()
    bad["text"] = "y" * 80
    with pytest.raises(ValidationError):
        strict.model_validate(bad)
    bad = step_payload()
    bad["board"]["nodes"][0]["unexpected"] = 1
    with pytest.raises(ValidationError):
        strict.model_validate(bad)


PROFILES = {
    "native": {"supports_json_schema_output": True, "supports_tools": True, "supports_text_output": True},
    "tool": {"supports_json_schema_output": False, "supports_tools": True, "supports_text_output": True},
    "prompted": {"supports_json_schema_output": False, "supports_tools": False, "supports_text_output": True},
}
INPUT = GenerationInput.from_messages([{"role": "user", "content": "synthetic generation"}])


def sdk_run(candidate, payloads, mode, *, validator=None):
    calls, modes = [], []
    def function(messages, info):
        calls.append(messages)
        actual = info.model_request_parameters.output_mode
        modes.append(actual)
        payload = payloads[min(len(calls) - 1, len(payloads) - 1)]
        return ModelResponse(parts=[ToolCallPart("submit_candidate", payload)] if actual == "tool"
            else [TextPart(json.dumps(payload))])
    deps = AgentDeps()
    model = FunctionModel(function, profile=PROFILES[mode])
    result = asyncio.run(execute_agent(model, candidate, INPUT, deps, output_validator=validator))
    return result, deps, calls, modes


@pytest.mark.parametrize("mode", PROFILES)
@pytest.mark.parametrize("extra_path", [(), ("stages", 0), ("stages", 0, "courses", 0)])
def test_sdk_each_mode_retries_root_and_nested_extra_without_mode_fallback(mode, extra_path):
    bad = route_payload()
    target = bad
    for key in extra_path:
        target = target[key]
    target["unexpected"] = 1
    seen = []
    def validator(ctx, output):
        assert isinstance(output, RouteCandidate)
        seen.append(output)
        validate_route_candidate(output)
        return output
    result, deps, calls, modes = sdk_run(RouteCandidate, [bad, route_payload()], mode, validator=validator)
    assert result.mode == mode and result.degraded_from == []
    assert deps.budget.logical_requests == deps.budget.usage.requests == result.requests == 2
    assert len(seen) == 1 and len(calls) == 2 and modes == [mode, mode]
    assert "unexpected" in str(calls[1]) and "Extra inputs are not permitted" in str(calls[1])


@pytest.mark.parametrize("mode", PROFILES)
def test_sdk_extra_exhaustion_is_controlled_without_mode_fallback(mode):
    bad = route_payload()
    bad["unexpected"] = 1
    calls = []
    def function(messages, info):
        calls.append(info.model_request_parameters.output_mode)
        return ModelResponse(parts=[ToolCallPart("submit_candidate", bad)] if mode == "tool"
            else [TextPart(json.dumps(bad))])
    deps = AgentDeps()
    with pytest.raises(PlanningError) as exc:
        asyncio.run(execute_agent(FunctionModel(function, profile=PROFILES[mode]), RouteCandidate, INPUT, deps))
    assert exc.value.code == "OUTPUT_INVALID"
    assert calls == [mode] * 3 and deps.budget.logical_requests == deps.budget.usage.requests == 3


def test_course_per_stage_boundary_is_eight_and_nine_rejected():
    strict = generation_model(RouteCandidate)
    assert validate_route_candidate(strict.model_validate(route_payload(8)))["course_count"] == 8
    with pytest.raises(PlanningError) as exc:
        validate_route_candidate(strict.model_validate(route_payload(9)))
    assert exc.value.code == "ROUTE_STRUCTURE_INVALID"


@pytest.mark.parametrize("mode", PROFILES)
def test_business_route_boundary_retry_remains_inside_sdk_in_each_mode(mode):
    def validator(ctx, output):
        try:
            validate_route_candidate(output)
        except PlanningError:
            raise ModelRetry("synthetic stage course maximum is eight")
        return output
    result, deps, calls, modes = sdk_run(RouteCandidate, [route_payload(9), route_payload(8)], mode,
                                      validator=validator)
    assert isinstance(result.payload, RouteCandidate)
    assert result.requests == deps.budget.logical_requests == 2 and modes == [mode, mode]
    assert "maximum is eight" in str(calls[1])


def test_saved_legacy_blueprints_remain_permissive_but_new_nested_fields_are_strict():
    payload = {"goals": [{"id": "g", "label": "goal", "legacy": True}],
        "stages": [{"phase": phase, "goal_ids": ["g"], "purpose": "synthetic", "legacy": True}
                   for phase in ["intuition", "concept", "example", "summary"]], "legacy": True}
    saved = LessonBlueprint.model_validate(payload)
    assert validate_blueprint(None, saved) is saved and saved.stages[0].segments == []
    with pytest.raises(ValidationError):
        generation_model(LessonBlueprint).model_validate(payload)
