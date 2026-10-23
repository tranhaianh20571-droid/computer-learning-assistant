"""Offline generation schemas and real SDK retry contracts."""
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
from pydantic_ai.models import _customize_output_object
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.output import OutputObjectDefinition
from pydantic_ai.profiles.google import GoogleJsonSchemaTransformer

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
    assert strict.model_config.get("extra") is None
    assert strict.model_json_schema()["properties"]["pet"]["discriminator"]["propertyName"] == "kind"


@pytest.mark.parametrize("path", [(), ("items", 0), ("lookup", "a"), ("pair", 1), ("union",), ("pet",)])
def test_all_nested_container_variants_ignore_extra_like_domain(path):
    payload = rich_payload()
    target = payload
    for key in path:
        target = target[key]
    target["unexpected"] = True
    original = RichCandidate.model_validate(payload)
    variant = generation_model(RichCandidate).model_validate(payload)
    assert isinstance(original, RichCandidate) and isinstance(variant, RichCandidate)
    assert variant.model_dump() == original.model_dump()


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


def test_recursive_and_mutually_recursive_graphs_are_cached_and_permissive():
    recursive = generation_model(Recursive)
    assert isinstance(recursive.model_validate({"value": 1, "children": [{"value": 2}]}).children[0], recursive)
    tolerant = recursive.model_validate({"value": 1, "children": [{"value": 2, "unexpected": 1}]})
    assert tolerant.children[0].value == 2 and not hasattr(tolerant.children[0], "unexpected")
    left = generation_model(Left)
    result = left.model_validate({"right": {"left": {"right": None}}})
    assert isinstance(result.right, generation_model(Right)) and isinstance(result.right.left, left)
    assert isinstance(left.model_validate({"right": {"left": {"unexpected": 1}}}).right, generation_model(Right))
    assert left.model_json_schema()


def test_concurrent_schema_cache_is_stable():
    with ThreadPoolExecutor(max_workers=4) as pool:
        variants = list(pool.map(generation_model, [RichCandidate] * 16))
    assert all(variant is variants[0] for variant in variants)


def test_root_container_preserves_root_semantics_and_permissive_nested_models():
    original = RootModel[list[Nested]]
    variant = generation_model(original)
    assert isinstance(variant.model_validate([{"value": 2}]), original)
    parsed = variant.model_validate([{"value": 2, "unexpected": 1}])
    assert parsed.root[0].value == 2 and not hasattr(parsed.root[0], "unexpected")
    assert original.model_validate([{"value": 2, "unexpected": 1}]).root[0].value == 2


CANDIDATES = [RouteCandidate, OutlineCandidate, ChatAnswer, IntakeQuestionSet,
    LearnerProfile, LessonBlueprint, LessonPlanCandidate, StepCandidate,
    FollowupCandidate, UnitAssessment, AssessmentFeedback]


@pytest.mark.parametrize("candidate", CANDIDATES)
def test_every_actual_candidate_schema_matches_domain_strictness(candidate):
    """A generation schema may not be stricter than the domain class it derives from.

    Only the domain classes that opt in themselves (the board block union) may carry
    ``additionalProperties``; ``generation_model`` must add none of its own.
    """
    original = deepcopy(candidate.model_json_schema())
    variant = generation_model(candidate)
    assert issubclass(variant, candidate)
    domain_text = json.dumps(original)
    schema_text = json.dumps(variant.model_json_schema())
    assert schema_text.count("additionalProperties") == domain_text.count("additionalProperties")
    assert '"additionalProperties": true' not in schema_text
    assert candidate.model_json_schema() == original


def route_payload(count=1):
    return {"stages": [{"title": "synthetic", "courses": [
        {"title": f"course-{number}"} for number in range(count)]}]}


def step_payload():
    return {"title": "synthetic", "board": {"title": "board", "nodes": [{"id": "n", "label": "node"}]},
            "beats": [{"id": "beat", "text": "x" * 80, "blocks": []}]}


def test_step_before_and_after_validators_preserve_derived_text_and_nested_types():
    variant = generation_model(StepCandidate)
    step = variant.model_validate(step_payload())
    assert step.text == "x" * 80 and step.beats[0].text == step.text
    assert isinstance(step, StepCandidate)
    bad = step_payload()
    bad["text"] = "y" * 80
    with pytest.raises(ValidationError):
        variant.model_validate(bad)
    tolerant = step_payload()
    tolerant["board"]["nodes"][0]["unexpected"] = 1
    parsed = variant.model_validate(tolerant)
    assert parsed.board.nodes[0].label == "node" and not hasattr(parsed.board.nodes[0], "unexpected")


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
def test_sdk_each_mode_accepts_root_and_nested_extra_without_retry(mode, extra_path):
    """A volunteered undeclared key must not fail generation (the 2026-10-10 regression)."""
    payload = route_payload()
    target = payload
    for key in extra_path:
        target = target[key]
    target["unexpected"] = 1
    target["scope"] = ""
    seen = []
    def validator(ctx, output):
        assert isinstance(output, RouteCandidate)
        seen.append(output)
        validate_route_candidate(output)
        return output
    result, deps, calls, modes = sdk_run(RouteCandidate, [payload], mode, validator=validator)
    assert result.mode == mode and result.degraded_from == []
    assert deps.budget.logical_requests == deps.budget.usage.requests == result.requests == 1
    assert len(seen) == 1 and len(calls) == 1 and modes == [mode]
    assert not hasattr(seen[0], "unexpected") and not hasattr(seen[0], "scope")
    assert validate_route_candidate(seen[0])["course_count"] == 1


