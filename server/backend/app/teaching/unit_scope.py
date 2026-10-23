"""Private, version-bound unit specifications independent of teaching records."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextvars import copy_context
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
import json
import threading
import uuid
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from pydantic_ai.exceptions import ModelRetry
from sqlalchemy import DateTime, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from ..logging_audit.models import Base
from ..materials.study_sources import block_text, chapter_catalog, digest, verify_sources
from ..planning.errors import error
from ..planning.models import (LessonRow, OutlineVersionRow,
                               load_json_field, utcnow)
from ..tasks.service import LearningTaskRow

SCOPE_POLICY_VERSION = 9
SCOPE_PROMPTS = ("unit_scope_v2", "unit_scope_v3", "unit_scope_v4", "unit_scope_v5", "unit_scope_v6")
SCOPE_REVIEW_PROMPTS = ("unit_scope_review_v1", "unit_scope_review_v2", "unit_scope_review_v3",
                       "unit_scope_review_v4", "unit_scope_review_v5")
STRUCTURED_SCOPE_REVIEWS = ("unit_scope_review_v4", "unit_scope_review_v5")


class ScopeReference(BaseModel):
    section_id: str = Field(min_length=1, max_length=160)
    quote: str = Field(min_length=1, max_length=400)


class ScopePoint(BaseModel):
    point_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    lesson_id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=160)
    capabilities: list[str] = Field(min_length=1, max_length=8)
    boundaries: list[str] = Field(default_factory=list, max_length=8)
    importance: str = Field(pattern=r"^(core|normal|light)$")
    source_refs: list[ScopeReference] = Field(default_factory=list, max_length=24)
    goal_basis: str = Field(default="", max_length=600)
    basis: Literal["source", "supplement", "goal"] = "source"
    supplement_reason: str = Field(default="", max_length=600)


class UnitScope(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    points: list[ScopePoint] = Field(min_length=1, max_length=120)
    source_corrections: list[str] = Field(default_factory=list, max_length=20)


class ScopeRequirement(BaseModel):
    requirement_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    lesson_id: str = Field(min_length=1, max_length=64)
    method_family: str = Field(min_length=1, max_length=160)
    cases: list[str] = Field(min_length=1, max_length=8)
    operations: list[str] = Field(min_length=1, max_length=6)
    goal_basis: str = Field(min_length=1, max_length=600)


class ScopeCoveragePlan(BaseModel):
    requirements: list[ScopeRequirement] = Field(min_length=1, max_length=64)


class ScopeCoverageMatch(BaseModel):
    requirement_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    point_ids: list[str] = Field(min_length=1, max_length=24)


class UnitScopeReview(BaseModel):
    additions: list[ScopePoint] = Field(default_factory=list, max_length=120)
    source_corrections: list[str] = Field(default_factory=list, max_length=20)
    rationale: str = Field(min_length=1, max_length=1200)
    coverage_matches: list[ScopeCoverageMatch] = Field(default_factory=list, max_length=64)


def validate_coverage_plan(plan, context):
    lessons = {lesson["lesson_id"]: lesson["goal"] for lesson in context["lessons"]}
    ids, allocated = set(), set()
    for requirement in plan.requirements:
        if (requirement.requirement_id in ids or requirement.lesson_id not in lessons
                or requirement.goal_basis != lessons[requirement.lesson_id]):
            raise ModelRetry("清单要求ID须唯一，讲次须来自输入且goal_basis原样引用本讲目标。")
        if any(not item.strip() or len(item) > 600
               for item in [requirement.method_family, *requirement.cases, *requirement.operations]):
            raise ModelRetry("清单的方法、适用情形与操作要求必须非空且限长。")
        ids.add(requirement.requirement_id)
        allocated.add(requirement.lesson_id)
    if allocated != set(lessons):
        raise ModelRetry("独立清单必须覆盖每个输入讲次，按方法族聚合，不遗漏讲次。")
    return plan


def validate_coverage_matches(review, merged, plan):
    requirements = {item.requirement_id: item for item in plan.requirements}
    points = {point.point_id: point for point in merged.points}
    matched = set()
    for match in review.coverage_matches:
        requirement = requirements.get(match.requirement_id)
        if requirement is None or match.requirement_id in matched:
            raise ModelRetry("覆盖对应须使用独立清单中的要求ID，每项恰好一次。")
        if (len(set(match.point_ids)) != len(match.point_ids) or any(
                ident not in points or points[ident].lesson_id != requirement.lesson_id
                for ident in match.point_ids)):
            raise ModelRetry("覆盖对应须引用最终考纲中同讲次的真实且不重复考点ID。")
        matched.add(match.requirement_id)
    if matched != set(requirements):
        raise ModelRetry("独立能力清单尚未逐项对应；先追加缺失能力，再引用合并后的考点ID。")
    return review


def merge_scope_review(review, candidate, context):
    try:
        merged = UnitScope(title=candidate.title, points=[*candidate.points, *review.additions],
                           source_corrections=list(dict.fromkeys(
                               [*candidate.source_corrections, *review.source_corrections])))
    except ValidationError:
        raise ModelRetry("审查合并后的考纲超过结构资源界限，请合并重复补充，不能删减原有能力。") from None
    return validate_scope(merged, context)


class UnitScopeRow(Base):
    __tablename__ = "unit_scopes"
    __table_args__ = (UniqueConstraint("owner_user_id", "unit_id", "input_hash",
                                      name="uq_unit_scope_owner_unit_input"),)
    scope_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    course_id: Mapped[str] = mapped_column(String(64), nullable=False)
    unit_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    outline_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    context_json: Mapped[str] = mapped_column(Text, nullable=False)
    scope_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def scope_capabilities(scope, scope_hash, lesson_id):
    result = []
    for point in scope.get("points", []):
        if point.get("lesson_id") != lesson_id:
            continue
        for index, capability in enumerate(point.get("capabilities", [])):
            ident = "sc_" + sha256(
                f"{scope_hash}:{point['point_id']}:{index}:{capability}".encode()).hexdigest()[:32]
            result.append({"capability_id": ident, "point_id": point["point_id"],
                "point_label": point["label"], "capability": capability,
                "boundaries": point.get("boundaries", []), "importance": point["importance"]})
    return result


def validate_scope(output, context):
    lessons = {item["lesson_id"] for item in context["lessons"]}
    sources = {}
    for item in [*context["source_sections"], *context.get("retrieved_source_sections", [])]:
        sources.setdefault(item["id"], []).append(item["text"])
    ids, covered, allocated = set(), set(), {}
    for point in output.points:
        if point.point_id in ids or point.lesson_id not in lessons:
            raise ModelRetry("考点ID必须唯一，归属讲次必须来自给定单元。")
        ids.add(point.point_id)
        allocated[point.lesson_id] = allocated.get(point.lesson_id, 0) + 1
        if any(not value.strip() or len(value) > 600 for value in point.capabilities + point.boundaries):
            raise ModelRetry("能力要求及条件边界必须是非空、限长的具体说明。")
        goal = next(item["goal"] for item in context["lessons"] if item["lesson_id"] == point.lesson_id)
        if point.basis == "supplement":
            if point.goal_basis != goal or not point.supplement_reason.strip():
                raise ModelRetry("补充考点须原样提供本讲goal_basis，并说明目标关联、资料缺口及必要性。")
        elif sources and (point.basis != "source" or not point.source_refs):
            raise ModelRetry("资料课程的每个考点必须有逐字来源依据。")
        if not sources and (point.source_refs or point.goal_basis != goal):
            raise ModelRetry("无资料考点必须使用本讲原始目标作为goal_basis，不能伪造资料引用。")
        if point.basis != "supplement" and point.supplement_reason:
            raise ModelRetry("非补充考点不能填写补充理由；资料未支持的能力须明确标记supplement。")
        for ref in point.source_refs:
            if ref.section_id not in sources or not any(ref.quote in text for text in sources[ref.section_id]):
                raise ModelRetry("引用必须属于给定资料小节，quote必须能在该小节正文逐字定位。")
            covered.add(ref.section_id)
    if set(allocated) != lessons:
        raise ModelRetry("每个讲次必须分配考点。")
    if context.get("scope_version", 1) < 3 and max(allocated.values()) > 4:
        raise ModelRetry("每个讲次必须分配考点，每讲最多四个；可合并同一方法的多个来源。")
    if not set(context["required_section_ids"]) <= covered:
        missing = sorted(set(context["required_section_ids"]) - covered)
        raise ModelRetry("必需小节尚无考点依据：" + "、".join(missing))
    return output


class UnitScopeService:
    def __init__(self, assessment):
        self.assessment = assessment
        self.teaching = assessment.teaching
        self.db, self.tasks, self.prompts = assessment.db, assessment.tasks, assessment.prompts
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="unit-scope")
        self.active, self.lock = set(), threading.Lock()

    def context(self, sess, course, unit, owner):
        lessons = list(sess.scalars(select(LessonRow).where(LessonRow.unit_id == unit.unit_id)
                                   .order_by(LessonRow.order_index)))
        if not lessons:
            raise error("invalid_field", "单元尚无讲次。", status=409)
        outline = sess.get(OutlineVersionRow, unit.outline_version_id)
        snapshot = load_json_field(outline.source_snapshot_json, {})
        docs = verify_sources(sess, owner, snapshot)
        catalog = chapter_catalog(docs)
        selected = {ref for lesson in lessons
                    for ref in snapshot.get("lessons", {}).get(lesson.lesson_id, [])}
        sections = [section for section in catalog if section["id"] in selected]
        # Include descendants of bound chapters; restricting input to titles
        # would silently omit methods described in the source body.
        sections = [section for section in catalog if not section["reference_only"] and any(
            section["id"].split("/")[0] == parent["id"].split("/")[0]
            and parent["offset"] <= section["offset"] < parent["end_offset"]
            for parent in sections)]
        from ..materials.source_models import source_records
        texts = {}
        for material, source in source_records(sess, owner, snapshot.get("material_ids", [])):
            state = load_json_field(source.result_json, {})
            pages = sorted(state.get("page_results", []), key=lambda item: item["page_no"])
            if not pages:
                pages = [{"page_no": c["page_no"], "blocks": [{"type": "text", "content": c["text"]}]}
                         for c in state.get("chunks", []) if c.get("kind") != "image_understanding"]
            separator = "\n" if state.get("unit") == "document" else "\f"
            texts[material.material_id] = separator.join(
                "\n".join(filter(None, (block_text(block) for block in page.get("blocks", []))))
                for page in pages)
        # Leaf sections are the structural minimum. Also include text before a
        # chapter's first child, so introductory requirements are not discarded.
        leaves = [section for section in sections if not any(
            other["id"] != section["id"]
            and other["id"].split("/")[0] == section["id"].split("/")[0]
            and section["offset"] < other["offset"] < section["end_offset"]
            and other["level"] > section["level"] for other in sections)]
        source_sections = [{**section, "text": texts[section["id"].split("/")[0]][
            section["offset"]:section["end_offset"]]} for section in sections]
        if sum(len(section["text"]) for section in source_sections) > 180000:
            raise error("invalid_field", "单元资料过大，请缩小单元资料范围后生成考纲。", status=409)
        if snapshot.get("material_ids") and not source_sections:
            raise error("invalid_field", "资料课程的单元尚未绑定可用章节。", status=409)
        return {"scope_version": SCOPE_POLICY_VERSION, "course_title": course.title_ref, "unit_title": unit.title_ref,
                "outline_version_id": unit.outline_version_id,
                "lessons": [{"lesson_id": item.lesson_id, "title": item.title_ref, "goal": item.goal_ref}
                            for item in lessons],
                "source_snapshot": snapshot, "source_sections": source_sections,
                "required_section_ids": [item["id"] for item in leaves]}

    def _owned(self, sess, ident, owner):
        row = sess.get(UnitScopeRow, ident)
        if not row or row.owner_user_id != owner:
            raise error("course_not_found", status=404)
        course, unit = self.assessment._unit(sess, row.course_id, row.unit_id, owner)
        if course.current_outline_version_id != row.outline_version_id:
            raise error("TASK_CANCELLED", status=409)
        if digest(self.context(sess, course, unit, owner)) != row.input_hash:
            raise error("TASK_CANCELLED", "考纲输入已变更，请重新生成。", status=409)
        return row

    def _view(self, sess, row):
        task = sess.get(LearningTaskRow, row.task_id)
        from ..logging_audit.models import TaskStateRow
        state = sess.get(TaskStateRow, row.task_id)
        return {"scope_id": row.scope_id, "task_id": row.task_id, "status": task.status,
                "error_code": state.error_code if state else None, "input_hash": row.input_hash,
                "scope_hash": row.scope_hash,
                "scope": load_json_field(row.scope_json, {}) if task.status == "succeeded" else None}

    def get(self, course_id, unit_id, owner):
        with self.db.session() as sess:
            course, unit = self.assessment._unit(sess, course_id, unit_id, owner)
            current = digest(self.context(sess, course, unit, owner))
            row = sess.scalar(select(UnitScopeRow).where(UnitScopeRow.owner_user_id == owner,
                UnitScopeRow.unit_id == unit_id, UnitScopeRow.input_hash == current))
            return self._view(sess, row) if row else {"status": "not_generated", "scope": None}

    def generate(self, course_id, unit_id, owner):
        with self.db.session() as sess:
            course, unit = self.assessment._unit(sess, course_id, unit_id, owner)
            sess.refresh(course, with_for_update=True)
            context = self.context(sess, course, unit, owner)
            fingerprint = digest(context)
            row = sess.scalar(select(UnitScopeRow).where(UnitScopeRow.owner_user_id == owner,
                UnitScopeRow.unit_id == unit_id, UnitScopeRow.input_hash == fingerprint))
            if row is None:
                task = self.tasks.create_task(owner_user_id=owner, kind="unit_scope_generate",
                    subject_id=course_id, idempotency_key="unit-scope:" + digest([owner, unit_id, fingerprint]),
                    stage="generate", lane="maintenance", deadline_seconds=2400, session=sess)
                row = UnitScopeRow(scope_id="scope_" + uuid.uuid4().hex[:16], owner_user_id=owner,
                    course_id=course_id, unit_id=unit_id, outline_version_id=unit.outline_version_id,
                    input_hash=fingerprint, task_id=task["task_id"],
                    context_json=json.dumps(context, ensure_ascii=False))
                sess.add(row)
                sess.flush()
                from ..agents.task_binding import AgentBudgetRow
                budget = sess.get(AgentBudgetRow, row.task_id)
                budget.request_limit = 24 if context["source_snapshot"].get("material_ids") else 12
                budget.tool_limit = 24
            ident, task_id = row.scope_id, row.task_id
        self.assessment._prepare_source_grant(task_id, owner)
        from ..prompts.service import PromptBindingRow
        review_name = self._bound_prompt_name(task_id, SCOPE_REVIEW_PROMPTS)
        bindings = [(self._bound_prompt_name(task_id, SCOPE_PROMPTS), "unit-scope-2"),
                    (review_name, "unit-scope-review-2" if review_name in STRUCTURED_SCOPE_REVIEWS
                     else "unit-scope-review-1")]
        if review_name in STRUCTURED_SCOPE_REVIEWS:
            bindings.append(("unit_scope_coverage_v2" if review_name == "unit_scope_review_v5"
                             else "unit_scope_coverage_v1", "unit-scope-coverage-1"))
        if context.get("scope_version", 1) >= 9 and context["source_snapshot"].get("material_ids"):
            bindings.append(("unit_scope_evidence_v1", "unit-scope-evidence-1"))
        for name, schema in bindings:
            with self.db.session() as sess:
                bound = sess.scalar(select(PromptBindingRow.binding_id).where(
                    PromptBindingRow.task_id == task_id, PromptBindingRow.prompt_name == name))
            if not bound:
                self.prompts.bind_for_task(task_id, name,
                    agent_code_version=str(SCOPE_POLICY_VERSION), output_schema_version=schema)
        self.kick(task_id)
        return {"scope_id": ident, "task_id": task_id}

    def retry(self, course_id, unit_id, owner):
        value = self.get(course_id, unit_id, owner)
        if not value.get("task_id"):
            raise error("invalid_field", status=409)
        self.assessment._prepare_source_grant(value["task_id"], owner)
        self.tasks.requeue_for_retry(value["task_id"], actor_id=owner)
        self.kick(value["task_id"])
        return {"scope_id": value["scope_id"], "task_id": value["task_id"]}

    def _bound_prompt_name(self, task_id, names):
        from ..prompts.service import PromptBindingRow
        with self.db.session() as sess:
            return sess.scalar(select(PromptBindingRow.prompt_name).where(
                PromptBindingRow.task_id == task_id, PromptBindingRow.prompt_name.in_(names))
                .order_by(PromptBindingRow.created_at)) or names[-1]

    def kick(self, task_id):
        with self.lock:
            if task_id in self.active:
                return
            self.active.add(task_id)
        self.pool.submit(copy_context().run, self._run, task_id)

    def _run(self, task_id):
        lease = None
        try:
            lease = self.tasks.claim(task_id, lease_owner="unit-scope", lease_seconds=600,
                                     allow_reclaim_active=False)
            with ThreadPoolExecutor(max_workers=1) as worker:
                future = worker.submit(copy_context().run, self._compute, task_id)
                while True:
                    try:
                        result = future.result(timeout=30)
                        break
                    except TimeoutError:
                        self.tasks.heartbeat(lease, lease_seconds=600)
            def publish(sess):
                self.assessment._verify_boundary(sess, task_id)
                saved = sess.scalar(select(UnitScopeRow).where(UnitScopeRow.task_id == task_id))
                row = self._owned(sess, saved.scope_id, saved.owner_user_id)
                row.scope_json = json.dumps(result, ensure_ascii=False)
                row.scope_hash = digest(result)
            self.tasks.submit_result(lease, stage="generate", status="succeeded",
                                     duration_ms=0, publish=publish)
        except Exception as exc:
            if lease:
                from ..logging_audit.contracts import validate_error_code
                try:
                    code = self.teaching._controlled_failure(exc)
                except Exception:
                    code = "INTERNAL_ERROR"
                try:
                    validate_error_code(code)
                except Exception:
                    code = "MODEL_CAPABILITY_UNAVAILABLE"
                try:
                    self.tasks.submit_result(lease, stage="generate", status="failed",
                                             error_code=code, duration_ms=0)
                except Exception:
                    pass
        finally:
            with self.lock:
                self.active.discard(task_id)

    def _compute(self, task_id):
        candidate, seed_hash = None, ""
        with self.db.session() as sess:
            row = sess.scalar(select(UnitScopeRow).where(UnitScopeRow.task_id == task_id))
            row = self._owned(sess, row.scope_id, row.owner_user_id)
            context, owner = load_json_field(row.context_json, {}), row.owner_user_id
            search_checkpoint = load_json_field(row.scope_json, {}).get("_material_search")
            from ..tasks.service import Lease
            active_task = sess.get(LearningTaskRow, task_id)
            search_lease = Lease(active_task.task_id, active_task.attempt_id, active_task.lease_token,
                active_task.lease_owner, active_task.generation, active_task.attempt,
                active_task.lease_until, active_task.deadline_at, owner)
            current_inputs = {key: value for key, value in context.items() if key != "scope_version"}
            seeds = sess.scalars(select(UnitScopeRow).join(
                LearningTaskRow, UnitScopeRow.task_id == LearningTaskRow.task_id).where(
                    UnitScopeRow.owner_user_id == owner, UnitScopeRow.unit_id == row.unit_id,
                    UnitScopeRow.scope_id != row.scope_id, LearningTaskRow.status == "succeeded")
                .order_by(UnitScopeRow.created_at.desc()))
            for seed in seeds:
                seed_inputs = load_json_field(seed.context_json, {})
                if {key: value for key, value in seed_inputs.items() if key != "scope_version"} != current_inputs:
                    continue
                value = load_json_field(seed.scope_json, {})
                if digest(value) != seed.scope_hash:
                    continue
                try:
                    candidate = validate_scope(UnitScope.model_validate(value), context)
                except ModelRetry:
                    continue
                seed_hash = seed.scope_hash
                break
        adapter = self.teaching.model_adapter_factory(owner, task_id) if self.teaching.model_adapter_factory else None
        if adapter is None:
            raise error("MODEL_CAPABILITY_UNAVAILABLE")
        if hasattr(adapter, "timeout"):
            adapter.timeout = 300
            adapter.separate_connect_timeout = True
        from ..agents.contracts import UnitScopeInput, UnitScopeReviewInput, UnitScopeCoverageInput
        from ..agents.roles import unit_scope_agent, unit_scope_review_agent, unit_scope_coverage_agent
        from ..agents.runner import deps_for_source
        prompt = self.prompts.resolve_for_task(
            task_id, self._bound_prompt_name(task_id, SCOPE_PROMPTS))["template_snapshot"]
        try:
            deps = replace(deps_for_source(adapter), omit_content_trace=True,
                           on_process=lambda *_args, **_kwargs: None)
            tools = []
            material_search = None
            if context.get("scope_version", 1) >= 8 and context["source_snapshot"].get("material_ids"):
                from .scope_materials import (LegacyScopeMaterialSearch, ScopeMaterialSearch,
                    SCOPE_MATERIAL_POLICY, ScopeEvidenceDecision)
                prompt += "\n" + SCOPE_MATERIAL_POLICY
                if context.get("scope_version", 1) >= 9:
                    from ..agents.runner import run_structured_agent
                    evidence_prompt = self.prompts.resolve_for_task(
                        task_id, "unit_scope_evidence_v1")["template_snapshot"]

                    def checkpoint_search(value):
                        deps.check()
                        with self.db.session() as sess:
                            current = sess.scalar(select(UnitScopeRow).where(UnitScopeRow.task_id == task_id))
                            current = self._owned(sess, current.scope_id, owner)
                            # Lock/check the active lease in the same transaction as the write.
                            self.tasks.validate_lease(search_lease,
                                actor_id=owner, session=sess)
                            current.scope_json = json.dumps({"_material_search": value}, ensure_ascii=False)

                    def decide_evidence(evidence_context):
                        def validate_evidence(_, output):
                            allowed = {s["id"] for s in [
                                *evidence_context.get("source_sections", []),
                                *evidence_context.get("retrieved_source_sections", [])]}
                            if (not set(output.evidence_section_ids) <= allowed
                                    or output.sufficient and not output.evidence_section_ids):
                                raise ModelRetry("只引用已提供正文或摘录的章节ID，判定充分必须提供依据。")
                            return output

                        return run_structured_agent(adapter, ScopeEvidenceDecision, [
                            {"role": "system", "content": evidence_prompt},
                            {"role": "user", "content": json.dumps(evidence_context, ensure_ascii=False)}],
                            deps=deps, agent_name="scope_evidence_review",
                            output_validator=validate_evidence, max_requests=3,
                            timeout_seconds=300).payload

                    material_search = ScopeMaterialSearch(self.teaching, owner, task_id, context,
                        deps, checkpoint=search_checkpoint, save=checkpoint_search)
                    material_search.prepare(decide_evidence)
                    prompt += "\nmaterial_evidence为服务端取证结论，未解决缺口须诚实作为补充或限制说明，不能伪造资料依据。"
                else:
                    material_search = LegacyScopeMaterialSearch(self.teaching, owner, task_id, context, deps)
                    tools = [material_search.search_materials]
                    for lesson in context["lessons"][:3]:
                        deps.budget.consume("tool")
                        material_search.search_materials(lesson["title"] + " " + lesson["goal"])
            review_name = self._bound_prompt_name(task_id, SCOPE_REVIEW_PROMPTS)
            plan = None
            if review_name in STRUCTURED_SCOPE_REVIEWS:
                plan_prompt = self.prompts.resolve_for_task(
                    task_id, "unit_scope_coverage_v2" if review_name == "unit_scope_review_v5"
                    else "unit_scope_coverage_v1")
                planned = unit_scope_coverage_agent.plan_coverage(
                    adapter, UnitScopeCoverageInput.from_messages([
                        {"role": "system", "content": plan_prompt["template_snapshot"] +
                         ("\n" + SCOPE_MATERIAL_POLICY if material_search else "")},
                        {"role": "user", "content": json.dumps(
                            {"stage": "scope_coverage_plan", "context": context}, ensure_ascii=False)}]),
                    deps, output_validator=lambda _, output: validate_coverage_plan(output, context),
                    timeout_seconds=600, tools=tools)
                plan = planned.payload
            generation_context = {**context, **({"coverage_plan": plan.model_dump()} if plan else {})}
            if candidate is None:
                result = unit_scope_agent.plan_scope(adapter, UnitScopeInput.from_messages([
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(generation_context, ensure_ascii=False)}]),
                    deps, output_validator=lambda _, output: validate_scope(output, context),
                    timeout_seconds=900, tools=tools)
                candidate = result.payload
                seed_hash = digest(candidate.model_dump())
            review_prompt = self.prompts.resolve_for_task(
                task_id, review_name)
            def validate_review(_, review):
                merged = merge_scope_review(review, candidate, context)
                if plan:
                    validate_coverage_matches(review, merged, plan)
                return review
            reviewed = unit_scope_review_agent.review_scope(
                adapter, UnitScopeReviewInput.from_messages([
                    {"role": "system", "content": review_prompt["template_snapshot"] +
                     ("\n" + SCOPE_MATERIAL_POLICY if material_search else "")},
                    {"role": "user", "content": json.dumps(
                        {"context": context, "candidate": candidate.model_dump(),
                         **({"coverage_plan": plan.model_dump()} if plan else {})}, ensure_ascii=False)}]),
                deps, output_validator=validate_review, timeout_seconds=600, tools=tools)
            value = merge_scope_review(reviewed.payload, candidate, context).model_dump()
            value["review"] = {"seed_scope_hash": seed_hash,
                               "prompt_sha256": review_prompt["template_sha256"],
                               "rationale": reviewed.payload.rationale}
            if plan:
                value["coverage_plan"] = plan.model_dump()
                value["coverage_matches"] = [
                    match.model_dump() for match in reviewed.payload.coverage_matches]
                value["review"]["coverage_prompt_sha256"] = plan_prompt["template_sha256"]
            if material_search:
                value["retrieved_source_sections"] = context.get("retrieved_source_sections", [])
                value["material_search"] = material_search.receipts
                if context.get("scope_version", 1) >= 9:
                    value["material_evidence"] = material_search.summary()
                    value["_material_search"] = material_search.state
            return value
        finally:
            if callable(getattr(adapter, "close", None)):
                adapter.close()
