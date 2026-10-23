"""Durable personal document attachments, isolated from the future knowledge library."""
from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path
import tempfile
import uuid
import zipfile
from threading import Event, Lock, Thread
from xml.etree import ElementTree

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from .errors import error
from .models import DocumentAttachmentRow, MaterialPageRow, MaterialRow, utcnow
from .storage import MaterialStorage, StorageError
from .upload import ValidatedFile, filename_hash, pdf_page_count
from .local_documents import MAX_TEXT_BYTES, parse_text, text_content
from .pptx_documents import (MAX_PPTX_BYTES, MAX_PPTX_SLIDES, inspect_pptx,
                             pptx_batch_bytes, plan_pptx_batches)
from .document_pipeline import (MAX_PDF_BYTES, MAX_PDF_PAGES, PROVIDER_MAX_BYTES,
    PROVIDER_MAX_PAGES, DIRECT_MAX_PAGES, document_strategy,
    plan_batches, pdf_batch_bytes, normalize_result, finalize_document, document_markdown,
    complex_table_pages, figure_review_pages, figure_understanding_pages)
from ..slice1.adapters.somark import SomarkAdapter
from ..slice1.adapters.paddle_vl import PaddleVLAdapter
from ..slice1.errors import Slice1Error
from ..slice1.models import ServiceConfigRow, DOCUMENT_DESTINATIONS
from ..tasks.service import LearningTaskRow, LeaseError

MAX_DOCUMENT_BYTES = MAX_PDF_BYTES
MAX_DOCUMENT_PAGES = MAX_PDF_PAGES
LOCAL_KINDS = ("txt", "md")


def attachment_strategy(kind, pages, size, target_bytes=45_000_000):
    if kind in LOCAL_KINDS:
        return "local"
    if kind == "pptx":
        return "batched" if size > 50_000_000 else "direct"
    return document_strategy(pages, size, target_bytes=target_bytes) if kind == "pdf" else "direct"


def document_deadline(pages, *, semantic=False):
    ocr = math.ceil((pages or 1) / DIRECT_MAX_PAGES) * 1800
    questions = math.ceil((pages or 1) / 4) * 900 if semantic else 0
    return min(8 * 3600, max(1800, ocr + questions))


def document_failure_code(exc, kind):
    code = {"credential_invalid": "MODEL_CREDENTIAL_INVALID", "quota_exceeded": "UPLOAD_QUOTA_EXCEEDED",
            "timeout": "OCR_PAGE_TIMEOUT", "connection_error": "OCR_PAGE_TIMEOUT",
            "rate_limited": "MODEL_RATE_LIMITED",
            "QUOTA_EXCEEDED": "UPLOAD_QUOTA_EXCEEDED",
            "TASK_CANCELLED": "TASK_CANCELLED", "disclosure_revoked": "MODEL_CREDENTIAL_INVALID",
            "disclosure_required": "MODEL_CREDENTIAL_INVALID"}.get(getattr(exc, "code", ""), "OCR_OUTPUT_INVALID")
    if kind in LOCAL_KINDS and code == "OCR_OUTPUT_INVALID":
        return "LOCAL_PARSE_INVALID"
    if isinstance(exc, ValueError) and str(exc) == "pptx_slide_too_large":
        return "PPTX_SLIDE_TOO_LARGE"
    return code


