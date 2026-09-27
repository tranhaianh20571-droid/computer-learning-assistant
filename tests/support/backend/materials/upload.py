"""资料上传校验与配额账本（切片 2 T02/T07）。

契约（交接文档 §3.1「上传接收契约」）：
- 扩展名 + MIME + 文件签名三重校验；
- 单文件 ≤20 MiB；单次 ≤10 个；个人占用 ≤5 GiB；
- PDF 解析页数，扫描 PDF > 1000 页 → `too_many_pages`；
- 加密/损坏 PDF 明确报错；TXT/Markdown 仅 UTF-8（含 BOM）；
- 预留配额在上传成功时记账，解析失败/删除时归还。
"""

from __future__ import annotations

import hashlib
import io
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import select

from ..logging_audit.db import Database
from .errors import MaterialsError, error
from .models import MaterialRow, MaterialUploadQuotaRow, utcnow

MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MiB
MAX_FILES_PER_BATCH = 10
DEFAULT_QUOTA_BYTES = 5 * 1024 * 1024 * 1024  # 5 GiB
MAX_SCANNED_PDF_PAGES = 1000

# 扩展名 → 允许的 kind
EXTENSION_KINDS = {
    ".pdf": "pdf",
    ".md": "md",
    ".markdown": "md",
    ".txt": "txt",
}

# 扩展名 → 允许的 MIME（宽松匹配，签名才是最终判据）
ALLOWED_MIME = {
    ".pdf": ("application/pdf", "application/x-pdf", "application/octet-stream"),
    ".md": ("text/markdown", "text/plain", "text/x-markdown", "application/octet-stream"),
    ".markdown": ("text/markdown", "text/plain", "text/x-markdown", "application/octet-stream"),
    ".txt": ("text/plain", "application/octet-stream"),
}

PDF_MAGIC = b"%PDF-"
BOM_UTF8 = b"\xef\xbb\xbf"


@dataclass
class ValidatedFile:
    kind: str
    size_bytes: int
    sha256: str
    filename_hash: str
    payload: bytes
    page_count: Optional[int] = None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def filename_hash(filename: str) -> str:
    """只存散列，不存完整文件名（安全规范）。"""
    return _sha256(os.path.basename(filename or "").encode("utf-8", errors="replace"))


def _extension(filename: str) -> str:
    _, ext = os.path.splitext(os.path.basename(filename or ""))
    return ext.lower()


def validate_extension_and_mime(filename: str, content_type: Optional[str]) -> str:
    ext = _extension(filename)
    if ext not in EXTENSION_KINDS:
        raise error("unsupported_media_type", f"unsupported extension {ext or '<none>'}")
    if content_type:
        allowed = ALLOWED_MIME[ext]
        if content_type.split(";")[0].strip().lower() not in allowed:
            raise error("unsupported_media_type", "mime/extension mismatch")
    return ext


def validate_signature(ext: str, payload: bytes) -> None:
    if not payload:
        raise error("empty_file")
    if ext == ".pdf":
        if not payload.lstrip().startswith(PDF_MAGIC) and PDF_MAGIC not in payload[:1024]:
            raise error("invalid_file_signature", "pdf signature missing")
        return
    # 文本类：拒绝明显二进制（NUL 字节）
    if b"\x00" in payload[:8192]:
        raise error("invalid_file_signature", "binary content in text file")


def decode_text(payload: bytes) -> str:
    """TXT/Markdown 仅 UTF-8（允许 BOM）。失败 → invalid_encoding。"""
    raw = payload[3:] if payload.startswith(BOM_UTF8) else payload
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise error("invalid_encoding", "not valid utf-8") from exc


def pdf_page_count(payload: bytes) -> int:
    """用 pypdfium2 读取页数；加密/损坏 PDF 映射到受控错误。"""
    import pypdfium2 as pdfium

    try:
        doc = pdfium.PdfDocument(payload)
    except Exception as exc:  # noqa: BLE001 - 供应商异常不外泄
        message = str(exc).lower()
        if "password" in message or "encrypt" in message:
            raise error("pdf_encrypted") from exc
        raise error("pdf_corrupt") from exc
    try:
        return len(doc)
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass


