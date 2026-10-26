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


def test_review_migration_round_trip(engine):
    with engine.begin() as connection:
        module = migration("l2b3c4d5e6f7_course_review.py", connection)
        module.upgrade()
        module.upgrade()
        tables = set(inspect(connection).get_table_names())
        assert {"review_cards", "review_sessions", "review_answers", "review_logs"} <= tables
        assert "draft_json" in {c["name"] for c in inspect(connection).get_columns("review_sessions")}
        module.downgrade()
        assert "review_sessions" not in inspect(connection).get_table_names()


def test_revision_chain_has_single_native_head():
    scripts = ScriptDirectory(str(ROOT / "server/backend/app/alembic"))
    assert scripts.get_heads() == ["n4d5e6f7g8h9"]
    assert scripts.get_revision("n4d5e6f7g8h9").down_revision == "m3c4d5e6f7g8"
    assert scripts.get_revision("m3c4d5e6f7g8").down_revision == "l2b3c4d5e6f7"
    assert scripts.get_revision("l2b3c4d5e6f7").down_revision == "k1a2b3c4d5e6"
    assert scripts.get_revision("k1a2b3c4d5e6").down_revision == "j0a1b2c3d4e5"
    assert scripts.get_revision("j0a1b2c3d4e5").down_revision == "i9p0q1r2s3t4"
    assert scripts.get_revision("i9p0q1r2s3t4").down_revision == "h8o9p0q1r2s3"
    assert scripts.get_revision("h8o9p0q1r2s3").down_revision == "g7n8o9p0q1r2"
    assert scripts.get_revision("g7n8o9p0q1r2").down_revision == "f6m7n8o9p0q1"
    assert scripts.get_revision("f6m7n8o9p0q1").down_revision == "e5l6m7n8o9p0"
    assert scripts.get_revision("e5l6m7n8o9p0").down_revision == "d4i5j6k7l8m9"
    assert scripts.get_revision("d4i5j6k7l8m9").down_revision == "c3d4e5f6a7b8"
    assert scripts.get_revision("c3d4e5f6a7b8").down_revision == "c3h4i5j6k7l8"
    assert scripts.get_revision("c3h4i5j6k7l8").down_revision == "b2g3h4i5j6k7"
    assert scripts.get_revision("b2g3h4i5j6k7").down_revision == "a1f2g3h4i5j6"
    assert scripts.get_revision("a1f2g3h4i5j6").down_revision == "z0e1f2a3b4c5"
    assert scripts.get_revision("z0e1f2a3b4c5").down_revision == "y9d0e1f2a3b4"
    assert scripts.get_revision("y9d0e1f2a3b4").down_revision == "x8c9d0e1f2a3"
    assert scripts.get_revision("x8c9d0e1f2a3").down_revision == "w7b8c9d0e1f2"
    assert scripts.get_revision("w7b8c9d0e1f2").down_revision == "v6a7b8c9d0e1"
    assert scripts.get_revision("v6a7b8c9d0e1").down_revision == "u5f6a7b8c9d0"
    assert scripts.get_revision("u5f6a7b8c9d0").down_revision == "t4f5a6b7c8d9"
    assert scripts.get_revision("t4f5a6b7c8d9").down_revision == "s3e4f5a6b7c8"
    assert scripts.get_revision("s3e4f5a6b7c8").down_revision == "r2f3a4b5c6d7"
    assert scripts.get_revision("r2f3a4b5c6d7").down_revision == "q1e2f3a4b5c6"
    assert scripts.get_revision("q1e2f3a4b5c6").down_revision == "p0d1e2f3a4b5"
    assert scripts.get_revision("p0d1e2f3a4b5").down_revision == "o9c0d1e2f3a4"
    assert scripts.get_revision("o9c0d1e2f3a4").down_revision == "n8b9c0d1e2f3"
    assert scripts.get_revision("n8b9c0d1e2f3").down_revision == "m7a8b9c0d1e2"
    assert scripts.get_revision("m7a8b9c0d1e2").down_revision == "l6f7a8b9c0d1"
    assert scripts.get_revision("l6f7a8b9c0d1").down_revision == "k5e6f7a8b9c0"
    assert scripts.get_revision("k5e6f7a8b9c0").down_revision == "j4d5e6f7a8b9"
    assert scripts.get_revision("j4d5e6f7a8b9").down_revision == "i3c4d5e6f7a8"
    assert scripts.get_revision("i3c4d5e6f7a8").down_revision == "h2b3c4d5e6f7"


def test_memory_lifecycle_upgrade_is_idempotent_and_preserves_stable_tombstones(engine):
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE memory_sources (source_id varchar(64) PRIMARY KEY, owner_user_id varchar(64))"))
        connection.execute(text("INSERT INTO memory_sources VALUES ('synthetic-old','synthetic-owner')"))
        module = migration("k1a2b3c4d5e6_memory_lifecycle.py", connection)
        module.upgrade()
        module.upgrade()
        assert connection.scalar(text("SELECT event_key FROM memory_sources")) is None
        assert inspect(connection).has_table("memory_learning_evidence")
        assert "native_committed" in {c["name"] for c in inspect(connection).get_columns("memory_maintenance_jobs")}
        connection.execute(text("UPDATE memory_sources SET event_key='synthetic-stable'"))
        with pytest.raises(RuntimeError, match="tombstones"):
            module.downgrade()
        connection.execute(text("UPDATE memory_sources SET event_key=NULL"))
        module.downgrade()
        assert connection.scalar(text("SELECT count(*) FROM memory_sources")) == 1


