"""共享数据库入口（代理 logging_audit.db）。"""

from .logging_audit.db import Database, get_db, get_database_url, set_db

__all__ = ["Database", "get_db", "get_database_url", "set_db"]
