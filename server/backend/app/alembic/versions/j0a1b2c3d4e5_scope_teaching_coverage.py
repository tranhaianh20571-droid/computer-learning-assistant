"""Bind lesson checkpoints and taught records to frozen scope capabilities."""
from alembic import op
import sqlalchemy as sa

revision = "j0a1b2c3d4e5"
down_revision = "i9p0q1r2s3t4"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    step_columns = {column["name"] for column in sa.inspect(bind).get_columns("teaching_steps")}
    taught_columns = {column["name"] for column in sa.inspect(bind).get_columns("taught_knowledge")}
    if "covered_scope_capability_ids_json" not in step_columns:
        op.add_column("teaching_steps", sa.Column(
            "covered_scope_capability_ids_json", sa.Text(), nullable=False, server_default="[]"))
        op.alter_column("teaching_steps", "covered_scope_capability_ids_json", server_default=None)
    if "scope_hash" not in taught_columns:
        op.add_column("taught_knowledge", sa.Column(
            "scope_hash", sa.String(length=64), nullable=False, server_default=""))
        op.alter_column("taught_knowledge", "scope_hash", server_default=None)
    if "scope_capability_ids_json" not in taught_columns:
        op.add_column("taught_knowledge", sa.Column(
            "scope_capability_ids_json", sa.Text(), nullable=False, server_default="[]"))
        op.alter_column("taught_knowledge", "scope_capability_ids_json", server_default=None)


def downgrade():
    bind = op.get_bind()
    if bind.scalar(sa.text(
            "SELECT count(*) FROM taught_knowledge WHERE scope_hash <> '' OR scope_capability_ids_json <> '[]'")):
        raise RuntimeError("Export scoped teaching coverage before downgrading.")
    if bind.scalar(sa.text(
            "SELECT count(*) FROM teaching_steps WHERE covered_scope_capability_ids_json <> '[]'")):
        raise RuntimeError("Export scoped teaching steps before downgrading.")
    op.drop_column("taught_knowledge", "scope_capability_ids_json")
    op.drop_column("taught_knowledge", "scope_hash")
    op.drop_column("teaching_steps", "covered_scope_capability_ids_json")
