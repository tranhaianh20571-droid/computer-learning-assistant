"""Actual migration DDL parity, legacy role seeding and downgrade fences."""
import uuid
import pytest
from sqlalchemy import create_engine,inspect,text
from sqlalchemy.engine import make_url
from backend.logging_audit.db import get_database_url
from backend.agents.model_routing import ModelRoleRow,TaskModelRoleRow
from backend.characters.persona import CharacterPersonaRow
from backend.characters.distillation_models import CharacterBuildRow,CharacterBuildStageRow
from test_native_migrations import migration


def test_persona_workflow_migration_parity_seed_and_preserve_nonempty():
    url=make_url(get_database_url());schema="persona_migration_"+uuid.uuid4().hex
    admin=create_engine(url)
    with admin.begin() as conn:conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine=create_engine(url.update_query_dict({"options":"-csearch_path="+schema}))
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE learning_tasks (task_id varchar(64) PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE character_revisions (card_id varchar(64),revision integer,PRIMARY KEY(card_id,revision))"))
            conn.execute(text("""CREATE TABLE service_configs (owner_user_id varchar(64),config_id varchar(64),
                kind varchar(40),is_active boolean,owner_scope varchar(20),created_at timestamptz)"""))
            conn.execute(text("""INSERT INTO service_configs VALUES
                ('synthetic','older','content_model',true,'user','2026-01-01'),
                ('synthetic','chosen','content_model',true,'user','2026-02-01')"""))
            script=migration("z0e1f2a3b4c5_persona_workflows.py",conn);script.upgrade()
            rows=conn.execute(text("SELECT role,config_id FROM model_role_bindings ORDER BY role")).all()
            assert rows==[("planning","chosen"),("teaching","chosen")]
            evidence=migration("b2g3h4i5j6k7_persona_evidence.py",conn)
            evidence.upgrade()
            for model in (ModelRoleRow,TaskModelRoleRow,CharacterPersonaRow,CharacterBuildRow,CharacterBuildStageRow):
                actual={column["name"]:column for column in inspect(conn).get_columns(model.__tablename__)}
                assert set(actual)==set(model.__table__.columns.keys())
                for column in model.__table__.columns:
                    assert actual[column.name]["nullable"]==column.nullable
                    assert str(actual[column.name]["type"].compile(dialect=conn.dialect))==str(column.type.compile(dialect=conn.dialect))
            conn.execute(text("INSERT INTO character_revisions VALUES ('synthetic-card',1)"))
            conn.execute(text("INSERT INTO character_personas VALUES ('synthetic-card',1,'{}')"))
            with pytest.raises(RuntimeError,match="nonempty"):script.downgrade()
            conn.execute(text("DELETE FROM character_personas"))
            evidence.downgrade()
            script.downgrade()
            assert "model_role_bindings" not in inspect(conn).get_table_names()
    finally:
        engine.dispose()
        with admin.begin() as conn:conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
