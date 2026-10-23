"""Local resource ledger, durable jobs and installation policy."""
from alembic import op
import sqlalchemy as sa
from app.cleanup.models import LocalResourceRow, LocalResourceJobRow, LocalResourcePolicyRow

revision = "h8o9p0q1r2s3"
down_revision = "g7n8o9p0q1r2"
branch_labels = None
depends_on = None


def upgrade():
    for model in (LocalResourceRow, LocalResourceJobRow, LocalResourcePolicyRow):
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    for name in ("local_resource_objects", "local_resource_jobs"):
        if op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {name}")):
            raise RuntimeError("Export local resource state before downgrading.")
    for model in (LocalResourceJobRow, LocalResourceRow, LocalResourcePolicyRow):
        model.__table__.drop(op.get_bind(), checkfirst=True)
