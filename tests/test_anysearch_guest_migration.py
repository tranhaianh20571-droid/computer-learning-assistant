"""Guest AnySearch owner migration using disposable synthetic PostgreSQL schemas."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from backend.logging_audit.db import get_database_url
from backend.slice1.models import (
    ACTIVE_SEARCH_OWNER_CHECK, ACTIVE_SEARCH_OWNER_EXPRESSION, ACTIVE_SEARCH_PREDICATE,
    SearchConfigOwnerRow, ServiceConfigRow,
)

ROOT = Path(__file__).resolve().parents[1]
REVISION = "p0d1e2f3a4b5"
PATH = ROOT / "server/backend/app/alembic/versions/p0d1e2f3a4b5_search_guest_owners.py"
GUEST = "guest_" + "a" * 32
OTHER_GUEST = "guest_" + "b" * 32


def migration(connection):
    spec = importlib.util.spec_from_file_location(REVISION, PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


@pytest.fixture
def engine():
    url = make_url(get_database_url())
    assert url.get_backend_name() == "postgresql", "run using the isolated native regression harness"
    schema = "anysearch_guest_migration_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = create_engine(url.update_query_dict({"options": "-csearch_path=" + schema}))
    try:
        with isolated.begin() as connection:
            connection.execute(text("CREATE TABLE users (user_id varchar(64) PRIMARY KEY)"))
            connection.execute(text("INSERT INTO users VALUES ('owner_a'),('owner_b')"))
            connection.execute(text("CREATE TABLE service_configs (config_id varchar(64) PRIMARY KEY, "
                "owner_user_id varchar(64), owner_scope varchar(20) NOT NULL DEFAULT 'user', "
                "kind varchar(30) NOT NULL DEFAULT 'search', protocol varchar(30) NOT NULL DEFAULT 'anysearch', "
                "endpoint varchar(500) NOT NULL DEFAULT 'https://api.anysearch.com', "
                "model_name varchar(200) NOT NULL DEFAULT '', encrypted_credentials text NOT NULL DEFAULT 'synthetic-ciphertext', "
                "credential_mask varchar(64) NOT NULL DEFAULT '****', "
                "capability_status text NOT NULL DEFAULT '{}', config_version integer NOT NULL DEFAULT 7, "
                "is_active boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP, search_test_requested_at timestamptz, "
                "active_search_owner_id varchar(64) GENERATED ALWAYS AS (" + ACTIVE_SEARCH_OWNER_EXPRESSION + ") STORED, "
                "CONSTRAINT ck_service_configs_search_owner CHECK (" + ACTIVE_SEARCH_OWNER_CHECK + "), "
                "CONSTRAINT fk_service_configs_search_owner FOREIGN KEY (active_search_owner_id) REFERENCES users(user_id))"))
            connection.execute(text("CREATE UNIQUE INDEX uq_service_configs_personal_search ON service_configs "
                "(owner_user_id) WHERE " + ACTIVE_SEARCH_PREDICATE))
            connection.execute(text("CREATE TABLE disclosure_grants (grant_id varchar(64) PRIMARY KEY, "
                "config_id varchar(64) NOT NULL, revoked_at timestamptz)"))
            connection.execute(text("CREATE TABLE audit_logs (audit_id varchar(64) PRIMARY KEY, event text NOT NULL)"))
            connection.execute(text("CREATE TABLE search_calls (query_id varchar(64) PRIMARY KEY, "
                "config_id varchar(64) NOT NULL REFERENCES service_configs(config_id), config_version integer NOT NULL, state text NOT NULL)"))
        yield isolated
    finally:
        isolated.dispose()
        assert schema.startswith("anysearch_guest_migration_") and len(schema) == 58
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def insert(connection, config_id, owner="owner_a", **kwargs):
    values = dict(config_id=config_id, owner_user_id=owner)
    values.update(kwargs)
    names = ",".join(values)
    binds = ",".join(":" + name for name in values)
    connection.execute(text(f"INSERT INTO service_configs ({names}) VALUES ({binds})"), values)


def snapshot(connection):
    return {name: connection.execute(text(f"SELECT * FROM {name} ORDER BY 1")).all()
        for name in ("users", "service_configs", "disclosure_grants", "audit_logs", "search_calls")}


def assert_fk(connection, table, column):
    fk, = inspect(connection).get_foreign_keys("service_configs")
    assert fk["name"] == "fk_service_configs_search_owner"
    assert fk["constrained_columns"] == ["active_search_owner_id"]
    assert fk["referred_table"] == table and fk["referred_columns"] == [column]


def test_revision_and_orm_contract():
    scripts = ScriptDirectory(str(ROOT / "server/backend/app/alembic"))
    assert scripts.get_revision(REVISION).down_revision == "o9c0d1e2f3a4"
    assert Path(scripts.get_revision(REVISION).path) == PATH
    owner = SearchConfigOwnerRow.__table__
    assert owner.name == "search_config_owners"
    assert list(owner.c.keys()) == ["owner_user_id"]
    assert owner.c.owner_user_id.type.length == 64 and owner.c.owner_user_id.primary_key
    assert not owner.c.owner_user_id.nullable and not owner.foreign_keys
    fk, = ServiceConfigRow.__table__.c.active_search_owner_id.foreign_keys
    assert fk.target_fullname == "search_config_owners.owner_user_id"


def test_upgrade_backfills_active_and_inactive_owners_and_preserves_all_rows(engine):
    with engine.begin() as connection:
        insert(connection, "active", capability_status='{"text":{"state":"available","config_version":7,"tested_at":"2026-01-01T00:00:00Z"}}',
            search_test_requested_at="2026-01-01T00:00:00Z")
        insert(connection, "inactive", "owner_b", is_active=False, search_test_requested_at="2026-01-02T00:00:00Z")
        insert(connection, "guest-history", GUEST, is_active=False, search_test_requested_at="2026-01-03T00:00:00Z")
        insert(connection, "guest-history-2", GUEST, is_active=False)
        insert(connection, "null-history", None, is_active=False)
        insert(connection, "empty-history", "", is_active=False)
        insert(connection, "admin-history", "admin-history-owner", owner_scope="admin", is_active=False)
        insert(connection, "legacy-history", "legacy-owner", protocol="tavily_hikari", is_active=False)
        insert(connection, "model", "model-owner", kind="content_model", protocol="openai")
        connection.execute(text("INSERT INTO disclosure_grants VALUES ('grant','active',NULL)"))
        connection.execute(text("INSERT INTO audit_logs VALUES ('audit','synthetic.existing')"))
        connection.execute(text("INSERT INTO search_calls VALUES ('query','active',7,'succeeded')"))
        before = snapshot(connection)
        generated = inspect(connection).get_columns("service_configs")[-1]["computed"]
        migration(connection).upgrade()
        assert snapshot(connection) == before
        assert connection.execute(text("SELECT owner_user_id FROM search_config_owners ORDER BY 1")).scalars().all() == [GUEST, "owner_a", "owner_b"]
        assert_fk(connection, "search_config_owners", "owner_user_id")
        assert inspect(connection).get_columns("service_configs")[-1]["computed"] == generated
        assert "ck_service_configs_search_owner" in {c["name"] for c in inspect(connection).get_check_constraints("service_configs")}
        index, = inspect(connection).get_indexes("service_configs")
        assert index["name"] == "uq_service_configs_personal_search" and index["unique"]


def test_guest_owner_needs_no_auth_user_and_generated_column_follows_activation(engine):
    with engine.begin() as connection:
        migration(connection).upgrade()
        connection.execute(text("INSERT INTO search_config_owners VALUES (:owner),(:other)"), {"owner": GUEST, "other": OTHER_GUEST})
        insert(connection, "guest", GUEST)
        insert(connection, "other", OTHER_GUEST)
        insert(connection, "history", GUEST, is_active=False)
        assert connection.execute(text("SELECT active_search_owner_id FROM service_configs WHERE config_id='guest'")).scalar_one() == GUEST
        connection.execute(text("UPDATE service_configs SET is_active=false WHERE config_id='guest'"))
        assert connection.execute(text("SELECT active_search_owner_id FROM service_configs WHERE config_id='guest'")).scalar_one() is None
        connection.execute(text("UPDATE service_configs SET is_active=true WHERE config_id='history'"))
        assert connection.execute(text("SELECT count(*) FROM users WHERE user_id LIKE 'guest_%'")).scalar_one() == 0


@pytest.mark.parametrize("owner,scope", [(None, "user"), ("", "user"), ("missing", "user"), (GUEST, "admin")])
def test_active_search_owner_constraints_remain_enforced(engine, owner, scope):
    with engine.begin() as connection:
        migration(connection).upgrade()
        connection.execute(text("INSERT INTO search_config_owners VALUES (:owner)"), {"owner": GUEST})
    with pytest.raises(IntegrityError), engine.begin() as connection:
        insert(connection, "invalid", owner, owner_scope=scope)


@pytest.mark.parametrize("statement", [
    "INSERT INTO search_config_owners VALUES ('')",
    "INSERT INTO search_config_owners VALUES (NULL)",
    "INSERT INTO search_config_owners VALUES ('owner_a')",
    "INSERT INTO service_configs (config_id,owner_user_id) VALUES ('duplicate','owner_a')",
    "DELETE FROM search_config_owners WHERE owner_user_id='owner_a'",
])
def test_owner_rows_and_active_search_uniqueness_remain_constrained(engine, statement):
    with engine.begin() as connection:
        insert(connection, "active")
        migration(connection).upgrade()
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text(statement))


def test_downgrade_refuses_active_guest_before_any_ddl(engine):
    with engine.begin() as connection:
        migration(connection).upgrade()
        connection.execute(text("INSERT INTO search_config_owners VALUES (:owner)"), {"owner": GUEST})
        insert(connection, "guest", GUEST)
        before = snapshot(connection)
        module = migration(connection)
        with pytest.raises(RuntimeError, match="active search owners lack auth users"):
            module.downgrade()
        # The connection is still usable: refusal precedes any DDL or SQL error.
        assert snapshot(connection) == before
        assert inspect(connection).has_table("search_config_owners")
        assert_fk(connection, "search_config_owners", "owner_user_id")


def test_safe_downgrade_restores_auth_fk_without_rewriting_inactive_guest_history(engine):
    with engine.begin() as connection:
        insert(connection, "active")
        insert(connection, "guest-history", GUEST, is_active=False, search_test_requested_at="2026-01-03T00:00:00Z")
        module = migration(connection)
        module.upgrade()
        before = snapshot(connection)
        module.downgrade()
        assert snapshot(connection) == before
        assert not inspect(connection).has_table("search_config_owners")
        assert_fk(connection, "users", "user_id")
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text("UPDATE service_configs SET is_active=true WHERE config_id='guest-history'"))


def test_upgrade_failure_rolls_back_owner_table_and_fk_replacement(engine, monkeypatch):
    with engine.begin() as connection:
        insert(connection, "active")
        before = snapshot(connection)
    with pytest.raises(RuntimeError, match="synthetic fk replacement failure"), engine.begin() as connection:
        module = migration(connection)
        def fail(*args, **kwargs):
            raise RuntimeError("synthetic fk replacement failure")
        monkeypatch.setattr(module.op, "create_foreign_key", fail)
        module.upgrade()
    with engine.begin() as connection:
        assert snapshot(connection) == before
        assert not inspect(connection).has_table("search_config_owners")
        assert_fk(connection, "users", "user_id")
