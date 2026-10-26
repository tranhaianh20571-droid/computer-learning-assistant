"""Desktop bootstrap contracts against the managed regression database."""
# ruff: noqa: F811
import hashlib
import os

import pytest
from backend.auth.models import LoginSessionRow
from backend.desktop_runtime import configure, local_session, memory_env
from sqlalchemy import select
from test_course_library import database, env  # noqa: F401


def configuration(tmp_path):
    return {"home": str(tmp_path), "worker": str(tmp_path / "worker"),
        "roots": {name: str(tmp_path / name) for name in ("core", "postgres", "memory", "video", "asr-small", "classroom")},
        "keys": {name: "synthetic-" + name + "-123456789" for name in ("database", "memory", "proxy", "session", "encryption")}}


def test_desktop_configuration_is_owned_and_loopback(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    config = configuration(tmp_path)
    for key in ("DATABASE_URL", "STUDY_DESKTOP_MODE", "MATERIALS_STORAGE_DIR", "CLASSROOM_NATIVE_ROOT",
                "LEARNING_DATABASE_URL", "APP_ENCRYPTION_KEY"):
        monkeypatch.setenv(key, "")
    app_db, memory_db = configure(config, 18181, 15432, 19079)
    assert "127.0.0.1:15432/study" in app_db
    assert "127.0.0.1:15432/study_memory" in memory_db
    assert "synthetic-proxy" not in app_db
    values = memory_env(config, 18181, 19079, memory_db)
    assert values["HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS"] == "2000"
    assert values["HINDSIGHT_API_LLM_BASE_URL"].startswith("http://127.0.0.1:18181/api/internal/memory/")


def test_local_owner_and_session_persist_without_guest_or_admin(database, tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "synthetic-desktop-key-for-regression")
    from backend.auth.service import AuthService
    from backend.logging_audit.repositories import PgAuditLog
    auth = AuthService(database, PgAuditLog(database))
    owner_file = tmp_path / "config" / "owner"
    first = local_session(database, owner_file)
    second = local_session(database, owner_file)
    owner = owner_file.read_text()
    assert first != second
    user = auth.resolve_session(first)
    assert user.user_id == owner == auth.resolve_session(second).user_id
    assert user.is_admin is False
    with database.session() as session:
        saved = session.scalar(select(LoginSessionRow).where(
            LoginSessionRow.session_token_hash == hashlib.sha256(first.encode()).hexdigest()))
        assert saved is not None
        assert saved.session_token_hash != first


def test_existing_owner_marker_cannot_silently_create_a_new_user(database, tmp_path):
    owner_file = tmp_path / "owner"
    owner_file.write_text("user_missing")
    with pytest.raises(RuntimeError, match="OWNER_NOT_FOUND"):
        local_session(database, owner_file)


@pytest.fixture
def desktop_schema_engine():
    import uuid

    from backend.logging_audit.db import get_database_url
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    url = make_url(get_database_url())
    schema = "desktop_upgrade_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url.update_query_dict({"options": "-csearch_path=" + schema}))
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE desktop_schema(version integer NOT NULL)"))
            connection.execute(text("INSERT INTO desktop_schema VALUES (1)"))
            connection.execute(text("CREATE TABLE courses(course_id varchar(64) PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE conversations(conversation_id varchar(64) PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE outline_versions(outline_version_id varchar(64) PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE prepared_first_segments(cache_key varchar(64) PRIMARY KEY)"))
            connection.execute(text("INSERT INTO prepared_first_segments VALUES ('existing-first')"))
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_desktop_upgrade_preserves_existing_cache_and_runs_only_once(desktop_schema_engine):
    from types import SimpleNamespace

    from backend.desktop_runtime import initialize
    from sqlalchemy import event, inspect, text

    engine = desktop_schema_engine
    initialize(SimpleNamespace(engine=engine))
    with engine.connect() as connection:
        from backend.desktop_runtime import SCHEMA_VERSION
        assert connection.scalar(text("SELECT version FROM desktop_schema")) == SCHEMA_VERSION
        assert connection.execute(text("SELECT cache_key,persistent FROM prepared_first_segments")).one() == (
            "existing-first", False)
        assert {"course_preparations", "course_preparation_items", "preparation_connections", "classroom_recordings"} <= set(
            inspect(connection).get_table_names())
    statements = []
    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", record)
    try:
        initialize(SimpleNamespace(engine=engine))
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert not any(statement.lstrip().upper().startswith(("CREATE", "ALTER", "UPDATE")) for statement in statements)


def test_desktop_upgrade_failure_rolls_back_tables_and_version(desktop_schema_engine):
    from types import SimpleNamespace

    from backend.desktop_runtime import initialize
    from sqlalchemy import event, inspect, text

    engine = desktop_schema_engine
    def fail(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("ALTER TABLE"):
            raise RuntimeError("synthetic-upgrade-interruption")
    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="synthetic-upgrade-interruption"):
            initialize(SimpleNamespace(engine=engine))
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version FROM desktop_schema")) == 1
        assert "course_preparations" not in inspect(connection).get_table_names()
        assert connection.scalar(text("SELECT cache_key FROM prepared_first_segments")) == "existing-first"
    initialize(SimpleNamespace(engine=engine))


def test_desktop_version_two_adds_recordings_without_changing_existing_rows(desktop_schema_engine):
    from types import SimpleNamespace

    from backend.desktop_runtime import initialize
    from sqlalchemy import inspect, text
    engine = desktop_schema_engine
    with engine.begin() as connection:
        connection.execute(text("UPDATE desktop_schema SET version=2"))
    initialize(SimpleNamespace(engine=engine))
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version FROM desktop_schema")) == 3
        assert "classroom_recordings" in inspect(connection).get_table_names()
        assert connection.scalar(text("SELECT cache_key FROM prepared_first_segments")) == "existing-first"


def test_recording_migration_compiles_in_a_fresh_process(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    code = (
        "import importlib; "
        "importlib.import_module('app.alembic.versions.n4d5e6f7g8h9_classroom_recordings'); "
        "from app.teaching.learning_models import ClassroomRecordingRow; "
        "from sqlalchemy.schema import CreateTable; "
        "from sqlalchemy.dialects import postgresql; "
        "ddl=str(CreateTable(ClassroomRecordingRow.__table__).compile(dialect=postgresql.dialect())); "
        "assert 'conversation_id VARCHAR(64)' in ddl; "
        "assert 'REFERENCES conversations (conversation_id) ON DELETE CASCADE' in ddl"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path,
        env=os.environ | {"PYTHONPATH": str(root / "server/backend")},
        capture_output=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_desktop_upgrade_rejects_unknown_version_and_unowned_schema(desktop_schema_engine):
    from types import SimpleNamespace

    from backend.desktop_runtime import initialize
    from sqlalchemy import inspect, text

    engine = desktop_schema_engine
    with engine.begin() as connection:
        connection.execute(text("UPDATE desktop_schema SET version=999"))
    with pytest.raises(RuntimeError, match="SCHEMA_UPGRADE_REQUIRED"):
        initialize(SimpleNamespace(engine=engine))
    with engine.begin() as connection:
        assert "course_preparations" not in inspect(connection).get_table_names()
        connection.execute(text("DROP TABLE desktop_schema"))
    with pytest.raises(RuntimeError, match="SCHEMA_NOT_OWNED"):
        initialize(SimpleNamespace(engine=engine))
