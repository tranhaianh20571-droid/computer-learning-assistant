"""slice1_capability_configs_connectors

Revision ID: b1c2d3e4f5a6
Revises: a42e8c1f0d9a
Create Date: 2026-09-28

切片 1：能力配置、连接器绑定、外发授权、连接器请求。
"""

from alembic import op
import sqlalchemy as sa

revision = "b1c2d3e4f5a6"
down_revision = "a42e8c1f0d9a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "service_configs",
        sa.Column("config_id", sa.String(length=64), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=64), nullable=True),
        sa.Column("owner_scope", sa.String(length=20), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("protocol", sa.String(length=30), nullable=False),
        sa.Column("endpoint", sa.String(length=500), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("encrypted_credentials", sa.Text(), nullable=False),
        sa.Column("credential_mask", sa.String(length=64), nullable=False),
        sa.Column("capability_status", sa.Text(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_service_configs_owner", "service_configs", ["owner_user_id"], unique=False)
    op.create_index(
        "idx_service_configs_kind",
        "service_configs",
        ["owner_scope", "kind", "is_active"],
        unique=False,
    )

    op.create_table(
        "connector_bindings",
        sa.Column("binding_id", sa.String(length=64), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.Column("device_name", sa.String(length=120), nullable=False),
        sa.Column("binding_token_hash", sa.String(length=128), nullable=True, unique=True),
        sa.Column("pairing_code_hash", sa.String(length=128), nullable=True),
        sa.Column("pairing_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_connector_bindings_owner", "connector_bindings", ["owner_user_id"], unique=False)
    op.create_index("idx_connector_bindings_status", "connector_bindings", ["status"], unique=False)

    op.create_table(
        "disclosure_grants",
        sa.Column("grant_id", sa.String(length=64), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("config_id", sa.String(length=64), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("service_kind", sa.String(length=30), nullable=False),
        sa.Column("service_endpoint", sa.String(length=500), nullable=False),
        sa.Column("content_category", sa.String(length=30), nullable=False),
        sa.Column("scope_snapshot", sa.Text(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_disclosure_grants_owner", "disclosure_grants", ["owner_user_id"], unique=False)
    op.create_index("idx_disclosure_grants_task", "disclosure_grants", ["task_id"], unique=False)
    op.create_index("idx_disclosure_grants_config", "disclosure_grants", ["config_id"], unique=False)

    op.create_table(
        "connector_requests",
        sa.Column("request_id", sa.String(length=64), primary_key=True),
        sa.Column("binding_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=False, unique=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_connector_requests_binding", "connector_requests", ["binding_id"], unique=False)
    op.create_index("idx_connector_requests_task", "connector_requests", ["task_id"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_connector_requests_task", table_name="connector_requests")
    op.drop_index("idx_connector_requests_binding", table_name="connector_requests")
    op.drop_table("connector_requests")
    op.drop_index("idx_disclosure_grants_config", table_name="disclosure_grants")
    op.drop_index("idx_disclosure_grants_task", table_name="disclosure_grants")
    op.drop_index("idx_disclosure_grants_owner", table_name="disclosure_grants")
    op.drop_table("disclosure_grants")
    op.drop_index("idx_connector_bindings_status", table_name="connector_bindings")
    op.drop_index("idx_connector_bindings_owner", table_name="connector_bindings")
    op.drop_table("connector_bindings")
    op.drop_index("idx_service_configs_kind", table_name="service_configs")
    op.drop_index("idx_service_configs_owner", table_name="service_configs")
    op.drop_table("service_configs")
