"""Stable source identities, learning evidence and bounded memory maintenance."""
from alembic import op
import sqlalchemy as sa
from app.memory.models import LearningEvidenceRow, MemoryMaintenancePolicy, MemoryMaintenanceJob

revision = "k1a2b3c4d5e6"
down_revision = "j0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if "event_key" not in {c["name"] for c in sa.inspect(bind).get_columns("memory_sources")}:
        op.add_column("memory_sources", sa.Column("event_key", sa.String(64), nullable=True))
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes("memory_sources")}
    if "ix_memory_source_event" not in indexes:
        op.create_index("ix_memory_source_event", "memory_sources", ["owner_user_id", "event_key"], unique=True)
    for model in (LearningEvidenceRow, MemoryMaintenancePolicy, MemoryMaintenanceJob):
        model.__table__.create(bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT count(*) FROM memory_sources WHERE event_key IS NOT NULL")):
        raise RuntimeError("Stable source tombstones cannot be removed by downgrade.")
    for table in ("memory_learning_evidence", "memory_maintenance_jobs"):
        if bind.scalar(sa.text(f"SELECT count(*) FROM {table}")):
            raise RuntimeError("Export learning evidence and maintenance dependencies before downgrading.")
    for model in (MemoryMaintenanceJob, MemoryMaintenancePolicy, LearningEvidenceRow):
        model.__table__.drop(bind, checkfirst=True)
    op.drop_index("ix_memory_source_event", table_name="memory_sources")
    op.drop_column("memory_sources", "event_key")
