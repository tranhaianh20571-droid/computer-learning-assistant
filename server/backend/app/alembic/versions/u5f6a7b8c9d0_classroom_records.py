"""Owned mentor preferences and version-bound presentation/knowledge records."""
from alembic import op
import sqlalchemy as sa

revision = "u5f6a7b8c9d0"
down_revision = "t4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("character_segments", sa.Column("expression_json", sa.Text(),
        nullable=False, server_default="{}"))
    op.create_table("character_preferences",
        sa.Column("owner_user_id", sa.String(64), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("card_id", sa.String(64)),
        sa.Column("revision", sa.Integer()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("character_expressions",
        sa.Column("cache_key", sa.String(64), primary_key=True),
        sa.Column("profile_hash", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(40), nullable=False),
        sa.Column("plan_json", sa.Text(), nullable=False))
    op.create_table("segment_coverage",
        sa.Column("conversation_id", sa.String(64), sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("step_no", sa.Integer(), primary_key=True),
        sa.Column("text_version", sa.Integer(), primary_key=True),
        sa.Column("audio_id", sa.String(64), primary_key=True),
        sa.Column("client_seq", sa.Integer(), nullable=False),
        sa.Column("intervals_json", sa.Text(), nullable=False),
        sa.Column("visible_chars", sa.Integer(), nullable=False),
        sa.Column("board_complete", sa.Boolean(), nullable=False))
    op.create_table("taught_knowledge",
        sa.Column("record_id", sa.String(64), primary_key=True),
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.Column("course_id", sa.String(64), nullable=False),
        sa.Column("lesson_id", sa.String(64), nullable=False),
        sa.Column("content_version_id", sa.String(64), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("checkpoint_id", sa.String(64), nullable=False),
        sa.Column("conversation_id", sa.String(64), sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"), nullable=False),
        sa.Column("after_step_no", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("source_json", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_taught_knowledge_owner_user_id", "taught_knowledge", ["owner_user_id"])
    op.create_index("ix_taught_knowledge_course_id", "taught_knowledge", ["course_id"])
    op.create_table("channel_presentations",
        sa.Column("conversation_id", sa.String(64), sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("channel_key", sa.String(80), primary_key=True),
        sa.Column("client_seq", sa.Integer(), nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("audio_id", sa.String(64), nullable=False),
        sa.Column("visible_chars", sa.Integer(), nullable=False),
        sa.Column("position_ms", sa.Integer(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False))


def downgrade():
    tables = ("channel_presentations", "taught_knowledge", "segment_coverage",
              "character_expressions", "character_preferences")
    for table in tables:
        if op.get_bind().execute(sa.text(f"SELECT count(*) FROM {table}")).scalar():
            raise RuntimeError("Preserve classroom records before downgrading.")
    if op.get_bind().execute(sa.text(
            "SELECT count(*) FROM character_segments WHERE expression_json <> '{}'")).scalar():
        raise RuntimeError("Preserve frozen character expression before downgrading.")
    for table in tables:
        op.drop_table(table)
    op.drop_column("character_segments", "expression_json")
