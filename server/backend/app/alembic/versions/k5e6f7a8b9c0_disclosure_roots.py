"""Bind derived disclosure grants to revocable root consent."""
from alembic import op
import sqlalchemy as sa

revision = "k5e6f7a8b9c0"
down_revision = "j4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("disclosure_grants", sa.Column("parent_grant_id", sa.String(64), nullable=True))
    op.create_index("ix_disclosure_grants_parent_grant_id", "disclosure_grants", ["parent_grant_id"])
    op.create_foreign_key("fk_disclosure_grants_parent", "disclosure_grants", "disclosure_grants",
                          ["parent_grant_id"], ["grant_id"], ondelete="CASCADE")


def downgrade():
    op.drop_constraint("fk_disclosure_grants_parent", "disclosure_grants", type_="foreignkey")
    op.drop_index("ix_disclosure_grants_parent_grant_id", table_name="disclosure_grants")
    op.drop_column("disclosure_grants", "parent_grant_id")
