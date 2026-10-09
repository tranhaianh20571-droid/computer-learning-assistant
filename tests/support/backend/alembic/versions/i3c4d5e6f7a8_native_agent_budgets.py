"""Durable task-wide native agent budgets and upstream trace context."""
from alembic import op
import sqlalchemy as sa

revision = "i3c4d5e6f7a8"
down_revision = "h2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("agent_task_budgets",
        sa.Column("task_id", sa.String(64), sa.ForeignKey("learning_tasks.task_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("logical_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wire_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_limit", sa.Integer(), nullable=False, server_default="128"),
        sa.Column("tool_limit", sa.Integer(), nullable=False, server_default="256"),
        sa.Column("traceparent", sa.String(55)))


def downgrade():
    op.drop_table("agent_task_budgets")
