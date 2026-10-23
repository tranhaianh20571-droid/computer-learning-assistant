"""逐页解析与 OCR 调度管线（切片 2 T03）。

流程（上传事务之后异步执行）：
1. `enqueue_parse`：创建 `kind=material_parse`（lane=ocr）任务 + 首个事件；
2. worker `run_parse`：逐页渲染 + 文本层可信判定：
   - 可信 → 文本直接写入 text_chunks（页标 success，不走 OCR）；
   - 不可信/扫描 → 外发核验（category=material）→ `PaddleOCRAdapter.submit()`
     → `material_pages.ocr_job_id` 记外部任务 → 创建 `kind=ocr_result_fetch`
     任务等待轮询；单页 180 秒截止；
3. `run_fetch`：查询外部任务，迟到/过期结果被任务围栏拒绝；
   结构化区域结果落 `material_regions`，正文（仅 OCR 文本区域）写入
   text_chunks 并入索引；页级 success/partial/failed 固化；
4. 失败页保留页码与原因，用户可重试（router retry → 新 parse 任务）。

真实 PaddleOCR 凭据为人工前置（切片 2 T01 外部阻断）；适配器经工厂注入，
测试用合成适配器驱动整条管线。OCR 响应与磁盘不落正文；任务事件只记
阶码、错误码、时长与外部 job id。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import select

from ..logging_audit.db import Database
from ..logging_audit.repositories import PgTaskEventStore
from ..slice1.disclosure_service import DisclosureService
from ..tasks.service import TaskService
from .chunk_index import ChunkIndexService, SOURCE_VERSION
from .crop import map_bbox_to_pixels
from .models import (
    FigureAssetRow,
    MaterialPageRow,
    MaterialRegionRow,
    MaterialRow,
    TextChunkRow,
    utcnow,
)
from .parse import RenderError, evaluate_page, page_count, render_page
from .storage import MaterialStorage

PAGE_DEADLINE_SECONDS = 180  # 单页 OCR 总截止（架构 §3.3）
PAGE_BATCH = 4               # 每轮领取处理的页数（有界）


class OcrPipelineError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class OcrPipelineService:
    def __init__(
        self,
        db: Database,
        tasks: TaskService,
        events: PgTaskEventStore,
        index: ChunkIndexService,
        storage: Optional[MaterialStorage] = None,
        ocr_adapter_factory: Optional[Callable[[str, str], Any]] = None,
        disclosures: Optional[DisclosureService] = None,
    ) -> None:
        """ocr_adapter_factory(owner_user_id, task_id) -> 适配器 | None。

        生产装配经切片 1 配置 + 外发核验；测试注入合成适配器。
        """
        self.db = db
        self.tasks = tasks
        self.events = events
        self.index = index
        self.storage = storage or MaterialStorage()
        self.ocr_adapter_factory = ocr_adapter_factory
        self.disclosures = disclosures

    # ------------------------------------------------------------------
    # 入队
    # ------------------------------------------------------------------
    def enqueue_parse(self, *, owner_user_id: str, material_id: str) -> dict:
        idem = f"parse:{material_id}:v1"
        created = self.tasks.create_task(
            owner_user_id=owner_user_id,
            kind="material_parse",
            subject_id=material_id,
            idempotency_key=idem,
            stage="parse",
            deadline_seconds=3600,
            progress_total=0,
        )
        if created.get("deduped"):
            # 已有任务：终态（成功/失败）时重试需要新任务（新幂等键、新租约代次）
            from sqlalchemy import select

            from ..tasks.service import LearningTaskRow

            with self.db.session() as sess:
                row = (
                    sess.execute(
                        select(LearningTaskRow).where(
                            LearningTaskRow.owner_user_id == owner_user_id,
                            LearningTaskRow.idempotency_key == idem,
                        )
                    )
                    .scalars()
                    .first()
                )
            if row is not None and row.status in ("succeeded", "failed", "cancelled", "expired"):
                created = self.tasks.create_task(
                    owner_user_id=owner_user_id,
                    kind="material_parse",
                    subject_id=material_id,
                    idempotency_key=f"parse:{material_id}:v1:{uuid.uuid4().hex[:8]}",
                    stage="parse",
                    deadline_seconds=3600,
                    progress_total=0,
                )
                return created
            return created
        with self.db.session() as sess:
            row = sess.get(MaterialRow, material_id)
            if row is not None:
                row.status = "parsing"
            sess.commit()
        return created

    # ------------------------------------------------------------------
    # 解析 worker（kind=material_parse）
    # ------------------------------------------------------------------
    def run_parse(self, task_id: str, *, lease_owner: str = "ocr-worker") -> dict:
        lease = self.tasks.claim(task_id, lease_owner=lease_owner, lease_seconds=300)
        try:
            task = self.tasks.get_task(task_id)
            owner = task["owner_user_id"]
            material_id = task["subject_id"]
            payload = self._original_payload(material_id, owner)
            if self._is_text_material(material_id):
                total = 1
            else:
                total = page_count(payload)
            self.events.record(
                task_id, stage="parse", status="started",
                progress_total=total, generation=lease.generation, attempt=lease.attempt,
            )

            ocr_pages: list[int] = []
            done = 0
            for page_no in range(1, min(total, PAGE_BATCH * 32) + 1):
                outcome = self._process_page(
                    task_id=task_id, lease=lease, owner_user_id=owner,
                    material_id=material_id, payload=payload, page_no=page_no,
                )
                done += 1
                if outcome == "ocr_submitted":
                    ocr_pages.append(page_no)
                if done % PAGE_BATCH == 0:
                    self.events.record(
                        task_id, stage="parse", status="succeeded",
                        progress_current=done, progress_total=total,
                        generation=lease.generation, attempt=lease.attempt,
                    )

            self._finalize_pages(material_id=material_id, owner_user_id=owner)
            self.events.record(
                task_id, stage="parse", status="succeeded",
                progress_current=done, progress_total=total,
                generation=lease.generation, attempt=lease.attempt,
            )
            self._submit(lease, "succeeded")
            return {"status": "succeeded", "pages": done, "ocr_submitted": ocr_pages}
        except OcrPipelineError as exc:
            self.events.record(
                task_id, stage="parse", status="failed", error_code=exc.code,
                generation=lease.generation, attempt=lease.attempt,
            )
            self._submit(lease, "failed", error_code=exc.code)
            return {"status": "failed", "error_code": exc.code}

    def _submit(self, lease, status: str, error_code: Optional[str] = None) -> None:
        self.tasks.submit_result(
            lease, stage="parse", status=status, error_code=error_code, duration_ms=0
        )

    def _original_payload(self, material_id: str, owner_user_id: str) -> bytes:
        with self.db.session() as sess:
            row = sess.get(MaterialRow, material_id)
            if row is None or row.owner_user_id != owner_user_id or row.deleted_at is not None:
                raise OcrPipelineError("material_not_found")
        key = MaterialStorage.original_key(material_id)
        try:
            payload = self.storage.get(key)
        except Exception as exc:  # noqa: BLE001
            raise OcrPipelineError("original_missing") from exc
        return payload

    def _process_page(
        self, *, task_id: str, lease, owner_user_id: str,
        material_id: str, payload: bytes, page_no: int,
    ) -> str:
        """处理单页；返回 'trusted' | 'ocr_submitted' | 'failed'。"""
        with self.db.session() as sess:
            page = (
                sess.execute(
                    select(MaterialPageRow).where(
                        MaterialPageRow.material_id == material_id,
                        MaterialPageRow.page_no == page_no,
                    )
                )
                .scalars()
                .first()
            )
            if page is not None and page.status in ("success", "partial"):
                return "trusted"  # 已处理页不重复入库/不重复 OCR

        if self._is_text_material(material_id):
            return self._process_text_page(material_id=material_id, page_no=page_no, payload=payload)

        try:
            rendered = render_page(payload, page_no)
        except RenderError as exc:
            self._mark_page(material_id, page_no, status="failed", reason="render_failed")
            return "failed"
        evaluation = evaluate_page(payload, page_no)

        if evaluation.trustworthy:
            self._store_trusted_text(
                material_id=material_id, page_no=page_no, text=evaluation.text,
                region_meta={"trustworthy": True, "reason": evaluation.reason},
            )
            self._mark_page(
                material_id, page_no, status="success", reason=None,
                width=rendered.width_px, height=rendered.height_px, trust=evaluation.as_dict(),
            )
            return "trusted"

        # 不可信 → 外发核验后提交 OCR
        adapter = self._adapter_for(owner_user_id, task_id)
        if adapter is None:
            self._mark_page(material_id, page_no, status="failed", reason="ocr_unavailable")
            return "failed"
        try:
            if self.disclosures is not None:
                self.disclosures.verify(
                    task_id=task_id, config_id=getattr(adapter, "config_id", ""),
                    content_category="material", actor_id=owner_user_id,
                    scope_snapshot={"material_ids":[material_id]},
                )
            submitted = adapter.submit(material_id=material_id, page_no=page_no)
        except Exception as exc:  # noqa: BLE001 - 供应商异常映射受控错误码
            code = getattr(exc, "code", "") or "ocr_submit_failed"
            self._mark_page(material_id, page_no, status="failed", reason=code[:40])
            return "failed"
        external_id = str(submitted.get("external_request_id") or "")
        if not external_id:
            self._mark_page(material_id, page_no, status="failed", reason="ocr_no_job_id")
            return "failed"
        self._mark_page(
            material_id, page_no, status="pending", reason=None,
            width=rendered.width_px, height=rendered.height_px,
            trust=evaluation.as_dict(), ocr_job_id=external_id,
        )
        # 结果轮询任务（新任务、新租约；单页 180 秒截止）
        self.tasks.create_task(
            owner_user_id=owner_user_id,
            kind="ocr_result_fetch",
            subject_id=f"{material_id}:{page_no}",
            idempotency_key=f"ocrfetch:{material_id}:{page_no}:{external_id[:24]}",
            stage="ocr",
            deadline_seconds=PAGE_DEADLINE_SECONDS,
        )
        return "ocr_submitted"

    def _is_text_material(self, material_id: str) -> bool:
        with self.db.session() as sess:
            row = sess.get(MaterialRow, material_id)
            return row is not None and row.kind in ("md", "txt")

    def _process_text_page(self, *, material_id: str, page_no: int, payload: bytes) -> str:
        from .upload import decode_text

        self._store_trusted_text(
            material_id=material_id, page_no=page_no, text=decode_text(payload),
            region_meta={"trustworthy": True, "reason": "text_material"},
        )
        self._mark_page(material_id, page_no, status="success", reason=None)
        return "trusted"

    def _store_trusted_text(
        self, *, material_id: str, page_no: int, text: str, region_meta: dict,
    ) -> None:
        with self.db.session() as sess:
            exists = (
                sess.execute(
                    select(TextChunkRow).where(
                        TextChunkRow.material_id == material_id,
                        TextChunkRow.page_no == page_no,
                    )
                )
                .scalars()
                .first()
            )
            if exists is not None:
                return  # 成功文字/裁切不重复入库
            region = MaterialRegionRow(
                region_id=f"reg_{uuid.uuid4().hex[:12]}",
                material_id=material_id,
                page_no=page_no,
                kind="text",
                trustworthy=True,
                status="success",
            )
            sess.add(region)
            sess.flush()
            chunk = TextChunkRow(
                chunk_id=f"chk_{uuid.uuid4().hex[:12]}",
                material_id=material_id,
                material_region_id=region.region_id,
                page_no=page_no,
                chunk_index=0,
                private_text=text,
                source_version=SOURCE_VERSION,
            )
            sess.add(chunk)
            sess.flush()
            # Body is private business storage; only token metadata enters the index.
            from sqlalchemy import func as sa_func

            from .chunk_index import build_tokens, to_tsvector_expr

            tokenized = build_tokens(text=text, page_no=page_no)
            sess.execute(
                TextChunkRow.__table__.update()
                .where(TextChunkRow.chunk_id == chunk.chunk_id)
                .values(tsvector=sa_func.to_tsvector("simple", to_tsvector_expr(tokenized.tokens)))
            )
            sess.commit()

    def _mark_page(
        self, material_id: str, page_no: int, *, status: str, reason: Optional[str],
        width: Optional[int] = None, height: Optional[int] = None,
        trust: Optional[dict] = None, ocr_job_id: Optional[str] = None,
    ) -> None:
        with self.db.session() as sess:
            page = (
                sess.execute(
                    select(MaterialPageRow).where(
                        MaterialPageRow.material_id == material_id,
                        MaterialPageRow.page_no == page_no,
                    )
                )
                .scalars()
                .first()
            )
            if page is None:
                page = MaterialPageRow(
                    page_id=f"pg_{uuid.uuid4().hex[:12]}",
                    material_id=material_id,
                    page_no=page_no,
                )
                sess.add(page)
            page.status = status
            page.failure_reason = reason
            if width is not None:
                page.width_px = width
            if height is not None:
                page.height_px = height
            if trust is not None:
                page.text_trustworthiness = json.dumps(trust, ensure_ascii=False)
            if ocr_job_id:
                page.ocr_job_id = ocr_job_id
            page.updated_at = utcnow()
            sess.commit()

    def _adapter_for(self, owner_user_id: str, task_id: str):
        if self.ocr_adapter_factory is None:
            return None
        return self.ocr_adapter_factory(owner_user_id, task_id)

    def _finalize_pages(self, *, material_id: str, owner_user_id: str) -> None:
        """全部页终态后固化资料状态与配额实际用量。"""
        with self.db.session() as sess:
            pages = (
                sess.execute(
                    select(MaterialPageRow).where(MaterialPageRow.material_id == material_id)
                )
                .scalars()
                .all()
            )
            row = sess.get(MaterialRow, material_id)
            if row is None:
                return
            if not pages:
                row.status = "parsed"
            else:
                states = {p.status for p in pages}
                if "failed" in states:
                    row.status = "partial" if "success" in states or "partial" in states else "failed"
                elif "pending" in states:
                    row.status = "parsing"
                else:
                    row.status = "parsed"
            sess.commit()

    # ------------------------------------------------------------------
    # OCR 结果轮询 worker（kind=ocr_result_fetch）
    # ------------------------------------------------------------------
    def run_fetch(self, task_id: str, *, lease_owner: str = "ocr-worker") -> dict:
        lease = self.tasks.claim(task_id, lease_owner=lease_owner, lease_seconds=120)
        try:
            task = self.tasks.get_task(task_id)
            owner = task["owner_user_id"]
            material_id, _, page_raw = task["subject_id"].partition(":")
            page_no = int(page_raw or "0")
            with self.db.session() as sess:
                page = (
                    sess.execute(
                        select(MaterialPageRow).where(
                            MaterialPageRow.material_id == material_id,
                            MaterialPageRow.page_no == page_no,
                        )
                    )
                    .scalars()
                    .first()
                )
                if page is None or not page.ocr_job_id:
                    self._submit(lease, "failed", error_code="ocr_job_missing")
                    return {"status": "failed", "error_code": "ocr_job_missing"}
                external_id = page.ocr_job_id
            adapter = self._adapter_for(owner, task_id)
            if adapter is None:
                self._submit(lease, "failed", error_code="ocr_unavailable")
                return {"status": "failed", "error_code": "ocr_unavailable"}
            result = adapter.fetch_result(external_id)
            regions = normalize_ocr_regions(result.get("result"))
            applied = self._apply_ocr_regions(
                material_id=material_id, page_no=page_no, owner_user_id=owner,
                regions=regions,
            )
            # 结构图等无文本区域未命中图块 → 该页最多 partial（T04 契约）
            has_empty_region = any(not r.get("text") for r in regions)
            self._mark_page(
                material_id, page_no,
                status="partial" if (has_empty_region or applied == 0) else "success",
                reason=None if applied and not has_empty_region else "figure_or_region_missing",
            )
            self.events.record(
                task_id, stage="ocr", status="succeeded",
                progress_current=applied, generation=lease.generation, attempt=lease.attempt,
            )
            self._submit(lease, "succeeded")
            return {"status": "succeeded", "regions": applied}
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", "") or "ocr_fetch_failed"
            self.events.record(
                task_id, stage="ocr", status="failed", error_code=code[:40],
                generation=lease.generation, attempt=lease.attempt,
            )
            self._submit(lease, "failed", error_code=code[:40])
            return {"status": "failed", "error_code": code}

    def _apply_ocr_regions(
        self, *, material_id: str, page_no: int, owner_user_id: str,
        regions: list[dict],
    ) -> int:
        """把结构化区域结果落库；OCR 文本区域进索引；坐标换算存 bbox（渲染像素）。"""
        applied = 0
        with self.db.session() as sess:
            page = (
                sess.execute(
                    select(MaterialPageRow).where(
                        MaterialPageRow.material_id == material_id,
                        MaterialPageRow.page_no == page_no,
                    )
                )
                .scalars()
                .first()
            )
            page_w = page.width_px if page else 0
            page_h = page.height_px if page else 0
            for reg in regions:
                mapped = map_bbox_to_pixels(
                    bbox=reg.get("bbox", {}),
                    page_width=page_w, page_height=page_h,
                    rotation=int(reg.get("rotation") or 0),
                    coordinate_system=reg.get("coordinate_system", "top_left"),
                )
                region_row = MaterialRegionRow(
                    region_id=f"reg_{uuid.uuid4().hex[:12]}",
                    material_id=material_id,
                    page_no=page_no,
                    bbox_left=mapped["left"], bbox_top=mapped["top"],
                    bbox_right=mapped["right"], bbox_bottom=mapped["bottom"],
                    coordinate_system="top_left",
                    rotation=0,
                    reading_order=int(reg.get("reading_order") or applied),
                    kind=reg.get("kind", "text"),
                    trustworthy=bool(reg.get("trustworthy", True)),
                    status="success" if reg.get("text") else "partial",
                )
                sess.add(region_row)
                applied += 1
                if reg.get("text"):
                    sess.flush()
                    chunk = TextChunkRow(
                        chunk_id=f"chk_{uuid.uuid4().hex[:12]}",
                        material_id=material_id,
                        material_region_id=region_row.region_id,
                        page_no=page_no,
                        chunk_index=applied,
                        private_text=reg["text"],
                        source_version=SOURCE_VERSION,
                    )
                    sess.add(chunk)
                    sess.flush()
                    from sqlalchemy import func as sa_func

                    from .chunk_index import build_tokens, to_tsvector_expr

                    tokenized = build_tokens(text=reg["text"], page_no=page_no)
                    sess.execute(
                        TextChunkRow.__table__.update()
                        .where(TextChunkRow.chunk_id == chunk.chunk_id)
                        .values(tsvector=sa_func.to_tsvector("simple", to_tsvector_expr(tokenized.tokens)))
                    )
            sess.commit()
        return applied


def normalize_ocr_regions(raw: Any) -> list[dict]:
    """把供应商结构化结果归一为区域列表（T01 契约字段；容忍包装层）。"""
    if raw is None:
        return []
    body = raw
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return []
    if not isinstance(body, dict):
        return []
    pages = body.get("layoutParsingResults") or body.get("regions") or []
    out: list[dict] = []
    if isinstance(pages, dict):
        pages = [pages]
    for item in pages if isinstance(pages, list) else []:
        if not isinstance(item, dict):
            continue
        pruned = item.get("prunedResult") or item
        blocks = pruned.get("parsing_res_list") if isinstance(pruned, dict) else None
        for block in blocks or []:
            if not isinstance(block, dict):
                continue
            bbox = block.get("block_bbox") or block.get("bbox") or []
            if len(bbox) != 4:
                continue
            out.append({
                "bbox": {"left": bbox[0], "top": bbox[1], "right": bbox[2], "bottom": bbox[3]},
                "kind": str(block.get("block_label") or "text"),
                "text": str(block.get("block_content") or "")[:4000],
                "reading_order": int(block.get("block_index") or len(out)),
                "rotation": int(body.get("rotation") or 0),
                "coordinate_system": str(body.get("coordinate_system") or "top_left"),
                "trustworthy": True,
            })
    return out


__all__ = [
    "OcrPipelineService", "OcrPipelineError", "normalize_ocr_regions",
    "PAGE_DEADLINE_SECONDS", "PAGE_BATCH",
]