def validate_one(
    *,
    filename: str,
    content_type: Optional[str],
    payload: bytes,
    check_pages: bool = True,
) -> ValidatedFile:
    ext = validate_extension_and_mime(filename, content_type)
    if len(payload) > MAX_FILE_BYTES:
        raise error("file_too_large", f"max {MAX_FILE_BYTES} bytes")
    validate_signature(ext, payload)
    kind = EXTENSION_KINDS[ext]
    page_count: Optional[int] = None
    if kind in ("md", "txt"):
        decode_text(payload)  # 触发编码校验
    if kind == "pdf" and check_pages:
        page_count = pdf_page_count(payload)
        if page_count <= 0:
            raise error("pdf_corrupt", "empty pdf")
        if page_count > MAX_SCANNED_PDF_PAGES:
            raise error("too_many_pages", f"max {MAX_SCANNED_PDF_PAGES} pages")
    return ValidatedFile(
        kind=kind,
        size_bytes=len(payload),
        sha256=_sha256(payload),
        filename_hash=filename_hash(filename),
        payload=payload,
        page_count=page_count,
    )


def validate_batch(files: list[tuple[str, Optional[str], bytes]]) -> list[ValidatedFile]:
    if not files:
        raise error("empty_file", "no files")
    if len(files) > MAX_FILES_PER_BATCH:
        raise error("too_many_files", f"max {MAX_FILES_PER_BATCH} files")
    return [
        validate_one(filename=name, content_type=ctype, payload=payload)
        for name, ctype, payload in files
    ]


class QuotaService:
    """上传配额账本。预留在上传时记账，解析完成按实际产物调整，失败/删除归还。"""

    def __init__(self, db: Database, *, default_quota_bytes: int = DEFAULT_QUOTA_BYTES) -> None:
        self.db = db
        self.default_quota_bytes = default_quota_bytes

    def _row(self, sess, owner_user_id: str) -> MaterialUploadQuotaRow:
        row = sess.get(MaterialUploadQuotaRow, owner_user_id)
        if row is None:
            row = MaterialUploadQuotaRow(
                owner_user_id=owner_user_id,
                quota_bytes=self.default_quota_bytes,
                reserved_bytes=0,
                used_bytes=0,
                updated_at=utcnow(),
            )
            sess.add(row)
            sess.flush()
        return row

    def snapshot(self, owner_user_id: str) -> dict:
        with self.db.session() as sess:
            row = self._row(sess, owner_user_id)
            sess.commit()
            return self._view(row)

    @staticmethod
    def _view(row: MaterialUploadQuotaRow) -> dict:
        return {
            "owner_user_id": row.owner_user_id,
            "quota_bytes": row.quota_bytes,
            "reserved_bytes": row.reserved_bytes,
            "used_bytes": row.used_bytes,
            "available_bytes": max(0, row.quota_bytes - row.reserved_bytes - row.used_bytes),
        }

    def reserve(self, owner_user_id: str, bytes_: int) -> dict:
        with self.db.session() as sess:
            row = self._row(sess, owner_user_id)
            if row.reserved_bytes + row.used_bytes + bytes_ > row.quota_bytes:
                raise error("quota_exceeded", "not enough quota")
            row.reserved_bytes += bytes_
            row.updated_at = utcnow()
            sess.flush()
            view = self._view(row)
            sess.commit()
            return view

    def settle(self, owner_user_id: str, *, reserved_delta: int, used_delta: int) -> dict:
        """解析完成：把预留转为实际占用（可为负，归还预留）。"""
        with self.db.session() as sess:
            row = self._row(sess, owner_user_id)
            row.reserved_bytes = max(0, row.reserved_bytes - reserved_delta)
            row.used_bytes = max(0, row.used_bytes + used_delta)
            row.updated_at = utcnow()
            sess.flush()
            view = self._view(row)
            sess.commit()
            return view

    def release(self, owner_user_id: str, bytes_: int) -> dict:
        """解析失败/取消：归还预留。"""
        with self.db.session() as sess:
            row = self._row(sess, owner_user_id)
            row.reserved_bytes = max(0, row.reserved_bytes - bytes_)
            row.updated_at = utcnow()
            sess.flush()
            view = self._view(row)
            sess.commit()
            return view


