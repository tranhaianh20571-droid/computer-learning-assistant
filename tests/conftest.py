"""测试公共装配（通用规范 testing-spec §2）。

集中处理：support 镜像路径、测试库连接串默认值、噪声开关。
单个测试文件不再重复路径样板；既有文件中的重复 insert 保持幂等无害。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / "tests" / "support"
if str(SUPPORT) not in sys.path:
    sys.path.insert(0, str(SUPPORT))

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

# 测试库默认值（真实环境变量 > server/.env 的 DATABASE_URL > db.py 内置默认）
from backend.logging_audit.db import get_database_url  # noqa: E402

os.environ.setdefault("LEARNING_DATABASE_URL", get_database_url())


import pytest  # noqa: E402


@pytest.fixture(autouse=True, scope="module")
def close_module_connection_pools():
    """Retire idle test pools even when a legacy fixture fails before yielding."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    engines = set()
    def track(connection):
        engines.add(connection.engine)
    event.listen(Engine, "engine_connect", track)
    try:
        yield
    finally:
        event.remove(Engine, "engine_connect", track)
        for engine in engines:
            engine.dispose()


@pytest.fixture(autouse=True)
def stop_planning_backgrounds(monkeypatch):
    """Stop dispatcher threads started by a test before the next test shares the DB."""
    try:
        from backend.planning.background import PlanningBackground
    except Exception:  # noqa: BLE001 - importing the test mirror is best effort
        yield
        return
    instances = []
    original_start = PlanningBackground.start

    def start(self):
        instances.append(self)
        return original_start(self)

    monkeypatch.setattr(PlanningBackground, "start", start)
    try:
        yield
    finally:
        for instance in instances:
            try:
                instance.close()
            except Exception:  # noqa: BLE001 - best-effort test cleanup
                pass


@pytest.fixture(autouse=True)
def native_synthetic_model_session(monkeypatch):
    """Legacy generate ducks only; configured adapters retain native wire checks.

    Conversion is test infrastructure, not a production capability fallback.
    TestModel/FunctionModel and AdapterBase are deliberately delegated unchanged.
    Services keep the original source and its mutable counters/config metadata.
    """
    from backend.agents import runner
    from native_model_support import synthetic_session_factory
    monkeypatch.setattr(runner, "native_model_session",
                        synthetic_session_factory(runner.native_model_session))
