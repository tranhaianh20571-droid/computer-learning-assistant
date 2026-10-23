"""切片 3 输出契约：Pydantic 输出模型与服务端二次校验（T03 冻结）。

- RouteCandidate / OutlineCandidate / ChatAnswer 为模型输出路径的 Pydantic 校验模型；
- 服务端二次校验（DAG、层级、排序、长度、节点数上限）独立于 PydanticAI，
  对 native/tool/prompted 三种输出模式统一执行；
- 板书声明 schema 仅冻结元素白名单常量（实现归切片 4）。

字段上限（契约冻结值，写入契约测试夹具）：
- 路线：阶段 ≤ 12，每阶段课程 ≤ 8，课程总数 ≤ 40；标题/目标 ≤ 120 字；
- 大纲：单元 ≤ 12，每单元讲次 ≤ 20，讲次总数 ≤ 120；标题 ≤ 120 字；
- 即时回答：≤ 4000 字。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .errors import PlanningError, error

# ---- 上下文预算五参数（T03 冻结默认值；任务/manifest 落库） ----
CONTEXT_BUDGET_DEFAULTS = {
    "max_input_tokens": 8000,
    "max_retrieval_chars": 24000,
    "max_retrieval_chunks": 24,
    "max_images": 4,
    "max_history_messages": 12,
}
# 预算超限处理优先级：历史消息 > 检索片段长度 > 检索片段数量 > 图片
TRUNCATION_PRIORITY = ("history_messages", "retrieval_chars", "retrieval_chunks", "images")

# ---- 板书元素白名单（场景转换层共用；板书时序声明归切片 4） ----
SCENE_ELEMENT_WHITELIST = ("rectangle", "ellipse", "text", "arrow", "line")
BOARD_ELEMENT_WHITELIST = SCENE_ELEMENT_WHITELIST

ROUTE_LIMITS = {"max_stages": 12, "max_courses_per_stage": 8, "max_courses": 40, "max_text_len": 120}
OUTLINE_LIMITS = {"max_units": 12, "max_lessons_per_unit": 20, "max_lessons": 120, "max_text_len": 120}
CHAT_LIMITS = {"max_answer_len": 4000}

PROMPTED_PARSE_RETRIES = 2


class OutlineLessonOut(BaseModel):
    title: str = Field(min_length=1, max_length=OUTLINE_LIMITS["max_text_len"])
    goal: str = Field(default="", max_length=OUTLINE_LIMITS["max_text_len"])
    # 生成期时间预算与路径分层；旧草稿缺省时按 45 分钟/core 处理。
    estimated_minutes: int = Field(default=45, ge=5, le=240)
    path_role: Literal["core", "extension"] = "core"
    source_section_ids: list[str] = Field(default_factory=list, max_length=8)
    source_supplement_reason: str = Field(default="", max_length=300)
    teaching_mode: Literal["standard", "source_reading"] = "standard"

    @field_validator("title", "goal")
    @classmethod
    def _no_control(cls, v: str) -> str:
        return "".join(ch for ch in v if ch >= " " or ch == "\t")


class OutlineUnitOut(BaseModel):
    title: str = Field(min_length=1, max_length=OUTLINE_LIMITS["max_text_len"])
    # 容器下限刻意不写在字段上：字段级 metadata 会在 SDK 反序列化阶段抛
    # ValidationError，output_validator 根本不会被调用，模型只会拿到裸英文
    # pydantic 错误并耗尽重试预算（OUTPUT_INVALID）。空单元改由
    # _validate_units 抛可读的 INVALID_OUTLINE_STRUCTURE("unit has no lesson")。
    lessons: list[OutlineLessonOut] = Field(default_factory=list)


class OutlinePrereqOut(BaseModel):
    from_index: int = Field(ge=0)
    to_index: int = Field(ge=0)


class RouteCourseOut(BaseModel):
    title: str = Field(min_length=1, max_length=ROUTE_LIMITS["max_text_len"])
    goal: str = Field(default="", max_length=ROUTE_LIMITS["max_text_len"])
    # 逐课细化（outline_refine）结果：课程内嵌单元/讲次；空列表表示该课仅到课程粒度
    units: list[OutlineUnitOut] = Field(default_factory=list)
    prerequisites: list[OutlinePrereqOut] = Field(default_factory=list)

    @field_validator("title", "goal")
    @classmethod
    def _no_control(cls, v: str) -> str:
        return "".join(ch for ch in v if ch >= " " or ch == "\t")


class RouteStageOut(BaseModel):
    title: str = Field(min_length=1, max_length=ROUTE_LIMITS["max_text_len"])
    # 同 OutlineUnitOut.lessons：空阶段由 validate_route_candidate 抛可读的
    # ROUTE_STRUCTURE_INVALID("stage has no course")，不在解析层拒绝。
    courses: list[RouteCourseOut] = Field(default_factory=list)


class RoutePrereqOut(BaseModel):
    from_index: int = Field(ge=0)
    to_index: int = Field(ge=0)


class CoverageMapping(BaseModel):
    """模型声明的覆盖区域 → 课程标题；服务端再绑定到具体节点路径。"""

    area: str = Field(min_length=1, max_length=120)
    course_titles: list[str] = Field(default_factory=list, max_length=ROUTE_LIMITS["max_courses"])


class RouteCandidate(BaseModel):
    # 大标题（可选）：为空时沿用路线标题
    title: str = Field(default="", max_length=ROUTE_LIMITS["max_text_len"])
    # 下限同样不写在字段上：空 stages 由 validate_route_candidate 抛可读的
    # ROUTE_STRUCTURE_INVALID("route needs at least one course")。
    stages: list[RouteStageOut] = Field(max_length=ROUTE_LIMITS["max_stages"])
    prerequisites: list[RoutePrereqOut] = Field(default_factory=list)
    # v3：覆盖声明与假设；确认事务不消费这两个字段，但草稿/版本保留。
    coverage_map: list[CoverageMapping] = Field(default_factory=list, max_length=24)
    assumptions: list[str] = Field(default_factory=list, max_length=12)


class OutlineCandidate(BaseModel):
    # 课程名（可选）：为空时沿用课程标题
    title: str = Field(default="", max_length=OUTLINE_LIMITS["max_text_len"])
    # 空 units 由 outline_generation_issue 抛可读原因，不在解析层拒绝。
    units: list[OutlineUnitOut] = Field(max_length=OUTLINE_LIMITS["max_units"])
    prerequisites: list[OutlinePrereqOut] = Field(default_factory=list)


class ChatAnswer(BaseModel):
    answer: str = Field(min_length=1, max_length=CHAT_LIMITS["max_answer_len"])


# ---------------------------------------------------------------------------
# 规划质量契约（v3）：范围、覆盖、前置、时间预算与评审
# ---------------------------------------------------------------------------


class TimeBudget(BaseModel):
    weeks: int = Field(ge=1, le=104)
    hours_per_week: float = Field(gt=0, le=80)
    total_hours: float = Field(gt=0, le=104 * 80)


class ScopeContract(BaseModel):
    target_outcome: str = Field(min_length=1, max_length=300)
    in_scope: list[str] = Field(min_length=1, max_length=12)
    out_of_scope: list[str] = Field(default_factory=list, max_length=12)
    assumptions: list[str] = Field(default_factory=list, max_length=12)
    time_budget: TimeBudget | None = None


class CoverageArea(BaseModel):
    area: str = Field(min_length=1, max_length=120)
    depth: Literal["基础", "核心", "进阶"] = "核心"
    search_query: str = Field(min_length=1, max_length=80)
    required: bool = True


class PrerequisitePolicy(BaseModel):
    assumed_general: list[str] = Field(default_factory=list, max_length=12)
    bridge_required: list[str] = Field(default_factory=list, max_length=12)
    notes: str = Field(default="", max_length=600)


class CoveragePlan(BaseModel):
    scope_contract: ScopeContract
    coverage_areas: list[CoverageArea] = Field(min_length=1, max_length=12)
    prerequisite_policy: PrerequisitePolicy = Field(default_factory=PrerequisitePolicy)
    # fallback 表示模型覆盖规划不可用；体检只做结构与时间检查，不伪造覆盖结论。
    status: Literal["ready", "fallback"] = "ready"


class QualityIssue(BaseModel):
    dimension: Literal[
        "coverage", "prerequisites", "sequencing", "actionability", "specificity", "transparency"
    ]
    severity: Literal["warning", "critical"] = "warning"
    message: str = Field(min_length=1, max_length=600)
    node_path: str = Field(default="", max_length=300)


class QualityDimensions(BaseModel):
    coverage: Literal["pass", "partial", "fail"] = "pass"
    prerequisites: Literal["pass", "partial", "fail"] = "pass"
    sequencing: Literal["pass", "partial", "fail"] = "pass"
    actionability: Literal["pass", "partial", "fail"] = "pass"
    specificity: Literal["pass", "partial", "fail"] = "pass"
    transparency: Literal["pass", "partial", "fail"] = "pass"


class QualityReview(BaseModel):
    verdict: Literal["pass", "needs_repair", "blocked"] = "pass"
    dimensions: QualityDimensions = Field(default_factory=QualityDimensions)
    issues: list[QualityIssue] = Field(default_factory=list, max_length=30)
    repairs: list[str] = Field(default_factory=list, max_length=20)
    acknowledgement_required: bool = False


# ---------------------------------------------------------------------------
# 服务端二次校验（对三种输出模式统一执行；失败只重试生成/转换环节）
# ---------------------------------------------------------------------------


def flat_course_titles(candidate: RouteCandidate) -> list[str]:
    return [c.title for stage in candidate.stages for c in stage.courses]


def flat_lesson_titles(candidate: OutlineCandidate) -> list[str]:
    return [l.title for unit in candidate.units for l in unit.lessons]


# 模型偶尔把提示词里的内部 id（目标对象）织进标题；生成与确认两条路径统一兜底剥除
_INTERNAL_ID_RE = re.compile(r"\b(?:route|outline|course|lesson|unit|stage|task|draft|cfg)_[0-9a-zA-Z]{6,}\b")


def _clean_title_text(title: str) -> str:
    cleaned = _INTERNAL_ID_RE.sub("", str(title))
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"[：:]\s*[：:]", "：", cleaned)
    cleaned = cleaned.strip(" ：:，, ")
    return cleaned or "未命名"


def internal_id_residue(text: str) -> bool:
    """标题/目标文本是否残留内部标识符（供生成侧 output_validator 回喂）。"""
    return bool(_INTERNAL_ID_RE.search(str(text)))


def strip_internal_ids_in_place(content: dict) -> None:
    """就地剥掉 content_json 标题/目标里的内部 id（route/outline 两种形状，含课程内嵌单元/讲次）。"""
    # 空/缺省标题不清洗（_clean_title_text 会把空值回填为「未命名」）
    if isinstance(content.get("title"), str) and content["title"]:
        content["title"] = _clean_title_text(content["title"])
    for stage in content.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        if isinstance(stage.get("title"), str):
            stage["title"] = _clean_title_text(stage["title"])
        for course in stage.get("courses") or []:
            if not isinstance(course, dict):
                continue
            if isinstance(course.get("title"), str):
                course["title"] = _clean_title_text(course["title"])
            if isinstance(course.get("goal"), str) and course["goal"]:
                course["goal"] = _clean_title_text(course["goal"])
            _strip_units_in_place(course.get("units"))
    _strip_units_in_place(content.get("units"))


def _strip_units_in_place(units) -> None:
    for unit in units or []:
        if not isinstance(unit, dict):
            continue
        if isinstance(unit.get("title"), str):
            unit["title"] = _clean_title_text(unit["title"])
        for lesson in unit.get("lessons") or []:
            if not isinstance(lesson, dict):
                continue
            if isinstance(lesson.get("title"), str):
                lesson["title"] = _clean_title_text(lesson["title"])
            if isinstance(lesson.get("goal"), str) and lesson["goal"]:
                lesson["goal"] = _clean_title_text(lesson["goal"])


def _validate_units(units: list[OutlineUnitOut]) -> int:
    """单元/讲次层级校验（单课程粒度）：清洗文本 + 数量上限；返回讲次总数。"""
    if len(units) > OUTLINE_LIMITS["max_units"]:
        raise error("INVALID_OUTLINE_STRUCTURE", "too many units")
    total = 0
    for unit in units:
        unit.title = _clean_title_text(unit.title)
        for lesson in unit.lessons:
            lesson.title = _clean_title_text(lesson.title)
            if lesson.goal:
                lesson.goal = _clean_title_text(lesson.goal)
        if not unit.lessons:
            raise error("INVALID_OUTLINE_STRUCTURE", "unit has no lesson")
        if len(unit.lessons) > OUTLINE_LIMITS["max_lessons_per_unit"]:
            raise error("INVALID_OUTLINE_STRUCTURE", "too many lessons in unit")
        total += len(unit.lessons)
    if total > OUTLINE_LIMITS["max_lessons"]:
        raise error("INVALID_OUTLINE_STRUCTURE", "too many lessons")
    return total


def validate_route_candidate(candidate: RouteCandidate) -> dict:
    """长度/数量/结构/DAG 校验。返回规范化摘要；失败抛受控错误码。"""
    candidate.title = _clean_title_text(candidate.title) if candidate.title else ""
    total = 0
    lesson_total = 0
    for stage in candidate.stages:
        stage.title = _clean_title_text(stage.title)
        for course in stage.courses:
            course.title = _clean_title_text(course.title)
            if course.goal:
                course.goal = _clean_title_text(course.goal)
            if course.units:
                count = _validate_units(course.units)
                lesson_total += count
                _validate_prereq_indices(course.prerequisites, count, lambda e: (e.from_index, e.to_index))
        if not stage.courses:
            raise error("ROUTE_STRUCTURE_INVALID", "stage has no course")
        if len(stage.courses) > ROUTE_LIMITS["max_courses_per_stage"]:
            raise error("ROUTE_STRUCTURE_INVALID", "too many courses in stage")
        total += len(stage.courses)
    if total < 1:
        raise error("ROUTE_STRUCTURE_INVALID", "route needs at least one course")
    if total > ROUTE_LIMITS["max_courses"]:
        raise error("ROUTE_STRUCTURE_INVALID", "too many courses")
    # 全路线讲次总量上限（防导图节点爆炸）：与单课程讲次上限同值
    if lesson_total > OUTLINE_LIMITS["max_lessons"]:
        raise error("ROUTE_STRUCTURE_INVALID", "too many lessons across courses")
    _validate_prereq_indices(
        candidate.prerequisites,
        total,
        lambda e: (e.from_index, e.to_index),
    )
    return {
        "stage_count": len(candidate.stages),
        "course_count": total,
        "lesson_count": lesson_total,
    }


def validate_outline_candidate(candidate: OutlineCandidate) -> dict:
    candidate.title = _clean_title_text(candidate.title) if candidate.title else ""
    total = _validate_units(candidate.units)
    _validate_prereq_indices(
        candidate.prerequisites, total, lambda e: (e.from_index, e.to_index)
    )
    return {"unit_count": len(candidate.units), "lesson_count": total}


def _validate_prereq_indices(edges, node_total: int, key) -> None:
    for edge in edges:
        f, t = key(edge)
        if f == t or f < 0 or t < 0 or f >= node_total or t >= node_total:
            raise error("INVALID_PREREQUISITE", "prerequisite index out of range")
    if _has_cycle(edges, node_total, key):
        raise error("DAG_CYCLE", "prerequisite graph has a cycle")


def _has_cycle(edges, node_total: int, key) -> bool:
    """DAG 无环判定：拓扑排序（Kahn）。"""
    indegree = [0] * node_total
    adj: dict[int, list[int]] = {}
    for edge in edges:
        f, t = key(edge)
        adj.setdefault(f, []).append(t)
        indegree[t] += 1
    queue = [i for i in range(node_total) if indegree[i] == 0]
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        for nxt in adj.get(node, ()):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    return visited != node_total


# ---- 生成期降级：模型可见的回喂原因 + 确定性修正 ----
#
# 落库入口（validate_content_json → validate_route_candidate /
# validate_outline_candidate）必须保持严格：草稿一旦入库就是冻结契约。
# 生成期的校验器面对的是模型输出，允许「先回喂可读原因、再确定性修正」，
# 以免模型在无法据以纠正的裸错误码上耗尽重试预算。

_GENERATION_FIX_HINTS = {
    "stage has no course": "每个阶段至少要有一门课程，不能留空阶段。",
    "route needs at least one course": "路线至少要有一门课程。",
    "too many courses in stage": f"每个阶段最多 {ROUTE_LIMITS['max_courses_per_stage']} 门课程，请合并或删除多余课程。",
    "too many courses": f"整条路线最多 {ROUTE_LIMITS['max_courses']} 门课程，请减少课程数量。",
    "too many lessons across courses": f"整条路线讲次总数不超过 {OUTLINE_LIMITS['max_lessons']}，请减少讲次。",
    "too many units": f"每个课程最多 {OUTLINE_LIMITS['max_units']} 个单元，请合并单元。",
    "unit has no lesson": "每个单元至少要有一讲，不能留空单元。",
    "too many lessons in unit": f"每个单元最多 {OUTLINE_LIMITS['max_lessons_per_unit']} 讲，请合并讲次。",
    "too many lessons": f"讲次总数不超过 {OUTLINE_LIMITS['max_lessons']}，请减少讲次。",
    "prerequisite index out of range": "prerequisites 的 from_index/to_index 只能引用已编号的节点，不能越界或自环。",
    "prerequisite graph has a cycle": "prerequisites 不能成环，请改为无环有向图。",
    "duplicate lesson titles": "同一课程内讲次标题不能重复，请改写重复标题。",
    "lesson goal is empty": "每一讲都要有 goal，不能留空。",
    "route structure invalid": "路线结构不符合约定：阶段内必须有课程，课程必须有标题。",
    "outline structure invalid": "大纲结构不符合约定：单元内必须有讲次，讲次必须有标题。",
}


def output_retry_reason(exc: PlanningError, notes=()) -> str:
    """生成期回喂文本：永远给出可读约束与修正方向，不回喂裸错误码。

    模型只能看到这段文本，所以必须点名约束（``exc.message``）并给出中文修正提示。
    """
    detail = (getattr(exc, "message", "") or exc.code).strip()
    hint = _GENERATION_FIX_HINTS.get(detail)
    parts = [f"{detail}：{hint}" if hint else detail]
    parts.extend(str(note) for note in notes if note)
    return "；".join(parts)


def _prune_prereq_edges(edges, node_total: int, key) -> tuple[list, bool]:
    """丢弃越界/自环/成环的先修边，返回 (保留的边, 是否发生丢弃)。"""
    kept = []
    for edge in edges:
        f, t = key(edge)
        if f == t or f < 0 or t < 0 or f >= node_total or t >= node_total:
            continue
        if _has_cycle([*kept, edge], node_total, key):
            continue
        kept.append(edge)
    return kept, len(kept) != len(edges)


def repair_route_candidate(candidate: RouteCandidate) -> list[str]:
    """生成期确定性修正：只放宽路线骨架里模型无法从提示词得知的约束。

    1. 空阶段（提示词要求「2-4 个阶段、总共 3-8 门课」，会邀请出留空阶段）：丢弃；
    2. 每阶段课程数超过 ``max_courses_per_stage``：截断到上限；
    3. 课程内嵌单元时，课程级先修边越界或成环：丢弃这些边；
    4. 路线级先修边（按课程展平编号）越界、自环或成环：丢弃这些边。

    提示词只说明「先修边引用课程编号、不能引用阶段编号、不能成环」，
    并未给出编号上界，模型无从得知越界边是非法的，故在生成期丢弃。

    其余约束（课程总数、讲次总量）仍由调用方按严格校验处理。
    返回已应用的修正说明；无需修正时为空列表。
    """
    notes: list[str] = []
    kept_stages = [stage for stage in candidate.stages if stage.courses]
    if len(kept_stages) != len(candidate.stages):
        candidate.stages = kept_stages
        notes.append("存在空阶段，已丢弃")
    limit = ROUTE_LIMITS["max_courses_per_stage"]
    for stage in candidate.stages:
        if len(stage.courses) > limit:
            del stage.courses[limit:]
            notes.append(f"阶段「{stage.title}」课程数超过 {limit}，已截断到 {limit} 门")
    for stage in candidate.stages:
        for course in stage.courses:
            if not course.units or not course.prerequisites:
                continue
            node_total = sum(len(unit.lessons) for unit in course.units)
            kept, dropped = _prune_prereq_edges(
                course.prerequisites, node_total, lambda e: (e.from_index, e.to_index)
            )
            if dropped:
                course.prerequisites = kept
                notes.append(f"课程「{course.title}」的先修边越界或成环，已丢弃无效边")
    if candidate.prerequisites:
        course_total = sum(len(stage.courses) for stage in candidate.stages)
        kept, dropped = _prune_prereq_edges(
            candidate.prerequisites, course_total, lambda e: (e.from_index, e.to_index)
        )
        if dropped:
            candidate.prerequisites = kept
            notes.append("路线级先修边越界、自环或成环，已丢弃无效边")
    return notes


def outline_generation_issue(candidate: OutlineCandidate, budget: int | None = None) -> str | None:
    """生成期可读原因：返回 None 表示当前输出可直接接受。"""
    if not candidate.units:
        return output_retry_reason(error("INVALID_OUTLINE_STRUCTURE", "unit has no lesson"))
    try:
        validate_outline_candidate(candidate)
    except PlanningError as exc:
        return output_retry_reason(exc)
    lessons = [lesson for unit in candidate.units for lesson in unit.lessons]
    limit = OUTLINE_LIMITS["max_lessons"] if budget is None else min(budget, OUTLINE_LIMITS["max_lessons"])
    if len(lessons) > limit:
        return output_retry_reason(error("INVALID_OUTLINE_STRUCTURE", "too many lessons"))
    if any(not lesson.goal.strip() for lesson in lessons):
        return output_retry_reason(error("INVALID_OUTLINE_STRUCTURE", "lesson goal is empty"))
    if len({lesson.title.strip().casefold() for lesson in lessons}) != len(lessons):
        return output_retry_reason(error("INVALID_OUTLINE_STRUCTURE", "duplicate lesson titles"))
    return None


def repair_outline_candidate(candidate: OutlineCandidate, budget: int | None = None) -> list[str]:
    """生成期确定性修正：把越界/超量/成环/重复的机械问题就地收敛。"""
    notes: list[str] = []
    kept_units = [unit for unit in candidate.units if unit.lessons]
    if len(kept_units) != len(candidate.units):
        if kept_units:
            candidate.units = kept_units
            notes.append("存在空单元，已丢弃")
    if len(candidate.units) > OUTLINE_LIMITS["max_units"]:
        del candidate.units[OUTLINE_LIMITS["max_units"]:]
        notes.append(f"单元数超过 {OUTLINE_LIMITS['max_units']}，已截断")
    limit = OUTLINE_LIMITS["max_lessons"] if budget is None else min(budget, OUTLINE_LIMITS["max_lessons"])
    total = 0
    seen: set[str] = set()
    for unit in candidate.units:
        if len(unit.lessons) > OUTLINE_LIMITS["max_lessons_per_unit"]:
            del unit.lessons[OUTLINE_LIMITS["max_lessons_per_unit"]:]
            notes.append(f"单元「{unit.title}」讲次数超过 {OUTLINE_LIMITS['max_lessons_per_unit']}，已截断")
        remaining = limit - total
        if len(unit.lessons) > remaining:
            del unit.lessons[remaining:]
            notes.append("讲次总量超过预算，已截断")
        for lesson in unit.lessons:
            if not lesson.goal.strip():
                lesson.goal = lesson.title
                notes.append(f"讲次「{lesson.title}」缺少 goal，已用标题补齐")
            key = lesson.title.strip().casefold()
            if key in seen:
                lesson.title = f"{lesson.title}（{len(seen) + 1}）"
                notes.append(f"讲次标题「{key}」重复，已改写")
                key = lesson.title.strip().casefold()
            seen.add(key)
        total += len(unit.lessons)
    if any(not unit.lessons for unit in candidate.units):
        candidate.units = [unit for unit in candidate.units if unit.lessons]
        notes.append("预算截断后存在空单元，已丢弃")
    node_total = sum(len(unit.lessons) for unit in candidate.units)
    kept, dropped = _prune_prereq_edges(
        candidate.prerequisites, node_total, lambda e: (e.from_index, e.to_index)
    )
    if dropped:
        candidate.prerequisites = kept
        notes.append("先修边越界或成环，已丢弃无效边")
    return notes


# ---- 生成前访谈（v2）：问题集由模型产出，服务端校验数量/类型/选项 ----

class IntakeOption(BaseModel):
    key: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=60)
    detail: str = Field(default="", max_length=120)


class IntakeQuestion(BaseModel):
    question_id: str = Field(min_length=1, max_length=40)
    question: str = Field(min_length=1, max_length=200)
    kind: str  # single | multi
    options: list[IntakeOption] = Field(min_length=2, max_length=8)
    skippable: bool = True
    purpose: Literal['detail_preference','prior_experience','mastery','goals','constraints','difficulties','learning_habits']
    topic_key: str = Field(min_length=1,max_length=80)
    information_gap: str = Field(min_length=1,max_length=240)
    planning_effect: str = Field(min_length=1,max_length=240)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in ("single", "multi"):
            raise ValueError("kind must be single|multi")
        return v


class IntakeQuestionSet(BaseModel):
    questions: list[IntakeQuestion] = Field(max_length=4)
    ready_to_plan: bool
    planning_scope: Literal['course','route']
    learner_profile: 'LearnerProfile'
    assumptions: list[str] = Field(default_factory=list,max_length=8)


class PriorExperience(BaseModel):
    domain: str = Field(min_length=1,max_length=120)
    level: Literal['unknown','exposure','guided','independent','project'] = Field(default='unknown', description='非 unknown 必须有 evidence_question_ids；无法确认依据时使用 unknown。')
    experience: str = Field(default='',max_length=500)
    familiar_concepts: list[str] = Field(default_factory=list,max_length=12)
    uncertain_concepts: list[str] = Field(default_factory=list,max_length=12)
    evidence_question_ids: list[str] = Field(default_factory=list,max_length=6,
        description='仅引用 original_request 或已回答且非跳过的问题 ID；非 unknown 掌握程度必须非空，不得补造依据。')


class KnowledgeTransfer(BaseModel):
    source: str = Field(min_length=1,max_length=160)
    target: str = Field(min_length=1,max_length=160)
    boundary: str = Field(min_length=1,max_length=400)
    evidence_question_ids: list[str] = Field(default_factory=list,max_length=6,
        description='必须非空，仅引用 original_request 或非跳过的已答问题 ID；无依据时不要生成该迁移条目。')


class LearnerProfile(BaseModel):
    learning_goal: str = Field(default='',max_length=2000)
    detail_preference: str = Field(default='',max_length=400)
    detail_status: Literal['confirmed','assumed','unknown'] = 'unknown'
    prior_experience: list[PriorExperience] = Field(default_factory=list,max_length=8)
    learning_constraints: list[str] = Field(default_factory=list,max_length=8)
    learning_difficulties: list[str] = Field(default_factory=list,max_length=8)
    knowledge_transfers: list[KnowledgeTransfer] = Field(default_factory=list,max_length=8)


IntakeQuestionSet.model_rebuild()


def validate_intake_question_set(s: IntakeQuestionSet) -> dict:
    if not s.questions:
        return {"questions": []}
    ids = [q.question_id for q in s.questions]
    if len(set(ids)) != len(ids):
        raise error("OUTPUT_INVALID", "duplicate intake question_id")
    return s.model_dump()


def validate_coverage_plan(plan: CoveragePlan) -> dict:
    """覆盖规划的可读约束：区域唯一、查询非空、时间预算自洽。"""
    names = [area.area.strip() for area in plan.coverage_areas]
    if len(set(names)) != len(names):
        raise error("OUTPUT_INVALID", "coverage areas must be unique")
    if any(not area.search_query.strip() for area in plan.coverage_areas):
        raise error("OUTPUT_INVALID", "coverage area search_query required")
    budget = plan.scope_contract.time_budget
    if budget is not None:
        expected = budget.weeks * budget.hours_per_week
        if abs(budget.total_hours - expected) > 0.5:
            budget.total_hours = round(expected, 2)
    return {
        "in_scope_count": len(plan.scope_contract.in_scope),
        "coverage_area_count": len(plan.coverage_areas),
        "bridge_required_count": len(plan.prerequisite_policy.bridge_required),
        "total_hours": plan.scope_contract.time_budget.total_hours if plan.scope_contract.time_budget else None,
    }


def validate_quality_review(review: QualityReview) -> dict:
    """评审结果只决定是否要求用户确认；关键问题不可静默忽略。"""
    critical = [issue for issue in review.issues if issue.severity == "critical"]
    if critical or review.verdict == "blocked":
        review.acknowledgement_required = True
    return {
        "verdict": review.verdict,
        "critical_count": len(critical),
        "warning_count": len(review.issues) - len(critical),
        "acknowledgement_required": review.acknowledgement_required,
    }


def validate_content_json(kind: str, content: dict) -> dict:
    """草稿/候选 content_json 的入口校验：按 kind 解析并二次校验。

    Pydantic 校验失败统一映射为受控错误码（不透出原始 ValidationError）。
    """
    from pydantic import ValidationError

    if kind in ("route", "route_generate"):
        strip_internal_ids_in_place(content)
        try:
            candidate = RouteCandidate.model_validate(content)
        except ValidationError:
            raise error("ROUTE_STRUCTURE_INVALID", "route structure invalid")
        return validate_route_candidate(candidate)
    if kind in ("course_outline", "outline_generate"):
        strip_internal_ids_in_place(content)
        try:
            candidate = OutlineCandidate.model_validate(content)
        except ValidationError:
            raise error("INVALID_OUTLINE_STRUCTURE", "outline structure invalid")
        return validate_outline_candidate(candidate)
    raise error("invalid_field", f"unknown content kind {kind}")


__all__ = [
    "IntakeOption", "IntakeQuestion", "IntakeQuestionSet", "validate_intake_question_set",
    "TimeBudget", "ScopeContract", "CoverageArea", "PrerequisitePolicy", "CoveragePlan",
    "QualityIssue", "QualityDimensions", "QualityReview", "CoverageMapping",
    "validate_coverage_plan", "validate_quality_review",
    "RouteCandidate", "OutlineCandidate", "ChatAnswer",
    "RouteStageOut", "RouteCourseOut", "OutlineUnitOut", "OutlineLessonOut",
    "CONTEXT_BUDGET_DEFAULTS", "TRUNCATION_PRIORITY", "SCENE_ELEMENT_WHITELIST",
    "BOARD_ELEMENT_WHITELIST", "ROUTE_LIMITS", "OUTLINE_LIMITS", "CHAT_LIMITS",
    "PROMPTED_PARSE_RETRIES",
    "validate_route_candidate", "validate_outline_candidate", "validate_content_json",
    "strip_internal_ids_in_place", "internal_id_residue",
    "flat_course_titles", "flat_lesson_titles",
    "output_retry_reason", "repair_route_candidate", "outline_generation_issue",
    "repair_outline_candidate",
]