class MaterialService:
    """资料登记与删除边界（本切片不实现恢复流程）。"""

    def __init__(self, db: Database, quotas: Optional[QuotaService] = None) -> None:
        self.db = db
        self.quotas = quotas or QuotaService(db)

    def create_material(
        self,
        *,
        owner_user_id: str,
        validated: ValidatedFile,
    ) -> dict:
        self.quotas.reserve(owner_user_id, validated.size_bytes)
        with self.db.session() as sess:
            material_id = f"mat_{uuid.uuid4().hex[:12]}"
            now = utcnow()
            row = MaterialRow(
                material_id=material_id,
                owner_user_id=owner_user_id,
                filename_hash=validated.filename_hash,
                source_bytes_sha256=validated.sha256,
                kind=validated.kind,
                size_bytes=validated.size_bytes,
                quota_reserved_bytes=validated.size_bytes,
                status="uploaded",
                created_at=now,
                uploaded_at=now,
            )
            sess.add(row)
            sess.flush()
            view = self._view(row)
            sess.commit()
            return view

    @staticmethod
    def _view(row: MaterialRow) -> dict:
        return {
            "material_id": row.material_id,
            "owner_user_id": row.owner_user_id,
            "filename_hash": row.filename_hash,
            "source_bytes_sha256": row.source_bytes_sha256,
            "kind": row.kind,
            "size_bytes": row.size_bytes,
            "quota_reserved_bytes": row.quota_reserved_bytes,
            "status": row.status,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
        }

    def get(self, material_id: str, *, actor_id: str) -> dict:
        with self.db.session() as sess:
            row = sess.get(MaterialRow, material_id)
            if row is None:
                raise error("material_not_found")
            if row.owner_user_id != actor_id:
                raise error("access_denied", "not owner")
            return self._view(row)

    def list_for_owner(self, owner_user_id: str, *, include_deleted: bool = False) -> list[dict]:
        with self.db.session() as sess:
            rows = (
                sess.execute(
                    select(MaterialRow)
                    .where(MaterialRow.owner_user_id == owner_user_id)
                    .order_by(MaterialRow.created_at.desc())
                )
                .scalars()
                .all()
            )
            return [
                self._view(r)
                for r in rows
                if include_deleted or r.deleted_at is None
            ]

    def delete(self, material_id: str, *, actor_id: str) -> dict:
        """删除资料：软删除 + 立即从检索范围移除 + 归还预留配额。"""
        from .models import AssetUsageLedgerRow, DeletionTombstoneRow

        with self.db.session() as sess:
            row = sess.get(MaterialRow, material_id)
            if row is None:
                raise error("material_not_found")
            if row.owner_user_id != actor_id:
                raise error("access_denied", "not owner")
            if row.deleted_at is not None:
                raise error("material_already_deleted")
            now = utcnow()
            row.deleted_at = now
            row.status = "failed"
            tombstone_id = f"tmb_{uuid.uuid4().hex[:12]}"
            sess.add(
                DeletionTombstoneRow(
                    tombstone_id=tombstone_id,
                    object_type="material",
                    object_id=material_id,
                    owner_user_id=actor_id,
                    deleted_at=now,
                    cleanup_stage="pending",
                )
            )
            sess.add(
                AssetUsageLedgerRow(
                    ledger_id=f"led_{uuid.uuid4().hex[:12]}",
                    object_type="material",
                    object_id=material_id,
                    owner_user_id=actor_id,
                    last_referenced_at=now,
                    tombstoned_at=now,
                    deletion_reason="user_delete",
                )
            )
            reserved = row.quota_reserved_bytes
            row.quota_reserved_bytes = 0
            sess.flush()
            view = self._view(row) | {"tombstone_id": tombstone_id}
            sess.commit()
        if reserved:
            self.quotas.release(actor_id, reserved)
        return view
