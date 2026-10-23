"""Owned lesson-end dialogues with genuine SDK validation and synthetic evidence."""
import json
import uuid
from types import SimpleNamespace

import pytest
from backend.planning.models import LessonProgressRow
from backend.teaching.errors import TeachingError
from backend.teaching.feynman import lesson_checkpoint
from backend.teaching.feynman_protocol import (
    LessonFeynmanEvaluation,
    validate_evaluation,
)
from backend.teaching.learning_models import (
    FeynmanSessionRow,
    FeynmanTurnRow,
    LessonBlueprintRow,
)
from backend.teaching.models import TeachingStepRow
from pydantic_ai.exceptions import ModelRetry
from sqlalchemy import select
from test_feynman_dialogues import env, model, opening, prepare, submit  # noqa: F401


def lesson_ready(env, monkeypatch):
    conv, point = prepare(env, monkeypatch)
    service = env.service
    active = opening(env, conv, point)
    service.feynman.action(conv, env.owner, active["revision"], "skip")
    with env.db.session() as sess:
        plan = sess.get(LessonBlueprintRow, conv)
        end = lesson_checkpoint(plan.plan_json)
        for number in range(point["after_step_no"] + 1, end["after_step_no"] + 1):
            sess.add(TeachingStepRow(step_id="step_" + uuid.uuid4().hex, conversation_id=conv,
                generation=1, step_no=number, status="ready", text="Synthetic lesson summary.",
                phase="summary", covered_goal_ids_json=json.dumps(end["goal_ids"]),
                lesson_finished=number == end["after_step_no"]))
    service.save_checkpoint(conv, actor_id=env.owner, client_seq=2, step_no=end["after_step_no"],
        position_ms=1000, board_state={"presentation": {"paused": True, "text_complete": True}})
    return conv, end


def evaluation(action="transfer", *, decision="probe", diagnosis="correct", gap_key=""):
    dimensions = {name: {"status": "demonstrated", "evidence": "Synthetic student evidence: " + name}
                  for name in ("accuracy", "clarity", "mechanism", "boundary")}
    if action == "finish" and decision == "understood":
        dimensions["transfer"] = {"status": "demonstrated", "evidence": "Synthetic novel example answer."}
    return {"protocol_version": 2, "mode": "check", "diagnosis": diagnosis, "action": action,
            "gap_key": gap_key, "primary_gap": "Synthetic unresolved mechanism." if gap_key else "",
            "dimension_evidence": dimensions, "decision": decision,
            "feedback": "Synthetic precise feedback and the minimum correction.",
            "evidence": ["Synthetic answer explains ordering."],
            "gaps": ["Transfer still requires an actual answer."] if decision == "probe" else [],
            "next_question": "What would change in a new unordered input example?" if decision == "probe" else "",
            "review_items": []}


def test_every_unfinished_lesson_has_an_independent_final_checkpoint(env, monkeypatch):
    conv, end = lesson_ready(env, monkeypatch)
    points = env.service.feynman.view(conv, env.owner)["checkpoints"]
    assert points[-1] == {**end, "resolved": False}
    with pytest.raises(TeachingError, match="费曼"):
        env.service.complete_lesson(conv, actor_id=env.owner)
    active = opening(env, conv, end)
    assert active["scope"] == "lesson" and active["protocol_version"] == 2
    with pytest.raises(TeachingError, match="不能跳过"):
        env.service.feynman.action(conv, env.owner, active["revision"], "skip")


