"""Persistent course review, isolated from optional semantic memory."""
from alembic import op
from app.review.models import ReviewAnswerRow, ReviewCardRow, ReviewLogRow, ReviewSessionRow

revision = "l2b3c4d5e6f7"
down_revision = "k1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    for model in (ReviewCardRow, ReviewSessionRow, ReviewAnswerRow, ReviewLogRow):
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    import sqlalchemy as sa
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM review_sessions")):
        raise RuntimeError("Export review records before downgrade.")
    for model in (ReviewLogRow, ReviewAnswerRow, ReviewSessionRow, ReviewCardRow):
        model.__table__.drop(op.get_bind(), checkfirst=True)
