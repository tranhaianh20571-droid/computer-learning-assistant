"""切片 2 T02/T05 集成测试：上传 API、状态、删除立即脱离检索、跨账户隔离。

使用真实 PostgreSQL 与 slice2 应用装配；会话通过 AuthService 注册后
直接建立（测试用合成账号，不含真实凭据）。
"""

from __future__ import annotations

import io
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

from backend.logging_audit.db import get_database_url  # noqa: E402

os.environ.setdefault("LEARNING_DATABASE_URL", get_database_url())

from fastapi.testclient import TestClient  # noqa: E402

from backend.logging_audit.db import Database  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.logging_audit.repositories import PgAuditLog  # noqa: E402
from backend.slice2_app import Slice2State, create_slice2_app  # noqa: E402

FAKE_PASSWORD = "synthetic-password-12345"


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db


@pytest.fixture(scope="module")
def client(database: Database):
    state = Slice2State(database, PgAuditLog(database))
    app = create_slice2_app(state=state)
    # create_slice2_app 会在缺少 slice1_state 时新建 slice1 应用，
    # 这里显式把 slice0 状态指向同一数据库，保证会话与资料同库。
    from backend.slice0_app import Slice0State

    slice0 = Slice0State(database, PgAuditLog(database))
    # 用测试自建 slice0 状态替换（同一 DB，等价）
    app.state.slice0 = slice0
    with TestClient(app) as c:
        yield c


def _make_user(client: TestClient, database: Database) -> str:
    """注册 + 审批 + 登录，返回 raw session token。"""
    email = f"synthetic.{uuid.uuid4().hex[:8]}@example.com"
    auth = client.app.state.slice0.auth
    registered = auth.register(email, FAKE_PASSWORD, display_name="synth")
    user_id = registered["user_id"]
    # 直接置为 approved（测试便利；审批流程本身由切片 0 测试覆盖）
    from backend.auth.models import UserRow

    with database.session() as sess:
        row = sess.get(UserRow, user_id)
        row.status = "approved"
        row.is_admin = False
    _, token = auth.login(email, FAKE_PASSWORD)
    return token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _txt_bytes(text: str = "神经网络 卷积 conv2d_block_7 v1.2.3") -> bytes:
    return text.encode("utf-8")


def _pdf_bytes() -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.drawString(50, 700, "Neural network chapter conv2d_block_7 v1.2.3")
    c.showPage()
    c.save()
    return buf.getvalue()


