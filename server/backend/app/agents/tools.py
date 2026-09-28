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


__all__ = ["lesson_step_issues", "lesson_step_output_validator"]
