"""Published system default mentor without per-user copies."""
from alembic import op
import sqlalchemy as sa

revision="a1f2g3h4i5j6"
down_revision="z0e1f2a3b4c5"
branch_labels=None
depends_on=None


def upgrade():
    op.create_table("character_system_default",
        sa.Column("key",sa.String(24),primary_key=True),
        sa.Column("card_id",sa.String(64),sa.ForeignKey("character_cards.card_id"),nullable=False),
        sa.Column("revision",sa.Integer(),nullable=False),
        sa.Column("version",sa.Integer(),nullable=False,server_default="1"))


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM character_system_default)")):
        raise RuntimeError("refusing nonempty system mentor downgrade")
    op.drop_table("character_system_default")
