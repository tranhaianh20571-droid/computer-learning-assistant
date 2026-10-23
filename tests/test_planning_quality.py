"""规划质量门：时间预算、前置闭包、覆盖绑定与评审确认。"""

from __future__ import annotations

import pytest

from backend.planning.errors import PlanningError
from backend.planning.outputs import (
    CoverageArea,
    CoveragePlan,
    OutlineLessonOut,
    PrerequisitePolicy,
    QualityDimensions,
    QualityIssue,
    QualityReview,
    RouteCandidate,
    ScopeContract,
    TimeBudget,
    validate_coverage_plan,
)
from backend.planning.quality import (
    bind_coverage,
    check_prerequisite_closure,
    final_gate,
    normalize_time_budget,
    split_core_extension,
)


def _plan(*, areas=None, bridge=None, status="ready") -> CoveragePlan:
    return CoveragePlan(
        scope_contract=ScopeContract(
            target_outcome="能独立完成可上线的网页应用",
            in_scope=["环境配置", "HTML", "CSS", "JavaScript", "部署"],
            out_of_scope=["后端服务"],
            assumptions=["无编程基础"],
            time_budget=TimeBudget(weeks=8, hours_per_week=6, total_hours=48),
        ),
        coverage_areas=areas or [
            CoverageArea(area="环境配置", depth="基础", search_query="前端环境配置"),
            CoverageArea(area="HTML", depth="基础", search_query="HTML 语义化"),
            CoverageArea(area="CSS", depth="核心", search_query="CSS 布局"),
            CoverageArea(area="JavaScript", depth="核心", search_query="JavaScript 入门"),
            CoverageArea(area="部署", depth="核心", search_query="前端部署上线"),
        ],
        prerequisite_policy=PrerequisitePolicy(
            assumed_general=["电脑操作"], bridge_required=bridge or ["终端", "开发者工具"],
        ),
        status=status,
    )


def _route(*, lessons=None, coverage_titles=None, course_title="开发环境与 HTML") -> dict:
    lessons = lessons or [
        {"title": "终端与编辑器配置", "goal": "能配置环境", "estimated_minutes": 50, "path_role": "core"},
        {"title": "HTML 语义化", "goal": "能写语义化页面", "estimated_minutes": 50, "path_role": "core"},
        {"title": "CSS 布局", "goal": "能完成响应式排版", "estimated_minutes": 50, "path_role": "core"},
        {"title": "JavaScript 交互", "goal": "能实现页面交互", "estimated_minutes": 50, "path_role": "core"},
        {"title": "部署上线", "goal": "能发布到公网", "estimated_minutes": 50, "path_role": "core"},
    ]
    coverage_titles = coverage_titles or ["环境配置", "HTML", "CSS", "JavaScript", "部署"]
    return {
        "title": "前端开发基础路线",
        "stages": [{"title": "起步", "courses": [{
            "title": course_title, "goal": "能完成网页开发",
            "units": [{"title": "基础", "lessons": lessons}], "prerequisites": [],
        }]}],
        "prerequisites": [],
        "coverage_map": [{"area": area, "course_titles": [course_title]} for area in coverage_titles],
        "assumptions": ["无编程基础"],
    }


def test_normalize_time_budget_defaults() -> None:
    budget = normalize_time_budget({})
    assert budget.weeks == 8
    assert budget.hours_per_week == 6
    assert budget.total_hours == 48


def test_core_over_budget_is_critical() -> None:
    lessons = [
        {"title": "A", "goal": "能", "estimated_minutes": 240, "path_role": "core"},
        {"title": "B", "goal": "能", "estimated_minutes": 240, "path_role": "core"},
    ]
    result = final_gate(_route(lessons=lessons), _plan(), TimeBudget(weeks=1, hours_per_week=4, total_hours=4), {})
    assert result.review.verdict == "blocked"
    assert result.review.acknowledgement_required is True
    assert result.review.dimensions.actionability == "fail"
    assert any(issue.dimension == "actionability" for issue in result.issues)


def test_explicit_extension_is_preserved() -> None:
    lessons = [
        {"title": "A", "goal": "能", "estimated_minutes": 60, "path_role": "core"},
        {"title": "B", "goal": "能", "estimated_minutes": 60, "path_role": "core"},
        {"title": "C", "goal": "能", "estimated_minutes": 60, "path_role": "extension"},
        {"title": "D", "goal": "能", "estimated_minutes": 60, "path_role": "extension"},
    ]
    summary = split_core_extension(_route(lessons=lessons), TimeBudget(weeks=1, hours_per_week=4, total_hours=4), "none")
    assert summary["declared_core_hours"] == 2
    assert summary["core_hours"] == 2
    assert summary["extension_hours"] == 2
    assert summary["core_within_budget"] is True


def test_coverage_gap_is_critical() -> None:
    route = _route(coverage_titles=["环境配置", "HTML", "CSS"])
    coverage, issues = bind_coverage(route, _plan())
    assert coverage["coverage_ratio"] < 1
    assert {"JavaScript", "部署"}.issubset(set(coverage["gaps"]))
    assert any(issue.severity == "critical" for issue in issues)


def test_missing_bridge_is_critical() -> None:
    route = _route()
    route["stages"][0]["courses"][0]["units"][0]["lessons"][0]["title"] = "HTML 入门"
    issues = check_prerequisite_closure(route, _plan(bridge=["JSON"]), {})
    assert any(issue.dimension == "prerequisites" and issue.severity == "critical" for issue in issues)


def test_fallback_plan_does_not_require_acknowledgement() -> None:
    route = _route()
    result = final_gate(route, _plan(status="fallback"), TimeBudget(weeks=8, hours_per_week=6, total_hours=48), {})
    assert result.review.verdict == "needs_repair"
    assert result.review.acknowledgement_required is False
    assert any(issue.dimension == "coverage" for issue in result.issues)


def test_semantic_critical_requires_acknowledgement() -> None:
    review = QualityReview(
        verdict="blocked",
        dimensions=QualityDimensions(coverage="fail"),
        issues=[QualityIssue(dimension="coverage", severity="critical", message="缺少部署模块")],
    )
    result = final_gate(
        _route(), _plan(), TimeBudget(weeks=8, hours_per_week=6, total_hours=48), {},
        semantic_review=review,
    )
    assert result.review.acknowledgement_required is True
    assert result.review.verdict == "blocked"


def test_coverage_plan_rejects_duplicate_areas() -> None:
    plan = _plan(areas=[
        CoverageArea(area="HTML", search_query="HTML"),
        CoverageArea(area="HTML", search_query="HTML2"),
    ])
    with pytest.raises(PlanningError) as exc:
        validate_coverage_plan(plan)
    assert exc.value.code == "OUTPUT_INVALID"


def test_lesson_defaults_for_legacy_payloads() -> None:
    lesson = OutlineLessonOut(title="旧讲次", goal="能")
    assert lesson.estimated_minutes == 45
    assert lesson.path_role == "core"
    course = RouteCandidate(title="路线", stages=[])
    assert course.coverage_map == []
    assert course.assumptions == []
