"""切片 5 测试：删除流、在线清理、备份轮换、恢复锁与墓碑重放、全量交叉回归。"""

from __future__ import annotations

import io
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

from backend.logging_audit.db import get_database_url  # noqa: E402

os.environ.setdefault("LEARNING_DATABASE_URL", get_database_url())

from sqlalchemy import select, update  # noqa: E402

from backend.logging_audit.db import Database  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.logging_audit.repositories import PgAuditLog, PgTaskEventStore  # noqa: E402
from backend.materials.chunk_index import ChunkIndexService  # noqa: E402
from backend.materials.models import DeletionTombstoneRow, MaterialRow  # noqa: E402
from backend.materials.storage import MaterialStorage  # noqa: E402
from backend.materials.upload import MaterialService, QuotaService  # noqa: E402
from backend.slice3_app import Slice3State, create_slice3_app  # noqa: E402
from backend.slice0_app import Slice0State  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from slice3_common import (  # noqa: E402
    ROUTE_OK_PAYLOAD,
    SyntheticModelAdapter,
    SyntheticSearchAdapter,
    make_user,
)


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db


@pytest.fixture()
def env(database: Database, tmp_path):
    # 测试库卫生：清掉上次运行可能遗留的恢复锁
    from backend.cleanup.models import SystemStateRow
    from backend.teaching.models import utcnow

    with database.session() as sess:
        row = sess.get(SystemStateRow, "restore_locked")
        if row is not None:
            row.value = "0"
            row.updated_at = utcnow()
            sess.commit()
    container = {"model": None, "search": None, "tts": None, "ocr": None}
    state = Slice3State(
        database, PgAuditLog(database),
        model_adapter_factory=lambda o, t: container["model"],
        search_adapter_factory=lambda o, t: container["search"],
        tts_adapter_factory=lambda o, t: container["tts"],
        ocr_adapter_factory=lambda o, t: container["ocr"],
    )
    app = create_slice3_app(state=state)
    app.state.slice0 = Slice0State(database, PgAuditLog(database))
    client = TestClient(app, raise_server_exceptions=False)
    return {"client": client, "state": state, "container": container, "database": database}


def _admin(env):
    """创建管理员并返回 token。"""
    from backend.auth.models import UserRow

    email = f"admin.{uuid.uuid4().hex[:8]}@example.com"
    auth = env["client"].app.state.slice0.auth
    uid = auth.register(email, "synthetic-password-12345", "admin")["user_id"]
    with env["database"].session() as sess:
        sess.get(UserRow, uid).status = "approved"
        sess.get(UserRow, uid).is_admin = True
    _, token = auth.login(email, "synthetic-password-12345")
    return uid, token


def _upload_material(env, owner_token: str) -> str:
    buf = io.BytesIO()
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(buf, pagesize=A4)
    c.drawString(60, 760, "Neural network chapter conv2d_block_7 v1.2.3")
    c.showPage()
    c.save()
    r = env["client"].post(
        "/api/materials", headers={"Authorization": f"Bearer {owner_token}"},
        files=[("files", ("doc.pdf", buf.getvalue(), "application/pdf"))],
    )
    assert r.status_code == 201, r.text
    return r.json()["data"][0]["material_id"]


class TestRouteDeletion:
    def test_delete_route_keeps_courses(self, env) -> None:
        user_id, token = make_user(env["client"], env["database"])
        client = env["client"]
        H = {"Authorization": f"Bearer {token}"}
        env["container"]["model"] = SyntheticModelAdapter(payload=ROUTE_OK_PAYLOAD)
        env["container"]["search"] = SyntheticSearchAdapter()
        route_id = client.post("/api/planning/routes", headers=H,
                               json={"title": "待删路线", "scenario": "self_study"}).json()["route_id"]
        t = client.post("/api/planning/tasks", headers=H,
                        json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}",
                              "task_kind": "route_generate", "planning_scope": "route",
                              "target_route_id": route_id}).json()["task_id"]
        body = client.post(f"/api/planning/tasks/{t}/run", headers=H).json()
        assert "draft" in body, body
        draft = env["state"].drafts.get_draft(body["draft"]["draft_id"], actor_id=user_id)
        client.post(f"/api/planning/drafts/{body['draft']['draft_id']}/confirm", headers=H,
                    json={"revision": draft["revision"], "idempotency_key": f"c{uuid.uuid4().hex[:8]}"})
        overview = client.get(f"/api/routes/{route_id}/overview", headers=H).json()
        course_id = overview["stages"][0]["courses"][0]["course_id"]

        # 删除路线
        r = client.delete(f"/api/planning/routes/{route_id}", headers=H)
        assert r.status_code == 200, r.text
        # 课程保留且可访问
        r2 = client.get(f"/api/courses/{course_id}/overview", headers=H)
        assert r2.status_code == 200
        # 墓碑记录
        with env["database"].session() as sess:
            tomb = sess.execute(
                select(DeletionTombstoneRow).where(
                    DeletionTombstoneRow.object_type == "route",
                    DeletionTombstoneRow.object_id == route_id,
                )
            ).scalars().first()
            assert tomb is not None and tomb.cleanup_stage == "pending"

    def test_cross_user_delete_denied(self, env) -> None:
        user_id, token = make_user(env["client"], env["database"])
        _, other = make_user(env["client"], env["database"])
        route_id = env["client"].post("/api/planning/routes", headers={"Authorization": f"Bearer {token}"},
                                      json={"title": "x", "scenario": "self_study"}).json()["route_id"]
        r = env["client"].delete(f"/api/planning/routes/{route_id}",
                                 headers={"Authorization": f"Bearer {other}"})
        assert r.status_code == 404


