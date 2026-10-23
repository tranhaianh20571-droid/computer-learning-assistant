"""Idempotent source-only DDL for installations awaiting unrelated migrations.

Does not stamp Alembic or replace the normal deployment upgrade.
"""
from __future__ import annotations


def install(db):
    from sqlalchemy import text
    from .source_models import (SourceProfileRow, SourceBatchRow, SourceItemRow,
        ImportedSourceRow, SourceChatTurnRow)
    from ..tasks.service import LearningTaskRow  # noqa: F401
    with db.engine.begin() as connection:
        if connection.dialect.name != "postgresql":
            raise RuntimeError("source compatibility DDL requires PostgreSQL")
        connection.execute(text("SET LOCAL lock_timeout = '3s'"))
        for model in (SourceProfileRow, SourceBatchRow, SourceItemRow, ImportedSourceRow, SourceChatTurnRow):
            model.__table__.create(connection, checkfirst=True)
        connection.execute(text("ALTER TABLE document_attachments ALTER COLUMN config_id DROP NOT NULL"))
        connection.execute(text("ALTER TABLE document_attachments ALTER COLUMN config_version DROP NOT NULL"))


if __name__ == "__main__":
    from ..logging_audit.db import Database
    db = Database()
    try:
        install(db)
        print("Source compatibility DDL installed; Alembic revision unchanged.")
    finally:
        db.dispose()
