"""Durable planning recovery and abandonment."""
from alembic import op
import sqlalchemy as sa

revision = "h2b3c4d5e6f7"
down_revision = "g1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("planning_task_states", sa.Column("abandoned_at", sa.DateTime(timezone=True)))
    op.add_column("planning_task_states", sa.Column("background_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("planning_task_states", sa.Column("auto_retry_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("planning_task_states", sa.Column("answer_draft_json", sa.Text(), nullable=False, server_default="[]"))
    op.add_column("planning_task_states", sa.Column("answer_draft_revision", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    for name in ("answer_draft_revision", "answer_draft_json", "auto_retry_count", "background_enabled", "abandoned_at"):
        op.drop_column("planning_task_states", name)
