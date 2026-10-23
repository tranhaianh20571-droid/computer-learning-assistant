"""确定性规划质量门：时间预算、前置闭包、覆盖绑定、具体性/去重。

这一层不调用模型，只消费已校验的结构化路线与覆盖规划。LLM 评审是补充，
不能替代这里的机械检查；任何关键缺口都通过受控错误码或 review 结果暴露。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .errors import error
from .outputs import (
    CoverageArea,
    CoveragePlan,
    QualityDimensions,
    QualityIssue,
    QualityReview,
    ScopeContract,
    TimeBudget,
)

DEFAULT_WEEKS = 8
DEFAULT_HOURS_PER_WEEK = 6.0
LESSON_MINUTES_BY_LEVEL = {
    "none": 50,
    "unknown": 50,
    "exposure": 45,
    "guided": 40,
    "independent": 35,
    "project": 30,
}
GENERIC_TITLES = {"进阶", "综合", "其他", "杂项", "实战", "练习", "入门"}
ENV_KEYWORDS = ("环境", "终端", "命令行", "编辑器", "VS Code", "安装", "开发工具", "开发者工具")
BRIDGE_KEYWORDS = {
    "json": ("JSON", "数据格式", "数据解析", "接口数据"),
    "终端": ("终端", "命令行", "Terminal", "目录导航", "cd "),
    "命令行": ("终端", "命令行", "Terminal", "目录导航", "cd "),
    "开发者工具": ("开发者工具", "DevTools", "控制台", "Console", "调试"),
    "devtools": ("开发者工具", "DevTools", "控制台", "Console", "调试"),
    "git": ("Git", "版本控制", "版本管理", "GitHub"),
    "版本": ("Git", "版本控制", "版本管理"),
}


def normalize_time_budget(constraints: Optional[dict], profile: Optional[dict] = None) -> TimeBudget:
    """从任务约束/画像得到结构化时间预算；缺失时使用显式默认值。"""
    constraints = constraints or {}
    raw = constraints.get("time_budget") if isinstance(constraints.get("time_budget"), dict) else constraints
    weeks = raw.get("weeks") if isinstance(raw, dict) else None
    hours = raw.get("hours_per_week") if isinstance(raw, dict) else None
    try:
        weeks = int(weeks) if weeks is not None else DEFAULT_WEEKS
    except (TypeError, ValueError):
        weeks = DEFAULT_WEEKS
    try:
        hours = float(hours) if hours is not None else DEFAULT_HOURS_PER_WEEK
    except (TypeError, ValueError):
        hours = DEFAULT_HOURS_PER_WEEK
    weeks = max(1, min(104, weeks))
    hours = max(0.5, min(80.0, hours))
    return TimeBudget(weeks=weeks, hours_per_week=hours, total_hours=round(weeks * hours, 2))


def profile_level(profile: Optional[dict]) -> str:
    """取画像中最高的已有经验等级；没有记录按 0 基础处理。"""
    if not profile:
        return "none"
    entries = profile.get("prior_experience") or []
    order = {"unknown": 0, "exposure": 1, "guided": 2, "independent": 3, "project": 4}
    levels = [entry.get("level", "unknown") for entry in entries if isinstance(entry, dict)]
    return max(levels, key=lambda level: order.get(level, 0), default="none")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lesson_nodes(route: dict) -> list[dict]:
    nodes = []
    for stage_index, stage in enumerate(route.get("stages") or []):
        for course_index, course in enumerate(stage.get("courses") or []):
            for unit_index, unit in enumerate(course.get("units") or []):
                for lesson_index, lesson in enumerate(unit.get("lessons") or []):
                    nodes.append({
                        "path": f"stage_{stage_index}.course_{course_index}.unit_{unit_index}.lesson_{lesson_index}",
                        "stage_index": stage_index,
                        "course_index": course_index,
                        "course": course,
                        "unit": unit,
                        "lesson": lesson,
                    })
    return nodes


def _course_nodes(route: dict) -> list[dict]:
    nodes = []
    for stage_index, stage in enumerate(route.get("stages") or []):
        for course_index, course in enumerate(stage.get("courses") or []):
            nodes.append({
                "path": f"stage_{stage_index}.course_{course_index}",
                "stage_index": stage_index,
                "course_index": course_index,
                "course": course,
            })
    return nodes


def _course_text(course: dict) -> str:
    parts = [_text(course.get("title")), _text(course.get("goal"))]
    for unit in course.get("units") or []:
        parts.append(_text(unit.get("title")))
        for lesson in unit.get("lessons") or []:
            parts.extend([_text(lesson.get("title")), _text(lesson.get("goal"))])
    return " ".join(parts)


def _issue(dimension: str, message: str, *, severity: str = "warning", node_path: str = "") -> QualityIssue:
    return QualityIssue(dimension=dimension, severity=severity, message=message, node_path=node_path)


def split_core_extension(route: dict, budget: TimeBudget, level: str) -> dict:
    """按计划顺序把讲次分为核心/扩展；核心优先填满预算的 90%。"""
    nodes = _lesson_nodes(route)
    total_minutes = 0
    declared_core_minutes = 0
    core_minutes = 0
    extension_minutes = 0
    core_capacity = budget.total_hours * 60 * 0.9
    for node in nodes:
        lesson = node["lesson"]
        minutes = int(lesson.get("estimated_minutes") or LESSON_MINUTES_BY_LEVEL.get(level, 45))
        total_minutes += minutes
        if lesson.get("path_role") == "extension":
            extension_minutes += minutes
            continue
        declared_core_minutes += minutes
        if core_minutes + minutes <= core_capacity:
            core_minutes += minutes
        else:
            extension_minutes += minutes
    return {
        "total_hours": round(total_minutes / 60, 2),
        "declared_core_hours": round(declared_core_minutes / 60, 2),
        "core_hours": round(core_minutes / 60, 2),
        "extension_hours": round(extension_minutes / 60, 2),
        "budget_hours": budget.total_hours,
        "core_within_budget": declared_core_minutes <= budget.total_hours * 60,
        "total_within_budget": total_minutes <= budget.total_hours * 60,
    }


def check_time_budget(route: dict, budget: TimeBudget, level: str) -> tuple[dict, list[QualityIssue]]:
    summary = split_core_extension(route, budget, level)
    issues: list[QualityIssue] = []
    if not summary["core_within_budget"]:
        issues.append(_issue(
            "actionability",
            f"核心路径约 {summary['declared_core_hours']} 小时，超过可用 {budget.total_hours} 小时；"
            "请减少核心讲次、把超出的讲次标为 extension，或调整时间预算。",
            severity="critical",
        ))
    elif not summary["total_within_budget"]:
        issues.append(_issue(
            "actionability",
            f"完整路径约 {summary['total_hours']} 小时，超过可用 {budget.total_hours} 小时；超出部分已标记为扩展路径。",
            severity="warning",
        ))
    return summary, issues


def _bridge_keywords(item: str) -> tuple[str, ...]:
    lowered = item.casefold()
    for key, keywords in BRIDGE_KEYWORDS.items():
        if key in lowered:
            return keywords
    chunks = [chunk for chunk in re.split(r"[、，,；;/\s]+", item) if len(chunk) >= 2]
    return tuple(chunks[:4]) or (item,)


def check_prerequisite_closure(route: dict, plan: CoveragePlan, profile: dict) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    if plan.status == "fallback":
        return [_issue(
            "prerequisites",
            "覆盖规划不可用，未做前置闭包核验；请人工核对起步内容。",
            severity="warning",
        )]
    level = profile_level(profile)
    courses = _course_nodes(route)
    if not courses:
        return [_issue("prerequisites", "路线没有课程，无法检查前置。", severity="critical")]

    if level in ("none", "unknown"):
        first_text = _course_text(courses[0]["course"])
        if not any(keyword.casefold() in first_text.casefold() for keyword in ENV_KEYWORDS):
            issues.append(_issue(
                "prerequisites",
                "0 基础学习者的第一门课没有出现环境/终端/编辑器/开发工具等起步内容；需要前置桥接。",
                severity="critical",
                node_path=courses[0]["path"],
            ))

    all_text = " ".join(_course_text(node["course"]) for node in courses).casefold()
    for item in plan.prerequisite_policy.bridge_required:
        if not any(keyword.casefold() in all_text for keyword in _bridge_keywords(item)):
            issues.append(_issue(
                "prerequisites",
                f"前置策略要求桥接「{item}」，但计划中没有可识别的对应讲次。",
                severity="critical",
            ))

    for edge in route.get("prerequisites") or []:
        if not isinstance(edge, dict):
            continue
        source, target = edge.get("from_index"), edge.get("to_index")
        if isinstance(source, int) and isinstance(target, int) and source >= target:
            issues.append(_issue(
                "sequencing",
                "路线先修边指向了更靠后的课程，学习顺序可能倒置。",
                severity="warning",
            ))
            break
    return issues


def bind_coverage(route: dict, plan: CoveragePlan) -> tuple[dict, list[QualityIssue]]:
    """把覆盖区域绑定到具体课程路径；模型自报的标题必须真实存在。"""
    if plan.status == "fallback":
        return {
            "areas": [],
            "coverage_ratio": None,
            "gaps": [],
            "required_total": 0,
            "required_covered": 0,
            "fallback": True,
        }, [_issue(
            "coverage",
            "覆盖规划不可用，未做区域覆盖核验；本次只检查结构与时间预算。",
            severity="warning",
        )]
    course_by_title = {_text(node["course"].get("title")): node for node in _course_nodes(route)}
    mapping: dict[str, list[str]] = {}
    for item in route.get("coverage_map") or []:
        if isinstance(item, dict):
            area = _text(item.get("area"))
            titles = item.get("course_titles") or []
        else:
            area = _text(getattr(item, "area", ""))
            titles = getattr(item, "course_titles", []) or []
        if area:
            mapping[area] = [_text(title) for title in titles if _text(title)]

    areas = []
    gaps = []
    required_total = 0
    required_covered = 0
    for area in plan.coverage_areas:
        titles = mapping.get(area.area, [])
        paths = [course_by_title[title]["path"] for title in titles if title in course_by_title]
        if not paths:
            # 没有声明映射时，用区域名与课程标题的直接包含关系做保守回退。
            paths = [
                node["path"] for title, node in course_by_title.items()
                if area.area in title or title in area.area
            ]
        status = "covered" if paths else "gap"
        if area.required:
            required_total += 1
            required_covered += 1 if paths else 0
        if not paths:
            gaps.append(area.area)
        areas.append({
            "area": area.area,
            "depth": area.depth,
            "required": area.required,
            "status": status,
            "node_paths": paths,
        })
    ratio = round(required_covered / required_total, 3) if required_total else 1.0
    issues: list[QualityIssue] = []
    if gaps:
        issues.append(_issue(
            "coverage",
            "以下必需覆盖区域没有绑定到任何课程：" + "；".join(gaps[:8]),
            severity="critical",
        ))
    return {
        "areas": areas,
        "coverage_ratio": ratio,
        "gaps": gaps,
        "required_total": required_total,
        "required_covered": required_covered,
    }, issues


def check_specificity(route: dict) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    titles = []
    for stage in route.get("stages") or []:
        for course in stage.get("courses") or []:
            titles.append(_text(course.get("title")))
            for unit in course.get("units") or []:
                titles.append(_text(unit.get("title")))
                for lesson in unit.get("lessons") or []:
                    titles.append(_text(lesson.get("title")))
    generic = [title for title in titles if title in GENERIC_TITLES or len(title) <= 2]
    if generic:
        issues.append(_issue(
            "specificity",
            "存在空泛或过短的标题：" + "；".join(generic[:8]),
            severity="warning",
        ))
    for course in [c for stage in route.get("stages") or [] for c in stage.get("courses") or []]:
        lessons = [
            _text(lesson.get("title")).casefold()
            for unit in course.get("units") or [] for lesson in unit.get("lessons") or []
        ]
        if len(set(lessons)) != len(lessons):
            issues.append(_issue(
                "specificity",
                f"课程「{_text(course.get('title'))}」存在重复讲次标题。",
                severity="warning",
            ))
    return issues


def _dimension(issues: list[QualityIssue], dimension: str) -> str:
    relevant = [issue for issue in issues if issue.dimension == dimension]
    if any(issue.severity == "critical" for issue in relevant):
        return "fail"
    return "partial" if relevant else "pass"


@dataclass
class QualityGateResult:
    review: QualityReview
    coverage: dict
    time: dict
    issues: list[QualityIssue] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "review": self.review.model_dump(),
            "coverage": self.coverage,
            "time": self.time,
        }


def final_gate(
    route: dict,
    plan: CoveragePlan,
    budget: TimeBudget,
    profile: dict,
    *,
    semantic_review: Optional[QualityReview] = None,
) -> QualityGateResult:
    """确定性门 + 可选语义评审；关键问题要求用户确认，不静默通过。"""
    coverage, coverage_issues = bind_coverage(route, plan)
    time_summary, time_issues = check_time_budget(route, budget, profile_level(profile))
    issues = coverage_issues + time_issues
    issues.extend(check_prerequisite_closure(route, plan, profile))
    issues.extend(check_specificity(route))
    if semantic_review is not None:
        issues.extend(semantic_review.issues)
        if semantic_review.verdict == "blocked":
            issues.append(_issue("coverage", "独立评审判定规划不可用。", severity="critical"))

    dimensions = QualityDimensions(
        coverage=_dimension(issues, "coverage"),
        prerequisites=_dimension(issues, "prerequisites"),
        sequencing=_dimension(issues, "sequencing"),
        actionability=_dimension(issues, "actionability"),
        specificity=_dimension(issues, "specificity"),
        transparency=_dimension(issues, "transparency"),
    )
    if any(issue.severity == "critical" for issue in issues):
        verdict = "blocked"
    elif issues:
        verdict = "needs_repair"
    else:
        verdict = "pass"
    review = QualityReview(
        verdict=verdict,
        dimensions=dimensions,
        issues=issues[:30],
        repairs=(semantic_review.repairs if semantic_review else [])[:20],
        acknowledgement_required=verdict == "blocked" or any(
            issue.severity == "critical" for issue in issues
        ),
    )
    return QualityGateResult(review=review, coverage=coverage, time=time_summary, issues=issues)


def default_coverage_plan(topic: str, budget: TimeBudget) -> CoveragePlan:
    """覆盖规划不可用时的保守回退：只声明主题，不伪造覆盖区域。"""
    topic = _text(topic) or "学习主题"
    return CoveragePlan(
        scope_contract=ScopeContract(
            target_outcome=f"围绕「{topic}」形成可执行的学习路径",
            in_scope=[topic],
            out_of_scope=[],
            assumptions=["覆盖规划模型输出不可用，按主题生成并保留人工核对。"],
            time_budget=budget,
        ),
        coverage_areas=[CoverageArea(area=topic, depth="核心", search_query=topic[:80], required=True)],
        status="fallback",
    )


__all__ = [
    "QualityGateResult", "normalize_time_budget", "profile_level", "split_core_extension",
    "check_time_budget", "check_prerequisite_closure", "bind_coverage", "check_specificity",
    "final_gate", "default_coverage_plan",
]
