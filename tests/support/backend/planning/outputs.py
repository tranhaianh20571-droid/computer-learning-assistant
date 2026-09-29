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

from pydantic import BaseModel, Field, field_validator

from .errors import error

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


class RouteCourseOut(BaseModel):
    title: str = Field(min_length=1, max_length=ROUTE_LIMITS["max_text_len"])
    goal: str = Field(default="", max_length=ROUTE_LIMITS["max_text_len"])

    @field_validator("title", "goal")
    @classmethod
    def _no_control(cls, v: str) -> str:
        return "".join(ch for ch in v if ch >= " " or ch == "\t")


class RouteStageOut(BaseModel):
    title: str = Field(min_length=1, max_length=ROUTE_LIMITS["max_text_len"])
    courses: list[RouteCourseOut] = Field(min_length=1)


class RoutePrereqOut(BaseModel):
    from_index: int = Field(ge=0)
    to_index: int = Field(ge=0)


class RouteCandidate(BaseModel):
    stages: list[RouteStageOut] = Field(min_length=1, max_length=ROUTE_LIMITS["max_stages"])
    prerequisites: list[RoutePrereqOut] = Field(default_factory=list)


class OutlineLessonOut(BaseModel):
    title: str = Field(min_length=1, max_length=OUTLINE_LIMITS["max_text_len"])
    goal: str = Field(default="", max_length=OUTLINE_LIMITS["max_text_len"])

    @field_validator("title", "goal")
    @classmethod
    def _no_control(cls, v: str) -> str:
        return "".join(ch for ch in v if ch >= " " or ch == "\t")


class OutlineUnitOut(BaseModel):
    title: str = Field(min_length=1, max_length=OUTLINE_LIMITS["max_text_len"])
    lessons: list[OutlineLessonOut] = Field(min_length=1)


class OutlinePrereqOut(BaseModel):
    from_index: int = Field(ge=0)
    to_index: int = Field(ge=0)


class OutlineCandidate(BaseModel):
    units: list[OutlineUnitOut] = Field(min_length=1, max_length=OUTLINE_LIMITS["max_units"])
    prerequisites: list[OutlinePrereqOut] = Field(default_factory=list)


class ChatAnswer(BaseModel):
    answer: str = Field(min_length=1, max_length=CHAT_LIMITS["max_answer_len"])


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
    """就地剥掉 content_json 标题/目标里的内部 id（route/outline 两种形状）。"""
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
    for unit in content.get("units") or []:
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


def validate_route_candidate(candidate: RouteCandidate) -> dict:
    """长度/数量/结构/DAG 校验。返回规范化摘要；失败抛受控错误码。"""
    total = 0
    for stage in candidate.stages:
        stage.title = _clean_title_text(stage.title)
        for course in stage.courses:
            course.title = _clean_title_text(course.title)
            if course.goal:
                course.goal = _clean_title_text(course.goal)
        if not stage.courses:
            raise error("ROUTE_STRUCTURE_INVALID", "stage has no course")
        total += len(stage.courses)
    if total < 1:
        raise error("ROUTE_STRUCTURE_INVALID", "route needs at least one course")
    if total > ROUTE_LIMITS["max_courses"]:
        raise error("ROUTE_STRUCTURE_INVALID", "too many courses")
    _validate_prereq_indices(
        candidate.prerequisites,
        total,
        lambda e: (e.from_index, e.to_index),
    )
    return {"stage_count": len(candidate.stages), "course_count": total}


def validate_outline_candidate(candidate: OutlineCandidate) -> dict:
    total = 0
    for unit in candidate.units:
        unit.title = _clean_title_text(unit.title)
        for lesson in unit.lessons:
            lesson.title = _clean_title_text(lesson.title)
            if lesson.goal:
                lesson.goal = _clean_title_text(lesson.goal)
        if not unit.lessons:
            raise error("INVALID_OUTLINE_STRUCTURE", "unit has no lesson")
        total += len(unit.lessons)
    if total > OUTLINE_LIMITS["max_lessons"]:
        raise error("INVALID_OUTLINE_STRUCTURE", "too many lessons")
    _validate_prereq_indices(
        candidate.prerequisites, total, lambda e: (e.from_index, e.to_index)
    )
    return {"unit_count": len(candidate.units), "lesson_count": total}


def _validate_prereq_indices(edges, node_total: int, key) -> None:
    for edge in edges:
        f, t = key(edge)
        if f == t or f < 0 or t < 0 or f >= node_total or t >= node_total:
            raise error("INVALID_PREREQUISITE", "prerequisite index out of range")
    # DAG 无环：拓扑排序（Kahn）
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
    if visited != node_total:
        raise error("DAG_CYCLE", "prerequisite graph has a cycle")


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

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in ("single", "multi"):
            raise ValueError("kind must be single|multi")
        return v


class IntakeQuestionSet(BaseModel):
    questions: list[IntakeQuestion] = Field(max_length=3)


def validate_intake_question_set(s: IntakeQuestionSet) -> dict:
    if not s.questions:
        return {"questions": []}
    ids = [q.question_id for q in s.questions]
    if len(set(ids)) != len(ids):
        raise error("OUTPUT_INVALID", "duplicate intake question_id")
    return s.model_dump()


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
    
    "RouteCandidate", "OutlineCandidate", "ChatAnswer",
    "RouteStageOut", "RouteCourseOut", "OutlineUnitOut", "OutlineLessonOut",
    "CONTEXT_BUDGET_DEFAULTS", "TRUNCATION_PRIORITY", "SCENE_ELEMENT_WHITELIST",
    "BOARD_ELEMENT_WHITELIST", "ROUTE_LIMITS", "OUTLINE_LIMITS", "CHAT_LIMITS",
    "PROMPTED_PARSE_RETRIES",
    "validate_route_candidate", "validate_outline_candidate", "validate_content_json",
    "strip_internal_ids_in_place", "internal_id_residue",
    "flat_course_titles", "flat_lesson_titles",
]