def validate_document(name, mime, payload):
    kind = name.rsplit(".", 1)[-1].lower()
    if kind == "markdown":
        kind = "md"
    allowed = {"pdf": {"application/pdf", "application/x-pdf"},
               "doc": {"application/msword"},
               "txt": {"text/plain"},
               "md": {"text/markdown", "text/plain", "text/x-markdown"},
               "pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
               "docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}}
    if kind not in allowed or mime and mime.split(";")[0].lower() not in allowed[kind] | {"application/octet-stream", ""}:
        raise error("unsupported_media_type", "支持 PDF、Word、PPTX、Markdown、TXT。")
    is_path = isinstance(payload, Path)
    size = payload.stat().st_size if is_path else len(payload)
    if not size:
        raise error("empty_file")
    maximum = (MAX_TEXT_BYTES if kind in LOCAL_KINDS else MAX_PPTX_BYTES if kind == "pptx"
               else MAX_DOCUMENT_BYTES if kind == "pdf" else PROVIDER_MAX_BYTES)
    if size > maximum:
        raise error("file_too_large", "文本最大 20 MiB，PPTX/Word 最大 200 MB，PDF 最大 512 MB。")
    pages = None
    if kind in LOCAL_KINDS:
        text_content(payload.read_bytes() if is_path else payload)
        pages = 0
    elif kind == "pptx":
        try:
            pages = len(inspect_pptx(payload))
        except Exception as exc:
            code = "too_many_pages" if str(exc) == "too_many_pages" else "invalid_file_signature"
            raise error(code, "PPTX 无效、含不支持的结构或超过 300 张。") from None
    elif kind == "pdf":
        with payload.open("rb") if is_path else io.BytesIO(payload) as stream:
            signature = stream.read(5)
        if signature != b"%PDF-":
            raise error("invalid_file_signature")
        pages = pdf_page_count(payload)
        if not 1 <= pages <= MAX_DOCUMENT_PAGES:
            raise error("too_many_pages", "每份 PDF 最多 2000 页。")
    elif kind == "doc":
        # Verify the compound document directory, not only the shared OLE signature.
        import olefile
        try:
            with olefile.OleFileIO(str(payload) if is_path else io.BytesIO(payload)) as doc:
                if not doc.exists("WordDocument"):
                    raise ValueError()
        except Exception:
            raise error("invalid_file_signature", "Word 文件无效或已加密。") from None
    else:
        try:
            with zipfile.ZipFile(payload if is_path else io.BytesIO(payload)) as doc:
                names = doc.namelist()
                if ("word/document.xml" not in names or "[Content_Types].xml" not in names
                        or any(n.lower().endswith("vbaproject.bin") for n in names)):
                    raise ValueError()
                for part in ("word/document.xml", "[Content_Types].xml"):
                    entry = doc.getinfo(part)
                    if entry.file_size > 8_000_000 or entry.flag_bits & 1:
                        raise ValueError()
                    root = ElementTree.fromstring(doc.read(part))
                    if part == "word/document.xml" and root.tag != "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document":
                        raise ValueError()
        except Exception:
            raise error("invalid_file_signature", "Word 文件无效或已加密。") from None
    with payload.open("rb") if is_path else io.BytesIO(payload) as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return ValidatedFile(kind=kind, size_bytes=size, sha256=digest,
                         filename_hash=filename_hash(name), payload=payload, page_count=pages)


class AttachmentService:
    def __init__(self, db, materials, configs, disclosures, tasks, storage, *, adapter_factory=None):
        self.db, self.materials, self.configs, self.disclosures = db, materials, configs, disclosures
        self.tasks, self.storage = tasks, storage
        self.adapter_factory = adapter_factory or (lambda view, credentials:
            (PaddleVLAdapter if view["protocol"] == "paddleocr_vl" else SomarkAdapter)(
            endpoint=view["endpoint"], model_name=view["model_name"], credentials=credentials,
            max_response_bytes=32_000_000))
        self.stop = Event()
        self.lock = Lock()
        self.thread = None
        from .question_service import QuestionService
        self.questions = QuestionService(self)

    def config(self, owner):
        view = self.configs.latest_active_for_owner(owner, "document_parser")
        return view if view and view["protocol"] == "paddleocr_vl" and view["owner_scope"] == "user" else None

    def _config(self, owner, ident, version):
        view = self.configs.get(ident, actor_id=owner, is_admin=False)
        if (view["kind"] != "document_parser" or DOCUMENT_DESTINATIONS.get(view["protocol"]) !=
                (view["endpoint"], view["model_name"]) or not view["is_active"]
                or view["config_version"] != version):
            raise Slice1Error("disclosure_revoked", "文档识别配置已变化，请重新确认。", 409)
        return view

    def _owned(self, sess, ident, owner, *, lock=False):
        material = sess.get(MaterialRow, ident, with_for_update=lock)
        row = sess.get(DocumentAttachmentRow, ident, with_for_update=lock)
        if row and row.owner_user_id != owner:
            self.configs.audit.append("access.denied", actor_id=owner, object_type="material",
                object_id=row.material_id, result="denied", reason="material_not_found")
        if (not material or not row or material.deleted_at or material.owner_user_id != owner
                or row.owner_user_id != owner):
            raise error("material_not_found")
        return material, row

    def view(self, ident, owner):
        with self.db.session() as sess:
            material, row = self._owned(sess, ident, owner)
            task = sess.get(LearningTaskRow, row.task_id)
            status = material.status
            if status == "parsing" and task and task.status in ("failed", "cancelled", "expired"):
                status = "failed"
            result = json.loads(row.result_json)
            batches = result.get("batches", [])
            page_results = result.get("page_results", [])
            processed = len(page_results) if batches else (result.get("pages", 0) if status == "parsed" else 0)
            return {"material_id": ident, "task_id": row.task_id, "status": status,
                    "kind": material.kind, "size_bytes": material.size_bytes,
                    "unit": result.get("unit", "page"), "local": material.kind in LOCAL_KINDS,
                    "requires_ocr": material.kind not in LOCAL_KINDS,
                    "line_count": result.get("line_count", 0),
                    "failed_slide": result.get("failed_slide"),
                    "page_count": result.get("pages") or 0, "failure_code": row.failure_code,
                    "processed_pages": processed, "strategy": result.get("strategy", "direct"),
                    "batch_count": len(batches), "completed_batches": sum(b["status"] == "completed" for b in batches),
                    "empty_pages": [p["page_no"] for p in page_results if p["status"] == "empty"],
                    "complex_table_pages": complex_table_pages(result),
                    "figure_review_pages": figure_review_pages(result),
                    "figure_understanding_pages": figure_understanding_pages(result),
                    "outline_count": len(result.get("outline", [])),
                    "question_extraction": {k: result.get("question_bank", {}).get(k) for k in
                        ("status", "counts")},
                    "extraction_destinations": {role: {k: target[k] for k in
                        ("config_id", "config_version", "endpoint")} for role, target in
                        result.get("extraction_binding", {}).get("targets", {}).items()},
                    "batches": [{"start_page": b["start"], "end_page": b["end"],
                                 "status": b["status"]} for b in batches],
                    "phase": "complete" if status == "parsed" else "failed" if status == "failed" else
                             "extracting_questions" if result.get("question_bank") else
                             "recognizing" if batches else "queued"}

    def preflight(self, validated, owner=None):
        view = self.config(owner) if owner else None
        target = 45_000_000 if view and view["protocol"] == "paddleocr_vl" else 100_000_000
        strategy = attachment_strategy(validated.kind, validated.page_count, validated.size_bytes, target)
        slide_metadata = inspect_pptx(validated.payload) if validated.kind == "pptx" else []
        return {"kind": validated.kind, "size_bytes": validated.size_bytes,
                "hidden_slides": sum(s["hidden"] for s in slide_metadata),
                "notes_slides": sum(bool(s["notes"]) for s in slide_metadata),
                "unit": "document" if validated.kind in LOCAL_KINDS else "slide" if validated.kind == "pptx" else "page",
                "local": validated.kind in LOCAL_KINDS, "requires_ocr": validated.kind not in LOCAL_KINDS,
                "page_count": validated.page_count, "strategy": strategy,
                "batch_pages": DIRECT_MAX_PAGES if strategy == "batched" else validated.page_count}

    def image(self, ident, owner, page_no, block_no, expected_crop=None):
        import pypdfium2 as pdfium
        from .upload import PDFIUM_LOCK
        with self.db.session() as sess:
            try:
                from .figure_crops import owned_pdf_state
                _, _, state = owned_pdf_state(sess, owner, ident)
                page = next(p for p in state["page_results"] if p["page_no"] == page_no)
                block = page["blocks"][block_no]
                if block_no < 0 or block["type"] != "figure":
                    raise ValueError()
                if block.get("crop"):
                    from .figure_crops import load_crop
                    if expected_crop and expected_crop != block["crop"]["sha256"]:
                        raise ValueError()
                    return load_crop(self.storage, ident, page_no, block_no, block["crop"])
                if expected_crop:
                    raise ValueError()
                box = block.get("source_bbox")
                if box is None and block.get("coordinate_space") == "processed":
                    box = [0, 0, 1, 1]
                if box is None:
                    raise ValueError()
            except (KeyError, IndexError, StopIteration, ValueError, TypeError, StorageError):
                raise error("material_not_found") from None
            # Local rendering under the ownership transaction; no provider URLs.
            source = self.storage.local_path(MaterialStorage.original_key(ident))
            with PDFIUM_LOCK, pdfium.PdfDocument(source) as doc:
                original = doc[page_no - 1]
                width, height = original.get_size()
                scale = min(2, 2400 / max(width, height))
                image = original.render(scale=scale).to_pil()
                pixel_width, pixel_height = image.size
                image = image.crop((int(box[0]*pixel_width), int(box[1]*pixel_height),
                                    math.ceil(box[2]*pixel_width), math.ceil(box[3]*pixel_height)))
                output = io.BytesIO()
                image.save(output, format="PNG")
                return output.getvalue()

    def markdown(self, ident, owner):
        with self.db.session() as sess:
            material, row = self._owned(sess, ident, owner)
            if material.status != "parsed":
                raise error("page_state_conflict", "资料尚未完整识别。")
            if material.kind in LOCAL_KINDS:
                return text_content(self.storage.get(MaterialStorage.original_key(ident)))
            return document_markdown(json.loads(row.result_json))

    def list(self, owner):
        with self.db.session() as sess:
            ids = list(sess.scalars(select(DocumentAttachmentRow.material_id).join(MaterialRow).where(
                DocumentAttachmentRow.owner_user_id == owner, MaterialRow.deleted_at.is_(None))
                .order_by(MaterialRow.created_at.desc()).limit(100)))
        return [self.view(ident, owner) for ident in ids]

    def create(self, owner, validated, config_id, version, key, *, extraction=None):
        local = validated.kind in LOCAL_KINDS
        destination = None if local else self._config(owner, config_id, version)
        if local:
            config_id, version = None, None
        upload_key = hashlib.sha256(key.encode()).hexdigest()
        ident = None
        created_new = False
        reserved = False
        try:
            # The owner lock serializes HTTP retries including quota reservations.
            with self.db.session() as sess:
                if sess.bind.dialect.name == "postgresql":
                    from sqlalchemy import func
                    sess.execute(select(func.pg_advisory_xact_lock(func.hashtextextended("attachment:" + owner, 0))))
                old = sess.scalar(select(DocumentAttachmentRow).where(
                    DocumentAttachmentRow.owner_user_id == owner, DocumentAttachmentRow.upload_key == upload_key))
                if old:
                    material, _ = self._owned(sess, old.material_id, owner)
                    if material.source_bytes_sha256 != validated.sha256:
                        raise error("page_state_conflict", "重复请求的文件不同。")
                    ident = old.material_id
                else:
                    self.materials.quotas.reserve(owner, validated.size_bytes)
                    reserved = True
                    ident = "mat_" + uuid.uuid4().hex[:12]
                    created_new = True
                    material = MaterialRow(material_id=ident, owner_user_id=owner,
                        filename_hash=validated.filename_hash, source_bytes_sha256=validated.sha256,
                        kind=validated.kind, size_bytes=validated.size_bytes,
                        quota_reserved_bytes=validated.size_bytes, status="parsing")
                    sess.add(material)
                    sess.flush()
                    if isinstance(validated.payload, Path):
                        self.storage.put_file(MaterialStorage.original_key(ident), validated.payload)
                    else:
                        self.storage.put(MaterialStorage.original_key(ident), validated.payload)
                    task = self.tasks.create_task(owner_user_id=owner, kind="material_parse", subject_id=ident,
                        idempotency_key="attachment:" + upload_key, stage="parse", lane="maintenance" if local else "ocr",
                        deadline_seconds=document_deadline(validated.page_count,
                            semantic=bool(extraction and extraction.get("semantic"))),
                        progress_total=validated.page_count or 0, session=sess)
                    state = {"pages": validated.page_count,
                        "unit": "document" if local else "slide" if validated.kind == "pptx" else "page",
                        "strategy": attachment_strategy(validated.kind, validated.page_count, validated.size_bytes,
                            45_000_000 if not destination or destination["protocol"] == "paddleocr_vl"
                            else 100_000_000)}
                    if destination and destination["protocol"] == "paddleocr_vl":
                        from ..slice1.adapters.paddle_vl import document_options, crop_document_options
                        state["ocr_options"] = document_options()
                        state["ocr_crop_options"] = crop_document_options()
                    if extraction and extraction.get("semantic"):
                        state["extraction_binding"] = self.questions.bind(sess, owner=owner,
                            task_id=task["task_id"], material_id=ident, sha256=validated.sha256,
                            targets=extraction, pages=validated.page_count)
                    sess.add(DocumentAttachmentRow(material_id=ident, owner_user_id=owner,
                        upload_key=upload_key, task_id=task["task_id"], config_id=config_id, config_version=version,
                        result_json=json.dumps(state)))
                    if not local:
                        self.disclosures.create(actor_id=owner, task_id=task["task_id"], config_id=config_id,
                            config_version=version, content_category="material",
                            scope_snapshot={"material_ids": [ident], "sha256": validated.sha256}, session=sess)
        except Exception:
            if ident and created_new:
                try:
                    self.storage.delete(MaterialStorage.original_key(ident))
                except Exception:
                    pass
            if reserved:
                self.materials.quotas.release(owner, validated.size_bytes)
            raise
        return self.view(ident, owner)

    def retry(self, ident, owner, config_id, version):
        with self.db.session() as sess:
            material, row = self._owned(sess, ident, owner, lock=True)
            local = material.kind in LOCAL_KINDS
            if local:
                config_id, version = None, None
            else:
                self._config(owner, config_id, version)
            task = sess.get(LearningTaskRow, row.task_id)
            if material.status != "failed" and task.status not in ("failed", "cancelled", "expired"):
                raise error("page_state_conflict", "仅失败的附件可重试。")
            state = json.loads(row.result_json)
            extraction = state.get("extraction_binding")
            previous_task = row.task_id
            if extraction:
                self.questions.guard(sess, owner, row.task_id, material, extraction)
            changed = (row.config_id, row.config_version) != (config_id, version)
            if changed:
                state = {"pages": state.get("pages"), "strategy": state.get("strategy", "direct"),
                         "unit": state.get("unit", "page")}
            else:
                for batch in state.get("batches", []):
                    if batch["status"] != "completed" and batch.get("job_id") == "submitting":
                        batch.pop("job_id", None)
                        batch["status"] = "pending"
                    if batch.get("crop_status") == "failed" or batch.get("crop_job_id") == "submitting":
                        batch.pop("crop_job_id", None)
                        batch.pop("crop_status", None)
            row.result_json = json.dumps(state, ensure_ascii=False)
            task = self.tasks.create_task(owner_user_id=owner, kind="material_parse", subject_id=ident,
                idempotency_key="attachment-retry:" + uuid.uuid4().hex, stage="parse", lane="maintenance" if local else "ocr",
                deadline_seconds=document_deadline(state.get("pages"), semantic=bool(extraction)),
                progress_total=state.get("pages") or 0, session=sess)
            if changed or row.external_job_id == "submitting":
                row.external_job_id = None
            row.task_id, row.config_id, row.config_version = task["task_id"], config_id, version
            row.failure_code, material.status = None, "parsing"
            if extraction:
                state["extraction_binding"] = self.questions.bind(sess, owner=owner,
                    task_id=task["task_id"], material_id=ident, sha256=material.source_bytes_sha256,
                    targets=extraction["targets"], pages=state.get("pages"),
                    previous_task=previous_task, previous=extraction)
                row.result_json = json.dumps(state, ensure_ascii=False)
            if not local:
                self.disclosures.create(actor_id=owner, task_id=task["task_id"], config_id=config_id,
                    config_version=version, content_category="material",
                    scope_snapshot={"material_ids": [ident], "sha256": material.source_bytes_sha256}, session=sess)
        return self.view(ident, owner)

    def remove(self, ident, owner):
        current = self.view(ident, owner)
        self.tasks.request_cancel(current["task_id"], actor_id=owner)
        with self.db.session() as sess:
            _, row = self._owned(sess, ident, owner, lock=True)
            row.result_json = "{}"
            self.configs.audit.append("resource.delete.requested", actor_id=owner, object_type="material",
                                     object_id=ident, result="requested", session=sess)
        return self.materials.delete(ident, actor_id=owner)

    def _guard(self, lease, ident, owner):
        self.tasks.validate_lease(lease, actor_id=owner)
        with self.db.session() as sess:
            material, row = self._owned(sess, ident, owner)
            if row.task_id != lease.task_id:
                raise LeaseError("LEASE_LOST", "attachment task changed")
            binding = json.loads(row.result_json).get("extraction_binding")
            if binding:
                self.questions.guard(sess, owner, row.task_id, material, binding)
            if material.kind in LOCAL_KINDS:
                return None, material.kind, None
            view = self._config(owner, row.config_id, row.config_version)
            self.disclosures.verify_or_audit(task_id=row.task_id, config_id=row.config_id,
                content_category="material", actor_id=owner,
                scope_snapshot={"material_ids": [ident], "sha256": material.source_bytes_sha256})
            return view, material.kind, row.external_job_id

    def _checkpoint(self, lease, ident, owner, state):
        self._guard(lease, ident, owner)
        with self.db.session() as sess:
            self.tasks.validate_lease(lease, actor_id=owner, session=sess)
            material, row = self._owned(sess, ident, owner, lock=True)
            if row.task_id != lease.task_id:
                raise LeaseError("LEASE_LOST", "attachment task changed")
            self._publish_guard(sess, material, row, lease.task_id, owner)
            row.result_json = json.dumps(state, ensure_ascii=False)
            active = next((b for b in state.get("batches", []) if b["status"] != "completed"), {})
            row.external_job_id = active.get("job_id")

    def _publish_guard(self, sess, material, row, task_id, owner):
        if row.task_id != task_id:
            raise LeaseError("LEASE_LOST", "attachment task changed")
        binding = json.loads(row.result_json).get("extraction_binding")
        if binding:
            self.questions.guard(sess, owner, task_id, material, binding)
        if material.kind not in LOCAL_KINDS:
            config = sess.get(ServiceConfigRow, row.config_id, with_for_update=True)
            if not config or not config.is_active or config.config_version != row.config_version:
                raise Slice1Error("disclosure_revoked", "文档识别配置已变化。", 409)
            self.disclosures.verify(task_id=task_id, config_id=row.config_id,
                content_category="material", actor_id=owner, session=sess,
                scope_snapshot={"material_ids": [material.material_id],
                                "sha256": material.source_bytes_sha256})

    def _publish(self, sess, lease, ident, owner, state):
        material, row = self._owned(sess, ident, owner, lock=True)
        self._publish_guard(sess, material, row, lease.task_id, owner)
        from .figure_crops import crop_storage_bytes
        from .models import MaterialUploadQuotaRow
        size = material.size_bytes + crop_storage_bytes(self.storage, ident, state)
        delta = size - material.quota_reserved_bytes
        if delta:
            quota = sess.get(MaterialUploadQuotaRow, owner, with_for_update=True)
            if delta > quota.quota_bytes - quota.reserved_bytes - quota.used_bytes:
                raise error("quota_exceeded")
            quota.reserved_bytes += delta
            material.quota_reserved_bytes = size
        row.result_json = json.dumps(state, ensure_ascii=False)
        row.failure_code, material.status = None, "parsed"
        from .corpus import ensure_document
        ensure_document(sess, owner, material, row)
        if material.kind not in LOCAL_KINDS:
            for page in state["page_results"]:
                found = sess.scalar(select(MaterialPageRow).where(
                    MaterialPageRow.material_id == ident, MaterialPageRow.page_no == page["page_no"]))
                if not found:
                    sess.add(MaterialPageRow(page_id="pg_" + uuid.uuid4().hex[:12],
                        material_id=ident, page_no=page["page_no"], status="success"))

    def process_document(self, source, kind, state, *, adapter, protocol, guard, checkpoint,
                         stop, external=None, image_prefix=None, material_id=None):
        """Parse a validated private original with caller-owned authorization and checkpoints."""
        guard()
        if protocol == "paddleocr_vl":
            from ..slice1.adapters.paddle_vl import DOCUMENT_OPTIONS, LEGACY_DOCUMENT_OPTIONS, document_options
            if "ocr_options" not in state:
                started = external or any(b.get("job_id") or b["status"] == "completed"
                                         for b in state.get("batches", []))
                state["ocr_options"] = dict(LEGACY_DOCUMENT_OPTIONS if started else DOCUMENT_OPTIONS)
                if not started:
                    from ..slice1.adapters.paddle_vl import crop_document_options
                    state["ocr_crop_options"] = crop_document_options(state["ocr_options"])
            adapter.document_options = document_options(state["ocr_options"])
        extraction_context = {k: state[k] for k in
            ("extraction_binding", "question_bank", "native_pdf_blocks", "ocr_options", "ocr_crop_options") if k in state}
        from .local_execution import execute_local
        if kind in LOCAL_KINDS:
            state = execute_local("text", source, kind=kind, guard=guard, stop=stop)
            if state is None:
                state = parse_text(source.read_bytes(), kind, guard=guard)
            guard()
            state.update(extraction_context)
            return state
        provider_bytes = 50_000_000 if protocol == "paddleocr_vl" else PROVIDER_MAX_BYTES
        target_bytes = 45_000_000 if protocol == "paddleocr_vl" else 100_000_000
        if not state.get("batches"):
            pages = state.get("pages") or (pdf_page_count(source) if kind == "pdf" else None)
            if kind == "pptx":
                state = execute_local("pptx", source, provider_bytes=min(provider_bytes, 50_000_000),
                                      guard=guard, stop=stop)
                if state is None:
                    state = plan_pptx_batches(source, provider_bytes=min(provider_bytes, 50_000_000), guard=guard)
            else:
                state = execute_local("pdf", source, pages=pages, target_bytes=target_bytes,
                    provider_bytes=provider_bytes, guard=guard, stop=stop) if kind == "pdf" else None
                if state is None:
                    state = plan_batches(source, kind=kind, pages=pages, size=source.stat().st_size,
                        target_bytes=target_bytes, provider_bytes=provider_bytes)
            if external:
                state["batches"][0]["job_id"] = external
            state.update(extraction_context)
            checkpoint(state)
        for batch in state["batches"]:
            if batch["status"] == "completed":
                continue
            guard()
            if stop.is_set():
                return None
            if batch.get("job_id") == "submitting":
                raise ValueError("ambiguous_submission")
            if not batch.get("job_id"):
                payload = execute_local(kind+"_bytes", source, start=batch["start"], end=batch["end"],
                    guard=guard, stop=stop) if kind == "pptx" or kind == "pdf" and state["strategy"] == "batched" else None
                if payload is None:
                    payload = (pptx_batch_bytes(source, batch["start"], batch["end"]) if kind == "pptx"
                           else pdf_batch_bytes(source, batch["start"], batch["end"])
                           if state["strategy"] == "batched" else source.read_bytes())
                if len(payload) > provider_bytes:
                    raise ValueError("batch_too_large")
                if batch.get("sha256") and hashlib.sha256(payload).hexdigest() != batch["sha256"]:
                    raise ValueError("pptx_invalid")
                batch.update(status="processing", job_id="submitting")
                checkpoint(state)
                guard()
                batch["job_id"] = adapter.submit_document(payload, kind)
                del payload
                checkpoint(state)
            while not stop.is_set():
                guard()
                result = adapter.check_document(batch["job_id"])
                status = result.get("status")
                if status == "SUCCESS":
                    expected = batch["end"] - batch["start"] + 1 if batch["end"] else None
                    try:
                        normalized = normalize_result(result, expected_pages=expected,
                            page_offset=batch["start"] - 1, image_understanding_enabled=True)
                        if protocol == "paddleocr_vl":
                            from .figure_crops import persist_crops
                            persist_crops(normalized, self.storage, material_id, adapter, guard)
                            if kind == "pdf" and material_id and state.get("ocr_crop_options"):
                                from .chart_crops import complete_chart_crops
                                if not complete_chart_crops(normalized, state, batch, source=source,
                                        storage=self.storage, material_id=material_id, adapter=adapter,
                                        guard=guard, checkpoint=checkpoint, stop=stop):
                                    return None
                        if kind == "pdf" and image_prefix:
                            for page in normalized["page_results"]:
                                for index, block in enumerate(page["blocks"]):
                                    if block["type"] == "figure" and (block.get("source_bbox")
                                            or block.get("coordinate_space") == "processed"):
                                        block["image_url"] = (
                                            f"{image_prefix}/{page['page_no']}/{index}")
                    except ValueError:
                        batch.update(status="failed", job_id=None)
                        checkpoint(state)
                        raise
                    for field in ("chunks", "page_results", "outline"):
                        state[field].extend(normalized[field])
                    if state["pages"] is None:
                        state["pages"], batch["end"] = normalized["pages"], normalized["pages"]
                    batch["status"] = "completed"
                    checkpoint(state)
                    break
                if status not in ("QUEUING", "PROCESSING"):
                    batch.update(status="failed", last_failed_job_id=batch.get("job_id"), job_id=None)
                    checkpoint(state)
                    raise ValueError("invalid_result")
                if stop.wait(4):
                    return None
            if stop.is_set():
                return None
        finalize_document(state)
        if kind == "pptx":
            for page, slide in zip(state["page_results"], state["slides"], strict=True):
                page["hidden"] = slide["hidden"]
                page["notes"] = slide["notes"]
                if slide["notes"] and not any(
                        chunk.get("kind") == "speaker_notes" and chunk["page_no"] == page["page_no"]
                        for chunk in state["chunks"]):
                    state["chunks"].append({"page_no": page["page_no"],
                        "kind": "speaker_notes", "text": "演讲者备注（本地提取）：\n" + slide["notes"]})
        checkpoint(state)
        return state

    def run(self, task_id):
        lease = self.tasks.claim(task_id, lease_owner="document-parser", lease_seconds=120, allow_reclaim_active=False)
        task = self.tasks.get_task(task_id)
        ident, owner = task["subject_id"], task["owner_user_id"]
        adapter = None
        kind = None
        try:
            view, kind, external = self._guard(lease, ident, owner)
            source = self.storage.local_path(MaterialStorage.original_key(ident))
            def guard():
                self.tasks.heartbeat(lease, lease_seconds=600)
                self._guard(lease, ident, owner)
            if kind not in LOCAL_KINDS:
                adapter = self.adapter_factory(view, self.configs.resolve_document_credentials(view["config_id"],
                    owner_user_id=owner, expected_version=view["config_version"]))
                if isinstance(adapter, PaddleVLAdapter):
                    adapter.request_guard = guard
            with self.db.session() as sess:
                _, row = self._owned(sess, ident, owner)
                state = json.loads(row.result_json)
            state = self.process_document(source, kind, state, adapter=adapter,
                protocol=view["protocol"] if view else None, guard=guard,
                checkpoint=lambda value: self._checkpoint(lease, ident, owner, value),
                stop=self.stop, external=external, image_prefix=f"/api/attachments/{ident}/images",
                material_id=ident)
            if state is None:
                return
            self.questions.run(state, owner=owner, lease=lease, source=source, kind=kind,
                guard=guard, checkpoint=lambda value: self._checkpoint(lease, ident, owner, value))
            guard()
            def publish(sess):
                self._publish(sess, lease, ident, owner, state)
            self.tasks.submit_result(lease, stage="parse", status="succeeded",
                progress_current=state["pages"], publish=publish)
        except Exception as exc:
            code = document_failure_code(exc, kind)
            failed_slide = getattr(exc, "page_no", None)
            def fail(sess):
                material, row = self._owned(sess, ident, owner, lock=True)
                material.status, row.failure_code = "failed", code
                if code == "PPTX_SLIDE_TOO_LARGE":
                    state = json.loads(row.result_json)
                    state["failed_slide"] = failed_slide
                    row.result_json = json.dumps(state, ensure_ascii=False)
            self.tasks.submit_result(lease, stage="parse", status="failed", error_code=code, publish=fail)
        finally:
            if adapter:
                adapter.close()

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = Thread(target=self._loop, name="document-parser", daemon=True)
            self.thread.start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                with self.db.session() as sess:
                    ident = sess.scalar(select(LearningTaskRow.task_id).join(DocumentAttachmentRow,
                        DocumentAttachmentRow.task_id == LearningTaskRow.task_id).where(
                        LearningTaskRow.status.in_(("queued", "leased", "running")),
                        or_(LearningTaskRow.status == "queued", LearningTaskRow.lease_until <= utcnow()))
                        .order_by(LearningTaskRow.created_at).limit(1))
                if ident:
                    self.run(ident)
            except Exception:
                # Durable queue remains recoverable after transient database failure.
                pass
            self.stop.wait(1)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)


