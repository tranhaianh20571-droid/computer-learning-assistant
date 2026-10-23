"""Authenticated native memory delivery metadata; no parallel memory engine."""
from alembic import op
import sqlalchemy as sa

revision = "x8c9d0e1f2a3"
down_revision = "w7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("memory_settings",
        sa.Column("owner_user_id", sa.String(64), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    op.create_table("memory_spaces",
        sa.Column("space_id", sa.String(64), primary_key=True),
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.Column("scope_key", sa.String(100), nullable=False),
        sa.Column("bank_id", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("owner_user_id", "scope_key", name="uq_memory_owner_scope"))
    op.create_index("ix_memory_spaces_owner_user_id", "memory_spaces", ["owner_user_id"])
    op.create_table("memory_sources",
        sa.Column("source_id", sa.String(64), primary_key=True),
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.Column("scope_key", sa.String(100), nullable=False),
        sa.Column("tag", sa.String(100), nullable=False),
        sa.Column("source_kind", sa.String(48), nullable=False),
        sa.Column("source_course_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("operation_id", sa.String(100)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_memory_sources_owner_user_id", "memory_sources", ["owner_user_id"])


def downgrade():
    connection = op.get_bind()
    names = ("memory_sources", "memory_spaces", "memory_settings")
    if any(connection.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {name})")) for name in names):
        raise RuntimeError("refusing nonempty memory downgrade")
    for name in names:
        op.drop_table(name)
