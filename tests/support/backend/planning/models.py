"""切片 3 数据模型：两级规划、草稿/候选/视图、即时对话与上下文清单。

对应交接文档 §3.1 核心契约。所有个人资源带 `owner_user_id`；
子对象经父对象校验所有权。`*_current_*_version_id` 指针列为可空普通列
（双向外键需要 use_alter，业务层在确认事务内同事务维护指针）；
版本表到父表的外键在 DB 层保证。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..logging_audit.models import Base

# 值集（契约冻结）
SCENARIOS = ("self_study", "school")
PLANNING_SCOPES = ("auto", "course", "route")
RESOLVED_SCOPES = ("course", "route")
DRAFT_KINDS = ("route", "course_outline")
DRAFT_SAVED_STATES = ("saving", "saved", "failed")
CANDIDATE_KINDS = ("route_generate", "outline_generate", "planning_revise")
LESSON_PREPARATION = ("not_generated", "generating", "ready", "failed")
LEARNING_STATES = ("not_started", "in_progress", "user_completed")
CONVERSATION_KINDS = ("chat",)
MESSAGE_ROLES = ("user", "assistant")
MESSAGE_STATUSES = ("completed", "interrupted", "failed")
TASK_KINDS = ("route_generate", "outline_generate", "planning_revise", "chat_answer")
WAIT_STATES = ("awaiting_scope_clarification", "awaiting_search_decision")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Optional[dict]) -> str:
    return json.dumps(value or {}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 路线（两级规划之一）
# ---------------------------------------------------------------------------


class LearningRouteRow(Base):
    __tablename__ = "learning_routes"
    __table_args__ = (Index("idx_learning_routes_owner", "owner_user_id"),)

    route_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario: Mapped[str] = mapped_column(String(20), nullable=False, default="self_study")
    title_ref: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    current_route_version_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RouteVersionRow(Base):
    __tablename__ = "route_versions"
    __table_args__ = (
        UniqueConstraint("route_id", "version_no", name="uq_route_versions_route_no"),
        Index("idx_route_versions_route", "route_id"),
    )

    route_version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    route_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("learning_routes.route_id"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_task_id: Mapped[str | None] = mapped_column(String(64))
    prompt_binding_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class RouteStageRow(Base):
    __tablename__ = "route_stages"
    __table_args__ = (Index("idx_route_stages_version", "route_version_id", "order_index"),)

    stage_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    route_version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("route_versions.route_version_id", ondelete="CASCADE"), nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title_ref: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class RouteCourseNodeRow(Base):
    __tablename__ = "route_course_nodes"
    __table_args__ = (
        Index("idx_route_course_nodes_version", "route_version_id", "order_index"),
        Index("idx_route_course_nodes_stage", "stage_id"),
    )

    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    route_version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("route_versions.route_version_id", ondelete="CASCADE"), nullable=False
    )
    stage_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("route_stages.stage_id"), nullable=False
    )
    course_id: Mapped[str] = mapped_column(String(64), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    goal_snapshot_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scope_snapshot_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    prereq_requirements_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")


class RoutePrerequisiteEdgeRow(Base):
    __tablename__ = "route_prerequisite_edges"
    __table_args__ = (
        Index("idx_route_prereq_version", "route_version_id"),
        UniqueConstraint(
            "route_version_id", "from_node_id", "to_node_id",
            name="uq_route_prereq_edge",
        ),
    )

    edge_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    route_version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("route_versions.route_version_id", ondelete="CASCADE"), nullable=False
    )
    from_node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    to_node_id: Mapped[str] = mapped_column(String(64), nullable=False)


# ---------------------------------------------------------------------------
# 课程与大纲（两级规划之二）
# ---------------------------------------------------------------------------


class CourseRow(Base):
    __tablename__ = "courses"
    __table_args__ = (
        Index("idx_courses_owner", "owner_user_id"),
        Index("idx_courses_source_node", "source_route_node_id"),
    )

    course_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario: Mapped[str] = mapped_column(String(20), nullable=False, default="self_study")
    title_ref: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    current_outline_version_id: Mapped[str | None] = mapped_column(String(64))
    source_route_version_id: Mapped[str | None] = mapped_column(String(64))
    source_route_node_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutlineVersionRow(Base):
    __tablename__ = "outline_versions"
    __table_args__ = (
        UniqueConstraint("course_id", "version_no", name="uq_outline_versions_course_no"),
        Index("idx_outline_versions_course", "course_id"),
    )

    outline_version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    course_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("courses.course_id"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_task_id: Mapped[str | None] = mapped_column(String(64))
    prompt_binding_id: Mapped[str | None] = mapped_column(String(64))
    # 由路线展开大纲时记录来源（§5.1）
    source_route_version_id: Mapped[str | None] = mapped_column(String(64))
    source_route_node_id: Mapped[str | None] = mapped_column(String(64))
    goal_snapshot_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scope_snapshot_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class CourseUnitRow(Base):
    __tablename__ = "course_units"
    __table_args__ = (Index("idx_course_units_version", "outline_version_id", "order_index"),)

    unit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    outline_version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("outline_versions.outline_version_id", ondelete="CASCADE"), nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title_ref: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class LessonRow(Base):
    __tablename__ = "lessons"
    __table_args__ = (
        Index("idx_lessons_version", "outline_version_id", "order_index"),
        Index("idx_lessons_unit", "unit_id"),
    )

    lesson_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    outline_version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("outline_versions.outline_version_id", ondelete="CASCADE"), nullable=False
    )
    unit_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("course_units.unit_id"), nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    goal_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status_preparation: Mapped[str] = mapped_column(String(20), nullable=False, default="not_generated")


class LessonContentVersionRow(Base):
    __tablename__ = "lesson_content_versions"
    __table_args__ = (
        UniqueConstraint("lesson_id", "content_version_no", name="uq_lesson_content_versions_lesson_no"),
        Index("idx_lesson_content_versions_lesson", "lesson_id"),
    )

    content_version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    lesson_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("lessons.lesson_id"), nullable=False
    )
    content_version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    goal_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scope_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LessonPrerequisiteEdgeRow(Base):
    __tablename__ = "lesson_prerequisite_edges"
    __table_args__ = (
        Index("idx_lesson_prereq_version", "outline_version_id"),
        UniqueConstraint(
            "outline_version_id", "from_lesson_id", "to_lesson_id",
            name="uq_lesson_prereq_edge",
        ),
    )

    edge_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    outline_version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("outline_versions.outline_version_id", ondelete="CASCADE"), nullable=False
    )
    from_lesson_id: Mapped[str] = mapped_column(String(64), nullable=False)
    to_lesson_id: Mapped[str] = mapped_column(String(64), nullable=False)


# ---------------------------------------------------------------------------
# 草稿 / 候选 / 视图状态
# ---------------------------------------------------------------------------


class PlanningDraftRow(Base):
    __tablename__ = "planning_drafts"
    __table_args__ = (
        Index("idx_planning_drafts_owner", "owner_user_id"),
        Index("idx_planning_drafts_target", "draft_kind", "target_id"),
    )

    draft_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    draft_kind: Mapped[str] = mapped_column(String(20), nullable=False)  # route | course_outline
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    base_version_id: Mapped[str | None] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    saved_state: Mapped[str] = mapped_column(String(20), nullable=False, default="saved")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class PlanningCandidateRow(Base):
    __tablename__ = "planning_candidates"
    __table_args__ = (
        Index("idx_planning_candidates_target", "target_id"),
        Index("idx_planning_candidates_draft", "draft_id"),
    )

    candidate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    draft_id: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    base_draft_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    generation_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    source_task_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class PlanningViewStateRow(Base):
    """画布位置/折叠/视角；不作为学习顺序来源（§3.1）。"""

    __tablename__ = "planning_view_states"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "target_kind", "target_id", name="uq_planning_view_states"),
    )

    view_state_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    canvas_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


# ---------------------------------------------------------------------------
# 即时文字对话
# ---------------------------------------------------------------------------


class ConversationRow(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("idx_conversations_owner", "owner_user_id"),
        Index("idx_conversations_lesson", "lesson_id"),
    )

    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="chat")
    scenario: Mapped[str] = mapped_column(String(20), nullable=False, default="self_study")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    material_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # 切片 4：课程会话绑定（kind=teaching 时必填业务校验，DB 层可空）
    course_id: Mapped[str | None] = mapped_column(String(64))
    outline_version_id: Mapped[str | None] = mapped_column(String(64))
    lesson_id: Mapped[str | None] = mapped_column(String(64))
    lesson_content_version_id: Mapped[str | None] = mapped_column(String(64))
    # 一次性原课程恢复点（追问时创建；连续追问沿用最初恢复点）
    restore_point_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    session_state: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MessageRow(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("idx_messages_conversation", "conversation_id", "generation"),
    )

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("conversations.conversation_id", ondelete="CASCADE"), nullable=False
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="completed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


# ---------------------------------------------------------------------------
# 上下文清单（不含正文）
# ---------------------------------------------------------------------------


class ContextManifestRow(Base):
    __tablename__ = "context_manifests"
    __table_args__ = (
        Index("idx_context_manifests_task", "task_id"),
        Index("idx_context_manifests_conversation", "conversation_id"),
    )

    manifest_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(String(64))
    conversation_id: Mapped[str | None] = mapped_column(String(64))
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    budget_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    retrieval_refs_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    history_range_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    truncation_reasons_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    summary_version: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


# ---------------------------------------------------------------------------
# 讲次进度（本切片落表与只读汇总；完成状态写入归切片 4）
# 与学习导航状态
# ---------------------------------------------------------------------------


class LessonProgressRow(Base):
    __tablename__ = "lesson_progress"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "lesson_id", name="uq_lesson_progress_owner_lesson"),
    )

    progress_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    lesson_id: Mapped[str] = mapped_column(String(64), nullable=False)
    lesson_content_version_id: Mapped[str | None] = mapped_column(String(64))
    learning_state: Mapped[str] = mapped_column(String(20), nullable=False, default="not_started")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LearningNavigationStateRow(Base):
    __tablename__ = "learning_navigation_states"

    owner_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_route_id: Mapped[str | None] = mapped_column(String(64))
    last_course_id: Mapped[str | None] = mapped_column(String(64))
    last_unit_id: Mapped[str | None] = mapped_column(String(64))
    last_lesson_id: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


# ---------------------------------------------------------------------------
# 规划任务扩展状态（learning_tasks 列不变；wait_state / resolved_scope 记录于此）
# ---------------------------------------------------------------------------


class PlanningTaskStateRow(Base):
    __tablename__ = "planning_task_states"
    __table_args__ = (Index("idx_planning_task_states_owner", "owner_user_id"),)

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    planning_scope: Mapped[str] = mapped_column(String(20), nullable=False, default="auto")
    resolved_scope: Mapped[str | None] = mapped_column(String(20))
    scope_signals_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    target_kind: Mapped[str | None] = mapped_column(String(20))
    target_id: Mapped[str | None] = mapped_column(String(64))
    draft_id: Mapped[str | None] = mapped_column(String(64))
    base_version_id: Mapped[str | None] = mapped_column(String(64))
    source_route_version_id: Mapped[str | None] = mapped_column(String(64))
    source_route_node_id: Mapped[str | None] = mapped_column(String(64))
    wait_state: Mapped[str | None] = mapped_column(String(40))
    clarification_question: Mapped[str | None] = mapped_column(String(300))
    # 生成前访谈（v2）：{round, questions:[{question_id,question,kind,options,skippable}], answers:[...], done}
    intake_json: Mapped[str | None] = mapped_column(Text)
    material_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    search_decision: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


def load_json_field(text: Optional[str], default):
    try:
        value = json.loads(text) if text else None
        return value if value is not None else default
    except json.JSONDecodeError:
        return default


__all__ = [
    "SCENARIOS", "PLANNING_SCOPES", "RESOLVED_SCOPES", "DRAFT_KINDS", "DRAFT_SAVED_STATES",
    "CANDIDATE_KINDS", "LESSON_PREPARATION", "LEARNING_STATES", "CONVERSATION_KINDS",
    "MESSAGE_ROLES", "MESSAGE_STATUSES", "TASK_KINDS", "WAIT_STATES",
    "LearningRouteRow", "RouteVersionRow", "RouteStageRow", "RouteCourseNodeRow",
    "RoutePrerequisiteEdgeRow", "CourseRow", "OutlineVersionRow", "CourseUnitRow",
    "LessonRow", "LessonContentVersionRow", "LessonPrerequisiteEdgeRow",
    "PlanningDraftRow", "PlanningCandidateRow", "PlanningViewStateRow",
    "ConversationRow", "MessageRow", "ContextManifestRow",
    "LessonProgressRow", "LearningNavigationStateRow", "PlanningTaskStateRow",
    "utcnow", "load_json_field", "_json", "BigInteger",
]
