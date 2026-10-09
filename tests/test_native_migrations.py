"""Exercise native budget and subject migrations in a disposable PostgreSQL schema."""
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
from sqlalchemy.exc import DataError
from backend.logging_audit.db import get_database_url

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "server/backend/app/alembic/versions"


def migration(filename, connection):
    spec = importlib.util.spec_from_file_location(filename, VERSIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


@pytest.fixture
def engine():
    url = make_url(get_database_url())
    if url.get_backend_name() != "postgresql":
        pytest.skip("native migrations require PostgreSQL")
    schema = "native_migration_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = create_engine(url.update_query_dict({"options": "-csearch_path=" + schema}))
    try:
        with isolated.begin() as connection:
            for table in ("learning_tasks", "task_states", "task_events"):
                connection.execute(text(f'CREATE TABLE "{table}" (task_id varchar(64) PRIMARY KEY, subject_id varchar(64) NOT NULL)'))
        yield isolated
    finally:
        isolated.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_revision_chain_has_single_native_head():
    scripts = ScriptDirectory(str(ROOT / "server/backend/app/alembic"))
    assert scripts.get_heads() == ["m7a8b9c0d1e2"]
    assert scripts.get_revision("m7a8b9c0d1e2").down_revision == "l6f7a8b9c0d1"
    assert scripts.get_revision("l6f7a8b9c0d1").down_revision == "k5e6f7a8b9c0"
    assert scripts.get_revision("k5e6f7a8b9c0").down_revision == "j4d5e6f7a8b9"
    assert scripts.get_revision("j4d5e6f7a8b9").down_revision == "i3c4d5e6f7a8"
    assert scripts.get_revision("i3c4d5e6f7a8").down_revision == "h2b3c4d5e6f7"


def test_upgrade_preserves_rows_and_accepts_full_refinement_subject(engine):
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO learning_tasks VALUES ('task_old','subject_old')"))
        migration("i3c4d5e6f7a8_native_agent_budgets.py", connection).upgrade()
        migration("j4d5e6f7a8b9_refinement_task_subjects.py", connection).upgrade()
        subject = "c" * 64 + ":" + "a" * 64
        for table in ("learning_tasks", "task_states", "task_events"):
            assert next(column for column in inspect(connection).get_columns(table) if column["name"] == "subject_id")["type"].length == 129
            connection.execute(text(f'INSERT INTO "{table}" VALUES (:id,:subject)'), {"id": "task_new", "subject": subject})
        connection.execute(text("INSERT INTO agent_task_budgets (task_id) VALUES ('task_new')"))
        assert connection.execute(text("SELECT logical_requests,wire_requests,tool_calls,request_limit,tool_limit FROM agent_task_budgets")).one() == (0, 0, 0, 128, 256)
        assert connection.execute(text("SELECT subject_id FROM learning_tasks WHERE task_id='task_old'")).scalar_one() == "subject_old"


def test_downgrade_refuses_to_truncate_long_subjects(engine):
    with engine.begin() as connection:
        migration("i3c4d5e6f7a8_native_agent_budgets.py", connection).upgrade()
        migration("j4d5e6f7a8b9_refinement_task_subjects.py", connection).upgrade()
        connection.execute(text("INSERT INTO learning_tasks VALUES ('task_long',:subject)"), {"subject": "x" * 129})
    with pytest.raises(DataError):
        with engine.begin() as connection:
            migration("j4d5e6f7a8b9_refinement_task_subjects.py", connection).downgrade()
    with engine.begin() as connection:
        assert connection.execute(text("SELECT length(subject_id) FROM learning_tasks")).scalar_one() == 129
        connection.execute(text("DELETE FROM learning_tasks"))
        migration("j4d5e6f7a8b9_refinement_task_subjects.py", connection).downgrade()
        migration("i3c4d5e6f7a8_native_agent_budgets.py", connection).downgrade()
        assert "agent_task_budgets" not in inspect(connection).get_table_names()
        assert inspect(connection).get_columns("learning_tasks")[1]["type"].length == 64


def test_consent_migrations_preserve_legacy_grants_and_revert(engine):
    with engine.begin() as connection:
        for table, key in (("courses", "course_id"), ("lessons", "lesson_id"),
                           ("conversations", "conversation_id")):
            connection.execute(text(f'CREATE TABLE "{table}" ("{key}" varchar(64) PRIMARY KEY)'))
        connection.execute(text('CREATE TABLE disclosure_grants (grant_id varchar(64) PRIMARY KEY)'))
        connection.execute(text("INSERT INTO disclosure_grants VALUES ('legacy_root')"))
        roots = migration("k5e6f7a8b9c0_disclosure_roots.py", connection)
        lessons = migration("l6f7a8b9c0d1_lesson_authorizations.py", connection)
        roots.upgrade()
        assert connection.execute(text("SELECT grant_id,parent_grant_id FROM disclosure_grants")).one() == ("legacy_root", None)
        connection.execute(text("INSERT INTO disclosure_grants VALUES ('child','legacy_root')"))
        lessons.upgrade()
        inspector = inspect(connection)
        columns = {column["name"]: column for column in inspector.get_columns("lesson_authorizations")}
        assert columns["fingerprint"]["type"].length == 64
        assert columns["context_json"]["nullable"] is False
        assert columns["prewarm_opt_in"]["nullable"] is False
        assert "false" in columns["prewarm_opt_in"]["default"].lower()
        assert {tuple(fk["constrained_columns"]) for fk in inspector.get_foreign_keys("lesson_authorizations")} == {
            ("course_id",), ("lesson_id",), ("conversation_id",),
            ("content_root_grant_id",), ("tts_root_grant_id",)}
        connection.execute(text("DELETE FROM disclosure_grants WHERE grant_id='legacy_root'"))
        assert connection.execute(text("SELECT count(*) FROM disclosure_grants")).scalar_one() == 0
        lessons.downgrade()
        roots.downgrade()
        inspector = inspect(connection)
        assert "lesson_authorizations" not in inspector.get_table_names()
        assert [column["name"] for column in inspector.get_columns("disclosure_grants")] == ["grant_id"]


def test_lesson_cache_migration_preserves_rows_and_refuses_lossy_downgrade(engine):
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE prepared_first_segments (cache_id varchar(64) PRIMARY KEY, "
            "owner_user_id varchar(64) NOT NULL, course_id varchar(64) NOT NULL, lesson_id varchar(64) NOT NULL, "
            "text_key varchar(64) NOT NULL, audio_key varchar(64) NOT NULL, "
            "CONSTRAINT uq_first_segment_owner_course UNIQUE(owner_user_id,course_id))"))
        connection.execute(text("INSERT INTO prepared_first_segments VALUES ('legacy-cache','owner','course','lesson-1','text-v1','audio-v1')"))
        caches = migration("m7a8b9c0d1e2_lesson_prewarm_keys.py", connection)
        caches.upgrade()
        assert connection.execute(text("SELECT cache_id,lesson_id,text_key FROM prepared_first_segments")).one() == ("legacy-cache", "lesson-1", "text-v1")
        caches.downgrade()
        assert inspect(connection).get_unique_constraints("prepared_first_segments")[0]["name"] == "uq_first_segment_owner_course"
        caches.upgrade()
        connection.execute(text("INSERT INTO prepared_first_segments VALUES "
            "('next-cache','owner','course','lesson-2','text-v1','audio-v1'),"
            "('version-cache','owner','course','lesson-1','text-v2','audio-v1')"))
        assert {tuple(item["column_names"]) for item in inspect(connection).get_unique_constraints("prepared_first_segments")} == {
            ("owner_user_id", "course_id", "lesson_id", "text_key", "audio_key")}
    with pytest.raises(RuntimeError, match="without discarding retained lesson versions"):
        with engine.begin() as connection:
            migration("m7a8b9c0d1e2_lesson_prewarm_keys.py", connection).downgrade()
    with engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM prepared_first_segments")).scalar_one() == 3
        assert connection.execute(text("SELECT text_key FROM prepared_first_segments WHERE cache_id='legacy-cache'")).scalar_one() == "text-v1"
