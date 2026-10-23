"""Private source collection intent and bounded stage diagnostics."""
from alembic import op
import sqlalchemy as sa

revision = "b2g3h4i5j6k7"
down_revision = "a1f2g3h4i5j6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("character_build_stages",
        sa.Column("state_json",sa.Text(),nullable=False,server_default="{}"))


def downgrade():
    if op.get_bind().scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM character_build_stages WHERE state_json <> '{}')")):
        raise RuntimeError("refusing nonempty research evidence downgrade")
    op.drop_column("character_build_stages","state_json")
