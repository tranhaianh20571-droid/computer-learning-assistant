"""Private source imports and reader profiles; no existing content is resent."""
from alembic import op
from app.materials.source_models import (SourceProfileRow, SourceBatchRow, SourceItemRow, ImportedSourceRow, SourceChatTurnRow)

revision = "g7n8o9p0q1r2"
down_revision = "f6m7n8o9p0q1"
branch_labels = None
depends_on = None


def upgrade():
    for model in (SourceProfileRow, SourceBatchRow, SourceItemRow, ImportedSourceRow, SourceChatTurnRow):
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    import sqlalchemy as sa
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM imported_sources")):
        raise RuntimeError("Remove imported sources before downgrading.")
    for model in (SourceChatTurnRow, ImportedSourceRow, SourceItemRow, SourceBatchRow, SourceProfileRow):
        model.__table__.drop(op.get_bind(), checkfirst=True)
