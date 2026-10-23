"""Independent frozen unit scopes."""
from alembic import op
import sqlalchemy as sa
from app.teaching.unit_scope import UnitScopeRow

revision = "i9p0q1r2s3t4"
down_revision = "h8o9p0q1r2s3"
branch_labels = None
depends_on = None


def upgrade():
    UnitScopeRow.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM unit_scopes")):
        raise RuntimeError("Export frozen unit scopes before downgrading.")
    UnitScopeRow.__table__.drop(op.get_bind(), checkfirst=True)
