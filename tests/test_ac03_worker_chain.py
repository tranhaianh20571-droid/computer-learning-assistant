"""AC-03：API→worker 链路与 PG 角色权限、回退相关验证。"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

os.environ.setdefault(
    "LEARNING_DATABASE_URL",
    "postgresql+psycopg://learning_assistant:learning_dev_pw@127.0.0.1:5432/learning_assistant",
)

from backend.logging_audit.app import AppState, create_app  # noqa: E402
from backend.logging_audit.db import Database  # noqa: E402
from backend.logging_audit.logger import MemoryLogStream, StructuredLogger  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.logging_audit.repositories import PgAuditLog, PgTaskEventStore  # noqa: E402
from backend.logging_audit.worker import StageWorker  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db


@pytest.fixture()
def state(database: Database):
    stream = MemoryLogStream()
    logger = StructuredLogger("api", "test", stream=stream)
    return AppState(db=database, logger=logger)


@pytest.fixture()
def client(state: AppState):
    app = create_app(state)
    with TestClient(app) as c:
        c.app_state = state  # type: ignore[attr-defined]
        c.log_stream = state.logger.stream  # type: ignore[attr-defined]
        yield c


class TestApiToWorkerChain:
    def test_same_task_id_attempt_increments_trace_links(self, client: TestClient, state: AppState):
        """AC-03：API 创建 → worker 阶段 → 重试，task_id 不变、attempt 递增、trace 可串。"""
        tid = f"task_{uuid.uuid4().hex[:8]}"
        trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"

        # API 入口
        r = client.post(
            "/tasks",
            json={
                "task_id": tid,
                "owner_user_id": "user_a",
                "subject_id": "mat_1",
                "stage": "upload",
                "attempt": 1,
            },
            headers={"X-Actor-Id": "user_a", "traceparent": f"00-{trace_id}-00f067aa0ba902b7-01"},
        )
        assert r.status_code == 201
        assert r.headers["X-Trace-Id"] == trace_id

        # worker 在同一 trace 下跑阶段
        worker = StageWorker(state.tasks, state.logger)
        res1 = worker.run_stage(
            tid,
            stage="ocr",
            attempt=1,
            generation=1,
            owner_user_id="user_a",
            upstream_trace_id=trace_id,
            status="failed",
            error_code="OCR_PAGE_TIMEOUT",
            duration_ms=50,
        )
        assert res1.task_id == tid
        assert res1.attempt == 1

        # 重试：attempt 递增，task_id 不变
        res2 = worker.run_stage(
            tid,
            stage="ocr",
            attempt=2,
            generation=1,
            owner_user_id="user_a",
            upstream_trace_id=trace_id,
            status="succeeded",
            duration_ms=30,
        )
        assert res2.task_id == tid
        assert res2.attempt == 2

        status = client.get(f"/tasks/{tid}/status", headers={"X-Actor-Id": "user_a"}).json()
        assert status["task_id"] == tid
        assert status["attempt"] == 2
        assert status["status"] == "succeeded"

        # trace 关联：运行日志含同一 trace_id 与 task_id
        logs = client.log_stream.records()  # type: ignore[attr-defined]
        matched = [x for x in logs if x.get("task_id") == tid]
        assert matched
        assert all(x.get("trace_id") == trace_id for x in matched if "trace_id" in x)

        events = client.get(f"/tasks/{tid}/events.json", headers={"X-Actor-Id": "user_a"}).json()["events"]
        stages = [e["stage"] for e in events]
        assert "upload" in stages and "ocr" in stages
        # 阶段顺序与耗时可定位
        ocr_events = [e for e in events if e["stage"] == "ocr"]
        assert len(ocr_events) == 2


class TestPgRolePermissions:
    def test_app_rw_role_cannot_update_audit(self, database: Database):
        """AC-06：数据库权限层——应用角色无 UPDATE 权限。"""
        with database.engine.connect() as conn:
            conn.execute(text("SET ROLE learning_app_rw"))
            with pytest.raises(Exception):
                conn.execute(text("UPDATE audit_logs SET result='tampered'"))
                conn.execute(text("COMMIT"))

    def test_trigger_blocks_even_owner_update(self, database: Database):
        """触发器双保险：即使有 UPDATE 权限也被拒。"""
        audit = PgAuditLog(database)
        from backend.logging_audit.audit import AuditImmutabilityError

        with pytest.raises(AuditImmutabilityError):
            audit.sql_update_forbidden()


class TestRollbackNotes:
    def test_alembic_version_recorded(self):
        """回退需要迁移版本号（写入证据文档）。"""
        versions = list(
            (ROOT / "tests" / "support" / "backend" / "alembic" / "versions").glob("*.py")
        )
        assert versions, "no alembic versions"
        names = [v.stem for v in versions]
        assert any("11fd41567866" in n for n in names)
