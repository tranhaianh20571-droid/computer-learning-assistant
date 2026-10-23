"""Freeze selected study sources in confirmed route and outline versions."""
from alembic import op
import sqlalchemy as sa

revision = "c3h4i5j6k7l8"
down_revision = "b2g3h4i5j6k7"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("route_versions", "outline_versions"):
        op.add_column(table, sa.Column("source_snapshot_json", sa.Text(),
                                      nullable=False, server_default="{}"))


def downgrade():
    for table in ("route_versions", "outline_versions"):
        if op.get_bind().scalar(sa.text(
                f"SELECT EXISTS (SELECT 1 FROM {table} WHERE source_snapshot_json <> '{{}}')")):
            raise RuntimeError("refusing nonempty study source downgrade")
    for table in ("route_versions", "outline_versions"):
        op.drop_column(table, "source_snapshot_json")
