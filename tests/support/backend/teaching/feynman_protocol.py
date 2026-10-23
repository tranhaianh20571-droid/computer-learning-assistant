"""Lesson dialogue protocol adapted from feiman-learning, pinned in ADR-051.

State is reduced from committed business records, never from model chat memory.
"""
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_ai.exceptions import ModelRetry

from ..planning.models import load_json_field
from .feynman_outputs import FeynmanEvaluation

DIMENSIONS = ("accuracy", "clarity", "mechanism", "boundary", "transfer")
Dimension = Literal["accuracy", "clarity", "mechanism", "boundary", "transfer"]
QUESTION_ACTIONS = ("probe", "restate", "teach_restate", "transfer")


class DimensionEvidence(BaseModel):
    status: Literal["unknown", "gap", "partial", "demonstrated"]
    evidence: str = Field(default="", max_length=600)

    @model_validator(mode="after")
    def supported(self):
        if self.status != "unknown" and not self.evidence.strip():
            raise ValueError("dimension judgement requires answer evidence")
        return self


class LessonFeynmanEvaluation(FeynmanEvaluation):
    protocol_version: Literal[2] = 2
    mode: Literal["check", "learn"]
    diagnosis: Literal["correct", "incomplete", "misleading", "incorrect", "uncertain"]
    action: Literal["probe", "restate", "teach_restate", "transfer", "hint", "teach", "finish"]
    gap_key: str = Field(default="", max_length=80)
    primary_gap: str = Field(default="", max_length=400)
    dimension_evidence: dict[Dimension, DimensionEvidence] = Field(default_factory=dict, max_length=5)
    feedback: str = Field(min_length=1, max_length=1800)
    next_question: str = Field(default="", max_length=400)
    review_items: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def action_consistent(self):
        if any(not item.strip() or len(item) > 400 for item in self.review_items):
            raise ValueError("review items must be bounded")
        if self.decision == "probe" and self.action not in QUESTION_ACTIONS:
            raise ValueError("a probe must identify its teaching action")
        if self.decision != "probe" and self.action in QUESTION_ACTIONS:
            raise ValueError("question actions require a formal question")
        if self.decision == "understood" and (
                self.action != "finish" or self.diagnosis != "correct"):
            raise ValueError("understanding requires a correct terminal evaluation")
        if self.primary_gap and not self.gap_key:
            raise ValueError("a gap requires a stable key")
        return self


def state_from_turns(turns):
    state = {"mode": "check", "current_action": "explain", "gap_key": "",
             "gap_attempts": 0, "help_level": 0, "needs_restatement": False,
             "transfer_verified": False, "dimensions": {}}
    for turn in turns:
        evaluation = load_json_field(turn.evaluation_json, {})
        if evaluation.get("protocol_version") != 2:
            continue
        if turn.answer and evaluation.get("_accepted_answer"):
            if turn.kind == "help":
                state["help_level"] = min(4, state["help_level"] + 1)
                state["needs_restatement"] = True
            elif turn.kind == "question":
                state["dimensions"].update(evaluation.get("dimension_evidence", {}))
                gap = evaluation.get("gap_key", "")
                state["gap_attempts"] = (
                    state["gap_attempts"] + 1 if gap and gap == state["gap_key"] else 1 if gap else 0)
                state["gap_key"] = gap
                if evaluation.get("_answered_action") in ("restate", "teach_restate"):
                    state["needs_restatement"] = evaluation.get("diagnosis") != "correct"
                if evaluation.get("_answered_action") == "transfer":
                    transfer = evaluation.get("dimension_evidence", {}).get("transfer", {})
                    state["transfer_verified"] = transfer.get("status") == "demonstrated"
                if evaluation.get("action") == "teach_restate":
                    state["help_level"] = 4
                    state["needs_restatement"] = True
            state["mode"] = evaluation.get("mode", state["mode"])
        if turn.kind == "question" and turn.published:
            state["current_action"] = evaluation.get("_prompt_action", "explain")
    return state


def validate_evaluation(candidate, state, *, intent, count, max_probes):
    if intent == "help":
        if candidate.decision != "uncertain" or candidate.action not in ("hint", "teach"):
            raise ModelRetry("当前仅为求助：给提示或最小补讲，不评分、不新增正式问题。")
        return candidate
    if candidate.decision == "probe" and count >= max_probes:
        raise ModelRetry("正式追问预算已用尽，评价本次回答并列出复习项，不再出题。")
    if candidate.action in ("hint", "teach"):
        raise ModelRetry("正式回答后的提示或补讲应请求用户重述，不能直接作为最终评价。")
    if candidate.action == "finish" and candidate.decision == "probe":
        raise ModelRetry("结束动作不能同时发布新题。")
    if (candidate.diagnosis in ("incorrect", "misleading") and candidate.decision == "probe"
            and candidate.action not in ("teach_restate", "restate")):
        raise ModelRetry("已有明确错误，请指出具体错误、最小纠正并请求重述，不只继续提问。")
    if (candidate.gap_key and candidate.gap_key == state["gap_key"]
            and state["gap_attempts"] >= 2 and candidate.decision == "probe"
            and candidate.action != "teach_restate" and
            not (state["help_level"] >= 4 and candidate.action == "restate")):
        raise ModelRetry("同一漏洞已反复尝试，请最小补讲并请求重述，不再连续审问。")
    if candidate.decision == "understood":
        dimensions = {**state["dimensions"],
                      **{key: value.model_dump() for key, value in candidate.dimension_evidence.items()}}
        transfer = (state["transfer_verified"] or state["current_action"] == "transfer"
                    and dimensions.get("transfer", {}).get("status") == "demonstrated")
        restated = not state["needs_restatement"] or state["current_action"] in (
            "restate", "teach_restate")
        if (not transfer or not restated or any(
                dimensions.get(key, {}).get("status") != "demonstrated" for key in DIMENSIONS)):
            raise ModelRetry("完整通过需要五维证据、补讲后的实际重述及已回答的迁移任务；"
                             "有预算时继续相应任务，否则记录复习或未验证，不能提前宣称掌握。")
    return candidate


def learning_record(turns, state):
    explanations, corrections, transfer, reviews = [], [], [], []
    for turn in turns:
        evaluation = load_json_field(turn.evaluation_json, {})
        if turn.kind != "question" or not turn.answer:
            continue
        explanations.append({"question": turn.question, "answer": turn.answer,
                             "assisted": bool(evaluation.get("_assisted", False))})
        if not evaluation.get("_accepted_answer"):
            continue
        if evaluation.get("primary_gap"):
            corrections.append({"gap": evaluation["primary_gap"], "feedback": turn.feedback})
        if evaluation.get("_answered_action") == "transfer":
            transfer.append({"question": turn.question, "answer": turn.answer,
                             "feedback": turn.feedback})
        reviews.extend(evaluation.get("review_items", []))
    return {"initial_explanation": explanations[0]["answer"] if explanations else "",
            "explanations": explanations, "corrections": corrections,
            "transfer_tests": transfer, "dimensions": state["dimensions"],
            "review_items": list(dict.fromkeys(reviews))}