class TestCourseDeletion:
    def test_delete_course_removes_versions_and_lessons(self, env) -> None:
        user_id, token = make_user(env["client"], env["database"])
        client = env["client"]
        H = {"Authorization": f"Bearer {token}"}
        env["container"]["model"] = SyntheticModelAdapter(payload=ROUTE_OK_PAYLOAD)
        env["container"]["search"] = SyntheticSearchAdapter()
        route_id = client.post("/api/planning/routes", headers=H,
                               json={"title": "r", "scenario": "self_study"}).json()["route_id"]
        t = client.post("/api/planning/tasks", headers=H,
                        json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}",
                              "task_kind": "route_generate", "planning_scope": "route",
                              "target_route_id": route_id}).json()["task_id"]
        body = client.post(f"/api/planning/tasks/{t}/run", headers=H).json()
        draft = env["state"].drafts.get_draft(body["draft"]["draft_id"], actor_id=user_id)
        client.post(f"/api/planning/drafts/{body['draft']['draft_id']}/confirm", headers=H,
                    json={"revision": draft["revision"], "idempotency_key": f"c{uuid.uuid4().hex[:8]}"})
        overview = client.get(f"/api/routes/{route_id}/overview", headers=H).json()
        course_id = overview["stages"][0]["courses"][0]["course_id"]

        r = client.delete(f"/api/planning/courses/{course_id}", headers=H)
        assert r.status_code == 200, r.text
        with env["database"].session() as sess:
            from backend.planning.models import LessonRow, OutlineVersionRow

            assert sess.execute(
                select(OutlineVersionRow).where(OutlineVersionRow.course_id == course_id)
            ).scalars().first() is None
            assert sess.execute(select(LessonRow).where(
                LessonRow.outline_version_id == course_id
            )).scalars().first() is None
            tomb = sess.execute(
                select(DeletionTombstoneRow).where(
                    DeletionTombstoneRow.object_type == "course",
                    DeletionTombstoneRow.object_id == course_id,
                )
            ).scalars().first()
            assert tomb is not None


class TestRestoreLockAndReplay:
    def test_restore_flow_replay_and_unlock(self, env) -> None:
        """注入：使用删除前备份恢复 → restore_locked 无用户访问 → 墓碑重放 → 解锁。"""
        user_id, token = make_user(env["client"], env["database"])
        client = env["client"]
        H = {"Authorization": f"Bearer {token}"}
        admin_id, admin_token = _admin(env)
        AH = {"Authorization": f"Bearer {admin_token}"}

        mid = _upload_material(env, token)
        # 删除（墓碑 pending）→ 模拟「删除前备份恢复」：把行改回未删除
        client.delete(f"/api/materials/{mid}", headers=H)
        with env["database"].session() as sess:
            sess.execute(update(MaterialRow).where(MaterialRow.material_id == mid)
                         .values(deleted_at=None))
        restored_alive = client.get(f"/api/materials/{mid}", headers=H).status_code
        assert restored_alive == 200  # 备份恢复让已删对象「复活」

        # 进入恢复锁：普通用户立即被拒绝访问（任何失败都要解锁，避免污染）
        try:
            r = client.post("/api/admin/restore/begin", headers=AH)
            assert r.status_code == 200, r.text
            # restore_locked 期间所有业务访问拒绝；仅 /api/admin 恢复操作放行
            assert client.get("/api/materials", headers=H).status_code == 423
            assert client.get("/api/materials", headers=AH).status_code == 423
            assert client.get("/api/admin/restore/state", headers=AH).status_code == 200

            # 墓碑重放：复活对象再次删除
            r = client.post("/api/admin/restore/replay", headers=AH)
            assert r.status_code == 200, r.text
            # 核验：所有墓碑对象不可访问 → 解锁
            r = client.post("/api/admin/restore/verify", headers=AH)
            assert r.status_code == 200, r.text
        finally:
            client.post("/api/admin/restore/verify", headers=AH)
            with env["database"].session() as sess:
                from backend.cleanup.models import SystemStateRow

                row = sess.get(SystemStateRow, "restore_locked")
                if row is not None:
                    row.value = "0"
                    sess.commit()
        # 已删除对象不复活
        assert client.get(f"/api/materials/{mid}", headers=H).status_code == 404
        # 普通用户访问恢复
        assert client.get("/api/materials", headers=H).status_code == 200


