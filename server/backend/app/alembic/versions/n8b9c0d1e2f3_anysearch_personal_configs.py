"""Migrate legacy search settings to private, unique, owner-fenced AnySearch configs."""
from alembic import op
import hashlib
import uuid
import sqlalchemy as sa

revision = "n8b9c0d1e2f3"
down_revision = "m7a8b9c0d1e2"
branch_labels = None
depends_on = None

PREDICATE = "owner_scope = 'user' AND kind = 'search' AND protocol = 'anysearch' AND is_active"
OWNER_EXPRESSION = "CASE WHEN kind = 'search' AND protocol = 'anysearch' AND is_active THEN owner_user_id ELSE NULL END"
OWNER_CHECK = "NOT (kind = 'search' AND protocol = 'anysearch' AND is_active) OR (owner_scope = 'user' AND owner_user_id IS NOT NULL AND owner_user_id <> '')"


def _audit_changed_rows(statement, *, object_type, reason):
    bind = op.get_bind()
    rows = bind.execute(sa.text(statement)).mappings().all()
    for row in rows:
        payload = "|".join(("config.changed", row["now"].isoformat(),
            "system:migration", object_type, row["object_id"], "migration_retired", ""))
        values = dict(row, audit_id="aud_" + uuid.uuid4().hex, object_type=object_type,
            reason=reason, content_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16])
        bind.execute(sa.text("""
            INSERT INTO audit_logs (audit_id,event,timestamp,actor_id,object_type,
                object_id,result,reason,role,content_hash)
            VALUES (:audit_id,'config.changed',:now,'system:migration',:object_type,
                :object_id,'migration_retired',:reason,'system',:content_hash)
        """), values)


def upgrade():
    # History, encrypted keys, endpoints and owners are preserved. Never promote
    # an administrator configuration or copy its secret into a learner account.
    _audit_changed_rows("UPDATE service_configs SET is_active = false, "
        "config_version = config_version + 1, capability_status = '{\"text\":\"unknown\"}', updated_at = CURRENT_TIMESTAMP "
        "WHERE protocol = 'tavily_hikari' RETURNING config_id AS object_id, CURRENT_TIMESTAMP AS now",
        object_type="config", reason="legacy_search_config_retired")
    _audit_changed_rows("UPDATE service_configs SET is_active = false, "
        "config_version = config_version + 1, capability_status = '{\"text\":\"unknown\"}', updated_at = CURRENT_TIMESTAMP "
        "WHERE kind = 'search' AND protocol = 'anysearch' AND is_active AND ("
        "owner_scope <> 'user' OR owner_user_id IS NULL OR owner_user_id = '' OR "
        "NOT EXISTS (SELECT 1 FROM users WHERE users.user_id = service_configs.owner_user_id) OR "
        "endpoint <> 'https://api.anysearch.com' OR model_name <> '') "
        "RETURNING config_id AS object_id, CURRENT_TIMESTAMP AS now",
        object_type="config", reason="invalid_search_config_retired")
    # Deterministically retain only the newest otherwise-valid personal config.
    _audit_changed_rows("WITH ranked AS (SELECT config_id, ROW_NUMBER() OVER ("
        "PARTITION BY owner_user_id ORDER BY created_at DESC, config_id DESC) AS n "
        "FROM service_configs WHERE " + PREDICATE + ") "
        "UPDATE service_configs SET is_active = false, config_version = config_version + 1, "
        "capability_status = '{\"text\":\"unknown\"}', "
        "updated_at = CURRENT_TIMESTAMP WHERE config_id IN (SELECT config_id FROM ranked WHERE n > 1) "
        "RETURNING config_id AS object_id, CURRENT_TIMESTAMP AS now",
        object_type="config", reason="duplicate_search_config_retired")
    _audit_changed_rows("UPDATE disclosure_grants SET revoked_at = CURRENT_TIMESTAMP "
        "WHERE revoked_at IS NULL AND config_id IN (SELECT config_id FROM service_configs "
        "WHERE protocol = 'tavily_hikari' OR (kind = 'search' AND protocol = 'anysearch')) "
        "RETURNING grant_id AS object_id, CURRENT_TIMESTAMP AS now",
        object_type="disclosure_grant", reason="search_disclosure_grant_revoked")
    op.execute(sa.text("UPDATE service_configs SET credential_mask = '****' WHERE kind = 'search'"))
    op.add_column("service_configs", sa.Column("search_test_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("service_configs", sa.Column("active_search_owner_id", sa.String(64),
        sa.Computed(OWNER_EXPRESSION, persisted=True), nullable=True))
    op.create_check_constraint("ck_service_configs_search_owner", "service_configs", OWNER_CHECK)
    op.create_foreign_key("fk_service_configs_search_owner", "service_configs", "users",
        ["active_search_owner_id"], ["user_id"])
    op.create_index("uq_service_configs_personal_search", "service_configs", ["owner_user_id"],
        unique=True, postgresql_where=sa.text(PREDICATE), sqlite_where=sa.text(PREDICATE))


def downgrade():
    # Deliberately do not reactivate revoked provider configs or disclosure grants.
    op.drop_index("uq_service_configs_personal_search", table_name="service_configs")
    op.drop_constraint("fk_service_configs_search_owner", "service_configs", type_="foreignkey")
    op.drop_constraint("ck_service_configs_search_owner", "service_configs", type_="check")
    op.drop_column("service_configs", "active_search_owner_id")
    op.drop_column("service_configs", "search_test_requested_at")
