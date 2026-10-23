"""Allow trusted guest search owners without creating authentication accounts."""
from alembic import op
import sqlalchemy as sa

revision = "p0d1e2f3a4b5"
down_revision = "o9c0d1e2f3a4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("search_config_owners",
        sa.Column("owner_user_id", sa.String(64), primary_key=True),
        sa.CheckConstraint("owner_user_id <> ''", name="ck_search_config_owners_nonempty"))
    # Keep the backfill and FK replacement fenced against concurrent config writes.
    op.execute(sa.text("LOCK TABLE service_configs IN ACCESS EXCLUSIVE MODE"))
    op.execute(sa.text("""
        INSERT INTO search_config_owners (owner_user_id)
        SELECT DISTINCT owner_user_id FROM service_configs
        WHERE owner_scope = 'user' AND kind = 'search' AND protocol = 'anysearch'
          AND owner_user_id IS NOT NULL AND owner_user_id <> ''
    """))
    # Inactive owners are included for serialization of their retained cooldown.
    # No config, probe metadata, ciphertext, version, grant or audit is rewritten.
    op.drop_constraint("fk_service_configs_search_owner", "service_configs", type_="foreignkey")
    op.create_foreign_key("fk_service_configs_search_owner", "service_configs", "search_config_owners",
        ["active_search_owner_id"], ["owner_user_id"])


def downgrade():
    op.execute(sa.text("LOCK TABLE service_configs IN ACCESS EXCLUSIVE MODE"))
    incompatible = op.get_bind().execute(sa.text("""
        SELECT EXISTS (
            SELECT 1 FROM service_configs c
            WHERE c.active_search_owner_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM users u WHERE u.user_id = c.active_search_owner_id)
        )
    """)).scalar_one()
    if incompatible:
        raise RuntimeError("cannot downgrade search guest owners while active search owners lack auth users")
    op.drop_constraint("fk_service_configs_search_owner", "service_configs", type_="foreignkey")
    op.create_foreign_key("fk_service_configs_search_owner", "service_configs", "users",
        ["active_search_owner_id"], ["user_id"])
    op.drop_table("search_config_owners")
