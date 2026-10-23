"""Additive scope migration and nonempty downgrade protection."""
import pytest
from sqlalchemy import inspect, text

from test_native_migrations import engine, migration  # noqa: F401


def test_scope_migration_is_additive_idempotent_and_preserves_frozen_data(engine):
    with engine.begin() as connection:
        module = migration("i9p0q1r2s3t4_unit_scopes.py", connection)
        module.upgrade()
        module.upgrade()
        assert "unit_scopes" in inspect(connection).get_table_names()
        connection.execute(text(
            "INSERT INTO unit_scopes (scope_id,owner_user_id,course_id,unit_id,outline_version_id,"
            "input_hash,task_id,context_json,scope_json,scope_hash,created_at) "
            "VALUES ('synthetic_scope','synthetic_owner','synthetic_course','synthetic_unit',"
            "'synthetic_outline','synthetic_input','synthetic_task','{}','{}','',CURRENT_TIMESTAMP)"))
        with pytest.raises(RuntimeError):
            module.downgrade()
        assert connection.scalar(text("SELECT count(*) FROM unit_scopes")) == 1
        connection.execute(text("DELETE FROM unit_scopes WHERE scope_id='synthetic_scope'"))
        module.downgrade()
        assert "unit_scopes" not in inspect(connection).get_table_names()


def test_scope_teaching_migration_preserves_records_and_refuses_lossy_downgrade(engine):
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE teaching_steps (step_id varchar(64) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE taught_knowledge (record_id varchar(64) PRIMARY KEY)"))
        module = migration("j0a1b2c3d4e5_scope_teaching_coverage.py", connection)
        module.upgrade()
        module.upgrade()
        assert {"covered_scope_capability_ids_json"} <= {
            column["name"] for column in inspect(connection).get_columns("teaching_steps")}
        assert {"scope_hash", "scope_capability_ids_json"} <= {
            column["name"] for column in inspect(connection).get_columns("taught_knowledge")}
        connection.execute(text(
            "INSERT INTO teaching_steps (step_id,covered_scope_capability_ids_json) "
            "VALUES ('synthetic_step','[\"cap_a\"]')"))
        connection.execute(text(
            "INSERT INTO taught_knowledge (record_id,scope_hash,scope_capability_ids_json) "
            "VALUES ('synthetic_record','scope_hash','[\"cap_a\"]')"))
        with pytest.raises(RuntimeError):
            module.downgrade()
        assert connection.scalar(text(
            "SELECT count(*) FROM taught_knowledge WHERE scope_hash='scope_hash'")) == 1
        connection.execute(text("DELETE FROM taught_knowledge"))
        connection.execute(text("DELETE FROM teaching_steps"))
        module.downgrade()
        assert {"covered_scope_capability_ids_json"} - {
            column["name"] for column in inspect(connection).get_columns("teaching_steps")}
        assert {"scope_hash", "scope_capability_ids_json"} - {
            column["name"] for column in inspect(connection).get_columns("taught_knowledge")}