def test_new_lesson_evaluation_never_receives_persona_or_its_tools(env, monkeypatch):
    from backend.agents.roles import feynman_agent
    from backend.characters.models import CharacterCardRow, CharacterSegmentRow
    from backend.characters.schemas import Profile
    conv, end = lesson_ready(env, monkeypatch)
    card_id = "synthetic_" + uuid.uuid4().hex
    with env.db.session() as sess:
        sess.add(CharacterCardRow(card_id=card_id, owner_user_id=env.owner, current_revision=1))
        sess.flush()
        sess.add(CharacterSegmentRow(conversation_id=conv, step_no=end["after_step_no"],
            card_id=card_id, revision=1, profile_json=Profile(name="Synthetic judge",
                identity="称呼学习者为同学", personality="SYNTHETIC_PERSONA_OVERRIDE_PASS").model_dump_json()))
    captured = []
    for method in ("question", "evaluate"):
        original = getattr(feynman_agent, method)
        def record(adapter, input, *args, _original=original, **kwargs):
            captured.append((input.messages, kwargs.get("tools", [])))
            return _original(adapter, input, *args, **kwargs)
        monkeypatch.setattr(feynman_agent, method, record)
    opening(env, conv, end)
    active = submit(env, conv, evaluation())
    assert active["probe_count"] == 1
    for messages, tools in captured:
        text = json.dumps(messages, ensure_ascii=False)
        assert "SYNTHETIC_PERSONA_OVERRIDE_PASS" not in text
        assert "character_expression" not in text and '"mentor"' not in text
        assert not tools
    with env.db.session() as sess:
        session = sess.get(FeynmanSessionRow, active["session_id"])
        assert json.loads(session.context_json)["persona_isolation_version"] == 1
        turn = sess.scalar(select(FeynmanTurnRow).where(
            FeynmanTurnRow.session_id == active["session_id"]).order_by(FeynmanTurnRow.turn_no.desc()))
        assert turn.public_text.startswith("同学，")
        assert json.loads(turn.evaluation_json)["decision"] == "probe"


def test_fsrs_guidance_only_prioritizes_the_v2_question_topic(env, monkeypatch):
    from backend.agents.roles import feynman_agent
    conv, end = lesson_ready(env, monkeypatch)
    env.service.memory = SimpleNamespace()
    monkeypatch.setattr("backend.teaching.feynman.review_topics",
        lambda *args: {"synthetic-point": "Synthetic concept"})
    monkeypatch.setattr("backend.memory.learning.context", lambda *args, **kwargs: {
        "status": "ready", "algorithm": "synthetic-fsrs", "as_of": "2026-10-22",
        "items": [{"concept_id": "synthetic-point", "action": "diagnose",
            "independent_count": 2, "last_correct": True, "retrievability": 0.42,
            "last_verified_date": "2026-08-01", "elapsed_days": 82}]})
    captured = []
    original = feynman_agent.question
    def capture(source, input, *args, **kwargs):
        captured.append(input.messages)
        return original(source, input, *args, **kwargs)
    monkeypatch.setattr(feynman_agent, "question", capture)
    opening(env, conv, end)
    payload = json.loads(captured[0][-1]["content"])
    assert payload["spaced_review_guidance"]["items"] == [{
        "concept_id": "synthetic-point", "topic": "Synthetic concept",
        "action": "recall_check", "retrievability": 0.42,
        "last_verified_date": "2026-08-01"}]
    assert "不因提示而降低或抬高标准" in captured[0][0]["content"]


def test_explain_transfer_and_finish_preserve_evidence_and_allow_completion(env, monkeypatch):
    conv, end = lesson_ready(env, monkeypatch)
    opening(env, conv, end)
    active = submit(env, conv, evaluation())
    assert active["probe_count"] == 1
    assert active["learning_state"]["current_action"] == "transfer"
    active = submit(env, conv, evaluation("finish", decision="understood"))
    assert active["result"] == "understood"
    record = active["learning_record"]
    assert record["initial_explanation"]
    assert len(record["transfer_tests"]) == 1
    assert set(record["dimensions"]) == {"accuracy", "clarity", "mechanism", "boundary", "transfer"}
    env.service.feynman.action(conv, env.owner, active["revision"], "continue")
    assert env.service.complete_lesson(conv, actor_id=env.owner)["completed"]
    assert env.service.complete_lesson(conv, actor_id=env.owner)["completed"]
    with env.db.session() as sess:
        progress = sess.scalar(select(LessonProgressRow).where(LessonProgressRow.lesson_id == env.lesson))
        assert progress.learning_state == "user_completed"


