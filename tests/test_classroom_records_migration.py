"""Exercise the production migration and nonempty downgrade guard."""
import importlib.util
from pathlib import Path
import uuid
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.engine import make_url
from backend.logging_audit.db import get_database_url


def test_classroom_tables_upgrade_and_nonempty_downgrade_guard():
    url = make_url(get_database_url())
    if url.get_backend_name() != "postgresql":
        pytest.skip("requires PostgreSQL")
    schema = "classroom_migration_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url.update_query_dict({"options": "-csearch_path=" + schema}))
    try:
        path = Path(__file__).resolve().parents[1] / "server/backend/app/alembic/versions/u5f6a7b8c9d0_classroom_records.py"
        spec = importlib.util.spec_from_file_location("classroom_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE conversations (conversation_id varchar(64) PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE character_segments (conversation_id varchar(64), step_no integer)"))
            module.op = Operations(MigrationContext.configure(conn))
            module.upgrade()
            assert {"character_preferences", "character_expressions", "segment_coverage", "taught_knowledge",
                "channel_presentations"} <= set(inspect(conn).get_table_names())
            conn.execute(text("INSERT INTO character_preferences VALUES ('synthetic',1,NULL,NULL,now())"))
            with pytest.raises(RuntimeError):
                module.downgrade()
            conn.execute(text("DELETE FROM character_preferences"))
            module.downgrade()
            assert set(inspect(conn).get_table_names()) == {"conversations", "character_segments"}
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_large_browser_sequences_migrate_without_truncation():
    from test_native_migrations import migration
    # This test owns its complete schema; it cannot affect application tables.
    url = make_url(get_database_url())
    schema = "classroom_sequence_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = create_engine(url.update_query_dict({"options": "-csearch_path=" + schema}))
    try:
        with isolated.begin() as conn:
            for table in ("segment_coverage", "channel_presentations"):
                conn.execute(text(f"CREATE TABLE {table} (client_seq integer NOT NULL)"))
                conn.execute(text(f"INSERT INTO {table} VALUES (1)"))
            module = migration("v6a7b8c9d0e1_presentation_sequences.py", conn)
            module.upgrade()
            for table in ("segment_coverage", "channel_presentations"):
                conn.execute(text(f"UPDATE {table} SET client_seq=1792129959389"))
                assert conn.scalar(text(f"SELECT client_seq FROM {table}")) == 1792129959389
            with pytest.raises(RuntimeError):
                module.downgrade()
            for table in ("segment_coverage", "channel_presentations"):
                conn.execute(text(f"DELETE FROM {table}"))
            module.downgrade()
    finally:
        isolated.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
