import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
assert config.config_file_name is not None
fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
# target_metadata = None

from app.models import SQLModel  # noqa
from app.infrastructure.config import settings # noqa
from app.logging_audit.models import Base as LoggingBase  # noqa
import app.tasks.service  # noqa: F401 — register task tables
import app.agents.task_binding  # noqa: F401 — register durable agent budgets
import app.search.repository  # noqa: F401 — register personal search budgets/calls
import app.prompts.service  # noqa: F401 — register prompt tables
import app.slice1.models  # noqa: F401 — register slice1 capability/connector tables
import app.characters.models  # noqa: F401
import app.characters.persona  # noqa: F401
import app.characters.distillation_models  # noqa: F401
import app.agents.model_routing  # noqa: F401
import app.materials.models  # noqa: F401 — register slice2 materials/parse/index tables
import app.memory.personal  # noqa: F401
import app.materials.corpus  # noqa: F401
import app.materials.source_models  # noqa: F401
import app.planning.feature_models  # noqa: F401 — register planning dialogue/cover tables
import app.planning.models  # noqa: F401 — register slice3 planning/chat/manifest tables
import app.teaching.learning_models  # noqa: F401 — pedagogy and private assessments
import app.teaching.unit_scope  # noqa: F401 - independent frozen unit specifications
import app.teaching.preparation_models  # noqa: F401
import app.review.models  # noqa: F401
import app.teaching.authorization  # noqa: F401 - explicit lesson consent
import app.teaching.models  # noqa: F401 — register slice4 teaching/playback tables
import app.cleanup.models  # noqa: F401 — register slice5 cleanup/backup tables

# 合并 SQLModel 与 SQLAlchemy Base 元数据（切片 0 任务/审计/提示词表）
from sqlalchemy import MetaData

target_metadata = MetaData()
for table in SQLModel.metadata.tables.values():
    table.to_metadata(target_metadata)
for table in LoggingBase.metadata.tables.values():
    table.to_metadata(target_metadata)

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def get_url():
    return str(settings.DATABASE_URL)


def run_migrations_offline():
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = get_url()
    context.configure(
        url=url, target_metadata=target_metadata, literal_binds=True, compare_type=True
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    configuration = config.get_section(config.config_ini_section)
    assert configuration is not None
    configuration["sqlalchemy.url"] = get_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, compare_type=True
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