class TestOnlineCleanup:
    def test_cleanup_removes_objects_and_advances_tombstone(self, env, tmp_path) -> None:
        storage = MaterialStorage(base_dir=tmp_path / "cleanup-storage")
        from backend.cleanup.service import CleanupService

        cleanup = CleanupService(env["database"], storage=storage)
        owner = f"user_{uuid.uuid4().hex[:8]}"
        from datetime import timedelta

        mid = f"mat_{uuid.uuid4().hex[:12]}"
        key = MaterialStorage.original_key(mid)
        storage.put(key, b"%PDF-fake")
        # 墓碑：删除时间早于 24h 在线清理延迟
        from backend.teaching.models import utcnow

        with env["database"].session() as sess:
            sess.add(DeletionTombstoneRow(
                tombstone_id=f"tmb_{uuid.uuid4().hex[:12]}",
                object_type="material", object_id=mid, owner_user_id=owner,
                deleted_at=utcnow() - timedelta(hours=25),
                cleanup_stage="pending",
            ))
            sess.commit()
        result = cleanup.run_online_cleanup()
        assert result["cleaned"] >= 1
        with env["database"].session() as sess:
            tomb = sess.execute(
                select(DeletionTombstoneRow).where(
                    DeletionTombstoneRow.object_type == "material",
                    DeletionTombstoneRow.object_id == mid,
                )
            ).scalars().first()
            assert tomb.cleanup_stage == "cleaned"
        import pytest as _pytest

        from backend.materials.storage import StorageError

        with _pytest.raises(StorageError):
            storage.get(key)


class TestBackupRotation:
    def test_backup_creates_and_rotates(self, env, tmp_path) -> None:
        storage = MaterialStorage(base_dir=tmp_path / "backup-storage")
        from backend.cleanup.service import CleanupService

        cleanup = CleanupService(env["database"], storage=storage)
        first = cleanup.create_backup()
        assert first["backup_key"].startswith("backups/")
        second = cleanup.create_backup()
        assert second["run_id"] != first["run_id"]

    def test_backup_excludes_30day_old_tombstones(self, env, tmp_path) -> None:
        from datetime import timedelta

        storage = MaterialStorage(base_dir=tmp_path / "backup-storage2")
        from backend.cleanup.service import CleanupService
        from backend.teaching.models import utcnow

        cleanup = CleanupService(env["database"], storage=storage)
        old_id = f"mat_{uuid.uuid4().hex[:8]}"
        with env["database"].session() as sess:
            sess.add(DeletionTombstoneRow(
                tombstone_id=f"tmb_{uuid.uuid4().hex[:12]}",
                object_type="material", object_id=old_id, owner_user_id="u",
                deleted_at=utcnow() - timedelta(days=45),
                cleanup_stage="cleaned",
            ))
            sess.commit()
        result = cleanup.create_backup()
        # 45 天前的已清理墓碑不进入新备份（备份内 30 天清理）
        import tarfile

        with env["database"].session() as sess:
            from backend.cleanup.models import BackupRunRow

            row = sess.get(BackupRunRow, result["run_id"])
            key = row.backup_key
        data = storage.get(key)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            manifest = json.load(tar.extractfile("manifest.json"))
        ids = [t["object_id"] for t in manifest["tombstones"]]
        assert old_id not in ids


class TestAdminGuard:
    def test_cleanup_endpoints_admin_only(self, env) -> None:
        user_id, token = make_user(env["client"], env["database"])
        H = {"Authorization": f"Bearer {token}"}
        assert env["client"].post("/api/admin/cleanup/run", headers=H).status_code == 403
        assert env["client"].post("/api/admin/backup", headers=H).status_code == 403
        assert env["client"].post("/api/admin/restore/begin", headers=H).status_code == 403