def test_fluent_answer_cannot_pass_without_an_actual_transfer_question(env, monkeypatch):
    conv, end = lesson_ready(env, monkeypatch)
    active = opening(env, conv, end)
    model(env, evaluation("finish", decision="understood"))
    task = env.service.feynman.answer(conv, env.owner, active["question_id"], active["revision"],
        "A fluent answer without any actual transfer task.", "premature")
    assert env.service.feynman.run(task["task_id"])["status"] == "failed"
    refreshed = env.service.feynman.view(conv, env.owner)["active"]
    assert refreshed["probe_count"] == 0 and refreshed["retry_available"]
    assert not refreshed["learning_state"]["transfer_verified"]
    assert refreshed["learning_record"]["dimensions"] == {}


def test_help_is_not_an_error_or_mastery_and_restatement_is_required(env, monkeypatch):
    conv, end = lesson_ready(env, monkeypatch)
    opening(env, conv, end)
    help_output = evaluation("teach", decision="uncertain", diagnosis="uncertain")
    help_output["mode"] = "learn"
    active = submit(env, conv, help_output, help=True)
    assert active["probe_count"] == 0
    assert active["learning_state"]["mode"] == "learn"
    assert active["learning_state"]["needs_restatement"]
    assert active["learning_record"]["dimensions"] == {}
    active = submit(env, conv, evaluation("restate", gap_key="causal_condition"))
    active = submit(env, conv, evaluation("transfer"))
    assert not active["learning_state"]["needs_restatement"]
    active = submit(env, conv, evaluation("finish", decision="understood"))
    assert active["result"] == "understood"
    assert active["learning_record"]["explanations"][0]["assisted"]


def test_stalled_gap_requires_teaching_or_restatement_and_budget_is_not_reset(env, monkeypatch):
    conv, end = lesson_ready(env, monkeypatch)
    active = opening(env, conv, end)
    for _ in range(2):
        active = submit(env, conv, evaluation("probe", diagnosis="incomplete", gap_key="condition"))
    state = active["learning_state"]
    with pytest.raises(ModelRetry, match="补讲"):
        validate_evaluation(LessonFeynmanEvaluation.model_validate(
            evaluation("probe", diagnosis="incomplete", gap_key="condition")), state,
            intent="answer", count=2, max_probes=5)
    active = submit(env, conv, evaluation("teach_restate", diagnosis="incomplete", gap_key="condition"))
    assert active["probe_count"] == 3 and active["learning_state"]["needs_restatement"]
    active = submit(env, conv, evaluation("restate", diagnosis="incomplete", gap_key="condition"))
    active = submit(env, conv, evaluation("restate", diagnosis="incomplete", gap_key="condition"))
    review = evaluation("finish", decision="review", diagnosis="incomplete", gap_key="condition")
    review["gaps"] = ["Synthetic unresolved condition."]
    review["review_items"] = ["Explain the ordering condition before applying exclusion."]
    active = submit(env, conv, review)
    assert active["probe_count"] == 5 and active["result"] == "review"
    assert active["learning_record"]["review_items"]


def test_changed_final_source_blocks_completion_even_after_closed_evaluation(env, monkeypatch):
    conv, end = lesson_ready(env, monkeypatch)
    opening(env, conv, end)
    submit(env, conv, evaluation())
    active = submit(env, conv, evaluation("finish", decision="understood"))
    env.service.feynman.action(conv, env.owner, active["revision"], "continue")
    with env.db.session() as sess:
        step = sess.scalar(select(TeachingStepRow).where(
            TeachingStepRow.conversation_id == conv, TeachingStepRow.step_no == 1))
        step.text = "Synthetic changed source."
    with pytest.raises(TeachingError):
        env.service.complete_lesson(conv, actor_id=env.owner)


def test_final_checkpoint_cannot_be_started_on_generation_alone(env, monkeypatch):
    conv, end = lesson_ready(env, monkeypatch)
    env.service.save_checkpoint(conv, actor_id=env.owner, client_seq=3, step_no=end["after_step_no"],
        position_ms=0, board_state={"presentation": {"text_complete": False}})
    with pytest.raises(TeachingError):
        env.service.feynman.start(conv, env.owner, end["checkpoint_id"])


