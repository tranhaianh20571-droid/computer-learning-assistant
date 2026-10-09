"""Role input boundaries fail before any model, tool or outbound request."""
import pytest
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel

from backend.agents.contracts import AgentDeps, GenerationInput, IntakeInput, TeachingStepInput
from backend.agents.roles import intake_agent


def test_role_rejects_other_role_input_before_execution():
    source = TestModel()
    with pytest.raises(TypeError, match="role-specific typed input"):
        intake_agent.interview(source, TeachingStepInput.from_messages([
            {"role": "user", "content": "synthetic"}]), AgentDeps())


def test_role_requires_explicit_trusted_deps():
    with pytest.raises(TypeError, match="trusted AgentDeps"):
        intake_agent.interview(TestModel(), IntakeInput.from_messages([
            {"role": "user", "content": "synthetic"}]), None)


@pytest.mark.parametrize("message", [
    {"role": "tool", "content": "synthetic"},
    {"role": "user", "content": 123},
    {"role": "user", "content": "synthetic", "authorization": "synthetic"},
    {"role": "user", "content": "x" * 262145},
])
def test_prompt_envelope_rejects_invalid_message(message):
    with pytest.raises(ValidationError):
        GenerationInput.from_messages([message])


def test_history_requires_native_model_messages():
    with pytest.raises(TypeError, match="native ModelMessage history"):
        GenerationInput.from_messages([{"role": "user", "content": "synthetic"}],
            [{"role": "assistant", "content": "synthetic"}])


def test_input_copies_caller_message_mapping():
    message = {"role": "user", "content": "synthetic"}
    bound = IntakeInput.from_messages([message])
    message["content"] = "modified"
    assert bound.messages[0]["content"] == "synthetic"
