"""数据库引擎与会话（PostgreSQL / 测试可用 SQLite）。"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, install_audit_triggers

DEFAULT_PG_URL = (
    "postgresql+psycopg://learning_assistant:learning_dev_pw@127.0.0.1:5432/learning_assistant"
)


def get_database_url() -> str:
    return os.environ.get("LEARNING_DATABASE_URL", DEFAULT_PG_URL)


class Database:
    def __init__(self, url: Optional[str] = None) -> None:
        self.url = url or get_database_url()
        self.engine = create_engine(self.url, pool_pre_ping=True, future=True)
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        if self.url.startswith("postgresql"):
            install_audit_triggers(self.engine)

    def drop_all(self) -> None:
        Base.metadata.drop_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        sess = self._factory()
        try:
            yield sess
            sess.commit()
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()

    def dispose(self) -> None:
        self.engine.dispose()


_db: Optional[Database] = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db


def set_db(db: Optional[Database]) -> None:
    global _db
    _db = db
