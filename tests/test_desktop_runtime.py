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
