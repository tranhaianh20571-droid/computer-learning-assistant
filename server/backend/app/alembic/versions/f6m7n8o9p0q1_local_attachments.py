"""Allow local text attachments without a document provider."""
from alembic import op
import sqlalchemy as sa

revision = "f6m7n8o9p0q1"
down_revision = "e5l6m7n8o9p0"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("document_attachments") as batch:
        batch.alter_column("config_id", existing_type=sa.String(64), nullable=True)
        batch.alter_column("config_version", existing_type=sa.Integer(), nullable=True)


def downgrade():
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT count(*) FROM document_attachments WHERE config_id IS NULL OR config_version IS NULL")):
        raise RuntimeError("Local attachments must be removed before downgrading.")
    with op.batch_alter_table("document_attachments") as batch:
        batch.alter_column("config_id", existing_type=sa.String(64), nullable=False)
        batch.alter_column("config_version", existing_type=sa.Integer(), nullable=False)