class RetryIn(BaseModel):
    config_id: str | None = None
    config_version: int | None = Field(default=None, ge=1)
    confirmed: bool = False


def create_attachment_router(service, resolve_user):
    router = APIRouter(prefix="/api/attachments", tags=["attachments"])

    @router.get("/config")
    def config(request: Request):
        owner = resolve_user(request).user_id
        configured = service.config(owner)
        return {"config": {**configured, "extraction": service.questions.defaults(owner)} if configured else None,
                "max_bytes": MAX_DOCUMENT_BYTES, "max_pages": MAX_DOCUMENT_PAGES,
                "max_text_bytes": MAX_TEXT_BYTES, "max_pptx_bytes": MAX_PPTX_BYTES,
                "max_pptx_slides": MAX_PPTX_SLIDES,
                "provider_max_bytes": 50_000_000, "provider_max_pages": PROVIDER_MAX_PAGES}

    def staged(file, action):
        extension = (file.filename or "").rsplit(".", 1)[-1].lower()
        limits = {"pdf": MAX_DOCUMENT_BYTES, "doc": PROVIDER_MAX_BYTES, "docx": PROVIDER_MAX_BYTES,
                  "pptx": MAX_PPTX_BYTES, "txt": MAX_TEXT_BYTES, "md": MAX_TEXT_BYTES,
                  "markdown": MAX_TEXT_BYTES}
        if extension not in limits:
            raise error("unsupported_media_type", "支持 PDF、Word、PPTX、Markdown、TXT。")
        with tempfile.TemporaryDirectory(prefix="document-upload-") as directory:
            path = Path(directory) / "document.bin"
            total = 0
            with path.open("wb") as output:
                while part := file.file.read(1024 * 1024):
                    total += len(part)
                    if total > limits[extension]:
                        raise error("file_too_large")
                    output.write(part)
            validated = validate_document(file.filename or "", file.content_type, path)
            return action(validated)

    @router.post("/preflight")
    async def preflight(request: Request, file: UploadFile = File(...)):
        owner = resolve_user(request).user_id
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(staged, file, lambda validated: service.preflight(validated, owner))

    @router.get("")
    def listing(request: Request):
        return {"data": service.list(resolve_user(request).user_id)}

    @router.post("", status_code=201)
    async def upload(request: Request, file: UploadFile = File(...), config_id: str | None = Form(None),
                     config_version: int | None = Form(None), confirmed: bool = Form(False),
                     semantic_config_id: str | None = Form(None),
                     semantic_config_version: int | None = Form(None),
                     visual_config_id: str | None = Form(None),
                     visual_config_version: int | None = Form(None),
                     idempotency_key: str = Form(..., min_length=1, max_length=100)):
        owner = resolve_user(request).user_id
        from starlette.concurrency import run_in_threadpool
        def create(validated):
            if validated.kind not in LOCAL_KINDS:
                if not confirmed or not config_id or not config_version:
                    raise Slice1Error("disclosure_required", "请先确认向 PaddleOCR 发送所选文件。", 409)
            extraction = {}
            for role, ident, version in (("semantic", semantic_config_id, semantic_config_version),
                                         ("visual", visual_config_id, visual_config_version)):
                if ident:
                    if not confirmed or not version:
                        raise Slice1Error("disclosure_required", "请确认语义抽题的模型目的地。", 409)
                    extraction[role] = {"config_id": ident, "config_version": version}
            return service.create(owner, validated, config_id, config_version, idempotency_key,
                                  extraction=extraction)
        return await run_in_threadpool(staged, file, create)

    @router.get("/{ident}/images/{page_no}/{block_no}")
    def image(ident: str, page_no: int, block_no: int, request: Request, crop_sha256: str | None = None):
        return Response(service.image(ident, resolve_user(request).user_id, page_no, block_no, crop_sha256),
            media_type="image/png", headers={"Cache-Control": "no-store",
                                           "X-Content-Type-Options": "nosniff"})

    @router.get("/{ident}/markdown")
    def markdown(ident: str, request: Request):
        owner = resolve_user(request).user_id
        text = service.markdown(ident, owner)
        kind = service.view(ident, owner)["kind"]
        ext = "txt" if kind == "txt" else "md"
        return Response(text, media_type="text/plain" if kind == "txt" else "text/markdown", headers={
            "Content-Disposition": f'attachment; filename="attachment.{ext}"', "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff"})

    @router.get("/{ident}")
    def get(ident: str, request: Request):
        return service.view(ident, resolve_user(request).user_id)

    @router.post("/{ident}/retry")
    def retry(ident: str, body: RetryIn, request: Request):
        owner = resolve_user(request).user_id
        if not service.view(ident, owner)["local"] and (not body.confirmed or not body.config_id or not body.config_version):
            raise Slice1Error("disclosure_required", "请重新确认外发。", 409)
        return service.retry(ident, owner, body.config_id, body.config_version)

    @router.delete("/{ident}")
    def remove(ident: str, request: Request):
        return service.remove(ident, resolve_user(request).user_id)
    return router