class TestUpload:
    def test_upload_txt_and_list(self, client, database):
        token = _make_user(client, database)
        r = client.post(
            "/api/materials",
            headers=_auth(token),
            files=[("files", ("notes.txt", _txt_bytes(), "text/plain"))],
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["kind"] == "txt"
        assert body["quota"]["reserved_bytes"] > 0

        r = client.get("/api/materials", headers=_auth(token))
        assert r.status_code == 200
        assert any(m["material_id"] == body["data"][0]["material_id"] for m in r.json()["data"])

    def test_upload_pdf(self, client, database):
        token = _make_user(client, database)
        r = client.post(
            "/api/materials",
            headers=_auth(token),
            files=[("files", ("doc.pdf", _pdf_bytes(), "application/pdf"))],
        )
        assert r.status_code == 201, r.text
        assert r.json()["data"][0]["kind"] == "pdf"

    def test_upload_rejects_bad_type(self, client, database):
        token = _make_user(client, database)
        r = client.post(
            "/api/materials",
            headers=_auth(token),
            files=[("files", ("bad.exe", b"MZ", "application/octet-stream"))],
        )
        assert r.status_code == 415
        assert r.json()["error_code"] == "unsupported_media_type"

    def test_upload_requires_session(self, client):
        r = client.post(
            "/api/materials",
            files=[("files", ("a.txt", b"x", "text/plain"))],
        )
        assert r.status_code in (401, 403)

    def test_response_hides_filename(self, client, database):
        token = _make_user(client, database)
        r = client.post(
            "/api/materials",
            headers=_auth(token),
            files=[("files", ("secret-name-abc.txt", _txt_bytes(), "text/plain"))],
        )
        assert r.status_code == 201
        assert "secret-name-abc" not in r.text
        assert "filename_hash" in r.text


class TestOwnership:
    def test_cross_account_get_denied(self, client, database):
        t1 = _make_user(client, database)
        t2 = _make_user(client, database)
        created = client.post(
            "/api/materials",
            headers=_auth(t1),
            files=[("files", ("a.txt", _txt_bytes(), "text/plain"))],
        ).json()["data"][0]
        r = client.get(f"/api/materials/{created['material_id']}", headers=_auth(t2))
        assert r.status_code == 403
        assert r.json()["error_code"] == "access_denied"

    def test_cross_account_delete_denied(self, client, database):
        t1 = _make_user(client, database)
        t2 = _make_user(client, database)
        created = client.post(
            "/api/materials",
            headers=_auth(t1),
            files=[("files", ("a.txt", _txt_bytes(), "text/plain"))],
        ).json()["data"][0]
        r = client.delete(f"/api/materials/{created['material_id']}", headers=_auth(t2))
        assert r.status_code == 403


class TestSearchAndDelete:
    def test_search_hits_chinese_and_identifiers(self, client, database):
        token = _make_user(client, database)
        created = client.post(
            "/api/materials",
            headers=_auth(token),
            files=[("files", ("a.txt", _txt_bytes(), "text/plain"))],
        ).json()["data"][0]
        mid = created["material_id"]

        # 直接写入索引块（T02 地基；逐页解析 worker 属 T03，受 T01 门禁）
        from backend.materials.chunk_index import ChunkIndexService

        index = ChunkIndexService(database)
        index.index_chunk(material_id=mid, page_no=1, text="神经网络 卷积 conv2d_block_7 v1.2.3")

        for query in ("神经网络", "conv2d_block_7", "v1.2.3"):
            r = client.post(
                "/api/materials/search",
                headers=_auth(token),
                json={"query": query, "material_ids": [mid]},
            )
            assert r.status_code == 200, (query, r.text)
            assert len(r.json()["data"]) >= 1, query
            assert r.json()["data"][0]["source"] == "material"

    def test_search_cannot_cross_account(self, client, database):
        t1 = _make_user(client, database)
        t2 = _make_user(client, database)
        created = client.post(
            "/api/materials",
            headers=_auth(t1),
            files=[("files", ("a.txt", _txt_bytes(), "text/plain"))],
        ).json()["data"][0]
        mid = created["material_id"]
        from backend.materials.chunk_index import ChunkIndexService

        ChunkIndexService(database).index_chunk(material_id=mid, page_no=1, text="神经网络")

        r = client.post(
            "/api/materials/search",
            headers=_auth(t2),
            json={"query": "神经网络", "material_ids": [mid]},
        )
        assert r.status_code == 200
        assert r.json()["data"] == []

    def test_delete_removes_from_search_immediately(self, client, database):
        token = _make_user(client, database)
        created = client.post(
            "/api/materials",
            headers=_auth(token),
            files=[("files", ("a.txt", _txt_bytes(), "text/plain"))],
        ).json()["data"][0]
        mid = created["material_id"]
        from backend.materials.chunk_index import ChunkIndexService

        ChunkIndexService(database).index_chunk(material_id=mid, page_no=1, text="神经网络")

        assert len(
            client.post(
                "/api/materials/search",
                headers=_auth(token),
                json={"query": "神经网络", "material_ids": [mid]},
            ).json()["data"]
        ) >= 1

        d = client.delete(f"/api/materials/{mid}", headers=_auth(token))
        assert d.status_code == 200
        assert d.json()["tombstone_id"].startswith("tmb_")

        after = client.post(
            "/api/materials/search",
            headers=_auth(token),
            json={"query": "神经网络", "material_ids": [mid]},
        )
        assert after.json()["data"] == []

    def test_retry_only_failed_pages(self, client, database):
        token = _make_user(client, database)
        created = client.post(
            "/api/materials",
            headers=_auth(token),
            files=[("files", ("a.pdf", _pdf_bytes(), "application/pdf"))],
        ).json()["data"][0]
        mid = created["material_id"]
        # 无该页记录 → index_out_of_range
        r = client.post(
            f"/api/materials/{mid}/pages/1/retry",
            headers=_auth(token),
            json={"reason": "user_retry"},
        )
        assert r.status_code == 400
        assert r.json()["error_code"] == "index_out_of_range"

    def test_tombstone_written_on_delete(self, client, database):
        from sqlalchemy import select

        from backend.materials.models import AssetUsageLedgerRow, DeletionTombstoneRow

        token = _make_user(client, database)
        created = client.post(
            "/api/materials",
            headers=_auth(token),
            files=[("files", ("a.txt", _txt_bytes(), "text/plain"))],
        ).json()["data"][0]
        mid = created["material_id"]
        client.delete(f"/api/materials/{mid}", headers=_auth(token))

        with database.session() as sess:
            tomb = (
                sess.execute(
                    select(DeletionTombstoneRow).where(DeletionTombstoneRow.object_id == mid)
                )
                .scalars()
                .first()
            )
            ledger = (
                sess.execute(
                    select(AssetUsageLedgerRow).where(AssetUsageLedgerRow.object_id == mid)
                )
                .scalars()
                .first()
            )
        assert tomb is not None
        assert tomb.cleanup_stage == "pending"
        assert ledger is not None
