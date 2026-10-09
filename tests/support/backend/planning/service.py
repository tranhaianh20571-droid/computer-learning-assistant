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
from ..agents.contracts import RoutePlanningInput, OutlinePlanningInput
from ..agents.runner import deps_for_source
from pydantic_ai.exceptions import ModelRetry
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
    OUTLINE_LIMITS,
    validate_content_json,
    validate_outline_candidate,
    validate_route_candidate,
)
from .outputs import IntakeQuestionSet, validate_intake_question_set
from .retrieval import PlanningRetrievalService, RetrievalResult, validate_material_ownership
from .scope import ScopeSignals, resolve_scope
from .search import SearchGate, SearchOutcome

PLAN_PROMPTS = {
    "route_generate": "route_generate_v2",
    "outline_generate": "outline_refine_v1",
    "planning_revise": "planning_revise_v1",
}

# 访谈问题总数由planning.intake的4～6题协议管理。


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
        from .workspace import PlanningWorkspace
        from .background import PlanningBackground
        self.workspace = PlanningWorkspace(self)
        self.background = PlanningBackground(self)

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
        target_kind = "course" if task_kind == "outline_generate" or planning_scope == "course" else "route"
        if target_id:
            from .locking import planning_target
            with self.db.session() as sess:
                planning_target(sess, target_kind, target_id, owner_user_id,
                                mutable=True, allow_abandoned=True)
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
            # A new explicit planning task starts a fresh draft. Keep the old
            # task row as an audit record, then detach and remove only its
            # unconfirmed draft so it cannot be reused by the new task.
            if target_id:
                from .models import PlanningDraftRow
                abandoned = list(sess.scalars(select(PlanningTaskStateRow).where(
                    PlanningTaskStateRow.owner_user_id == owner_user_id,
                    PlanningTaskStateRow.target_kind == target_kind,
                    PlanningTaskStateRow.target_id == target_id,
                    PlanningTaskStateRow.abandoned_at.is_not(None),
                    PlanningTaskStateRow.draft_id.is_not(None),
                ).with_for_update()))
                for old_state in abandoned:
                    old_draft = sess.get(PlanningDraftRow, old_state.draft_id)
                    if old_draft is not None:
                        sess.delete(old_draft)
                    old_state.draft_id = None
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
            if task_kind in ("route_generate", "planning_revise"):
                self.prompts.bind_for_task(task_id, "outline_refine_v1")
            if task_kind=="route_generate":self.prompts.bind_for_task(task_id,"learning_interview_v3")
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
        state = self._state(task_id)
        lease = self.tasks.claim(task_id, lease_owner=lease_owner, lease_seconds=120)
        try:
            kind = self.tasks.get_task(task_id)["kind"]
            try:
                prompt = self.prompts.resolve_for_task(task_id, PLAN_PROMPTS[kind])
            except PromptUnavailable:
                raise error("PROMPT_UNAVAILABLE")
            from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
            with ThreadPoolExecutor(max_workers=1) as pool:
                from contextvars import copy_context
                future = pool.submit(copy_context().run, self._pipeline, lease, task_id, kind, state, prompt)
                while True:
                    try:
                        summary = future.result(timeout=30)
                        break
                    except FutureTimeout:
                        self.tasks.heartbeat(lease, lease_seconds=120)
            status = "partial" if summary.get("incomplete") else "succeeded"
            self.tasks.submit_result(lease, stage="generate", status=status, duration_ms=0, preserve_partial=True)
            return summary | {"task_status": status}
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
            if row.abandoned_at:
                raise error("TASK_CANCELLED", status=409)
            sess.expunge(row)
            return row

    def _enter_wait(self, task_id: str, wait: WaitStateSignal) -> None:
        with self.db.session() as sess:
            row = sess.get(PlanningTaskStateRow, task_id, with_for_update=True)
            if row is not None and not row.abandoned_at:
                row.wait_state = wait.wait_state
                row.clarification_question = (wait.question or "")[:300]
                row.updated_at = utcnow()
                sess.commit()

    def _pipeline(self, lease: Lease, task_id: str, kind: str, state: PlanningTaskStateRow, prompt: dict) -> dict:
        material_ids = load_json_field(state.material_ids_json, [])

        repair = load_json_field(state.scope_signals_json, {}).get("refine_draft")
        if repair:
            draft = self.drafts.get_draft(repair["draft_id"], actor_id=state.owner_user_id)
            if draft["revision"] != repair["revision"]:
                raise error("DRAFT_REVISION_MISMATCH", status=409)
            content = draft["content"]
            self._fill_route_units(state, task_id, content, repair.get("course_index"), lease=lease)
            summary = validate_content_json("route", content)
            result = self.drafts.apply_candidate(
                owner_user_id=state.owner_user_id, target_id=state.target_id,
                candidate_kind="planning_revise", content=content, source_task_id=task_id, lease=lease,
                base_draft_revision=repair["revision"],
            )
            with self.db.session() as sess:
                sess.get(PlanningTaskStateRow, task_id).draft_id = draft["draft_id"]
                sess.commit()
            return {"draft": {**result, "draft_id": draft["draft_id"]}, "summary": summary,
                    "incomplete": any(not c.get("units") for st in content["stages"] for c in st["courses"])}

        # 0. AI访谈：累计4～6题，内部判断范围，失败保留历史等待重试。
        intake_profile = None
        if kind == "route_generate":
            intake_profile = self._intake_gate(lease, task_id, state)

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
        if (kind == "route_generate" and state.resolved_scope != "course") or (kind == "planning_revise" and state.resolved_scope == "route"):
            return self._generate_route(lease, task_id, state, target_id, prompt, search_outcome, manifest)
        if kind=="route_generate" and state.resolved_scope=="course":
            target_id=self._course_for_interview(state,task_id)
            prompt=self.prompts.resolve_for_task(task_id,"outline_refine_v1")
        return self._generate_outline(lease, task_id, state, target_id, prompt, search_outcome, manifest)

    def _course_for_interview(self,state,task_id):
        with self.db.session() as sess:
            from .locking import planning_target
            planning_target(sess,state.target_kind,state.target_id,state.owner_user_id,mutable=True)
            row=sess.get(PlanningTaskStateRow,task_id,with_for_update=True)
            if row.abandoned_at:raise error("TASK_CANCELLED",status=409)
            if row.target_kind=="course":return row.target_id
            route=sess.get(LearningRouteRow,row.target_id)
            if not route or route.deleted_at or route.owner_user_id!=state.owner_user_id:raise error("route_not_found",status=404)
            course_id="crs_"+uuid.uuid4().hex[:12]
            sess.add(CourseRow(course_id=course_id,owner_user_id=state.owner_user_id,title_ref=route.title_ref,scenario=route.scenario))
            row.target_kind="course";row.target_id=course_id
            source_route_id = route.route_id
            covers = getattr(self, "cover_service", None)
            reused = covers.reuse_cover("route", source_route_id, "course", course_id,
                state.owner_user_id, session=sess) if covers else None
            if not route.current_route_version_id:route.deleted_at=utcnow()
            state.target_kind="course";state.target_id=course_id
        if covers:
            if reused and reused.get("task_id"):
                covers.kick(reused["task_id"])
            elif reused is None:
                covers.enqueue_cover("course", course_id, state.owner_user_id, "scope-local", automatic=True)
        return course_id

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
        from ..agents.roles import planning_agent
        from ..agents.tools import make_search_materials_tool, planning_title_output_validator

        material_ids = load_json_field(state.material_ids_json, [])
        tools = [make_search_materials_tool(self.retrieval, owner_user_id=state.owner_user_id,
                                            material_ids=material_ids)] if material_ids else None
        title_validator = planning_title_output_validator(route.title_ref)
        def validate(ctx, output):
            output = title_validator(ctx, output)
            try: validate_route_candidate(output)
            except PlanningError as exc: raise ModelRetry(exc.code) from None
            return output
        run = planning_agent.generate_route(
            adapter, RoutePlanningInput.from_messages(messages), deps_for_source(adapter),
            tools=tools, output_validator=validate,
        )
        candidate = run.payload
        summary = validate_route_candidate(candidate)
        content = candidate.model_dump()
        self._fill_route_units(state, task_id, content, lease=lease)
        summary = validate_content_json("route", content)
        incomplete = any(not c.get("units") for st in content["stages"] for c in st["courses"])
        draft_result = self.drafts.apply_candidate(
            owner_user_id=state.owner_user_id,
            target_id=target_id,
            candidate_kind="route_generate",
            content=content,
            source_task_id=task_id, lease=lease,
        )
        self.events.record(
            task_id, stage="generate", status="partial" if incomplete else "succeeded",
            template_params={"mode": run.mode},
            progress_current=sum(bool(c.get("units")) for st in content["stages"] for c in st["courses"]),
            generation=lease.generation, attempt=lease.attempt,
        )
        with self.db.session() as sess:
            row = sess.get(PlanningTaskStateRow, task_id)
            row.draft_id = draft_result.get("draft_id")
            sess.commit()
        return {"mode": run.mode, "degraded_from": run.degraded_from, "summary": summary, "draft": draft_result, "incomplete": incomplete}

    def _fill_route_units(self, state, task_id, content, course_index=None, lease=None):
        courses = [c for st in content["stages"] for c in st["courses"]]
        content["structure_version"] = 2
        try:
            prompt = self.prompts.resolve_for_task(task_id, "outline_refine_v1")
        except PromptUnavailable:
            # Legacy task: bind once on explicit execution, never on page reads.
            try:
                self.prompts.bind_for_task(task_id, "outline_refine_v1")
                prompt = self.prompts.resolve_for_task(task_id, "outline_refine_v1")
            except PromptUnavailable:
                for c in courses:
                    if not c.get("units"):
                        c.update(refinement_status="failed", refinement_error="PROMPT_UNAVAILABLE")
                return
        adapter = self._require_model_adapter(state.owner_user_id, task_id)
        used = sum(len(u["lessons"]) for c in courses for u in c.get("units", []))
        pending = [i for i, c in enumerate(courses) if not c.get("units")]
        for index in pending:
            self._state(task_id)  # Do not issue further calls after abandonment.
            c = courses[index]
            if course_index is not None and index != course_index:
                continue
            budget = (OUTLINE_LIMITS["max_lessons"] - used) // len(pending)
            try:
                if budget < 1:
                    raise error("INVALID_OUTLINE_STRUCTURE")
                from .outputs import RouteCourseOut
                result = self._refine_course_units(adapter, state, RouteCourseOut.model_validate(c), prompt, budget)
                c["units"] = result["units"]
                c["prerequisites"] = result["prerequisites"]
                c["refinement_status"] = "ready"
                c.pop("refinement_error", None)
            except Exception as exc:
                from ..agents.limits import allows_content_fallback
                if not allows_content_fallback(exc):
                    raise
                c["refinement_status"] = "failed"
                c["refinement_error"] = "INVALID_OUTLINE_STRUCTURE"
            self.events.record(task_id, stage="generate", status="succeeded" if c.get("units") else "partial",
                               progress_current=sum(bool(x.get("units")) for x in courses),
                               error_code=None if c.get("units") else "INVALID_OUTLINE_STRUCTURE",
                               generation=lease.generation if lease else None, attempt=lease.attempt if lease else None)

    def _refine_course_units(self, adapter, state, course, prompt, budget=120) -> dict:
        """逐课细化：按课程名/目标产出单元/讲次（OutlineCandidate）；失败抛受控错误。"""
        template = prompt.get("template_snapshot") or prompt.get("template", "")
        if not template.strip():
            raise error("PROMPT_UNAVAILABLE")
        template += f"\n本课程讲次总数不得超过 {budget}，按范围调整单元和讲次数量，不得以省略内容满足预算。"
        user_content = (
            f"{template}\n课程名称：{course.title}\n课程学习目标：{course.goal or course.title}\n"
            "注意：所有 title/goal 字段只写自然语言，不得出现 course_、unit_ 等内部标识符，"
            "也不要加「第 N 单元」「步骤 N」等编号前缀。"
        )
        intake = load_json_field(state.intake_json, None)
        if intake and intake.get("answers"):
            profile = self._profile_text(intake)
            if profile:
                user_content += f"\n学习者画像（来自生成前访谈）：{profile}"
        messages = [
            {"role": "system", "content": "你是学习规划助手。只输出符合要求的 JSON。"},
            {"role": "user", "content": user_content},
        ]
        from ..agents.roles import planning_agent
        from ..agents.tools import planning_title_output_validator

        title_validator = planning_title_output_validator(course.title)
        def validate(ctx, output):
            output = title_validator(ctx, output)
            try: validate_outline_candidate(output)
            except PlanningError as exc: raise ModelRetry(exc.code) from None
            lessons = [l for u in output.units for l in u.lessons]
            if len(lessons) > budget or any(not l.goal.strip() for l in lessons):
                raise ModelRetry("INVALID_OUTLINE_STRUCTURE")
            if len({l.title.strip().casefold() for l in lessons}) != len(lessons):
                raise ModelRetry("duplicate lesson titles")
            return output
        run = planning_agent.refine_outline(
            adapter, OutlinePlanningInput.from_messages(messages), deps_for_source(adapter),
            stage="refine", output_validator=validate,
        )
        validate_outline_candidate(run.payload)
        lessons = [l for u in run.payload.units for l in u.lessons]
        if len(lessons) > budget or any(not l.goal.strip() for l in lessons):
            raise error("INVALID_OUTLINE_STRUCTURE")
        titles = [l.title.strip().casefold() for l in lessons]
        if len(set(titles)) != len(titles):
            raise error("INVALID_OUTLINE_STRUCTURE")
        return run.payload.model_dump()

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
        from ..agents.roles import planning_agent
        from ..agents.tools import make_search_materials_tool, planning_title_output_validator

        material_ids = load_json_field(state.material_ids_json, [])
        tools = [make_search_materials_tool(self.retrieval, owner_user_id=state.owner_user_id,
                                            material_ids=material_ids)] if material_ids else None
        title_validator = planning_title_output_validator(course.title_ref)
        def validate(ctx, output):
            output = title_validator(ctx, output)
            try: validate_outline_candidate(output)
            except PlanningError as exc: raise ModelRetry(exc.code) from None
            return output
        run = planning_agent.generate_outline(
            adapter, OutlinePlanningInput.from_messages(messages), deps_for_source(adapter),
            tools=tools, output_validator=validate,
        )
        candidate = run.payload
        summary = validate_outline_candidate(candidate)
        content = candidate.model_dump()
        draft_result = self.drafts.apply_candidate(
            owner_user_id=state.owner_user_id,
            target_id=target_id,
            candidate_kind="outline_generate",
            content=content,
            source_task_id=task_id, lease=lease,
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
        from ..tasks.service import LearningTaskRow
        with self.db.session() as sess:
            execution = sess.get(LearningTaskRow, task_id)
            if execution is not None and execution.cancel_requested:
                raise error("TASK_CANCELLED", status=409)
        if self.model_adapter_factory is None:
            raise error("MODEL_CAPABILITY_UNAVAILABLE", "no model adapter configured")
        adapter = self.model_adapter_factory(owner_user_id, task_id)
        if adapter is None:
            raise error("MODEL_CAPABILITY_UNAVAILABLE", "no active model config")
        return adapter

    def _target_label(self, state: PlanningTaskStateRow) -> str:
        """给模型的人类可读学习主题；绝不把内部 id 写进提示词（防模型把 id 织进标题）。"""
        label = ""
        if state.target_id:
            with self.db.session() as sess:
                row = (sess.get(CourseRow, state.target_id)
                       if state.target_kind == "course"
                       else sess.get(LearningRouteRow, state.target_id))
            label = (row.title_ref or "").strip() if row is not None else ""
        return label or "（尚未指定主题，请依据访谈画像自行拟定合适的主题）"

    def _compose_messages(self, kind: str, state: PlanningTaskStateRow, prompt: dict, search_outcome: Optional[SearchOutcome]) -> list[dict]:
        """提示词组装：模板快照 + 受控上下文（不含原始材料正文，只含受限摘录）。"""
        template = prompt.get("template_snapshot", "")
        external_note = ""
        if search_outcome and search_outcome.ok:
            external_note = f"（联网参考：{len(search_outcome.results)} 条外部来源）"
        user_content = (
            f"{template}\n学习主题：{self._target_label(state)} {external_note}\n"
            "注意：所有 title/goal 字段只写自然语言，不得出现 route_、outline_、cfg_ 等内部标识符，"
            "也不要加「第 N 阶段」「步骤 N」等编号前缀（阶段与先修结构本身已表达顺序）。"
        )
        intake = load_json_field(state.intake_json, None)
        if intake and intake.get("answers"):
            profile = self._profile_text(intake)
            if profile:
                user_content += f"\n学习者画像（来自生成前访谈）：{profile}"
        return [
            {"role": "system", "content": "你是学习规划助手。只输出符合要求的 JSON。"},
            {"role": "user", "content": user_content},
        ]

    # ------------------------------------------------------------------
    # AI访谈：完整历史、问题额度与学习者画像
    # ------------------------------------------------------------------
    def _intake_gate(self,lease,task_id,state):
        from .intake import IntakeService
        return IntakeService(self).gate(lease,task_id,state)

    def _intake_save(self,task_id,payload):
        with self.db.session() as sess:
            row=sess.get(PlanningTaskStateRow,task_id)
            if row:
                row.intake_json=json.dumps(payload,ensure_ascii=False);row.updated_at=utcnow()

    def _profile_text(self,intake):
        from .intake import profile_text
        return profile_text(intake)

    def decide_intake(self,task_id,*,actor_id,answers):
        from .intake import IntakeService
        return IntakeService(self).decide(task_id,actor_id,answers)

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
            from ..tasks.service import LearningTaskRow
            from ..logging_audit.models import TaskStateRow
            execution = sess.get(LearningTaskRow, task_id)
            event = sess.get(TaskStateRow, task_id)
            parent = sess.get(CourseRow if row.target_kind == "course" else LearningRouteRow, row.target_id)
            confirmed = bool(parent and (parent.current_outline_version_id if row.target_kind == "course" else parent.current_route_version_id))
            intake = load_json_field(row.intake_json, None)
            from .models import PlanningDraftRow
            draft = sess.get(PlanningDraftRow, row.draft_id) if row.draft_id else None
            content = load_json_field(draft.content_json, {}) if draft else {}
            courses = [c for st in content.get("stages", []) for c in st["courses"]]
            incomplete = bool(courses) and any(not c.get("units") for c in courses)
            return {
                "task_status": "cancelled" if row.abandoned_at else execution.status,
                "error_code": "TASK_CANCELLED" if row.abandoned_at else event.error_code if event else None,
                "generation": execution.generation,
                "attempt": execution.attempt,
                "progress_current": event.progress_current if event else 0,
                "progress_total": event.progress_total if event else 0,
                "abandoned": bool(row.abandoned_at),
                "confirmed": confirmed,
                "title": parent.title_ref if parent else "",
                "auto_retry_count": row.auto_retry_count,
                "answer_draft": load_json_field(row.answer_draft_json, []),
                "answer_draft_revision": row.answer_draft_revision,
                "structure_status": "partial" if incomplete else "ready" if courses else "pending",
                "task_id": row.task_id,
                "planning_scope": row.planning_scope,
                "resolved_scope": row.resolved_scope,
                "target_kind": row.target_kind,
                "target_id": row.target_id,
                "draft_id": row.draft_id,
                "wait_state": row.wait_state,
                "clarification_question": row.clarification_question,
                "search_decision": row.search_decision,
                "intake": {
                    "round": (intake or {}).get("round", 0),
                    "schema_version":(intake or {}).get("schema_version",0),
                    "min_questions":4,"max_questions":6,
                    "shown_count":(intake or {}).get("shown_count",len((intake or {}).get("questions",[]))),
                    "history":(intake or {}).get("history",(intake or {}).get("questions",[])),
                    "learner_profile":(intake or {}).get("learner_profile",{}),
                    "questions": (intake or {}).get("questions", []),
                    "answers": (intake or {}).get("answers", []),
                    "done": bool((intake or {}).get("done")),
                },
            }


__all__ = ["PlanningService", "PLAN_PROMPTS"]
