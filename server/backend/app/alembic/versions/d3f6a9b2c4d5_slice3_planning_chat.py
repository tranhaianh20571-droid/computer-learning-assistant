"""slice3_planning_chat

Revision ID: d3f6a9b2c4d5
Revises: c2d3e4f5a6b7
Create Date: 2026-09-27

切片 3：两级规划（路线/大纲）、草稿/候选/视图状态、即时对话（会话/消息）、
上下文清单（不含正文）、讲次进度与导航状态、规划任务扩展状态。
`learning_tasks` 列不变（wait_state / resolved_scope 记录在 planning_task_states）。
"""

import sqlalchemy as sa
from alembic import op

revision = "d3f6a9b2c4d5"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- 路线 ----
    op.create_table(
        "learning_routes",
        sa.Column("route_id", sa.String(length=64), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.Column("scenario", sa.String(length=20), nullable=False),
        sa.Column("title_ref", sa.String(length=200), nullable=False),
        sa.Column("current_route_version_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_learning_routes_owner", "learning_routes", ["owner_user_id"])

    op.create_table(
        "route_versions",
        sa.Column("route_version_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "route_id", sa.String(length=64),
            sa.ForeignKey("learning_routes.route_id", name="fk_route_versions_route"),
            nullable=False,
        ),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("source_task_id", sa.String(length=64), nullable=True),
        sa.Column("prompt_binding_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("route_id", "version_no", name="uq_route_versions_route_no"),
    )
    op.create_index("idx_route_versions_route", "route_versions", ["route_id"])

    op.create_table(
        "route_stages",
        sa.Column("stage_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "route_version_id", sa.String(length=64),
            sa.ForeignKey("route_versions.route_version_id", ondelete="CASCADE", name="fk_route_stages_version"),
            nullable=False,
        ),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("title_ref", sa.String(length=200), nullable=False),
    )
    op.create_index("idx_route_stages_version", "route_stages", ["route_version_id", "order_index"])

    # ---- 课程 ----
    op.create_table(
        "courses",
        sa.Column("course_id", sa.String(length=64), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.Column("scenario", sa.String(length=20), nullable=False),
        sa.Column("title_ref", sa.String(length=200), nullable=False),
        sa.Column("current_outline_version_id", sa.String(length=64), nullable=True),
        sa.Column("source_route_version_id", sa.String(length=64), nullable=True),
        sa.Column("source_route_node_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_courses_owner", "courses", ["owner_user_id"])
    op.create_index("idx_courses_source_node", "courses", ["source_route_node_id"])

    op.create_table(
        "route_course_nodes",
        sa.Column("node_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "route_version_id", sa.String(length=64),
            sa.ForeignKey("route_versions.route_version_id", ondelete="CASCADE", name="fk_route_course_nodes_version"),
            nullable=False,
        ),
        sa.Column(
            "stage_id", sa.String(length=64),
            sa.ForeignKey("route_stages.stage_id", name="fk_route_course_nodes_stage"),
            nullable=False,
        ),
        sa.Column("course_id", sa.String(length=64), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("goal_snapshot_ref", sa.Text(), nullable=False),
        sa.Column("scope_snapshot_ref", sa.Text(), nullable=False),
        sa.Column("prereq_requirements_ref", sa.Text(), nullable=False),
    )
    op.create_index("idx_route_course_nodes_version", "route_course_nodes", ["route_version_id", "order_index"])
    op.create_index("idx_route_course_nodes_stage", "route_course_nodes", ["stage_id"])

    op.create_table(
        "route_prerequisite_edges",
        sa.Column("edge_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "route_version_id", sa.String(length=64),
            sa.ForeignKey("route_versions.route_version_id", ondelete="CASCADE", name="fk_route_prereq_version"),
            nullable=False,
        ),
        sa.Column("from_node_id", sa.String(length=64), nullable=False),
        sa.Column("to_node_id", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("route_version_id", "from_node_id", "to_node_id", name="uq_route_prereq_edge"),
    )
    op.create_index("idx_route_prereq_version", "route_prerequisite_edges", ["route_version_id"])

    # ---- 大纲 ----
    op.create_table(
        "outline_versions",
        sa.Column("outline_version_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "course_id", sa.String(length=64),
            sa.ForeignKey("courses.course_id", name="fk_outline_versions_course"),
            nullable=False,
        ),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("source_task_id", sa.String(length=64), nullable=True),
        sa.Column("prompt_binding_id", sa.String(length=64), nullable=True),
        sa.Column("source_route_version_id", sa.String(length=64), nullable=True),
        sa.Column("source_route_node_id", sa.String(length=64), nullable=True),
        sa.Column("goal_snapshot_ref", sa.Text(), nullable=False),
        sa.Column("scope_snapshot_ref", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("course_id", "version_no", name="uq_outline_versions_course_no"),
    )
    op.create_index("idx_outline_versions_course", "outline_versions", ["course_id"])

    op.create_table(
        "course_units",
        sa.Column("unit_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "outline_version_id", sa.String(length=64),
            sa.ForeignKey("outline_versions.outline_version_id", ondelete="CASCADE", name="fk_course_units_version"),
            nullable=False,
        ),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("title_ref", sa.String(length=200), nullable=False),
    )
    op.create_index("idx_course_units_version", "course_units", ["outline_version_id", "order_index"])

    op.create_table(
        "lessons",
        sa.Column("lesson_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "outline_version_id", sa.String(length=64),
            sa.ForeignKey("outline_versions.outline_version_id", ondelete="CASCADE", name="fk_lessons_version"),
            nullable=False,
        ),
        sa.Column(
            "unit_id", sa.String(length=64),
            sa.ForeignKey("course_units.unit_id", name="fk_lessons_unit"),
            nullable=False,
        ),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("goal_ref", sa.Text(), nullable=False),
        sa.Column("status_preparation", sa.String(length=20), nullable=False),
    )
    op.create_index("idx_lessons_version", "lessons", ["outline_version_id", "order_index"])
    op.create_index("idx_lessons_unit", "lessons", ["unit_id"])

    op.create_table(
        "lesson_content_versions",
        sa.Column("content_version_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "lesson_id", sa.String(length=64),
            sa.ForeignKey("lessons.lesson_id", name="fk_lesson_content_versions_lesson"),
            nullable=False,
        ),
        sa.Column("content_version_no", sa.Integer(), nullable=False),
        sa.Column("goal_ref", sa.Text(), nullable=False),
        sa.Column("scope_ref", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("lesson_id", "content_version_no", name="uq_lesson_content_versions_lesson_no"),
    )
    op.create_index("idx_lesson_content_versions_lesson", "lesson_content_versions", ["lesson_id"])

    op.create_table(
        "lesson_prerequisite_edges",
        sa.Column("edge_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "outline_version_id", sa.String(length=64),
            sa.ForeignKey("outline_versions.outline_version_id", ondelete="CASCADE", name="fk_lesson_prereq_version"),
            nullable=False,
        ),
        sa.Column("from_lesson_id", sa.String(length=64), nullable=False),
        sa.Column("to_lesson_id", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("outline_version_id", "from_lesson_id", "to_lesson_id", name="uq_lesson_prereq_edge"),
    )
    op.create_index("idx_lesson_prereq_version", "lesson_prerequisite_edges", ["outline_version_id"])

    # ---- 草稿 / 候选 / 视图 ----
    op.create_table(
        "planning_drafts",
        sa.Column("draft_id", sa.String(length=64), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.Column("draft_kind", sa.String(length=20), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("base_version_id", sa.String(length=64), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("saved_state", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_planning_drafts_owner", "planning_drafts", ["owner_user_id"])
    op.create_index("idx_planning_drafts_target", "planning_drafts", ["draft_kind", "target_id"])

    op.create_table(
        "planning_candidates",
        sa.Column("candidate_id", sa.String(length=64), primary_key=True),
        sa.Column("draft_id", sa.String(length=64), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_kind", sa.String(length=30), nullable=False),
        sa.Column("base_draft_revision", sa.Integer(), nullable=False),
        sa.Column("generation_revision", sa.Integer(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("source_task_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_planning_candidates_target", "planning_candidates", ["target_id"])
    op.create_index("idx_planning_candidates_draft", "planning_candidates", ["draft_id"])

    op.create_table(
        "planning_view_states",
        sa.Column("view_state_id", sa.String(length=64), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.Column("target_kind", sa.String(length=20), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("canvas_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_user_id", "target_kind", "target_id", name="uq_planning_view_states"),
    )

    # ---- 即时对话 ----
    op.create_table(
        "conversations",
        sa.Column("conversation_id", sa.String(length=64), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("scenario", sa.String(length=20), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("material_snapshot_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_conversations_owner", "conversations", ["owner_user_id"])

    op.create_table(
        "messages",
        sa.Column("message_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "conversation_id", sa.String(length=64),
            sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE", name="fk_messages_conversation"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content_ref", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_messages_conversation", "messages", ["conversation_id", "generation"])

    # ---- 上下文清单（不含正文） ----
    op.create_table(
        "context_manifests",
        sa.Column("manifest_id", sa.String(length=64), primary_key=True),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("conversation_id", sa.String(length=64), nullable=True),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("budget_json", sa.Text(), nullable=False),
        sa.Column("retrieval_refs_json", sa.Text(), nullable=False),
        sa.Column("history_range_json", sa.Text(), nullable=False),
        sa.Column("truncation_reasons_json", sa.Text(), nullable=False),
        sa.Column("summary_version", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_context_manifests_task", "context_manifests", ["task_id"])
    op.create_index("idx_context_manifests_conversation", "context_manifests", ["conversation_id"])

    # ---- 进度与导航 ----
    op.create_table(
        "lesson_progress",
        sa.Column("progress_id", sa.String(length=64), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.Column("lesson_id", sa.String(length=64), nullable=False),
        sa.Column("lesson_content_version_id", sa.String(length=64), nullable=True),
        sa.Column("learning_state", sa.String(length=20), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_user_id", "lesson_id", name="uq_lesson_progress_owner_lesson"),
    )

    op.create_table(
        "learning_navigation_states",
        sa.Column("owner_user_id", sa.String(length=64), primary_key=True),
        sa.Column("last_route_id", sa.String(length=64), nullable=True),
        sa.Column("last_course_id", sa.String(length=64), nullable=True),
        sa.Column("last_unit_id", sa.String(length=64), nullable=True),
        sa.Column("last_lesson_id", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ---- 规划任务扩展状态 ----
    op.create_table(
        "planning_task_states",
        sa.Column("task_id", sa.String(length=64), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.Column("planning_scope", sa.String(length=20), nullable=False),
        sa.Column("resolved_scope", sa.String(length=20), nullable=True),
        sa.Column("scope_signals_json", sa.Text(), nullable=False),
        sa.Column("target_kind", sa.String(length=20), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("draft_id", sa.String(length=64), nullable=True),
        sa.Column("base_version_id", sa.String(length=64), nullable=True),
        sa.Column("source_route_version_id", sa.String(length=64), nullable=True),
        sa.Column("source_route_node_id", sa.String(length=64), nullable=True),
        sa.Column("wait_state", sa.String(length=40), nullable=True),
        sa.Column("clarification_question", sa.String(length=300), nullable=True),
        sa.Column("material_ids_json", sa.Text(), nullable=False),
        sa.Column("search_decision", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_planning_task_states_owner", "planning_task_states", ["owner_user_id"])


def downgrade() -> None:
    for table in (
        "planning_task_states",
        "learning_navigation_states",
        "lesson_progress",
        "context_manifests",
        "messages",
        "conversations",
        "planning_view_states",
        "planning_candidates",
        "planning_drafts",
        "lesson_prerequisite_edges",
        "lesson_content_versions",
        "lessons",
        "course_units",
        "outline_versions",
        "route_prerequisite_edges",
        "route_course_nodes",
        "courses",
        "route_stages",
        "route_versions",
        "learning_routes",
    ):
        op.drop_table(table)