def test_study_sources_migration_preserves_legacy_and_refuses_loss(engine):
    with engine.begin() as connection:
        for table in ("route_versions", "outline_versions"):
            connection.execute(text(f"CREATE TABLE {table} (version_id text PRIMARY KEY)"))
            connection.execute(text(f"INSERT INTO {table} VALUES ('synthetic-legacy')"))
        module = migration("c3h4i5j6k7l8_study_sources.py", connection)
        module.upgrade()
        assert connection.scalar(text("SELECT source_snapshot_json FROM outline_versions")) == "{}"
        connection.execute(text("UPDATE outline_versions SET source_snapshot_json=:value"), {"value": '{"version":1}'})
        with pytest.raises(RuntimeError):
            module.downgrade()
        connection.execute(text("UPDATE outline_versions SET source_snapshot_json='{}'"))
        module.downgrade()
        assert connection.scalar(text("SELECT count(*) FROM outline_versions")) == 1


def test_personal_retrieval_migration_preserves_chunks_and_blocks_loss(engine):
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE text_chunks (chunk_id text PRIMARY KEY)"))
        connection.execute(text("INSERT INTO text_chunks VALUES ('synthetic-old')"))
        module = migration("d4i5j6k7l8m9_personal_retrieval.py", connection)
        module.upgrade()
        assert connection.scalar(text("SELECT private_text FROM text_chunks")) == ""
        assert inspect(connection).has_table("personal_memory_migrations")
        connection.execute(text("UPDATE text_chunks SET private_text='synthetic private text'"))
        with pytest.raises(RuntimeError):
            module.downgrade()


def test_memory_migration_refuses_nonempty_downgrade(engine):
    with engine.begin() as connection:
        module = migration("x8c9d0e1f2a3_user_memory.py", connection)
        module.upgrade()
        connection.execute(text("INSERT INTO memory_settings(owner_user_id, enabled) VALUES ('synthetic_owner', false)"))
        with pytest.raises(RuntimeError):
            module.downgrade()
        connection.execute(text("DELETE FROM memory_settings"))
        module.downgrade()


def test_document_attachment_migration_preserves_state_and_refuses_lossy_downgrade(engine):
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE materials (material_id varchar(64) PRIMARY KEY)"))
        module = migration("y9d0e1f2a3b4_document_attachments.py", connection)
        module.upgrade()
        connection.execute(text("INSERT INTO materials VALUES ('synthetic-material')"))
        connection.execute(text("""INSERT INTO document_attachments
            (material_id, owner_user_id, upload_key, task_id, config_id, config_version)
            VALUES ('synthetic-material', 'synthetic-owner', 'synthetic-upload',
                    'synthetic-task', 'synthetic-config', 1)"""))
        with pytest.raises(RuntimeError):
            module.downgrade()
        assert connection.scalar(text("SELECT count(*) FROM document_attachments")) == 1
        connection.execute(text("DELETE FROM document_attachments"))
        module.downgrade()


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


def test_course_preparation_migration_preserves_legacy_and_refuses_lossy_downgrade(engine):
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE courses(course_id varchar(64) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE outline_versions(outline_version_id varchar(64) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE prepared_first_segments(cache_id varchar(64) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO prepared_first_segments VALUES ('legacy')"))
        module=migration("m3c4d5e6f7g8_course_preparation.py",connection)
        module.upgrade()
        assert connection.execute(text("SELECT persistent FROM prepared_first_segments")).scalar_one() is False
        connection.execute(text("UPDATE prepared_first_segments SET persistent=true"))
    with pytest.raises(RuntimeError,match="persistent first segments"):
        with engine.begin() as connection:
            migration("m3c4d5e6f7g8_course_preparation.py",connection).downgrade()
    with engine.begin() as connection:
        connection.execute(text("UPDATE prepared_first_segments SET persistent=false"))
        migration("m3c4d5e6f7g8_course_preparation.py",connection).downgrade()
        assert connection.execute(text("SELECT cache_id FROM prepared_first_segments")).scalar_one()=="legacy"
        assert "course_preparation_items" not in inspect(connection).get_table_names()


def test_classroom_recording_migration_preserves_metadata_and_refuses_loss(engine):
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE conversations(conversation_id varchar(64) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO conversations VALUES ('synthetic-recording-conversation')"))
        module = migration("n4d5e6f7g8h9_classroom_recordings.py", connection)
        module.upgrade()
        connection.execute(text("""INSERT INTO classroom_recordings
            (recording_id,owner_user_id,conversation_id,content_version_id,status,mime_type,chunks_json,
             events_json,storage_key,size_bytes,duration_ms,created_at,updated_at)
            VALUES ('synthetic-rec','owner','synthetic-recording-conversation','v1','recording',
                    'video/webm','[]','[]','',0,0,now(),now())"""))
        with pytest.raises(RuntimeError, match="Export classroom recordings"):
            module.downgrade()
        assert connection.scalar(text("SELECT count(*) FROM classroom_recordings")) == 1
        connection.execute(text("DELETE FROM classroom_recordings"))
        module.downgrade()
        assert not inspect(connection).has_table("classroom_recordings")
