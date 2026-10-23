"""生成期降级契约：回喂可读约束原因 + 确定性修正，在既有重试预算内收敛。

背景：提交 7c052ab 把 ``validate_route_candidate`` 接成 Agent 的 ``output_validator``，
失败时 ``raise ModelRetry(exc.code)`` 只回喂裸错误码，同时新增了提示词从未告知模型的
两条硬约束（每阶段课程数上限、课程内嵌单元时的课程级先修索引）。模型无法据以纠正，
连续 3 次结构不合规后由 SDK 抛 ``UnexpectedModelBehavior`` → ``OUTPUT_INVALID``。

本文件锁定修复后的契约：
- 落库入口（``validate_content_json`` / ``validate_route_candidate``）**仍然严格**；
- 生成期改为「先回喂可读原因，再确定性修正」，且不提高 ``OUTPUT_RETRIES``；
- 容器下限（空阶段 / 空单元 / 空根列表）**不写在字段上**：字段级 metadata 会在 SDK
  反序列化阶段抛 ``ValidationError``，``output_validator`` 根本不被调用，模型只会拿到
  裸英文错误并耗尽重试预算。这些约束改由业务层抛可读原因，再由确定性修正收敛。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from backend.agents.contracts import AgentDeps, GenerationInput
from backend.agents.limits import failure_reason
from backend.agents.runner import OUTPUT_RETRIES, execute_agent
from backend.planning.errors import PlanningError, error
from backend.planning.outputs import (
    OUTLINE_LIMITS,
    ROUTE_LIMITS,
    OutlineCandidate,
    RouteCandidate,
    outline_generation_issue,
    output_retry_reason,
    repair_outline_candidate,
    repair_route_candidate,
    validate_content_json,
    validate_outline_candidate,
    validate_route_candidate,
)

PROFILE = {"supports_json_schema_output": True, "supports_tools": True, "supports_text_output": True}
INPUT = GenerationInput.from_messages([{"role": "user", "content": "synthetic generation"}])


def route_payload(courses_per_stage: int) -> dict:
    return {"stages": [{"title": "阶段一", "courses": [
        {"title": f"课程 {i}"} for i in range(courses_per_stage)
    ]}]}


# ---- 回喂文本：永远是可读原因，绝不是裸错误码 ----


def test_retry_reason_names_constraint_and_gives_chinese_hint():
    reason = output_retry_reason(error("ROUTE_STRUCTURE_INVALID", "too many courses in stage"))
    assert "too many courses in stage" in reason
    assert str(ROUTE_LIMITS["max_courses_per_stage"]) in reason
    assert reason != "ROUTE_STRUCTURE_INVALID"


def test_retry_reason_carries_repair_notes():
    reason = output_retry_reason(
        error("INVALID_PREREQUISITE", "prerequisite index out of range"),
        ["课程「数据库基础」的先修边越界或成环，已丢弃无效边"],
    )
    assert "prerequisite index out of range" in reason
    assert "已丢弃无效边" in reason


def test_retry_reason_falls_back_to_code_for_unknown_message():
    assert output_retry_reason(error("ROUTE_STRUCTURE_INVALID")) == "ROUTE_STRUCTURE_INVALID"


# ---- 确定性修正：只放宽提示词未告知的两条约束 ----


def test_repair_truncates_courses_per_stage_and_then_validates():
    candidate = RouteCandidate.model_validate(route_payload(9))
    with pytest.raises(PlanningError) as exc:
        validate_route_candidate(candidate)
    assert exc.value.code == "ROUTE_STRUCTURE_INVALID"

    notes = repair_route_candidate(candidate)
    assert notes and "已截断" in notes[0]
    assert validate_route_candidate(candidate)["course_count"] == ROUTE_LIMITS["max_courses_per_stage"]


def test_repair_leaves_within_limit_route_untouched():
    candidate = RouteCandidate.model_validate(route_payload(3))
    assert repair_route_candidate(candidate) == []
    assert validate_route_candidate(candidate)["course_count"] == 3


def test_repair_drops_course_prereq_edges_beyond_lesson_count():
    payload = {"stages": [{"title": "阶段一", "courses": [{
        "title": "数据库基础",
        "units": [{"title": "单元一", "lessons": [{"title": "第 1 讲"}, {"title": "第 2 讲"}]}],
        # 以讲次数为界：索引 5 越界（提示词只说课程编号，模型无法得知这条规则）
        "prerequisites": [{"from_index": 0, "to_index": 1}, {"from_index": 0, "to_index": 5}],
    }]}]}
    candidate = RouteCandidate.model_validate(payload)
    with pytest.raises(PlanningError) as exc:
        validate_route_candidate(candidate)
    assert exc.value.code == "INVALID_PREREQUISITE"

    notes = repair_route_candidate(candidate)
    assert notes and "已丢弃无效边" in notes[0]
    assert [(e.from_index, e.to_index) for e in candidate.stages[0].courses[0].prerequisites] == [(0, 1)]
    assert validate_route_candidate(candidate)["lesson_count"] == 2


def test_repair_skips_course_without_units():
    payload = {"stages": [{"title": "阶段一", "courses": [
        {"title": "数据库基础", "prerequisites": [{"from_index": 0, "to_index": 3}]},
    ]}]}
    candidate = RouteCandidate.model_validate(payload)
    # 无内嵌单元 ⇒ 课程级先修边不参与课程内校验（node_total 为 0，不可据此剪边）
    assert repair_route_candidate(candidate) == []
    validate_route_candidate(candidate)


# ---- 容器下限：必须在业务层报可读原因，不能在解析层抢先 ----
#
# 提示词要求「2-4 个阶段、总共 3-8 门课」，会自然邀请出留空阶段。若把
# ``min_length=1`` 写在字段上，pydantic 会在 SDK 反序列化阶段抛 ValidationError，
# ``output_validator`` 根本不被调用，模型只会拿到裸英文错误并耗尽重试预算。

EMPTY_STAGE_PAYLOAD = {"stages": [
    {"title": "阶段一", "courses": [{"title": "课程 A"}]},
    {"title": "阶段二", "courses": [{"title": "课程 B"}]},
    {"title": "阶段三", "courses": [{"title": "课程 C"}]},
    {"title": "阶段四", "courses": []},
]}


def test_empty_stage_reaches_business_validator_with_readable_reason():
    # 解析层必须放行，否则业务校验与确定性修正都是死代码。
    candidate = RouteCandidate.model_validate(EMPTY_STAGE_PAYLOAD)
    assert candidate.stages[-1].courses == []
    with pytest.raises(PlanningError) as exc:
        validate_route_candidate(candidate)
    assert exc.value.code == "ROUTE_STRUCTURE_INVALID"
    assert exc.value.message == "stage has no course"
    reason = output_retry_reason(exc.value)
    assert "stage has no course" in reason and reason != "ROUTE_STRUCTURE_INVALID"


def test_repair_drops_empty_stage_and_then_validates():
    candidate = RouteCandidate.model_validate(EMPTY_STAGE_PAYLOAD)
    notes = repair_route_candidate(candidate)
    assert notes and "空阶段" in notes[0]
    assert validate_route_candidate(candidate)["course_count"] == 3


def test_empty_units_reach_business_validator_with_readable_reason():
    payload = {"units": [
        {"title": "单元一", "lessons": [{"title": "第 1 讲"}]},
        {"title": "单元二", "lessons": []},
    ]}
    candidate = OutlineCandidate.model_validate(payload)
    assert candidate.units[-1].lessons == []
    assert "unit has no lesson" in (outline_generation_issue(candidate) or "")
    notes = repair_outline_candidate(candidate)
    assert notes and "空单元" in notes[0]
    assert outline_generation_issue(candidate) is None


def test_empty_root_containers_reach_business_validator():
    # 空 stages / 空 units 同样不能在解析层被拒。
    assert RouteCandidate.model_validate({"stages": []}).stages == []
    assert OutlineCandidate.model_validate({"units": []}).units == []


# ---- 落库入口保持严格（生成期降级不得外溢） ----


def test_storage_entry_still_rejects_nine_courses_per_stage():
    with pytest.raises(PlanningError) as exc:
        validate_content_json("route", route_payload(9))
    assert exc.value.code == "ROUTE_STRUCTURE_INVALID"


def test_storage_entry_still_rejects_course_prereq_out_of_range():
    payload = {"stages": [{"title": "阶段一", "courses": [{
        "title": "数据库基础",
        "units": [{"title": "单元一", "lessons": [{"title": "第 1 讲"}]}],
        "prerequisites": [{"from_index": 0, "to_index": 9}],
    }]}]}
    with pytest.raises(PlanningError) as exc:
        validate_content_json("route", payload)
    assert exc.value.code == "INVALID_PREREQUISITE"


# ---- 大纲：可读原因 + 修正 ----


def outline_payload(*, titles, goals=None, per_unit=2):
    goals = goals if goals is not None else [f"{t} 的目标" for t in titles]
    return {"units": [{"title": "单元一", "lessons": [
        {"title": titles[i], "goal": goals[i]} for i in range(per_unit)
    ]}]}


def test_outline_issue_is_readable_for_duplicate_titles():
    issue = outline_generation_issue(OutlineCandidate.model_validate(outline_payload(titles=["讲次", "讲次"])))
    assert issue is not None and "duplicate lesson titles" in issue
    assert issue != "duplicate lesson titles"


def test_outline_issue_none_when_acceptable():
    assert outline_generation_issue(OutlineCandidate.model_validate(outline_payload(titles=["甲", "乙"]))) is None


def test_outline_issue_reports_empty_goal():
    issue = outline_generation_issue(
        OutlineCandidate.model_validate(outline_payload(titles=["甲", "乙"], goals=["", "乙的目标"])))
    assert issue is not None and "lesson goal is empty" in issue


def test_outline_issue_respects_caller_budget():
    candidate = OutlineCandidate.model_validate(outline_payload(titles=["甲", "乙"]))
    assert outline_generation_issue(candidate, 2) is None
    assert "too many lessons" in (outline_generation_issue(candidate, 1) or "")


def test_repair_outline_fixes_titles_goals_and_budget():
    candidate = OutlineCandidate.model_validate(
        outline_payload(titles=["讲次", "讲次"], goals=["目标", ""]))
    notes = repair_outline_candidate(candidate, 2)
    lessons = [lesson for unit in candidate.units for lesson in unit.lessons]
    assert len(lessons) == 2
    assert len({lesson.title.casefold() for lesson in lessons}) == 2
    assert all(lesson.goal.strip() for lesson in lessons)
    assert notes
    assert outline_generation_issue(candidate, 2) is None


def test_repair_outline_enforces_total_budget_across_units():
    # 每单元都合法（= 单单元上限），但总量 12 × 20 = 240 超过 max_lessons = 120。
    payload = {"units": [
        {"title": f"单元 {i}",
         "lessons": [{"title": f"讲次 {i}-{j}", "goal": "目标"} for j in range(OUTLINE_LIMITS["max_lessons_per_unit"])]}
        for i in range(OUTLINE_LIMITS["max_units"])
    ]}
    candidate = OutlineCandidate.model_validate(payload)
    assert "too many lessons" in (outline_generation_issue(candidate) or "")
    repair_outline_candidate(candidate)
    assert outline_generation_issue(candidate) is None
    validate_outline_candidate(candidate)


# ---- 端到端：可读原因 + 修正让生成在既有重试预算内收敛 ----


def test_readable_reason_and_repair_converge_inside_existing_retry_budget():
    """模拟 service._generate_route 的校验器：首次回喂可读原因，第二次修正后就地接受。"""
    calls, fed_back = [], []
    fed = {"done": False}

    def validator(ctx, output):
        try:
            validate_route_candidate(output)
            return output
        except PlanningError as exc:
            if fed["done"]:
                notes = repair_route_candidate(output)
                if notes:
                    try:
                        validate_route_candidate(output)
                        return output
                    except PlanningError as retry_exc:
                        exc = retry_exc
                reason = output_retry_reason(exc, notes)
            else:
                fed["done"] = True
                reason = output_retry_reason(exc)
            fed_back.append(reason)
            raise ModelRetry(reason) from None

    payloads = [route_payload(9), route_payload(9)]

    def function(messages, info):
        calls.append(messages)
        return ModelResponse(parts=[TextPart(json.dumps(payloads[min(len(calls) - 1, 1)]))])

    deps = AgentDeps()
    result = asyncio.run(execute_agent(FunctionModel(function, profile=PROFILE), RouteCandidate, INPUT, deps,
                                       output_validator=validator))
    # 首次回喂的是可读约束（不是裸码），第二次靠确定性修正通过 —— 不消耗额外重试。
    assert "too many courses in stage" in fed_back[0]
    assert str(ROUTE_LIMITS["max_courses_per_stage"]) in fed_back[0]
    assert result.requests == deps.budget.logical_requests == 2
    assert validate_route_candidate(result.payload)["course_count"] == ROUTE_LIMITS["max_courses_per_stage"]


def test_retry_budget_is_still_two():
    assert OUTPUT_RETRIES == 2


def test_empty_stage_converges_inside_existing_retry_budget():
    """空阶段：首次回喂可读原因，第二次靠丢弃空阶段就地收敛（不消耗额外重试）。"""
    calls, fed_back = [], []
    fed = {"done": False}

    def validator(ctx, output):
        try:
            validate_route_candidate(output)
            return output
        except PlanningError as exc:
            if fed["done"]:
                notes = repair_route_candidate(output)
                if notes:
                    try:
                        validate_route_candidate(output)
                        return output
                    except PlanningError as retry_exc:
                        exc = retry_exc
                reason = output_retry_reason(exc, notes)
            else:
                fed["done"] = True
                reason = output_retry_reason(exc)
            fed_back.append(reason)
            raise ModelRetry(reason) from None

    def function(messages, info):
        calls.append(messages)
        return ModelResponse(parts=[TextPart(json.dumps(EMPTY_STAGE_PAYLOAD))])

    deps = AgentDeps()
    result = asyncio.run(execute_agent(FunctionModel(function, profile=PROFILE), RouteCandidate, INPUT, deps,
                                       output_validator=validator))
    assert "stage has no course" in fed_back[0]
    assert "每个阶段至少要有一门课程" in fed_back[0]
    assert result.requests == deps.budget.logical_requests == 2
    assert validate_route_candidate(result.payload)["course_count"] == 3


def test_failure_reason_reports_structure_without_model_content():
    """诊断通道只带异常类型与 pydantic 约束的 type/loc，绝不带模型原始输出。"""

    def boom(messages, info):
        return ModelResponse(parts=[TextPart("synthetic-model-secret")])

    with pytest.raises(PlanningError) as exc:
        asyncio.run(execute_agent(FunctionModel(boom, profile=PROFILE), RouteCandidate, INPUT, AgentDeps(),
                                  output_validator=lambda ctx, out: out))
    reason = failure_reason(exc.value)
    assert "ValidationError" in reason
    assert "synthetic-model-secret" not in reason
