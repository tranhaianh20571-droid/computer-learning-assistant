import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
import pytest

from backend.logging_audit.db import get_database_url


def test_local_resources_upgrade_idempotent_and_nonempty_downgrade_refused():
    url = make_url(get_database_url())
    schema="local_resources_migration_"+uuid.uuid4().hex
    admin=create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine=create_engine(url.update_query_dict({"options":"-csearch_path="+schema}))
    try:
        path=Path(__file__).resolve().parents[1]/"server/backend/app/alembic/versions/h8o9p0q1r2s3_local_resources.py"
        spec=importlib.util.spec_from_file_location("local_resource_upgrade",path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with engine.begin() as connection:
            module.op=Operations(MigrationContext.configure(connection))
            module.upgrade();module.upgrade()
            assert set(inspect(connection).get_table_names())=={
                "local_resource_objects","local_resource_jobs","local_resource_policy"}
            connection.execute(text("INSERT INTO local_resource_jobs "
                "(job_id,owner_user_id,request_key,kind,status,request_json,result_json,progress,total,attempts,created_at,updated_at)"
                " VALUES ('synthetic','synthetic','synthetic','scan','queued','{}','{}',0,0,0,now(),now())"))
            with pytest.raises(RuntimeError):
                module.downgrade()
            connection.execute(text("DELETE FROM local_resource_jobs"))
            module.downgrade()
            assert not inspect(connection).get_table_names()
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
