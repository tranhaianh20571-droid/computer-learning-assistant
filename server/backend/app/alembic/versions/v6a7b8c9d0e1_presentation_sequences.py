"""Allow browser timestamp sequence values without losing existing coverage."""
from alembic import op
import sqlalchemy as sa

revision = "v6a7b8c9d0e1"
down_revision = "u5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("segment_coverage", "channel_presentations"):
        op.alter_column(table, "client_seq", existing_type=sa.Integer(),
            type_=sa.BigInteger(), existing_nullable=False)


def downgrade():
    for table in ("segment_coverage", "channel_presentations"):
        if op.get_bind().execute(sa.text(
                f"SELECT count(*) FROM {table} WHERE client_seq > 2147483647")).scalar():
            raise RuntimeError("Preserve presentation sequences before downgrading.")
    for table in ("segment_coverage", "channel_presentations"):
        op.alter_column(table, "client_seq", existing_type=sa.BigInteger(),
            type_=sa.Integer(), existing_nullable=False)
