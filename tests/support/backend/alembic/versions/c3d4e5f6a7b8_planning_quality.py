"""Planning quality: constraints, scope contract, coverage and review snapshots."""

from alembic import op
import sqlalchemy as sa

revision = "c3d4e5f6a7b8"
down_revision = "c3h4i5j6k7l8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("planning_task_states",
        sa.Column("constraints_json", sa.Text(), nullable=False, server_default="{}"))
    op.add_column("planning_task_states",
        sa.Column("scope_contract_json", sa.Text(), nullable=True))
    op.add_column("planning_task_states",
        sa.Column("coverage_json", sa.Text(), nullable=True))
    op.add_column("planning_task_states",
        sa.Column("review_json", sa.Text(), nullable=True))
    op.add_column("planning_drafts",
        sa.Column("coverage_json", sa.Text(), nullable=True))
    op.add_column("planning_drafts",
        sa.Column("review_json", sa.Text(), nullable=True))
    op.add_column("route_versions",
        sa.Column("scope_snapshot_json", sa.Text(), nullable=True))
    op.add_column("route_versions",
        sa.Column("coverage_snapshot_json", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("route_versions", "coverage_snapshot_json")
    op.drop_column("route_versions", "scope_snapshot_json")
    op.drop_column("planning_drafts", "review_json")
    op.drop_column("planning_drafts", "coverage_json")
    op.drop_column("planning_task_states", "review_json")
    op.drop_column("planning_task_states", "coverage_json")
    op.drop_column("planning_task_states", "scope_contract_json")
    op.drop_column("planning_task_states", "constraints_json")
