"""Synthetic goal-only experiment inputs; no provider calls or private content."""
import json

import pytest
from pydantic import ValidationError

from unit_scope_live import load_goal_outline, resume_configuration


def outline():
    return {"title": "Synthetic course", "units": [
        {"title": "Synthetic unit", "lessons": [
            {"title": "Synthetic lesson", "goal": "Explain a synthetic operation."}]}]}


def test_goal_outline_preserves_exact_lesson_goals_without_material_fields():
    value = outline()
    assert load_goal_outline(json.dumps(value)) == value


@pytest.mark.parametrize("mutation", ["extra", "empty_units", "empty_lessons", "blank_goal", "long_goal"])
def test_goal_outline_rejects_invalid_or_unexpected_context(mutation):
    value = outline()
    if mutation == "extra":
        value["exam"] = "Unexpected context"
    elif mutation == "empty_units":
        value["units"] = []
    elif mutation == "empty_lessons":
        value["units"][0]["lessons"] = []
    else:
        value["units"][0]["lessons"][0]["goal"] = " " if mutation == "blank_goal" else "x" * 601
    with pytest.raises(ValidationError):
        load_goal_outline(json.dumps(value))


def test_resume_uses_owned_pinned_configuration_without_role_reselection():
    class Configs:
        def get(self, ident, *, actor_id, is_admin):
            assert (ident, actor_id, is_admin) == ("synthetic-config", "synthetic-owner", False)
            return {"is_active": True, "config_version": 2, "kind": "content_model"}

    result = resume_configuration(Configs(), "synthetic-owner", {
        "source_owner": "synthetic-owner", "source_config_id": "synthetic-config",
        "source_config_version": 2})
    assert result["config_version"] == 2


@pytest.mark.parametrize("state", ["foreign_owner", "inactive", "version_changed", "wrong_kind"])
def test_resume_rejects_invalid_pinned_configuration(state):
    class Configs:
        def get(self, ident, **kwargs):
            assert state != "foreign_owner"
            return {"is_active": state != "inactive",
                    "config_version": 3 if state == "version_changed" else 2,
                    "kind": "tts" if state == "wrong_kind" else "content_model"}

    with pytest.raises(SystemExit):
        resume_configuration(Configs(), "synthetic-owner", {
            "source_owner": "another-owner" if state == "foreign_owner" else "synthetic-owner",
            "source_config_id": "synthetic-config", "source_config_version": 2})
