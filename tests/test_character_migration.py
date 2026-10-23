"""Frozen migration, schema parity and destructive downgrade protection."""
import importlib.util
from pathlib import Path
import uuid
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from backend.logging_audit.db import get_database_url
from backend.characters.models import CharacterCardRow, CharacterAssetRow, CharacterSegmentRow


def test_character_migration_upgrade_parity_and_nonempty_downgrade():
    schema = "character_migration_" + uuid.uuid4().hex
    url = make_url(get_database_url())
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url.update_query_dict({"options": "-csearch_path=" + schema}))
    try:
        path = Path(__file__).resolve().parents[1] / "server/backend/app/alembic/versions/s3e4f5a6b7c8_character_cards.py"
        spec = importlib.util.spec_from_file_location("character_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE conversations (conversation_id varchar(64) PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE learning_tasks (task_id varchar(64) PRIMARY KEY)"))
            module.op = Operations(MigrationContext.configure(conn))
            module.upgrade()
            from test_native_migrations import migration
            classroom = migration("u5f6a7b8c9d0_classroom_records.py", conn)
            classroom.upgrade()
            for model in (CharacterCardRow, CharacterAssetRow, CharacterSegmentRow):
                actual = {c["name"]: c for c in inspect(conn).get_columns(model.__tablename__)}
                assert set(actual) == set(model.__table__.columns.keys())
                for col in model.__table__.columns:
                    assert actual[col.name]["nullable"] == col.nullable
                    assert str(actual[col.name]["type"].compile(dialect=conn.dialect)) == str(col.type.compile(dialect=conn.dialect))
            conn.execute(text("INSERT INTO character_cards VALUES ('synthetic-card','synthetic-owner',1,NULL,NULL,CURRENT_TIMESTAMP)"))
            with pytest.raises(RuntimeError, match="requires_empty"):
                module.downgrade()
            assert conn.scalar(text("SELECT count(*) FROM character_cards")) == 1
            conn.execute(text("DELETE FROM character_cards"))
            classroom.downgrade()
            module.downgrade()
            assert not any(name.startswith("character_") for name in inspect(conn).get_table_names())
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
