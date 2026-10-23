"""Personal AnySearch migration against a disposable synthetic PostgreSQL schema."""
from __future__ import annotations

import importlib.util
import hashlib
import json
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

ROOT = Path(__file__).resolve().parents[1]
REVISION = "n8b9c0d1e2f3"
PATH = ROOT / "server/backend/app/alembic/versions/n8b9c0d1e2f3_anysearch_personal_configs.py"


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
    schema = "anysearch_migration_" + uuid.uuid4().hex
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
                "kind varchar(30) NOT NULL DEFAULT 'search', protocol varchar(30) NOT NULL, "
                "endpoint varchar(500) NOT NULL DEFAULT 'https://api.anysearch.com', "
                "model_name varchar(200) NOT NULL DEFAULT '', encrypted_credentials text NOT NULL DEFAULT 'synthetic-ciphertext', "
                "credential_mask varchar(64) NOT NULL DEFAULT 'sk-ab****xy', "
                "capability_status text NOT NULL DEFAULT '{\"text\":{\"state\":\"available\",\"config_version\": 7}}', "
                "config_version integer NOT NULL DEFAULT 7, "
                "is_active boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP)"))
            connection.execute(text("CREATE TABLE disclosure_grants (grant_id varchar(64) PRIMARY KEY, "
                "config_id varchar(64) NOT NULL, revoked_at timestamptz)"))
            connection.execute(text("CREATE TABLE audit_logs (audit_id varchar(64) PRIMARY KEY, "
                "event varchar(120) NOT NULL, timestamp timestamptz NOT NULL, actor_id varchar(64) NOT NULL, "
                "object_type varchar(40), object_id varchar(64), result varchar(40) NOT NULL CHECK(char_length(result)>0), "
                "trace_id varchar(64), reason varchar(200), admin_id varchar(64), role varchar(40) NOT NULL, "
                "target_actor_id varchar(64), content_hash varchar(32) NOT NULL)"))
        yield isolated
    finally:
        isolated.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def insert(connection, config_id, owner="owner_a", protocol="anysearch", scope="user", **kwargs):
    values = dict(config_id=config_id, owner_user_id=owner, owner_scope=scope, protocol=protocol,
        endpoint="https://api.anysearch.com", kind="search")
    values.update(kwargs)
    names = ",".join(values)
    binds = ",".join(":" + name for name in values)
    connection.execute(text(f"INSERT INTO service_configs ({names}) VALUES ({binds})"), values)


def test_config_revision_is_chained_before_search_tables():
    scripts = ScriptDirectory(str(ROOT / "server/backend/app/alembic"))
    assert scripts.get_revision(REVISION).down_revision == "m7a8b9c0d1e2"
    assert Path(scripts.get_revision(REVISION).path) == PATH


def test_upgrade_preserves_owner_ciphertext_history_and_revokes_legacy_grants(engine):
    with engine.begin() as connection:
        insert(connection, "legacy_admin", None, "tavily_hikari", "admin", endpoint="https://legacy.example.test")
        insert(connection, "legacy_personal", "owner_a", "tavily_hikari")
        insert(connection, "legacy_inactive", "owner_b", "tavily_hikari", is_active=False)
        insert(connection, "valid", "owner_a")
        insert(connection, "admin_invalid", None, scope="admin")
        insert(connection, "orphan_invalid", "missing-owner")
        insert(connection, "endpoint_invalid", "owner_b", endpoint="https://evil.example.test")
        insert(connection, "other_capability", "unregistered-model-owner", "openai", kind="content_model")
        connection.execute(text("INSERT INTO disclosure_grants VALUES "
            "('legacy_grant','legacy_admin',NULL),('valid_grant','valid',NULL),"
            "('other_grant','other_capability',NULL)"))
        before = {row.config_id: (row.owner_user_id, row.owner_scope, row.encrypted_credentials, row.endpoint)
            for row in connection.execute(text("SELECT * FROM service_configs"))}
        migration(connection).upgrade()
        rows = {row.config_id: row for row in connection.execute(text("SELECT * FROM service_configs"))}
        for name, row in rows.items():
            assert (row.owner_user_id, row.owner_scope, row.encrypted_credentials, row.endpoint) == before[name]
        for name in ("legacy_admin", "legacy_personal", "legacy_inactive", "admin_invalid", "orphan_invalid", "endpoint_invalid"):
            assert rows[name].is_active is False and rows[name].config_version == 8
        assert rows["valid"].is_active and rows["valid"].config_version == 7
        assert rows["other_capability"].is_active and rows["other_capability"].config_version == 7
        grants = {row.grant_id: row.revoked_at for row in connection.execute(text("SELECT * FROM disclosure_grants"))}
        assert grants["legacy_grant"] is not None and grants["valid_grant"] is not None and grants["other_grant"] is None
        assert rows["valid"].credential_mask == "****" and rows["valid"].search_test_requested_at is None
        assert rows["valid"].active_search_owner_id == "owner_a"
        indexes = {item["name"]: item for item in inspect(connection).get_indexes("service_configs")}
        assert indexes["uq_service_configs_personal_search"]["unique"] is True
        audits = connection.execute(text("SELECT * FROM audit_logs ORDER BY object_id")).all()
        expected_configs = {"legacy_admin", "legacy_personal", "legacy_inactive", "admin_invalid", "orphan_invalid", "endpoint_invalid"}
        assert {(r.object_type,r.object_id,r.reason) for r in audits} == {
            ("config",name,"legacy_search_config_retired" if name.startswith("legacy") else "invalid_search_config_retired")
            for name in expected_configs
        } | {("disclosure_grant","legacy_grant","search_disclosure_grant_revoked"),
             ("disclosure_grant","valid_grant","search_disclosure_grant_revoked")}
        assert len(audits) == 8
        for audit in audits:
            assert audit.event == "config.changed" and audit.actor_id == "system:migration"
            assert audit.role == "system" and audit.result == "migration_retired"
            assert audit.trace_id is None and audit.admin_id is None and audit.target_actor_id is None
            payload = "|".join((audit.event,audit.timestamp.isoformat(),audit.actor_id,audit.object_type,audit.object_id,audit.result,""))
            assert audit.content_hash == hashlib.sha256(payload.encode()).hexdigest()[:16]
        for name in expected_configs:
            assert json.loads(rows[name].capability_status) == {"text":"unknown"}
        assert json.loads(rows["valid"].capability_status)["text"]["state"] == "available"
        assert json.loads(rows["other_capability"].capability_status)["text"]["state"] == "available"


