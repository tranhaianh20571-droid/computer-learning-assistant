"""observability_deletion_refs（ADR-022）

Revision ID: a8d0f2b4c6e8
Revises: c7e9f1a3b5d7
Create Date: 2026-09-28

ADR-022：外部观测内容（trace）删除任务登记表；
用户资源删除时登记 pending，清理过程核销，失败可重试。
"""

import sqlalchemy as sa
from alembic import op

revision = "a8d0f2b4c6e8"
down_revision = "c7e9f1a3b5d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "observability_deletion_refs",
        sa.Column("ref_id", sa.String(64), primary_key=True),
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(40), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(30), nullable=False, server_default="langfuse"),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("reason", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_obsdel_owner_resource",
        "observability_deletion_refs",
        ["owner_user_id", "resource_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_obsdel_owner_resource", table_name="observability_deletion_refs")
    op.drop_table("observability_deletion_refs")
