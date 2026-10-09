"""add lesson title snapshot for course structure graphs"""

from alembic import op
import sqlalchemy as sa


revision = "f4a5b6c7d8e9"
down_revision = "a8d0f2b4c6e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lessons", sa.Column("title_ref", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("lessons", "title_ref")
