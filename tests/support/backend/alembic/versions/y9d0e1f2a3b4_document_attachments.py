"""Personal document parser task attachments."""
from alembic import op
import sqlalchemy as sa

revision = "y9d0e1f2a3b4"
down_revision = "x8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("document_attachments",
        sa.Column("material_id", sa.String(64), sa.ForeignKey("materials.material_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.Column("upload_key", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("config_id", sa.String(64), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("external_job_id", sa.String(128)),
        sa.Column("result_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("failure_code", sa.String(64)),
        sa.UniqueConstraint("owner_user_id", "upload_key", name="uq_attachment_upload"))
    op.create_index("ix_document_attachments_owner_user_id", "document_attachments", ["owner_user_id"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM document_attachments)")):
        raise RuntimeError("refusing nonempty document attachment downgrade")
    op.drop_table("document_attachments")
