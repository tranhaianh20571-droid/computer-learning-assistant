"""slice4_teaching_playback

Revision ID: e5a7c9d1b3f6
Revises: d3f6a9b2c4d5
Create Date: 2026-09-27

切片 4：教学步骤、板书场景、音频资产、播放检查点；
conversations 扩展课程会话绑定（course/outline_version/lesson/content_version）、
一次性恢复点与会话状态。
"""

import sqlalchemy as sa
from alembic import op

revision = "e5a7c9d1b3f6"
down_revision = "d3f6a9b2c4d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("course_id", sa.String(length=64), nullable=True))
    op.add_column("conversations", sa.Column("outline_version_id", sa.String(length=64), nullable=True))
    op.add_column("conversations", sa.Column("lesson_id", sa.String(length=64), nullable=True))
    op.add_column("conversations", sa.Column("lesson_content_version_id", sa.String(length=64), nullable=True))
    op.add_column("conversations", sa.Column("restore_point_json", sa.Text(), nullable=False, server_default="{}"))
    op.add_column("conversations", sa.Column("session_state", sa.String(length=20), nullable=False, server_default="active"))
    op.create_index("idx_conversations_lesson", "conversations", ["lesson_id"])

    op.create_table(
        "teaching_steps",
        sa.Column("step_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "conversation_id", sa.String(length=64),
            sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE", name="fk_teaching_steps_conv"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("text_version_no", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("board_scene_id", sa.String(length=64), nullable=True),
        sa.Column("audio_id", sa.String(length=64), nullable=True),
        sa.Column("source_task_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "generation", "step_no", name="uq_teaching_steps_conv_gen_no"),
    )
    op.create_index("idx_teaching_steps_conv", "teaching_steps", ["conversation_id", "step_no"])

    op.create_table(
        "board_scenes",
        sa.Column("scene_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "conversation_id", sa.String(length=64),
            sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE", name="fk_board_scenes_conv"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("scene_version_no", sa.Integer(), nullable=False),
        sa.Column("declaration_json", sa.Text(), nullable=False),
        sa.Column("scene_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "generation", "step_no", "scene_version_no",
                            name="uq_board_scenes_version"),
    )
    op.create_index("idx_board_scenes_conv", "board_scenes", ["conversation_id", "step_no"])

    op.create_table(
        "audio_assets",
        sa.Column("audio_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "conversation_id", sa.String(length=64),
            sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE", name="fk_audio_assets_conv"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("text_version_no", sa.Integer(), nullable=False),
        sa.Column("text_sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=200), nullable=False),
        sa.Column("audio_format", sa.String(length=10), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("subtitle_json", sa.Text(), nullable=False),
        sa.Column("provider_trace_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("invalid_reason", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "generation", "step_no", "text_version_no",
                            name="uq_audio_assets_version"),
    )
    op.create_index("idx_audio_assets_conv", "audio_assets", ["conversation_id", "step_no"])

    op.create_table(
        "playback_checkpoints",
        sa.Column("checkpoint_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "conversation_id", sa.String(length=64),
            sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE", name="fk_playback_checkpoints_conv"),
            nullable=False,
        ),
        sa.Column("client_seq", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("position_ms", sa.Integer(), nullable=False),
        sa.Column("board_state_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "client_seq", name="uq_playback_checkpoints_seq"),
    )
    op.create_index("idx_playback_checkpoints_conv", "playback_checkpoints", ["conversation_id"])


def downgrade() -> None:
    for table in ("playback_checkpoints", "audio_assets", "board_scenes", "teaching_steps"):
        op.drop_table(table)
    op.drop_index("idx_conversations_lesson", table_name="conversations")
    for column in ("course_id", "outline_version_id", "lesson_id", "lesson_content_version_id",
                   "restore_point_json", "session_state"):
        op.drop_column("conversations", column)
