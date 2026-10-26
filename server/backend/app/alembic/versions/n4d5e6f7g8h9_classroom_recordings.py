"""Owned classroom video metadata; media bytes stay in managed object storage."""
import sqlalchemy as sa
from alembic import op

from app.planning.models import (
    ConversationRow,  # noqa: F401 - resolve the migration's foreign key in a fresh process
)
from app.teaching.learning_models import ClassroomRecordingRow

revision = "n4d5e6f7g8h9"
down_revision = "m3c4d5e6f7g8"
branch_labels = None
depends_on = None


def upgrade():
    ClassroomRecordingRow.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM classroom_recordings")):
        raise RuntimeError("Export classroom recordings before downgrade.")
    ClassroomRecordingRow.__table__.drop(op.get_bind(), checkfirst=True)
