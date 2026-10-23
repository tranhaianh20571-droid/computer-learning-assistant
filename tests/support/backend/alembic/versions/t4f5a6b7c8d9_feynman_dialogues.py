"""Persist knowledge checkpoints and independent dialogue/audio turns."""
from alembic import op
import sqlalchemy as sa

revision = "t4f5a6b7c8d9"
down_revision = "s3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("feynman_sessions",
        sa.Column("session_id", sa.String(64), primary_key=True),
        sa.Column("conversation_id", sa.String(64), sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.Column("checkpoint_id", sa.String(64), nullable=False),
        sa.Column("after_step_no", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("context_json", sa.Text(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("probe_count", sa.Integer(), nullable=False),
        sa.Column("result", sa.String(24), nullable=False),
        sa.Column("task_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "checkpoint_id", name="uq_feynman_checkpoint"))
    op.create_index("idx_feynman_owner", "feynman_sessions", ["owner_user_id"])
    op.create_table("feynman_turns",
        sa.Column("turn_id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(64), sa.ForeignKey("feynman_sessions.session_id", ondelete="CASCADE"), nullable=False),
        sa.Column("turn_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("criteria_json", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("feedback", sa.Text(), nullable=False),
        sa.Column("public_text", sa.Text(), nullable=False),
        sa.Column("evaluation_json", sa.Text(), nullable=False),
        sa.Column("playback_json", sa.Text(), nullable=False),
        sa.Column("audio_id", sa.String(64), sa.ForeignKey("audio_assets.audio_id")),
        sa.Column("audio_task_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "turn_no", name="uq_feynman_turn"))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT count(*) FROM feynman_sessions")).scalar():
        raise RuntimeError("Preserve knowledge dialogue records before downgrading.")
    op.drop_table("feynman_turns")
    op.drop_index("idx_feynman_owner", table_name="feynman_sessions")
    op.drop_table("feynman_sessions")
