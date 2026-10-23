"""Persist reasoning settings/evidence and private planning process snapshots.

Revision ID: q1e2f3a4b5c6
Revises: p0d1e2f3a4b5
"""
from alembic import op
import sqlalchemy as sa

revision = "q1e2f3a4b5c6"
down_revision = "p0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("service_configs", sa.Column("reasoning_settings", sa.Text(), nullable=False,
        server_default='{"mode":"default","show_summary":true}'))
    op.add_column("service_configs", sa.Column("reasoning_validation", sa.Text(), nullable=False, server_default="{}"))
    op.add_column("service_configs", sa.Column("reasoning_test_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("planning_task_states", sa.Column("process_json", sa.Text(), nullable=False, server_default="{}"))


def downgrade():
    op.drop_column("planning_task_states", "process_json")
    op.drop_column("service_configs", "reasoning_test_requested_at")
    op.drop_column("service_configs", "reasoning_validation")
    op.drop_column("service_configs", "reasoning_settings")
