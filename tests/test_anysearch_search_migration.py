"""Disposable search-table migration and original-task legacy convergence."""
from __future__ import annotations

from pathlib import Path
import hashlib
import importlib.util
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from backend.logging_audit.db import get_database_url


def migration(connection):
    path = Path(__file__).resolve().parents[1]/"server/backend/app/alembic/versions/o9c0d1e2f3a4_search_execution.py"
    spec = importlib.util.spec_from_file_location("search_tables_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


@pytest.fixture
def connection():
    url = make_url(get_database_url())
    assert url.get_backend_name() == "postgresql"
    schema = "search_tables_" + uuid.uuid4().hex
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = create_engine(url.update_query_dict({"options":"-csearch_path="+schema}))
    try:
        with isolated.begin() as conn:
            # Minimal real lifecycle columns, not an ID-only substitute schema.
            definitions = {
                "learning_tasks": "task_id varchar(64) PRIMARY KEY, owner_user_id varchar(64) NOT NULL, "
                    "subject_id varchar(129) NOT NULL, stage varchar(40) NOT NULL, status varchar(40) NOT NULL, "
                    "generation integer NOT NULL, revision integer NOT NULL, attempt integer NOT NULL, "
                    "lease_owner varchar(64), lease_token varchar(64), lease_until timestamptz, "
                    "deadline_at timestamptz, cancel_requested boolean NOT NULL, updated_at timestamptz NOT NULL",
                "task_states": "task_id varchar(64) PRIMARY KEY, owner_user_id varchar(64) NOT NULL, "
                    "subject_id varchar(129) NOT NULL, stage varchar(40) NOT NULL, status varchar(40) NOT NULL, "
                    "generation integer NOT NULL, revision integer NOT NULL, attempt integer NOT NULL, "
                    "progress_current integer NOT NULL, progress_total integer NOT NULL, error_code varchar(64)",
                "task_attempts": "attempt_id varchar(64) PRIMARY KEY, "
                    "task_id varchar(64) NOT NULL REFERENCES learning_tasks(task_id) ON DELETE CASCADE, "
                    "finished_at timestamptz, result_status varchar(40)",
                "planning_task_states": "task_id varchar(64) PRIMARY KEY, owner_user_id varchar(64) NOT NULL, "
                    "wait_state varchar(40), search_decision varchar(20), abandoned_at timestamptz, "
                    "updated_at timestamptz NOT NULL",
                "task_events": "event_id varchar(64) PRIMARY KEY, "
                    "task_id varchar(64) NOT NULL REFERENCES task_states(task_id) ON DELETE CASCADE, "
                    "owner_user_id varchar(64) NOT NULL, subject_id varchar(129) NOT NULL, sequence integer NOT NULL, "
                    "stage varchar(40) NOT NULL, status varchar(40) NOT NULL, error_code varchar(64), "
                    "progress_current integer NOT NULL, progress_total integer NOT NULL, template_id varchar(100), "
                    "template_params text NOT NULL, created_at timestamptz NOT NULL, generation integer NOT NULL, "
                    "attempt integer NOT NULL, duration_ms integer, revision integer NOT NULL, UNIQUE(task_id,sequence)",
                "audit_logs": "audit_id varchar(64) PRIMARY KEY, event varchar(120) NOT NULL, timestamp timestamptz NOT NULL, "
                    "actor_id varchar(64) NOT NULL, object_type varchar(40), object_id varchar(64), "
                    "result varchar(40) NOT NULL CHECK(char_length(result)>0), trace_id varchar(64), reason varchar(200), "
                    "admin_id varchar(64), role varchar(40) NOT NULL, target_actor_id varchar(64), content_hash varchar(32) NOT NULL",
                "agent_task_budgets": "task_id varchar(64) PRIMARY KEY REFERENCES learning_tasks(task_id) ON DELETE CASCADE, "
                    "logical_requests integer NOT NULL, wire_requests integer NOT NULL, tool_calls integer NOT NULL",
                "service_configs": "config_id varchar(64) PRIMARY KEY",
            }
            for table, columns in definitions.items():
                conn.execute(text(f"CREATE TABLE {table} ({columns})"))
            seed(conn, "synthetic-parent", status="queued", code=None, wait=None)
            conn.execute(text("INSERT INTO service_configs VALUES ('synthetic-parent')"))
        with isolated.begin() as conn:
            yield conn
    finally:
        isolated.dispose()
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def seed(connection, task_id, *, status="partial", code="AWAITING_SEARCH_DECISION",
         wait="awaiting_search_decision", decision=None, cancel=False, expired=False, abandoned=False,
         planning=True):
    params = dict(task_id=task_id, status=status, code=code, wait=wait, decision=decision,
        cancel=cancel, expired=expired, abandoned=abandoned)
    connection.execute(text("""
        INSERT INTO learning_tasks VALUES (:task_id,'synthetic-owner',:task_id,'search',:status,
            7,11,3,'old-worker','old-lease',CURRENT_TIMESTAMP+interval '1 hour',
            CASE WHEN :expired THEN CURRENT_TIMESTAMP-interval '1 hour'
                 ELSE CURRENT_TIMESTAMP+interval '1 hour' END,:cancel,CURRENT_TIMESTAMP)
    """), params)
    connection.execute(text("""
        INSERT INTO task_states VALUES (:task_id,'synthetic-owner',:task_id,'search',:status,
            7,19,3,2,5,:code)
    """), params)
    connection.execute(text("INSERT INTO task_attempts VALUES (:task_id,:task_id,NULL,NULL)"), params)
    if planning:
        connection.execute(text("""
            INSERT INTO planning_task_states VALUES (:task_id,'synthetic-owner',:wait,:decision,
                CASE WHEN :abandoned THEN CURRENT_TIMESTAMP ELSE NULL END,CURRENT_TIMESTAMP)
        """), params)
    connection.execute(text("""
        INSERT INTO task_events (event_id,task_id,owner_user_id,subject_id,sequence,stage,status,
            error_code,progress_current,progress_total,template_params,created_at,generation,attempt,revision)
        VALUES (:task_id,:task_id,'synthetic-owner',:task_id,8,'search',:status,:code,2,5,'{}',CURRENT_TIMESTAMP,7,3,19)
    """), params)
    connection.execute(text("INSERT INTO agent_task_budgets VALUES (:task_id,2,3,4)"), params)


def budget_v2_migration(connection):
    path = Path(__file__).resolve().parents[1]/"server/backend/app/alembic/versions/r2f3a4b5c6d7_search_budget_v2.py"
    spec = importlib.util.spec_from_file_location("search_budget_v2_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


def seed_old_budget(connection):
    migration(connection).upgrade()
    connection.execute(text("INSERT INTO search_task_budgets "
        "(task_id,owner_user_id,logical_count,wire_count,search_window_started_at,search_deadline_at) "
        "VALUES ('synthetic-parent','synthetic-owner',1,2,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP + interval '5 seconds')"))
    connection.execute(text("INSERT INTO search_calls "
        "(query_id,task_id,owner_user_id,attempt_id,config_id,config_version,logical_slot,wire_count,state,created_at) "
        "VALUES ('synthetic-query','synthetic-parent','synthetic-owner','synthetic-parent','synthetic-parent',1,1,2,'failed',CURRENT_TIMESTAMP)"))


def test_budget_v2_upgrade_preserves_historical_window_and_consumed_counts(connection):
    seed_old_budget(connection)
    tables = ("search_task_budgets", "search_calls", "learning_tasks", "task_events", "audit_logs")
    before = {table: [dict(row._mapping) for row in connection.execute(text(f"SELECT * FROM {table} ORDER BY 1"))]
        for table in tables}
    upgrade = budget_v2_migration(connection)
    assert upgrade.down_revision == "q1e2f3a4b5c6"
    upgrade.upgrade()
    for table in tables:
        after = [dict(row._mapping) for row in connection.execute(text(f"SELECT * FROM {table} ORDER BY 1"))]
        if table in ("search_task_budgets", "search_calls"):
            assert all(row.pop("retry_count") == 0 for row in after)
        assert after == before[table]
    columns = {item["name"] for item in inspect(connection).get_columns("search_calls")}
    assert not {"query", "prompt", "url", "title", "response", "api_key"} & columns
    upgrade.downgrade()
    assert "retry_count" not in {item["name"] for item in inspect(connection).get_columns("search_calls")}
    assert connection.execute(text("SELECT wire_count FROM search_task_budgets")).scalar_one() == 2


def test_budget_v2_accepts_six_wires_five_retries_and_refuses_lossy_downgrade(connection):
    seed_old_budget(connection)
    upgrade = budget_v2_migration(connection)
    upgrade.upgrade()
    for table in ("search_task_budgets", "search_calls"):
        connection.execute(text(f"UPDATE {table} SET wire_count=6,retry_count=5"))
    before = {table: connection.execute(text(f"SELECT * FROM {table}")).all()
        for table in ("search_task_budgets", "search_calls")}
    with pytest.raises(RuntimeError, match="search_budget_v2_downgrade_requires_empty_new_budgets"):
        upgrade.downgrade()
    for table, rows in before.items():
        assert connection.execute(text(f"SELECT * FROM {table}")).all() == rows


@pytest.mark.parametrize("table,assignments", [
    ("search_task_budgets", "wire_count=7"),
    ("search_task_budgets", "retry_count=6"),
    ("search_task_budgets", "retry_count=-1"),
    ("search_task_budgets", "logical_count=4"),
    ("search_calls", "wire_count=7"),
    ("search_calls", "retry_count=6"),
    ("search_calls", "retry_count=-1"),
    ("search_calls", "logical_slot=4"),
])
def test_budget_v2_database_constraints_reject_over_budget_counts(connection, table, assignments):
    seed_old_budget(connection)
    budget_v2_migration(connection).upgrade()
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(text(f"UPDATE {table} SET {assignments}"))
    assert connection.execute(text(f"SELECT wire_count,retry_count FROM {table}")).one() == (2,0)


def test_search_table_upgrade_and_parent_retention(connection):
    upgrade = migration(connection)
    upgrade.upgrade()
    assert {"search_task_budgets","search_calls"}.issubset(inspect(connection).get_table_names())
    columns = {c["name"] for c in inspect(connection).get_columns("search_calls")}
    assert not {"query","query_hash","url","title","content","api_key"} & columns
    connection.execute(text("INSERT INTO search_task_budgets (task_id,owner_user_id,search_window_started_at,search_deadline_at) "
        "VALUES ('synthetic-parent','synthetic-owner',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP + interval '5 seconds')"))
    connection.execute(text("INSERT INTO search_calls (query_id,task_id,owner_user_id,attempt_id,config_id,config_version,logical_slot,created_at) "
        "VALUES ('synthetic-query','synthetic-parent','synthetic-owner','synthetic-parent','synthetic-parent',1,1,CURRENT_TIMESTAMP)"))
    assert connection.execute(text("SELECT wire_count,logical_count FROM search_task_budgets")).one() == (0,0)
    upgrade.downgrade()
    assert "search_calls" not in inspect(connection).get_table_names()
    assert connection.execute(text("SELECT count(*) FROM learning_tasks")).scalar_one() == 1


@pytest.mark.parametrize("options,status,code", [
    ({}, "failed", "SEARCH_INTERRUPTED"),
    ({"planning": False}, "failed", "SEARCH_INTERRUPTED"),
    ({"code": None}, "failed", "SEARCH_INTERRUPTED"),
    ({"decision": "offline_continue"}, "failed", "SEARCH_INTERRUPTED"),
    ({"cancel": True, "expired": True}, "cancelled", "TASK_CANCELLED"),
    ({"expired": True}, "expired", "TASK_EXPIRED"),
    ({"abandoned": True}, "cancelled", "TASK_CANCELLED"),
])
def test_legacy_wait_terminal_convergence_is_body_free_and_one_time(connection, options, status, code):
    seed(connection, "legacy", **options)
    connection.execute(text("INSERT INTO task_attempts VALUES ('finished','legacy',CURRENT_TIMESTAMP,'partial')"))
    upgrade = migration(connection)
    upgrade.upgrade()
    assert connection.execute(text("SELECT status,generation,revision,attempt,lease_owner,lease_token,lease_until "
        "FROM learning_tasks WHERE task_id='legacy'")).one() == (status,7,12,3,None,None,None)
    event_status = "cancelled" if status == "cancelled" else "failed"
    assert connection.execute(text("SELECT status,error_code,generation,revision,attempt,progress_current,progress_total "
        "FROM task_states WHERE task_id='legacy'")).one() == (event_status,code,7,20,3,2,5)
    attempts = {r.attempt_id: r for r in connection.execute(text("SELECT * FROM task_attempts WHERE task_id='legacy'"))}
    assert attempts["legacy"].finished_at is not None and attempts["legacy"].result_status == status
    assert attempts["finished"].result_status == "partial"
    if options.get("planning", True):
        assert connection.execute(text("SELECT wait_state,search_decision FROM planning_task_states WHERE task_id='legacy'")).one() == (
            None, options.get("decision"))
    event = connection.execute(text("SELECT * FROM task_events WHERE task_id='legacy' ORDER BY sequence DESC LIMIT 1")).one()
    assert (event.sequence,event.status,event.error_code,event.generation,event.attempt,event.revision,event.template_params) == (
        9,event_status,code,7,3,20,"{}")
    audit = connection.execute(text("SELECT * FROM audit_logs")).one()
    assert (audit.event,audit.actor_id,audit.role,audit.object_type,audit.object_id,audit.result,audit.reason) == (
        "search.execution","system:migration","system","task","legacy","migration_retired","legacy_search_wait_converged")
    payload = "|".join((audit.event,audit.timestamp.isoformat(),audit.actor_id,audit.object_type,audit.object_id,audit.result,""))
    assert audit.content_hash == hashlib.sha256(payload.encode()).hexdigest()[:16]
    assert audit.trace_id is None and audit.admin_id is None and audit.target_actor_id is None
    assert connection.execute(text("SELECT logical_requests,wire_requests,tool_calls FROM agent_task_budgets WHERE task_id='legacy'")).one() == (2,3,4)
    assert connection.execute(text("SELECT count(*) FROM search_calls")).scalar_one() == 0
    assert connection.execute(text("SELECT count(*) FROM search_task_budgets")).scalar_one() == 0
    upgrade._converge_legacy_waits()
    assert connection.execute(text("SELECT count(*) FROM audit_logs")).scalar_one() == 1
    assert connection.execute(text("SELECT count(*) FROM task_events WHERE task_id='legacy'")).scalar_one() == 2
    upgrade.downgrade()
    assert connection.execute(text("SELECT status,generation,revision FROM learning_tasks WHERE task_id='legacy'")).one() == (status,7,12)


@pytest.mark.parametrize("options", [
    {"status": "cancelled"}, {"status": "expired"}, {"status": "failed"}, {"status": "succeeded"},
    {"wait": "awaiting_scope_clarification"}, {"wait": None,"code": "AWAITING_SCOPE_CLARIFICATION"},
])
def test_non_search_waits_and_terminal_tasks_are_unchanged(connection, options):
    seed(connection, "untouched", **options)
    before = {table: connection.execute(text(f"SELECT * FROM {table} ORDER BY 1")).all()
        for table in ("learning_tasks","task_states","planning_task_states","task_attempts","task_events","agent_task_budgets")}
    migration(connection).upgrade()
    for table, rows in before.items():
        assert connection.execute(text(f"SELECT * FROM {table} ORDER BY 1")).all() == rows
    assert connection.execute(text("SELECT count(*) FROM audit_logs")).scalar_one() == 0


def test_stale_generation_and_deleted_task_do_not_reappear(connection):
    seed(connection, "stale")
    connection.execute(text("UPDATE task_states SET generation=8 WHERE task_id='stale'"))
    seed(connection, "deleted")
    connection.execute(text("DELETE FROM learning_tasks WHERE task_id='deleted'"))
    migration(connection).upgrade()
    assert connection.execute(text("SELECT task_id,status,generation FROM learning_tasks ORDER BY task_id")).all() == [
        ("stale","partial",7),("synthetic-parent","queued",7)]
    assert connection.execute(text("SELECT count(*) FROM audit_logs")).scalar_one() == 0


def test_audit_failure_rolls_back_entire_terminal_transition(connection):
    seed(connection, "legacy")
    connection.execute(text("ALTER TABLE audit_logs ADD CONSTRAINT reject_migration CHECK(actor_id <> 'system:migration')"))
    with pytest.raises(IntegrityError), connection.begin_nested():
        migration(connection).upgrade()
    assert connection.execute(text("SELECT status,revision FROM learning_tasks WHERE task_id='legacy'")).one() == ("partial",11)
    assert connection.execute(text("SELECT error_code,revision FROM task_states WHERE task_id='legacy'")).one() == ("AWAITING_SEARCH_DECISION",19)
    assert connection.execute(text("SELECT wait_state FROM planning_task_states WHERE task_id='legacy'")).scalar_one() == "awaiting_search_decision"
    assert connection.execute(text("SELECT count(*) FROM task_events WHERE task_id='legacy'")).scalar_one() == 1
    assert "search_calls" not in inspect(connection).get_table_names()
