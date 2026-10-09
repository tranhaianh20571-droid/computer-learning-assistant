"""slice5_cleanup_restore

Revision ID: f7b9d0e2a4c1
Revises: e5a7c9d1b3f6
Create Date: 2026-09-27

切片 5：系统状态（restore_locked）与备份运行账本。
"""

import sqlalchemy as sa
from alembic import op

revision = "f7b9d0e2a4c1"
down_revision = "e5a7c9d1b3f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_state",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "backup_runs",
        sa.Column("run_id", sa.String(length=64), primary_key=True),
        sa.Column("backup_key", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("payload_bytes", sa.Integer(), nullable=False),
        sa.Column("deleted_cutoff_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("backup_runs")
    op.drop_table("system_state")
