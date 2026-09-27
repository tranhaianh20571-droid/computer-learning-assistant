"""规划范围解析（T04）。

规则（交接文档 §1.1 / 架构 §5.1）：
- 用户明确选择（planning_scope=course|route）优先，直接作为 resolved_scope；
- auto：依据「目标表述、基础表述、边界表述」三类信号形成建议；
  不得按附件数量、场景（self_study/school）或主题关键词硬决定；
- 存在会改变结果的歧义（同时出现跨领域目标与单教材边界、或两类信号均缺失）
  → 抛 WaitStateSignal(awaiting_scope_clarification)。

实现为纯函数：输入为用户提交的结构化字段（不含正文副本，只判定信号布尔值），
便于契约测试穷举。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import WaitStateSignal

RESOLVED_COURSE = "course"
RESOLVED_ROUTE = "route"


@dataclass(frozen=True)
class ScopeSignals:
    """从用户输入解析出的信号（由提交字段派生，不存正文）。"""

    cross_domain_goal: bool = False   # 目标跨多个领域/职业路径（如「转行后端并补足算法基础」）
    career_oriented: bool = False     # 以职业路径为边界组织（多门课先后衔接）
    single_textbook: bool = False     # 边界=单本教材/单门课（用户明确「只学这本书/这门课」）
    explicit_course: bool = False     # 明确指定了单一课程名
    ambiguous: bool = False           # 上述信号同时成立或全部缺失


def resolve_scope(
    planning_scope: str,
    signals: ScopeSignals,
    *,
    ambiguous_override: bool | None = None,
) -> str:
    """auto → resolved_scope；歧义进入澄清等待。用户明确选择优先。"""
    if planning_scope == "course":
        return RESOLVED_COURSE
    if planning_scope == "route":
        return RESOLVED_ROUTE
    if planning_scope != "auto":
        from .errors import error

        raise error("invalid_field", f"unknown planning_scope {planning_scope}")

    ambiguous = signals.ambiguous or (
        (signals.cross_domain_goal or signals.career_oriented) and signals.single_textbook
    ) or (not signals.cross_domain_goal and not signals.career_oriented and not signals.single_textbook and not signals.explicit_course)
    if ambiguous_override is not None:
        ambiguous = ambiguous_override
    if ambiguous:
        raise WaitStateSignal(
            "awaiting_scope_clarification",
            question="这个规划是围绕一门课程展开，还是一条跨阶段的学习路线？",
        )
    if signals.single_textbook or signals.explicit_course:
        return RESOLVED_COURSE
    return RESOLVED_ROUTE


__all__ = ["ScopeSignals", "resolve_scope", "RESOLVED_COURSE", "RESOLVED_ROUTE"]