def test_duplicates_resolved_deterministically_and_downgrade_never_reactivates(engine):
    with engine.begin() as connection:
        insert(connection, "older", created_at="2025-01-01T00:00:00Z")
        insert(connection, "newer", created_at="2026-01-01T00:00:00Z")
        connection.execute(text("INSERT INTO disclosure_grants VALUES ('duplicate_grant','older',NULL)"))
        upgrade = migration(connection)
        upgrade.upgrade()
        assert connection.execute(text("SELECT config_id FROM service_configs WHERE is_active")).scalar_one() == "newer"
        assert connection.execute(text("SELECT config_version FROM service_configs WHERE config_id='older'")).scalar_one() == 8
        assert json.loads(connection.execute(text("SELECT capability_status FROM service_configs WHERE config_id='older'")).scalar_one()) == {"text":"unknown"}
        audits = connection.execute(text("SELECT * FROM audit_logs ORDER BY object_id")).all()
        assert {(row.object_type,row.object_id,row.reason) for row in audits} == {
            ("config","older","duplicate_search_config_retired"),
            ("disclosure_grant","duplicate_grant","search_disclosure_grant_revoked")}
        upgrade.downgrade()
        assert connection.execute(text("SELECT * FROM audit_logs ORDER BY object_id")).all() == audits
        assert connection.execute(text("SELECT config_id FROM service_configs WHERE is_active")).scalar_one() == "newer"
        assert connection.execute(text("SELECT revoked_at FROM disclosure_grants")).scalar_one() is not None
        columns = {column["name"] for column in inspect(connection).get_columns("service_configs")}
        assert "active_search_owner_id" not in columns and "search_test_requested_at" not in columns
        assert "uq_service_configs_personal_search" not in {item["name"] for item in inspect(connection).get_indexes("service_configs")}


@pytest.mark.parametrize("owner,scope", [(None, "user"), ("", "user"), ("missing", "user"), (None, "admin")])
def test_upgraded_database_rejects_invalid_active_owners(engine, owner, scope):
    with engine.begin() as connection:
        migration(connection).upgrade()
    with pytest.raises(IntegrityError), engine.begin() as connection:
        insert(connection, "invalid-new", owner, scope=scope)


def test_partial_unique_index_allows_inactive_history_and_distinct_owners(engine):
    with engine.begin() as connection:
        migration(connection).upgrade()
        insert(connection, "a")
        insert(connection, "b", "owner_b")
        insert(connection, "history", is_active=False)
        insert(connection, "model", protocol="openai", kind="content_model")
    with pytest.raises(IntegrityError), engine.begin() as connection:
        insert(connection, "duplicate")
    with engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM service_configs")).scalar_one() == 4


def test_previously_revoked_grant_is_not_reaudited(engine):
    with engine.begin() as connection:
        insert(connection, "valid")
        connection.execute(text("INSERT INTO disclosure_grants VALUES ('already-revoked','valid',CURRENT_TIMESTAMP)"))
        before = connection.execute(text("SELECT revoked_at FROM disclosure_grants")).scalar_one()
        migration(connection).upgrade()
        assert connection.execute(text("SELECT revoked_at FROM disclosure_grants")).scalar_one() == before
        assert connection.execute(text("SELECT count(*) FROM audit_logs")).scalar_one() == 0


def test_audit_failure_rolls_back_retirement_and_schema(engine):
    with engine.begin() as connection:
        insert(connection, "legacy", protocol="tavily_hikari")
        connection.execute(text("INSERT INTO disclosure_grants VALUES ('legacy-grant','legacy',NULL)"))
        connection.execute(text("ALTER TABLE audit_logs ADD CONSTRAINT reject_migration CHECK(actor_id <> 'system:migration')"))
    with pytest.raises(IntegrityError), engine.begin() as connection:
        migration(connection).upgrade()
    with engine.begin() as connection:
        assert connection.execute(text("SELECT is_active,config_version FROM service_configs")).one() == (True,7)
        assert json.loads(connection.execute(text("SELECT capability_status FROM service_configs")).scalar_one())["text"]["state"] == "available"
        assert connection.execute(text("SELECT revoked_at FROM disclosure_grants")).scalar_one() is None
        assert connection.execute(text("SELECT count(*) FROM audit_logs")).scalar_one() == 0
        assert "search_test_requested_at" not in {c["name"] for c in inspect(connection).get_columns("service_configs")}
