"""数据库引擎与会话（PostgreSQL / 测试可用 SQLite）。

连接串单一事实来源：`server/.env` 的 `DATABASE_URL`。
优先级：LEARNING_DATABASE_URL 环境变量 > server/.env 的 DATABASE_URL > 内置开发默认值。
这样 alembic CLI（经 app.core.config 读 server/.env）与测试使用同一个库，
避免“迁移成功但测试报缺列”的双库不一致。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, install_audit_triggers

# 不可达 server/.env 时的最后回退（仅本地开发）：库名与 server/.env 保持一致，
# 密码从环境变量取，源码中不出现凭证字面量。
_FALLBACK_DB_NAME = "learning_assistant_tpl"
_FALLBACK_USER = os.environ.get("LEARNING_FALLBACK_DB_USER", "learning_assistant")
_FALLBACK_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")
_FALLBACK_HOST = os.environ.get("LEARNING_FALLBACK_DB_HOST", "127.0.0.1:5432")


def _fallback_url() -> str:
    auth = _FALLBACK_USER if not _FALLBACK_PASSWORD else f"{_FALLBACK_USER}:{_FALLBACK_PASSWORD}"
    return f"postgresql+psycopg://{auth}@{_FALLBACK_HOST}/{_FALLBACK_DB_NAME}"

# server/.env 位置随包所在层级不同：
#   生产 server/backend/app/logging_audit/db.py -> 上溯 3 层 = server/
#   测试 tests/support/backend/logging_audit/db.py -> 上溯 3 层 = tests/（不是 server/）
# 因此逐个候选探测，而不是假定单一层级。
_ENV_FILE_CANDIDATES = (
    Path(__file__).resolve().parents[3] / ".env",
    Path(__file__).resolve().parents[3] / "server" / ".env",
    Path(__file__).resolve().parents[4] / "server" / ".env",
)


def _env_file() -> Optional[Path]:
    for candidate in _ENV_FILE_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def _normalize_driver(url: str) -> str:
    """把 SQLAlchemy 不认的裸 postgres:// 统一成 psycopg 驱动。"""
    for scheme in ("postgres://", "postgresql://"):
        if url.startswith(scheme):
            return url.replace(scheme, "postgresql+psycopg://", 1)
    return url


def _read_env_database_url() -> Optional[str]:
    """只读取 server/.env 的 DATABASE_URL，不引入 settings 依赖。"""
    env_file = _env_file()
    if env_file is None:
        return None
    try:
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "DATABASE_URL":
                value = value.strip().strip('"').strip("'")
                return value or None
    except OSError:
        return None
    return None


def get_database_url() -> str:
    explicit = os.environ.get("LEARNING_DATABASE_URL")
    if explicit:
        return _normalize_driver(explicit)
    from_env_file = _read_env_database_url()
    if from_env_file:
        return _normalize_driver(from_env_file)
    return _fallback_url()


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
