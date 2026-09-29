"""S2：教学步骤/板书业务校验——字段级回喂源（方案 §3 S2）。

超出 pydantic schema 的业务规则在这里校验，经 ModelRetry 以结构化
中文错误清单回喂模型（RetryPromptPart），替代 model_runner 时代的
二值盲重试：
- 步骤时长：len(text)/4 秒，>120s 硬上限必须拆分；
- 板书边引用：edge 指向的 from_id/to_id 必须存在于 nodes；
- 时标对齐：reveal_ms 长度与 points 对齐（若两者均非空）。
"""

from __future__ import annotations

from typing import List

from pydantic_ai.exceptions import ModelRetry

from ..planning.errors import PlanningError
from ..teaching.outputs import LessonPlanCandidate, declaration_to_scene, validate_step_duration


def lesson_step_issues(candidate: LessonPlanCandidate) -> List[str]:
    issues: List[str] = []
    for idx, step in enumerate(candidate.steps, 1):
        try:
            validate_step_duration(step.text)
        except PlanningError:
            issues.append(
                f"步骤{idx}：讲解文本 {len(step.text)} 字（约 {len(step.text) // 4} 秒），"
                "超过 120 秒硬上限，必须拆分为多个步骤"
            )
        try:
            declaration_to_scene(step.board)
        except PlanningError as exc:
            issues.append(f"步骤{idx}板书：{getattr(exc, 'message', None) or exc}")
        board = step.board
        if board.reveal_ms and board.points and len(board.reveal_ms) != len(board.points):
            issues.append(
                f"步骤{idx}板书：reveal_ms 时标数（{len(board.reveal_ms)}）"
                f"与要点数（{len(board.points)}）不对齐"
            )
    return issues


def lesson_step_output_validator(ctx, output: LessonPlanCandidate) -> LessonPlanCandidate:
    """挂到 Agent.output_validator：业务校验失败 → ModelRetry 字段级清单。"""
    issues = lesson_step_issues(output)
    if issues:
        raise ModelRetry("；".join(issues))
    return output


def planning_title_issues(candidate) -> List[str]:
    """路线/大纲候选标题是否残留内部标识符（route_xxx 等），供生成侧回喂。

    只检测不修改：剥除仍由 validate_*_candidate / strip_internal_ids_in_place
    统一兜底，这里负责把问题以字段级清单还给模型自纠。
    """
    from ..planning.outputs import internal_id_residue

    issues: List[str] = []
    stages = getattr(candidate, "stages", None) or getattr(candidate, "units", None) or []
    child_label = "courses" if hasattr(candidate, "stages") else "lessons"
    for si, stage in enumerate(stages):
        if internal_id_residue(stage.title):
            issues.append(f"{type(candidate).__name__}.stages[{si}].title 含内部标识符，请改为纯自然语言标题")
        children = getattr(stage, "courses", None) or getattr(stage, "lessons", None) or []
        for ci, child in enumerate(children):
            if internal_id_residue(child.title):
                issues.append(f"{child_label}[{ci}].title 含内部标识符，请改为纯自然语言标题")
            goal = getattr(child, "goal", "")
            if goal and internal_id_residue(goal):
                issues.append(f"{child_label}[{ci}].goal 含内部标识符，请改为自然语言描述")
    return issues


def planning_title_output_validator():
    """挂到路线/大纲 Agent.output_validator：标题残留 ID → 有界 ModelRetry。

    首次检出回喂字段级清单让模型自纠；再犯直接放行——剥除兜底
    （validate_*_candidate / strip_internal_ids_in_place）保证落库干净，
    静态/劣质适配器不会在重试上限内死循环拖垮整个生成。
    """
    fed_back = {"done": False}

    def validator(ctx, output):
        if fed_back["done"]:
            return output
        issues = planning_title_issues(output)
        if issues:
            fed_back["done"] = True
            raise ModelRetry("；".join(issues))
        return output

    return validator


__all__ = [
    "lesson_step_issues", "lesson_step_output_validator",
    "planning_title_issues", "planning_title_output_validator",
    "make_search_materials_tool",
]


def make_search_materials_tool(retrieval_service, *, owner_user_id: str, material_ids):
    """S3：受控检索工具（挂到路线/大纲 Agent）。

    返回受限摘要 JSON（材料标题 + 限长摘录），不透传正文全集；
    检索失败抛 ModelRetry 让模型改走无检索路径，不炸整个 run。
    """

    def search_materials(query: str, limit: int = 3) -> str:
        """按关键词检索学习者已选资料，返回受限摘要（标题+限长摘录）。

        query：检索关键词；limit：返回条数上限（默认 3）。
        """
        import json as _json

        try:
            result = retrieval_service.assemble(
                owner_user_id=owner_user_id, material_ids=list(material_ids), query=query,
            )
        except Exception:  # noqa: BLE001 — 检索失败降级为提示，不终止
            raise ModelRetry("检索暂不可用，请基于已有信息继续") from None
        items = [
            {
                "title": str(e.get("title", ""))[:60],
                "excerpt": str(e.get("excerpt", e.get("text", "")))[:200],
            }
            for e in result.excerpts[: max(1, min(limit, 5))]
        ]
        payload = {"items": items, "chars_total": result.chars_total,
                   "truncated": bool(result.truncation_reasons)}
        return _json.dumps(payload, ensure_ascii=False)

    return search_materials
