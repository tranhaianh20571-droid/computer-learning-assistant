"""规划生成 worker（T04）：route_generate / outline_generate / planning_revise。

流水线（任务事件在阶段边界可见）：
1. 范围解析（auto → resolved_scope；歧义 → awaiting_scope_clarification）；
2. 资料选择快照 + 检索组装 + context_manifest（不含正文）；
3. 真实搜索尝试（失败 → awaiting_search_decision，等用户决策）；
4. prompt 绑定（同事务、版本固定，重试不得重新解析 production 标签）；
5. 能力探测选择输出模式 → 结构化候选 → 服务端二次校验；
6. 候选 + 草稿落库（候选记录 base_draft_revision）；
7. 任务结果提交（切片 0 围栏）。

等待态不计为失败：worker 记录 partial 事件 + planning_task_states.wait_state，
用户决策（scope/search decision）后从对应阶段恢复。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from sqlalchemy import select

from ..logging_audit.db import Database
from ..logging_audit.repositories import PgAuditLog, PgTaskEventStore
from ..prompts.service import PromptService, PromptUnavailable
from ..slice1.models import ServiceConfigRow
from ..tasks.service import Lease, LeaseError, TaskService
from .drafts import DraftService
from .errors import PlanningError, WaitStateSignal, error
from .models import (
    CourseRow,
    LearningRouteRow,
    PlanningTaskStateRow,
    RESOLVED_SCOPES,
    load_json_field,
    utcnow,
)
from .model_runner import ModelCapabilityGate, run_structured
from .outputs import (
    OutlineCandidate,
    RouteCandidate,
    validate_outline_candidate,
    validate_route_candidate,
)
from .retrieval import PlanningRetrievalService, RetrievalResult, validate_material_ownership
from .scope import ScopeSignals, resolve_scope
from .search import SearchGate, SearchOutcome

PLAN_PROMPTS = {
    "route_generate": "route_generate_v1",
    "outline_generate": "outline_generate_v1",
    "planning_revise": "planning_revise_v1",
}


class PlanningService:
    def __init__(
        self,
        db: Database,
        tasks: TaskService,
        events: PgTaskEventStore,
        prompts: PromptService,
        drafts: DraftService,
        retrieval: PlanningRetrievalService,
        audit: Optional[PgAuditLog] = None,
        model_adapter_factory: Optional[Any] = None,
        search_adapter_factory: Optional[Any] = None,
    ) -> None:
        """adapter factory：(owner_user_id, task_id) -> adapter | None。

        生产实现经切片 1 配置与外发核验装配；测试注入合成工厂。
        """
        self.db = db
        self.tasks = tasks
        self.events = events
        self.prompts = prompts
        self.drafts = drafts
        self.retrieval = retrieval
        self.audit = audit
        self.model_adapter_factory = model_adapter_factory
        self.search_adapter_factory = search_adapter_factory

    # ------------------------------------------------------------------
    # 任务创建（四入口）
    # ------------------------------------------------------------------
    def create_planning_task(
        self,
        *,
        owner_user_id: str,
        task_kind: str,
        scenario: str,
        planning_scope: str = "auto",
        goal_text: str = "",
        material_ids: Optional[list[str]] = None,
        target_id: Optional[str] = None,
        source_route_node_id: Optional[str] = None,
        signals: Optional[ScopeSignals] = None,
        idempotency_key: str = "",
    ) -> dict:
        """创建规划任务 + planning_task_states + prompt 绑定（同一提交内）。"""
        if task_kind not in PLAN_PROMPTS:
            raise error("invalid_field", f"unknown task kind {task_kind}")
        if planning_scope not in ("auto", "course", "route"):
            raise error("invalid_field", f"unknown planning_scope {planning_scope}")
        owned_materials = validate_material_ownership(self.db, owner_user_id, material_ids or [])
        signals_payload = {
            "cross_domain_goal": bool(signals and signals.cross_domain_goal),
            "career_oriented": bool(signals and signals.career_oriented),
            "single_textbook": bool(signals and signals.single_textbook),
            "explicit_course": bool(signals and signals.explicit_course),
            "ambiguous": bool(signals and signals.ambiguous),
        }

        created = self.tasks.create_task(
            owner_user_id=owner_user_id,
            kind=task_kind,
            subject_id=target_id or "planning",
            idempotency_key=idempotency_key,
            stage="generate",
        )
        task_id = created["task_id"]
        if created.get("deduped"):
            # 幂等：不重复插入状态行/绑定，返回既有绑定
            binding_id = None
            try:
                binding_id = self.prompts.resolve_for_task(task_id)["binding_id"]
            except PromptUnavailable:
                pass
            return {
                "task_id": task_id,
                "status": created["status"],
                "deduped": True,
                "prompt_binding_id": binding_id,
                "material_ids": owned_materials,
            }
        with self.db.session() as sess:
            sess.add(
                PlanningTaskStateRow(
                    task_id=task_id,
                    owner_user_id=owner_user_id,
                    planning_scope=planning_scope,
                    resolved_scope=planning_scope if planning_scope in ("course", "route") else None,
                    scope_signals_json=json.dumps(signals_payload, ensure_ascii=False),
                    target_kind="course" if task_kind == "outline_generate" else "route",
                    target_id=target_id,
                    source_route_node_id=source_route_node_id,
                    material_ids_json=json.dumps(owned_materials, ensure_ascii=False),
                )
            )
            sess.commit()

        binding = None
        try:
            binding = self.prompts.bind_for_task(
                task_id,
                PLAN_PROMPTS[task_kind],
                allow_fallback=True,
                source="local_fallback",
            )
        except PromptUnavailable as exc:
            self.events.record(
                task_id, stage="generate", status="failed",
                error_code="PROMPT_UNAVAILABLE", template_params={"reason": exc.reason[:40]},
            )
        return {
            "task_id": task_id,
            "status": created["status"],
            "deduped": created.get("deduped", False),
            "prompt_binding_id": binding["binding_id"] if binding else None,
            "material_ids": owned_materials,
        }

    # ------------------------------------------------------------------
    # worker 主流程
    # ------------------------------------------------------------------
    def run_task(self, task_id: str, *, lease_owner: str = "planning-worker") -> dict:
        """领取并执行规划任务；返回最终状态摘要（供 API/测试断言）。"""
        lease = self.tasks.claim(task_id, lease_owner=lease_owner, lease_seconds=120)
        state = self._state(task_id)
        prompt = self.prompts.resolve_for_task(task_id)
        try:
            kind = self.tasks.get_task(task_id)["kind"]
            summary = self._pipeline(lease, task_id, kind, state, prompt)
            self._submit(lease, "generate", "succeeded")
            return summary | {"task_status": "succeeded"}
        except WaitStateSignal as wait:
            self._enter_wait(task_id, wait)
            self.events.record(
                task_id, stage="search" if wait.wait_state == "awaiting_search_decision" else "generate",
                status="partial", error_code=wait.error_code,
                generation=lease.generation, attempt=lease.attempt,
            )
            self._submit(lease, "generate", "partial")
            return {"task_status": "partial", "wait_state": wait.wait_state, "question": wait.question}
        except PlanningError as exc:
            self.events.record(
                task_id, stage="generate", status="failed", error_code=exc.code,
                generation=lease.generation, attempt=lease.attempt,
            )
            self._submit(lease, "generate", "failed", error_code=exc.code)
            return {"task_status": "failed", "error_code": exc.code}

    def _submit(self, lease: Lease, stage: str, status: str, error_code: Optional[str] = None) -> None:
        self.tasks.submit_result(
            lease, stage=stage, status=status, error_code=error_code, duration_ms=0
        )

    def _state(self, task_id: str) -> PlanningTaskStateRow:
        with self.db.session() as sess:
            row = sess.get(PlanningTaskStateRow, task_id)
            if row is None:
                raise error("task_not_found", status=404)
            sess.expunge(row)
            return row

    def _enter_wait(self, task_id: str, wait: WaitStateSignal) -> None:
        with self.db.session() as sess:
            row = sess.get(PlanningTaskStateRow, task_id)
            if row is not None:
                row.wait_state = wait.wait_state
                row.clarification_question = (wait.question or "")[:300]
                row.updated_at = utcnow()
                sess.commit()

    def _pipeline(self, lease: Lease, task_id: str, kind: str, state: PlanningTaskStateRow, prompt: dict) -> dict:
        material_ids = load_json_field(state.material_ids_json, [])

        # 1. 范围解析
        resolved_scope = state.resolved_scope
        if not resolved_scope:
            signals = ScopeSignals(**load_json_field(state.scope_signals_json, {}))
            resolved_scope = resolve_scope(state.planning_scope, signals)
            with self.db.session() as sess:
                row = sess.get(PlanningTaskStateRow, task_id)
                row.resolved_scope = resolved_scope  # type: ignore[index]
                sess.commit()

        # 2. 检索 + manifest
        retrieval = self.retrieval.assemble(
            owner_user_id=state.owner_user_id,
            material_ids=material_ids,
            query=state.clarification_question or "",
            budget=None,
        )
        manifest = self.retrieval.save_manifest(task_id=task_id, retrieval=retrieval)
        self.events.record(
            task_id, stage="retrieve", status="succeeded",
            progress_current=len(retrieval.refs),
            generation=lease.generation, attempt=lease.attempt,
        )

        # 3. 真实搜索尝试（等待用户决策）
        search_outcome = self._attempt_search(task_id, lease, state)
        if search_outcome is not None:
            self.events.record(
                task_id, stage="search", status="succeeded",
                progress_current=len(search_outcome.results),
                generation=lease.generation, attempt=lease.attempt,
            )

        # 4-6. 模型候选 → 校验 → 草稿/候选落库
        target_id = state.target_id
        if kind in ("route_generate", "planning_revise"):
            return self._generate_route(lease, task_id, state, target_id, prompt, search_outcome, manifest)
        return self._generate_outline(lease, task_id, state, target_id, prompt, search_outcome, manifest)

    def _attempt_search(self, task_id: str, lease: Lease, state: PlanningTaskStateRow) -> Optional[SearchOutcome]:
        """真实尝试搜索；失败 → 等待决策（已决策 offline 时放行）。"""
        if state.search_decision == "offline_continue":
            return None
        adapter = self.search_adapter_factory(state.owner_user_id, task_id) if self.search_adapter_factory else None
        gate = SearchGate(adapter)
        outcome = gate.attempt("学习目标与资料边界检索")
        try:
            gate.require_success_or_wait(outcome, decision=state.search_decision)
        except WaitStateSignal:
            if state.search_decision == "offline_continue":
                return None
            raise
        return outcome

    # ------------------------------------------------------------------
    # 生成：路线
    # ------------------------------------------------------------------
    def _generate_route(self, lease, task_id, state, target_id, prompt, search_outcome, manifest) -> dict:
        if not target_id:
            raise error("invalid_field", "route_generate requires target route")
        with self.db.session() as sess:
            route = sess.get(LearningRouteRow, target_id)
            if route is None or route.owner_user_id != state.owner_user_id:
                raise error("route_not_found", status=404)
        adapter = self._require_model_adapter(state.owner_user_id, task_id)
        messages = self._compose_messages("route_generate", state, prompt, search_outcome)
        run = run_structured(
            adapter, RouteCandidate, messages,
            schema=RouteCandidate.model_json_schema(),
            tool_name="submit_route_candidate",
        )
        candidate = run.payload
        summary = validate_route_candidate(candidate)
        content = candidate.model_dump()
        draft_result = self.drafts.apply_candidate(
            owner_user_id=state.owner_user_id,
            target_id=target_id,
            candidate_kind="route_generate",
            content=content,
            source_task_id=task_id,
        )
        self.events.record(
            task_id, stage="generate", status="succeeded",
            template_params={"mode": run.mode},
            progress_current=summary["course_count"],
            generation=lease.generation, attempt=lease.attempt,
        )
        with self.db.session() as sess:
            row = sess.get(PlanningTaskStateRow, task_id)
            row.draft_id = draft_result.get("draft_id")
            sess.commit()
        return {"mode": run.mode, "degraded_from": run.degraded_from, "summary": summary, "draft": draft_result}

    # ------------------------------------------------------------------
    # 生成：大纲（含由路线节点展开）
    # ------------------------------------------------------------------
    def _generate_outline(self, lease, task_id, state, target_id, prompt, search_outcome, manifest) -> dict:
        if not target_id:
            raise error("invalid_field", "outline_generate requires target course")
        with self.db.session() as sess:
            course = sess.get(CourseRow, target_id)
            if course is None or course.owner_user_id != state.owner_user_id:
                raise error("course_not_found", status=404)
            if course.current_outline_version_id and state.source_route_node_id:
                # 同一课程已有大纲：重选节点不进生成流程
                return {"skipped": True, "reason": "outline_exists",
                        "outline_version_id": course.current_outline_version_id}
        adapter = self._require_model_adapter(state.owner_user_id, task_id)
        messages = self._compose_messages("outline_generate", state, prompt, search_outcome)
        run = run_structured(
            adapter, OutlineCandidate, messages,
            schema=OutlineCandidate.model_json_schema(),
            tool_name="submit_outline_candidate",
        )
        candidate = run.payload
        summary = validate_outline_candidate(candidate)
        content = candidate.model_dump()
        draft_result = self.drafts.apply_candidate(
            owner_user_id=state.owner_user_id,
            target_id=target_id,
            candidate_kind="outline_generate",
            content=content,
            source_task_id=task_id,
            base_draft_revision=0,
        )
        self.events.record(
            task_id, stage="generate", status="succeeded",
            template_params={"mode": run.mode},
            progress_current=summary["lesson_count"],
            generation=lease.generation, attempt=lease.attempt,
        )
        with self.db.session() as sess:
            row = sess.get(PlanningTaskStateRow, task_id)
            row.draft_id = draft_result.get("draft_id")
            sess.commit()
        return {"mode": run.mode, "degraded_from": run.degraded_from, "summary": summary, "draft": draft_result}

    def _require_model_adapter(self, owner_user_id: str, task_id: str) -> Any:
        if self.model_adapter_factory is None:
            raise error("MODEL_CAPABILITY_UNAVAILABLE", "no model adapter configured")
        adapter = self.model_adapter_factory(owner_user_id, task_id)
        if adapter is None:
            raise error("MODEL_CAPABILITY_UNAVAILABLE", "no active model config")
        return adapter

    def _compose_messages(self, kind: str, state: PlanningTaskStateRow, prompt: dict, search_outcome: Optional[SearchOutcome]) -> list[dict]:
        """提示词组装：模板快照 + 受控上下文（不含原始材料正文，只含受限摘录）。"""
        template = prompt.get("template_snapshot", "")
        external_note = ""
        if search_outcome and search_outcome.ok:
            external_note = f"（联网参考：{len(search_outcome.results)} 条外部来源）"
        user_content = f"{template}\n目标对象：{state.target_id or 'route'} {external_note}"
        return [
            {"role": "system", "content": "你是学习规划助手。只输出符合要求的 JSON。"},
            {"role": "user", "content": user_content},
        ]

    # ------------------------------------------------------------------
    # 用户决策恢复
    # ------------------------------------------------------------------
    def decide_scope(self, task_id: str, *, actor_id: str, resolved_scope: str) -> dict:
        if resolved_scope not in RESOLVED_SCOPES:
            raise error("invalid_field", f"unknown resolved_scope {resolved_scope}")
        with self.db.session() as sess:
            row = sess.get(PlanningTaskStateRow, task_id)
            if row is None or row.owner_user_id != actor_id:
                raise error("task_not_found", status=404)
            if row.wait_state != "awaiting_scope_clarification":
                raise error("state_invalid", "task not awaiting scope clarification", status=409)
            row.resolved_scope = resolved_scope
            row.wait_state = None
            row.clarification_question = None
            sess.commit()
        return {"task_id": task_id, "resolved_scope": resolved_scope, "wait_state": None}

    def decide_search(self, task_id: str, *, actor_id: str, decision: str) -> dict:
        if decision not in ("retry", "offline_continue"):
            raise error("invalid_field", "decision must be retry | offline_continue")
        with self.db.session() as sess:
            row = sess.get(PlanningTaskStateRow, task_id)
            if row is None or row.owner_user_id != actor_id:
                raise error("task_not_found", status=404)
            if row.wait_state != "awaiting_search_decision":
                raise error("state_invalid", "task not awaiting search decision", status=409)
            row.search_decision = decision
            row.wait_state = None
            sess.commit()
        return {"task_id": task_id, "search_decision": decision, "wait_state": None}

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_planning_state(self, task_id: str, *, actor_id: str) -> dict:
        with self.db.session() as sess:
            row = sess.get(PlanningTaskStateRow, task_id)
            if row is None or row.owner_user_id != actor_id:
                raise error("task_not_found", status=404)
            return {
                "task_id": row.task_id,
                "planning_scope": row.planning_scope,
                "resolved_scope": row.resolved_scope,
                "target_kind": row.target_kind,
                "target_id": row.target_id,
                "draft_id": row.draft_id,
                "wait_state": row.wait_state,
                "clarification_question": row.clarification_question,
                "search_decision": row.search_decision,
            }


__all__ = ["PlanningService", "PLAN_PROMPTS"]