@pytest.mark.parametrize("mode", PROFILES)
def test_sdk_each_mode_retries_business_rejection_without_mode_fallback(mode):
    reject_once = {"done": False}
    def validator(ctx, output):
        if not reject_once["done"]:
            reject_once["done"] = True
            raise ModelRetry("synthetic business rejection")
        return output
    result, deps, calls, modes = sdk_run(RouteCandidate, [route_payload(), route_payload()], mode,
                                         validator=validator)
    assert result.mode == mode and result.degraded_from == []
    assert deps.budget.logical_requests == deps.budget.usage.requests == result.requests == 2
    assert len(calls) == 2 and modes == [mode, mode]
    assert "synthetic business rejection" in str(calls[1])


@pytest.mark.parametrize("mode", PROFILES)
def test_sdk_validator_exhaustion_is_controlled_without_mode_fallback(mode):
    def validator(ctx, output):
        raise ModelRetry("synthetic permanent rejection")
    calls = []
    def function(messages, info):
        calls.append(info.model_request_parameters.output_mode)
        payload = route_payload()
        return ModelResponse(parts=[ToolCallPart("submit_candidate", payload)] if mode == "tool"
            else [TextPart(json.dumps(payload))])
    deps = AgentDeps()
    with pytest.raises(PlanningError) as exc:
        asyncio.run(execute_agent(FunctionModel(function, profile=PROFILES[mode]), RouteCandidate, INPUT, deps,
                                  output_validator=validator))
    assert exc.value.code == "OUTPUT_INVALID"
    assert calls == [mode] * 3 and deps.budget.logical_requests == deps.budget.usage.requests == 3


def test_course_per_stage_boundary_is_eight_and_nine_rejected():
    variant = generation_model(RouteCandidate)
    assert validate_route_candidate(variant.model_validate(route_payload(8)))["course_count"] == 8
    with pytest.raises(PlanningError) as exc:
        validate_route_candidate(variant.model_validate(route_payload(9)))
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


def test_saved_and_generated_blueprints_are_equally_permissive():
    payload = {"goals": [{"id": "g", "label": "goal", "legacy": True}],
        "stages": [{"phase": phase, "goal_ids": ["g"], "purpose": "synthetic", "legacy": True}
                   for phase in ["intuition", "concept", "example", "summary"]], "legacy": True}
    saved = LessonBlueprint.model_validate(payload)
    assert validate_blueprint(None, saved) is saved and saved.stages[0].segments == []
    generated = generation_model(LessonBlueprint).model_validate(payload)
    assert generated.model_dump() == saved.model_dump()
    assert generated.stages[0].purpose == "synthetic"


@pytest.mark.parametrize("candidate", CANDIDATES)
def test_generation_schema_adds_no_additional_properties(candidate):
    """Regression guard for task_bfa3eee77319: generation_model must not inject `additionalProperties`.

    Nested domain classes may still declare ``extra="forbid"`` on purpose (the board block
    union in ``backend/teaching/board_blocks.py``); the invariant is that the generation
    subclass adds none of its own and stays strict-compatible for Google.
    """
    domain = json.dumps(candidate.model_json_schema())
    variant = generation_model(candidate)
    object_definition = _customize_output_object(
        GoogleJsonSchemaTransformer,
        OutputObjectDefinition(json_schema=variant.model_json_schema()))
    text = json.dumps(object_definition.json_schema)
    assert text.count("additionalProperties") == domain.count("additionalProperties")
    assert object_definition.strict is True


@pytest.mark.parametrize("candidate", [RouteCandidate, OutlineCandidate, ChatAnswer,
                                       IntakeQuestionSet, LearnerProfile])
def test_route_generation_schema_carries_no_additional_properties(candidate):
    """The route/outline generation path that failed must reach the provider with a free schema."""
    variant = generation_model(candidate)
    object_definition = _customize_output_object(
        GoogleJsonSchemaTransformer,
        OutputObjectDefinition(json_schema=variant.model_json_schema()))
    text = json.dumps(object_definition.json_schema)
    assert "additionalProperties" not in text
    assert object_definition.strict is True
