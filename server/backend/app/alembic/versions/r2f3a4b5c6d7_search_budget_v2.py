"""Persist shared search retries and six wire requests; keep historical windows.

Revision ID: r2f3a4b5c6d7
Revises: q1e2f3a4b5c6
"""
from alembic import op
import sqlalchemy as sa

revision = "r2f3a4b5c6d7"
down_revision = "q1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade():
    for table, name, counts in (
        ("search_task_budgets", "ck_search_budget_counts", "logical_count >= 0 AND logical_count <= 3"),
        ("search_calls", "ck_search_call_counts", "logical_slot >= 1 AND logical_slot <= 3"),
    ):
        op.add_column(table, sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"))
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, counts + " AND wire_count >= 0 AND wire_count <= 6 AND retry_count >= 0 AND retry_count <= 5")
    op.alter_column("search_task_budgets", "budget_policy_version", server_default="personal-v2")


def downgrade():
    # Do not discard consumed budgets to force them into the old three-wire policy.
    connection = op.get_bind()
    for table in ("search_task_budgets", "search_calls"):
        if connection.scalar(sa.text(f"SELECT count(*) FROM {table} WHERE wire_count > 3 OR retry_count > 0")):
            raise RuntimeError("search_budget_v2_downgrade_requires_empty_new_budgets")
    for table, name, counts in (
        ("search_task_budgets", "ck_search_budget_counts", "logical_count >= 0 AND logical_count <= 3"),
        ("search_calls", "ck_search_call_counts", "logical_slot >= 1 AND logical_slot <= 3"),
    ):
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, counts + " AND wire_count >= 0 AND wire_count <= 3")
        op.drop_column(table, "retry_count")
    op.alter_column("search_task_budgets", "budget_policy_version", server_default="personal-v1")
