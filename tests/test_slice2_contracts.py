"""切片 2 T02 契约测试：数据模型、上传校验、配额账本、渲染与文本可信判定。

不依赖真实 OCR 凭据；OCR 写路径（T03/T04）不在本文件覆盖范围。
合成样本由 scripts/slice2_sample_generate.py 生成，不使用任何真实教材。
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

from backend.logging_audit.db import Database  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.materials import errors as merrors  # noqa: E402
from backend.materials.chunk_index import (  # noqa: E402
    JIEBA_VERSION,
    SOURCE_VERSION,
    build_tokens,
    jieba_tokens,
)
from backend.materials.parse import (  # noqa: E402
    MAX_PAGE_PIXELS,
    evaluate_page,
    evaluate_text_layer,
    page_count,
    render_page,
)
from backend.materials.upload import (  # noqa: E402
    MAX_FILE_BYTES,
    MAX_FILES_PER_BATCH,
    MaterialService,
    QuotaService,
    ValidatedFile,
    decode_text,
    validate_batch,
    validate_one,
)

sys.path.insert(0, str(ROOT / "scripts"))
from slice2_sample_generate import SAMPLES, generate  # noqa: E402


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db


@pytest.fixture(scope="module")
def samples(tmp_path_factory) -> dict:
    out = tmp_path_factory.mktemp("slice2-samples")
    return generate(out)


@pytest.fixture()
def owner() -> str:
    return f"user_{uuid.uuid4().hex[:8]}"


def _pdf_bytes(lines=("hello world",)) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = A4[1] - 50
    for line in lines:
        c.drawString(50, y, line)
        y -= 20
    c.showPage()
    c.save()
    return buf.getvalue()


class TestErrorCodes:
    def test_frozen_codes_present(self):
        for code in (
            "material_not_found",
            "invalid_file_signature",
            "invalid_encoding",
            "too_many_pages",
            "file_too_large",
            "too_many_files",
            "quota_exceeded",
            "material_already_deleted",
            "index_out_of_range",
            "page_state_conflict",
        ):
            assert code in merrors.ERROR_CODES

    def test_error_helper_status(self):
        assert merrors.error("material_not_found").status == 404
        assert merrors.error("quota_exceeded").status == 409
        assert merrors.error("file_too_large").status == 413


class TestUploadValidation:
    def test_valid_pdf(self):
        v = validate_one(filename="doc.pdf", content_type="application/pdf", payload=_pdf_bytes())
        assert v.kind == "pdf"
        assert v.page_count == 1
        assert v.size_bytes > 0
        assert len(v.sha256) == 64

    def test_rejects_unsupported_extension(self):
        with pytest.raises(merrors.MaterialsError) as ctx:
            validate_one(filename="a.exe", content_type="application/octet-stream", payload=b"MZ")
        assert ctx.value.code == "unsupported_media_type"

    def test_rejects_mime_mismatch(self):
        with pytest.raises(merrors.MaterialsError) as ctx:
            validate_one(filename="a.txt", content_type="image/png", payload=b"hello")
        assert ctx.value.code == "unsupported_media_type"

    def test_rejects_bad_pdf_signature(self):
        with pytest.raises(merrors.MaterialsError) as ctx:
            validate_one(filename="a.pdf", content_type="application/pdf", payload=b"not a pdf")
        assert ctx.value.code == "invalid_file_signature"

    def test_rejects_empty(self):
        with pytest.raises(merrors.MaterialsError) as ctx:
            validate_one(filename="a.txt", content_type="text/plain", payload=b"")
        assert ctx.value.code == "empty_file"

    def test_rejects_oversize(self):
        payload = b"%PDF-" + b"0" * (MAX_FILE_BYTES + 1)
        with pytest.raises(merrors.MaterialsError) as ctx:
            validate_one(filename="a.pdf", content_type="application/pdf", payload=payload)
        assert ctx.value.code == "file_too_large"

    def test_rejects_non_utf8_text(self):
        with pytest.raises(merrors.MaterialsError) as ctx:
            validate_one(
                filename="a.txt",
                content_type="text/plain",
                payload=b"\xff\xfe\x00\x01",
            )
        assert ctx.value.code in ("invalid_encoding", "invalid_file_signature")

    def test_decode_text_accepts_bom(self):
        assert decode_text(b"\xef\xbb\xbfhello") == "hello"

    def test_rejects_invalid_encoding(self):
        with pytest.raises(merrors.MaterialsError) as ctx:
            decode_text(b"\xc3\x28")
        assert ctx.value.code == "invalid_encoding"

    def test_batch_limits(self):
        files = [("a.txt", "text/plain", b"x") for _ in range(MAX_FILES_PER_BATCH)]
        assert len(validate_batch(files)) == MAX_FILES_PER_BATCH
        too_many = files + [("b.txt", "text/plain", b"y")]
        with pytest.raises(merrors.MaterialsError) as ctx:
            validate_batch(too_many)
        assert ctx.value.code == "too_many_files"

    def test_rejects_encrypted_pdf(self):
        import pypdfium2 as pdfium  # noqa: F401

        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        c.drawString(50, 700, "secret")
        c.showPage()
        c.save()
        # reportlab 用 StandardEncryption 生成加密 PDF
        from reportlab.lib import pdfencrypt

        enc = pdfencrypt.StandardEncryption("pw", canPrint=0)
        buf2 = io.BytesIO()
        c2 = canvas.Canvas(buf2, pagesize=A4, encrypt=enc)
        c2.drawString(50, 700, "secret")
        c2.showPage()
        c2.save()
        with pytest.raises(merrors.MaterialsError) as ctx:
            validate_one(filename="enc.pdf", content_type="application/pdf", payload=buf2.getvalue())
        assert ctx.value.code in ("pdf_encrypted", "pdf_corrupt")


class TestQuotaLedger:
    def test_reserve_and_release(self, database, owner):
        q = QuotaService(database, default_quota_bytes=1000)
        q.reserve(owner, 300)
        assert q.snapshot(owner)["reserved_bytes"] == 300
        q.release(owner, 300)
        assert q.snapshot(owner)["reserved_bytes"] == 0

    def test_quota_exceeded(self, database, owner):
        q = QuotaService(database, default_quota_bytes=100)
        with pytest.raises(merrors.MaterialsError) as ctx:
            q.reserve(owner, 200)
        assert ctx.value.code == "quota_exceeded"

    def test_settle_moves_reserved_to_used(self, database, owner):
        q = QuotaService(database, default_quota_bytes=1000)
        q.reserve(owner, 400)
        view = q.settle(owner, reserved_delta=400, used_delta=350)
        assert view["reserved_bytes"] == 0
        assert view["used_bytes"] == 350

    def test_create_material_reserves(self, database, owner):
        svc = MaterialService(database, QuotaService(database, default_quota_bytes=10_000))
        v = ValidatedFile(
            kind="txt", size_bytes=120, sha256="a" * 64, filename_hash="b" * 64,
            payload=b"x", page_count=None,
        )
        view = svc.create_material(owner_user_id=owner, validated=v)
        assert view["status"] == "uploaded"
        assert svc.quotas.snapshot(owner)["reserved_bytes"] == 120

    def test_delete_releases_reservation_and_tombstones(self, database, owner):
        svc = MaterialService(database, QuotaService(database, default_quota_bytes=10_000))
        v = ValidatedFile(
            kind="txt", size_bytes=64, sha256="c" * 64, filename_hash="d" * 64,
            payload=b"y", page_count=None,
        )
        created = svc.create_material(owner_user_id=owner, validated=v)
        deleted = svc.delete(created["material_id"], actor_id=owner)
        assert deleted["deleted_at"] is not None
        assert deleted["tombstone_id"].startswith("tmb_")
        assert svc.quotas.snapshot(owner)["reserved_bytes"] == 0
        with pytest.raises(merrors.MaterialsError) as ctx:
            svc.delete(created["material_id"], actor_id=owner)
        assert ctx.value.code == "material_already_deleted"

    def test_cross_account_delete_denied(self, database, owner):
        svc = MaterialService(database, QuotaService(database, default_quota_bytes=10_000))
        v = ValidatedFile(
            kind="txt", size_bytes=8, sha256="e" * 64, filename_hash="f" * 64,
            payload=b"z", page_count=None,
        )
        created = svc.create_material(owner_user_id=owner, validated=v)
        with pytest.raises(merrors.MaterialsError) as ctx:
            svc.delete(created["material_id"], actor_id="intruder")
        assert ctx.value.code == "access_denied"


class TestRendering:
    @staticmethod
    def _payload(samples: dict, name: str) -> bytes:
        return Path(samples[name]["path"]).read_bytes()

    def test_200dpi_render(self, samples):
        payload = self._payload(samples, "text_layer")
        r = render_page(payload, 1)
        assert r.dpi == 200
        assert r.width_px > 0 and r.height_px > 0
        assert not r.degraded

    def test_page_out_of_range(self, samples):
        from backend.materials.errors import RenderError

        payload = self._payload(samples, "text_layer")
        with pytest.raises(RenderError) as ctx:
            render_page(payload, 99)
        assert ctx.value.code == "page_out_of_range"

    def test_pixel_budget_degrade(self):
        # A0 尺寸在 300 DPI 下会超过像素预算 → 降级
        from reportlab.lib.pagesizes import A0

        buf = io.BytesIO()
        from reportlab.pdfgen import canvas

        c = canvas.Canvas(buf, pagesize=A0)
        c.drawString(50, 50, "big")
        c.showPage()
        c.save()
        r = render_page(buf.getvalue(), 1, dpi=300)
        assert r.degraded
        assert r.width_px * r.height_px <= MAX_PAGE_PIXELS


class TestTextTrustworthiness:
    @staticmethod
    def _payload(samples: dict, name: str) -> bytes:
        return Path(samples[name]["path"]).read_bytes()

    def test_text_layer_trusted(self, samples):
        payload = self._payload(samples, "text_layer")
        ev = evaluate_page(payload, 1)
        assert ev.trustworthy is True
        assert ev.reason == "ok"

    def test_scanned_and_handwriting_untrusted(self, samples):
        for name in ("scanned_text", "handwriting"):
            ev = evaluate_page(self._payload(samples, name), 1)
            assert ev.trustworthy is False, name
            assert ev.effective_ratio < 0.6

    def test_garbled_text_untrusted(self):
        ev = evaluate_text_layer("\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd" * 3)
        assert ev.trustworthy is False
        assert "high_garbage_ratio" in ev.reason
        assert ev.garbage_ratio > 0.15

    def test_too_few_chars_untrusted(self):
        ev = evaluate_text_layer("hi")
        assert ev.trustworthy is False
        assert "too_few_chars" in ev.reason

    def test_pure_ascii_trusted(self):
        ev = evaluate_text_layer("The quick brown fox jumps over the lazy dog 1234567890")
        assert ev.trustworthy is True


class TestTokenizer:
    def test_jieba_version_locked(self):
        import jieba

        assert jieba.__version__ == JIEBA_VERSION

    def test_chinese_tokens(self):
        tokens = jieba_tokens("神经网络与反向传播")
        assert "神经网络" in tokens
        # jieba 0.42.1 将“反向传播”切为“反向”+“传播”；记录确定性行为，
        # 若升级词典导致切分变化必须递增 source_version 并重建索引。
        assert tokens == ["神经网络", "与", "反向", "传播"]

    def test_chinese_tokens_are_deterministic(self):
        assert jieba_tokens("卷积层特征提取") == jieba_tokens("卷积层特征提取")

    def test_identifiers_and_versions_are_tokens(self):
        t = build_tokens(text="conv2d_block_7 v1.2.3", page_no=4)
        assert "conv2d_block_7" in t.tokens
        assert "v1.2.3" in t.tokens
        assert "p4" in t.tokens
        assert t.source_version == SOURCE_VERSION
