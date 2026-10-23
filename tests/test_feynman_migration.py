"""Upgrade/downgrade only an owned temporary PostgreSQL schema."""
from sqlalchemy import inspect, text
import pytest
from backend.teaching.learning_models import FeynmanSessionRow
from test_native_migrations import engine, migration  # noqa: F401


def test_feynman_tables_have_round_keys_and_private_audio_reference(engine):
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE conversations (conversation_id varchar(64) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE audio_assets (audio_id varchar(64) PRIMARY KEY)"))
        module=migration("t4f5a6b7c8d9_feynman_dialogues.py",connection)
        module.upgrade()
        tables=inspect(connection)
        assert {"feynman_sessions","feynman_turns"} <= set(tables.get_table_names())
        assert {"uq_feynman_checkpoint"} <= {key["name"] for key in tables.get_unique_constraints("feynman_sessions")}
        assert {"uq_feynman_turn"} <= {key["name"] for key in tables.get_unique_constraints("feynman_turns")}
        assert any(foreign["referred_table"]=="audio_assets" for foreign in tables.get_foreign_keys("feynman_turns"))
        assert {"criteria_json","evaluation_json","playback_json","published"} <= {
            column["name"] for column in tables.get_columns("feynman_turns")}
        connection.execute(text("INSERT INTO conversations VALUES ('synthetic-conversation')"))
        connection.execute(FeynmanSessionRow.__table__.insert().values(session_id="synthetic-session",
            conversation_id="synthetic-conversation",owner_user_id="synthetic-owner",
            checkpoint_id="synthetic-checkpoint",after_step_no=1,source_hash="0"*64,context_json="{}"))
        with pytest.raises(RuntimeError,match="Preserve"):
            module.downgrade()
        connection.execute(text("DELETE FROM feynman_sessions"))
        module.downgrade()
        assert "feynman_turns" not in inspect(connection).get_table_names()
