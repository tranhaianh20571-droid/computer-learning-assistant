"""backend 包：切片 0 应用入口与共享组件。"""

from .logging_audit.db import Database, get_db, set_db

__all__ = ["Database", "get_db", "set_db"]
