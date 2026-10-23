"""范围契约/覆盖规划/独立评审/有界修复的 PydanticAI 服务。

模型调用统一走 `agents/roles.py` 的固定角色与 typed output，不直接拼供应商请求；
确定性质量门在 `planning/quality.py`，模型评审只补充语义判断。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from pydantic_ai.exceptions import ModelRetry

from ..agents.contracts import CoveragePlanningInput, ReviewInput, RoutePlanningInput
from ..agents.roles import planning_agent
from ..prompts.service import PromptUnavailable
from .errors import PlanningError, error
from .models import PlanningTaskStateRow, load_json_field, utcnow
from .outputs import (
    CoveragePlan,
    QualityReview,
    RouteCandidate,
    TimeBudget,
    output_retry_reason,
    validate_coverage_plan,
    validate_quality_review,
    validate_route_candidate,
)
from .quality import default_coverage_plan


def _message(system: str, payload: dict) -> list[dict]:
    from ..materials.study_sources import LEARNING_FOCUS_POLICY
    return [
        {"role": "system", "content": system + "\n" + LEARNING_FOCUS_POLICY},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _resolve_prompt(planning, task_id: str, name: str) -> str:
    try:
        return planning.prompts.resolve_for_task(task_id, name)["template_snapshot"]
    except PromptUnavailable:
        planning.prompts.bind_for_task(task_id, name, allow_fallback=True)
        return planning.prompts.resolve_for_task(task_id, name)["template_snapshot"]


def _context(planning, state: PlanningTaskStateRow, budget: TimeBudget, profile: dict) -> dict:
    materials = load_json_field(state.material_ids_json, [])
    original = ""
    topic = ""
    scenario = "self_study"
    with planning.db.session() as sess:
        from .models import CourseRow, LearningRouteRow
        parent = sess.get(CourseRow if state.target_kind == "course" else LearningRouteRow, state.target_id)
        if parent is not None:
            original = getattr(parent, "input_ref", "") or getattr(parent, "title_ref", "")
            topic = getattr(parent, "title_ref", "") or ""
            scenario = getattr(parent, "scenario", "self_study") or "self_study"
    return {
        "original_request": original or topic,
        "topic": topic,
        "scenario": scenario,
        "learner_profile": profile,
        "time_budget": budget.model_dump(),
        "selected_materials": {"count": len(materials),
                              "source_context": planning._study_source_context(state) if materials else ""},
        "explicit_scope": state.planning_scope if state.planning_scope != "auto" else None,
    }


def _evidence_summary(process: Any) -> dict:
    """评审只拿来源标题/URL/计数，不拿网页正文。"""
    if process is None:
        return {"status": "unavailable", "sources": [], "queries": []}
    data = getattr(process, "data", {}) or {}
    search = data.get("search") or {}
    return {
        "status": search.get("status"),
        "result_count": search.get("result_count", 0),
        "queries": search.get("queries", []),
        "sources": [
            {"title": item.get("title", ""), "url": item.get("url", ""), "used": item.get("used", False)}
            for item in (search.get("sources") or [])[:20]
        ],
    }


class CoverageService:
    def __init__(self, planning) -> None:
        self.planning = planning

    def plan(
        self,
        task_id: str,
        state: PlanningTaskStateRow,
        *,
        budget: TimeBudget,
        profile: dict,
    ) -> CoveragePlan:
        adapter = self.planning._require_model_adapter(state.owner_user_id, task_id)
        prompt = _resolve_prompt(self.planning, task_id, "planning_coverage_v1")
        context = _context(self.planning, state, budget, profile)
        messages = _message(prompt, context)

        def validate(ctx, output):
            try:
                validate_coverage_plan(output)
            except PlanningError as exc:
                raise ModelRetry(output_retry_reason(exc)) from None
            return output

        run = planning_agent.plan_coverage(
            adapter,
            CoveragePlanningInput.from_messages(messages),
            self.planning._generation_deps(adapter),
            output_validator=validate,
        )
        plan = run.payload
        with self.planning.db.session() as sess:
            row = sess.get(PlanningTaskStateRow, task_id, with_for_update=True)
            if row is not None and not row.abandoned_at:
                row.scope_contract_json = json.dumps(plan.scope_contract.model_dump(), ensure_ascii=False)
                row.coverage_json = json.dumps(plan.model_dump(), ensure_ascii=False)
                row.updated_at = utcnow()
                sess.commit()
        return plan

    def fallback(self, topic: str, budget: TimeBudget) -> CoveragePlan:
        plan = default_coverage_plan(topic, budget)
        return plan

    def review(
        self,
        task_id: str,
        state: PlanningTaskStateRow,
        route: dict,
        plan: CoveragePlan,
        *,
        budget: TimeBudget,
        profile: dict,
        process: Any = None,
    ) -> QualityReview:
        adapter = self.planning._require_model_adapter(state.owner_user_id, task_id)
        prompt = _resolve_prompt(self.planning, task_id, "planning_review_v1")
        payload = {
            "route": route,
            "source_context": self.planning._study_source_context(state),
            "scope_contract": plan.scope_contract.model_dump(),
            "coverage_areas": [area.model_dump() for area in plan.coverage_areas],
            "prerequisite_policy": plan.prerequisite_policy.model_dump(),
            "learner_profile": profile,
            "time_budget": budget.model_dump(),
            "evidence": _evidence_summary(process),
        }

        def validate(ctx, output):
            try:
                validate_quality_review(output)
            except PlanningError as exc:
                raise ModelRetry(output_retry_reason(exc)) from None
            return output

        run = planning_agent.review_route(
            adapter,
            ReviewInput.from_messages(_message(prompt, payload)),
            self.planning._generation_deps(adapter),
            output_validator=validate,
        )
        review = run.payload
        with self.planning.db.session() as sess:
            row = sess.get(PlanningTaskStateRow, task_id, with_for_update=True)
            if row is not None and not row.abandoned_at:
                row.review_json = json.dumps(review.model_dump(), ensure_ascii=False)
                row.updated_at = utcnow()
                sess.commit()
        return review

    def repair(
        self,
        task_id: str,
        state: PlanningTaskStateRow,
        route: dict,
        plan: CoveragePlan,
        review: QualityReview,
        *,
        budget: TimeBudget,
        profile: dict,
    ) -> dict:
        adapter = self.planning._require_model_adapter(state.owner_user_id, task_id)
        prompt = _resolve_prompt(self.planning, task_id, "planning_repair_v1")
        payload = {
            "route": route,
            "source_context": self.planning._study_source_context(state),
            "scope_contract": plan.scope_contract.model_dump(),
            "coverage_areas": [area.model_dump() for area in plan.coverage_areas],
            "prerequisite_policy": plan.prerequisite_policy.model_dump(),
            "learner_profile": profile,
            "time_budget": budget.model_dump(),
            "review": review.model_dump(),
        }

        def validate(ctx, output):
            self.planning._validate_study_sources(state, output)
            try:
                validate_route_candidate(output)
            except PlanningError as exc:
                raise ModelRetry(output_retry_reason(exc)) from None
            return output

        run = planning_agent.repair_route(
            adapter,
            RoutePlanningInput.from_messages(_message(prompt, payload)),
            self.planning._generation_deps(adapter),
            output_validator=validate,
        )
        return run.payload.model_dump()


__all__ = ["CoverageService"]
