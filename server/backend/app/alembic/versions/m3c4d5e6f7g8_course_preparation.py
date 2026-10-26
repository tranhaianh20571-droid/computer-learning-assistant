"""Durable whole-course preparation without starting learning."""
from alembic import op
import sqlalchemy as sa
from app.teaching.preparation_models import (
    CoursePreparationRow, PreparationConnectionRow, PreparationItemRow)

revision = "m3c4d5e6f7g8"
down_revision = "l2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade():
    for model in (CoursePreparationRow, PreparationItemRow, PreparationConnectionRow):
        model.__table__.create(op.get_bind(), checkfirst=True)
    op.add_column("prepared_first_segments",
                  sa.Column("persistent", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT count(*) FROM course_preparations")):
        raise RuntimeError("Export course preparation records before downgrade.")
    if bind.scalar(sa.text("SELECT count(*) FROM prepared_first_segments WHERE persistent")):
        raise RuntimeError("Export persistent first segments before downgrade.")
    op.drop_column("prepared_first_segments", "persistent")
    for model in (PreparationItemRow, CoursePreparationRow, PreparationConnectionRow):
        model.__table__.drop(bind, checkfirst=True)