def test_lesson_prompt_and_output_contract_are_frozen_separately(env, monkeypatch):
    from backend.prompts.service import PromptBindingRow
    conv, end = lesson_ready(env, monkeypatch)
    active = opening(env, conv, end)
    with env.db.session() as sess:
        binding = sess.scalar(select(PromptBindingRow).where(PromptBindingRow.task_id == active["task_id"]))
        assert binding.prompt_name == "feynman_question_v2"
        assert binding.output_schema_version == "feynman-2"


def test_accepted_course_memory_distinguishes_help_and_is_not_content_traced(env, monkeypatch):
    from backend.agents.roles import feynman_agent
    calls, traces = [], []
    monkeypatch.setattr("backend.memory.service.enqueue",
        lambda session, owner, kind, ident, payload, **scope:
            calls.append((owner, kind, payload, scope)) or "synthetic-source")
    original = feynman_agent.evaluate
    def evaluate(source, input, deps, **options):
        traces.append(deps.omit_content_trace)
        return original(source, input, deps, **options)
    monkeypatch.setattr(feynman_agent, "evaluate", evaluate)
    conv, end = lesson_ready(env, monkeypatch)
    env.service.memory = SimpleNamespace(context=lambda *args, **kwargs: "")
    opening(env, conv, end)
    help_output = evaluation("teach", decision="uncertain", diagnosis="uncertain")
    help_output["mode"] = "learn"
    submit(env, conv, help_output, help=True)
    assert calls == []
    submit(env, conv, evaluation("restate", gap_key="condition"))
    assert len(calls) == 1
    owner, kind, payload, scope = calls[0]
    assert owner == env.owner and kind == "course_feynman"
    assert scope["course"] == env.course
    assert payload["assisted"] and payload["scope"] == "lesson"
    assert payload["dimension_evidence"]
    assert traces == [True, True]


def test_already_completed_legacy_lesson_is_not_reopened(env, monkeypatch):
    conv, _ = prepare(env, monkeypatch)
    from backend.planning.models import ConversationRow
    with env.db.session() as sess:
        conversation = sess.get(ConversationRow, conv)
        progress = sess.scalar(select(LessonProgressRow).where(
            LessonProgressRow.owner_user_id == env.owner, LessonProgressRow.lesson_id == env.lesson))
        progress.learning_state = "user_completed"
        progress.lesson_content_version_id = conversation.lesson_content_version_id
    assert not any(point.get("scope") == "lesson"
        for point in env.service.feynman.view(conv, env.owner)["checkpoints"])
    assert env.service.complete_lesson(conv, actor_id=env.owner)["completed"]


@pytest.mark.parametrize("diagnosis,attempts", [("incorrect", 0), ("misleading", 0), ("incomplete", 2)])
def test_correction_and_stalled_attempts_cannot_degenerate_into_more_questions(diagnosis, attempts):
    state = {"gap_key": "condition", "gap_attempts": attempts, "help_level": 0}
    candidate = LessonFeynmanEvaluation.model_validate(
        evaluation("probe", diagnosis=diagnosis, gap_key="condition"))
    with pytest.raises(ModelRetry):
        validate_evaluation(candidate, state, intent="answer", count=attempts, max_probes=5)
    teaching = LessonFeynmanEvaluation.model_validate(
        evaluation("teach_restate", diagnosis=diagnosis, gap_key="condition"))
    assert validate_evaluation(teaching, state, intent="answer", count=attempts, max_probes=5) == teaching


def test_formal_answer_teaching_cannot_silently_finish_without_restatement():
    candidate = LessonFeynmanEvaluation.model_validate(
        evaluation("teach", decision="uncertain", diagnosis="uncertain"))
    with pytest.raises(ModelRetry, match="重述"):
        validate_evaluation(candidate, {}, intent="answer", count=0, max_probes=5)
    assert validate_evaluation(candidate, {}, intent="help", count=0, max_probes=5) == candidate
