"""Only the new cache migration, against an isolated disposable PostgreSQL schema."""
import importlib.util
from pathlib import Path
import uuid
from sqlalchemy import text,inspect
from alembic.migration import MigrationContext
from alembic.operations import Operations
from backend.planning.models import CourseRow
from backend.logging_audit.db import Database


def test_new_cache_migration_fk_shape_repeatability_and_downgrade():
    db=Database();schema='first_prewarm_migration_'+uuid.uuid4().hex[:12]
    assert schema.startswith('first_prewarm_migration_') and schema.replace('_','').isalnum()
    path=Path(__file__).resolve().parent/'support/backend/alembic/versions/g1a2b3c4d5e6_first_segment_prewarm.py'
    spec=importlib.util.spec_from_file_location('synthetic_first_migration',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    try:
        with db.engine.begin() as connection:
            connection.execute(text('CREATE SCHEMA '+schema));connection.execute(text('SET LOCAL search_path TO '+schema))
            CourseRow.__table__.create(connection)
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade();module.upgrade()
                assert inspect(connection).has_table('prepared_first_segments',schema=schema)
                columns={column['name'] for column in inspect(connection).get_columns('prepared_first_segments',schema=schema)}
                assert {'cache_id','owner_user_id','text_key','audio_object_key','consumed_conversation_id','expires_at'}<=columns
                assert any(set(item['column_names'])=={'owner_user_id','course_id'} for item in inspect(connection).get_unique_constraints('prepared_first_segments',schema=schema))
                connection.execute(text("INSERT INTO courses(course_id,owner_user_id,title_ref,scenario,created_at) VALUES ('synthetic-course','synthetic-owner','合成迁移','self_study',now())"))
                connection.execute(text("INSERT INTO prepared_first_segments(cache_id,owner_user_id,course_id,lesson_id,text_key,task_id,context_json,plan_json,expires_at) VALUES ('synthetic-cache','synthetic-owner','synthetic-course','synthetic-lesson','synthetic-text-key','synthetic-task','{}','{}',now()+interval '1 day')"))
                connection.execute(text("DELETE FROM courses WHERE course_id='synthetic-course'"))
                assert connection.execute(text('SELECT count(*) FROM prepared_first_segments')).scalar()==0
                module.downgrade();assert not inspect(connection).has_table('prepared_first_segments',schema=schema)
                assert inspect(connection).has_table('courses',schema=schema)
    finally:
        with db.engine.begin() as connection:connection.execute(text('DROP SCHEMA IF EXISTS '+schema+' CASCADE'))
        db.dispose()
