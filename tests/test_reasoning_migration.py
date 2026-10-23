"""Additive reasoning/process migration preserves synthetic existing records."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from backend.logging_audit.db import get_database_url
from backend.slice1.models import ServiceConfigRow
from backend.slice1.reasoning import DEFAULT_SETTINGS

ROOT = Path(__file__).resolve().parents[1]
REVISION = "q1e2f3a4b5c6"
PATH = ROOT / "server/backend/app/alembic/versions/q1e2f3a4b5c6_reasoning_process.py"


def migration(connection):
    spec = importlib.util.spec_from_file_location(REVISION, PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


@pytest.fixture
def engine():
    url = make_url(get_database_url())
    assert url.get_backend_name() == "postgresql", "use the isolated native regression harness"
    schema = "reasoning_migration_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = create_engine(url.update_query_dict({"options": "-csearch_path=" + schema}))
    try:
        with isolated.begin() as connection:
            connection.execute(text("CREATE TABLE service_configs (config_id varchar(64) PRIMARY KEY, owner_user_id varchar(64), "
                "kind varchar(30) NOT NULL, protocol varchar(30) NOT NULL, endpoint varchar(500) NOT NULL, "
                "model_name varchar(200) NOT NULL, encrypted_credentials text NOT NULL, "
                "capability_status text NOT NULL DEFAULT '{}', config_version integer NOT NULL, "
                "is_active boolean NOT NULL DEFAULT true, search_test_requested_at timestamptz)"))
            connection.execute(text("CREATE TABLE planning_task_states (task_id varchar(64) PRIMARY KEY, "
                "owner_user_id varchar(64) NOT NULL, state varchar(30) NOT NULL, payload_json text NOT NULL)"))
            connection.execute(text("CREATE TABLE audit_logs (audit_id varchar(64) PRIMARY KEY, event text NOT NULL)"))
            connection.execute(text("CREATE TABLE disclosure_grants (grant_id varchar(64) PRIMARY KEY, "
                "config_id varchar(64) REFERENCES service_configs(config_id), revoked_at timestamptz)"))
            connection.execute(text("CREATE TABLE search_calls (query_id varchar(64) PRIMARY KEY, "
                "config_id varchar(64) REFERENCES service_configs(config_id), config_version integer, state text)"))
            connection.execute(text("INSERT INTO service_configs VALUES "
                "('model','synthetic-owner','content_model','openai','https://relay.invalid/v1','synthetic-model',"
                "'synthetic-ciphertext','{\"text\":{\"state\":\"available\"}}',7,true,NULL),"
                "('search','synthetic-owner','search','anysearch','https://api.anysearch.com','',"
                "'synthetic-search-ciphertext','{}',3,true,'2026-01-01T00:00:00Z')"))
            connection.execute(text("INSERT INTO planning_task_states VALUES ('task','synthetic-owner','generating','{}')"))
            connection.execute(text("INSERT INTO audit_logs VALUES ('audit','synthetic.existing')"))
            connection.execute(text("INSERT INTO disclosure_grants VALUES ('grant','search',NULL)"))
            connection.execute(text("INSERT INTO search_calls VALUES ('query','search',3,'succeeded')"))
        yield isolated
    finally:
        isolated.dispose()
        assert schema.startswith("reasoning_migration_") and len(schema) == 52
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def snapshot(connection):
    return {table: connection.execute(text("SELECT " + columns + " FROM " + table + " ORDER BY 1")).all()
        for table, columns in {
            "service_configs": "config_id,owner_user_id,kind,protocol,endpoint,model_name,encrypted_credentials,capability_status,config_version,is_active,search_test_requested_at",
            "planning_task_states": "task_id,owner_user_id,state,payload_json", "audit_logs": "*",
            "disclosure_grants": "*", "search_calls": "*"}.items()}


def test_revision_and_orm_contract():
    scripts = ScriptDirectory(str(ROOT / "server/backend/app/alembic"))
    assert len(scripts.get_heads()) == 1
    assert REVISION in {item.revision for item in scripts.walk_revisions()}
    assert scripts.get_revision(REVISION).down_revision == "p0d1e2f3a4b5"
    assert Path(scripts.get_revision(REVISION).path) == PATH
    for name in ("reasoning_settings", "reasoning_validation"):
        column = ServiceConfigRow.__table__.c[name]
        assert not column.nullable and column.server_default is not None
    assert ServiceConfigRow.__table__.c.reasoning_test_requested_at.nullable


def test_upgrade_defaults_roundtrip_and_downgrade_preserve_records(engine):
    with engine.begin() as connection:
        before = snapshot(connection)
        module = migration(connection)
        module.upgrade()
        assert snapshot(connection) == before
        for settings, validation, requested in connection.execute(text(
                "SELECT reasoning_settings,reasoning_validation,reasoning_test_requested_at FROM service_configs")):
            assert json.loads(settings) == DEFAULT_SETTINGS and validation == "{}" and requested is None
        assert connection.execute(text("SELECT process_json FROM planning_task_states")).scalar_one() == "{}"
        selected = {"mode": "effort", "effort": "high", "show_summary": False}
        connection.execute(text("UPDATE service_configs SET reasoning_settings=:settings WHERE config_id='model'"), {"settings": json.dumps(selected)})
        assert json.loads(connection.execute(text("SELECT reasoning_settings FROM service_configs WHERE config_id='model'")).scalar_one()) == selected
        module.downgrade()
        assert snapshot(connection) == before
        assert "reasoning_settings" not in {c["name"] for c in inspect(connection).get_columns("service_configs")}
        assert "process_json" not in {c["name"] for c in inspect(connection).get_columns("planning_task_states")}


def test_failed_upgrade_rolls_back_added_columns_and_records(engine, monkeypatch):
    with engine.begin() as connection:
        before = snapshot(connection)
    with pytest.raises(RuntimeError, match="synthetic migration failure"), engine.begin() as connection:
        module = migration(connection)
        real = module.op.add_column
        def fail(table, column):
            if table == "planning_task_states":
                raise RuntimeError("synthetic migration failure")
            return real(table, column)
        monkeypatch.setattr(module.op, "add_column", fail)
        module.upgrade()
    with engine.begin() as connection:
        assert snapshot(connection) == before
        assert "reasoning_settings" not in {c["name"] for c in inspect(connection).get_columns("service_configs")}
