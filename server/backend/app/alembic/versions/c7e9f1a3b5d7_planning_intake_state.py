"""planning_intake_state

Revision ID: c7e9f1a3b5d7
Revises: f7b9d0e2a4c1
Create Date: 2026-09-28

v2 生成前访谈：planning_task_states 增加 intake_json（问题/答案/轮次）。
"""

import sqlalchemy as sa
from alembic import op

revision = "c7e9f1a3b5d7"
down_revision = "f7b9d0e2a4c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("planning_task_states", sa.Column("intake_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("planning_task_states", "intake_json")
