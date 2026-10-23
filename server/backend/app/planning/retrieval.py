"""检索组装与上下文清单（T04）。

- 只消费用户选定材料、且切片 2 已可用的 `success` 区域检索结果（不扩大范围）；
- 按 T03 冻结的预算五参数裁剪：历史消息 > 片段长度 > 片段数量 > 图片；
- `context_manifests` 只保存引用（片段/图块 ID、历史范围、截断原因、哈希），
  不含任何正文；
- 超预算进入可见状态（CONTEXT_BUDGET_EXCEEDED 事件），不静默截断当前目标。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from sqlalchemy import select

from ..logging_audit.db import Database
from ..materials.chunk_index import ChunkIndexService
from .errors import error
from .models import ContextManifestRow, utcnow
from .outputs import CONTEXT_BUDGET_DEFAULTS


@dataclass
class RetrievalResult:
    refs: list[dict] = field(default_factory=list)
    chars_total: int = 0
    truncation_reasons: list[str] = field(default_factory=list)
    budget: dict = field(default_factory=lambda: dict(CONTEXT_BUDGET_DEFAULTS))
    # 供提示词组装使用的受限文本片段（不落库、不进日志）
    excerpts: list[dict] = field(default_factory=list)


class PlanningRetrievalService:
    def __init__(
        self,
        db: Database,
        index: Optional[ChunkIndexService] = None,
        text_resolver: Optional[Callable[[dict], Optional[str]]] = None,
    ) -> None:
        """text_resolver：按命中 {chunk_id, material_id, page_no} 返回受限正文。

        切片 2 的分块正文存储尚未落库（OCR worker 属其切片余量），因此正文
        解析为可注入项：缺省只组装引用与 manifest（不含正文），提示词组装侧
        收到的 excerpts 为空；切片 2 补齐正文存储后注入真实 resolver 即可。
        """
        self.db = db
        self.index = index or ChunkIndexService(db)
        self.text_resolver = text_resolver

    def assemble(
        self,
        *,
        owner_user_id: str,
        material_ids: list[str],
        query: str,
        budget: Optional[dict] = None,
        source_sections: Optional[list[dict]] = None,
    ) -> RetrievalResult:
        """组装检索上下文：owner 过滤 + 选定材料 + 可用区域 + 预算裁剪。"""
        b = {**CONTEXT_BUDGET_DEFAULTS, **(budget or {})}
        result = RetrievalResult(budget=b)
        if not material_ids:
            result.truncation_reasons.append("no_material_selected")
            return result
        if self.text_resolver is None and self.db is None:
            result.truncation_reasons.append("text_resolver_unavailable")
        hits = self.index.search(
            owner_user_id=owner_user_id, query=query, material_ids=list(material_ids)
        ) if query.strip() else []
        if self.db is not None:
            from ..materials.corpus import search_corpus
            attachment_hits, reasons = search_corpus(
                self.db, owner_user_id, material_ids, query, source_sections)
            result.truncation_reasons.extend(reasons)
        else:
            attachment_hits = []
        hits = attachment_hits + hits
        # 只保留区域可用的命中（OCR 来源区域不可用时天然被过滤）
        kept = 0
        chars = 0
        for hit in hits:
            if source_sections is not None and not any(
                    section["id"].startswith(hit["material_id"] + "/")
                    and section["page_no"] <= hit.get("page_no", 0) <= section["end_page"]
                    for section in source_sections):
                continue
            region_id = hit.get("region_id")
            if region_id and not self.index.region_is_available(region_id):
                continue
            if kept >= int(b["max_retrieval_chunks"]):
                result.truncation_reasons.append("max_retrieval_chunks")
                break
            remaining = max(0, int(b["max_retrieval_chars"]) - chars)
            if "attachment_text" in hit:
                text = hit["attachment_text"]
                excerpt = {"chunk_id": hit["chunk_id"], "material_id": hit["material_id"],
                    "page_no": hit["page_no"], "text": text[:remaining], "truncated": len(text) > remaining,
                    **{key: hit[key] for key in ("title", "section_id", "content_hash", "offset", "end_offset",
                                                 "unit", "line_start", "line_end", "source_start", "source_end")
                       if key in hit}}
            else:
                excerpt = self._excerpt(hit, owner_user_id, max_chars=remaining)
            if excerpt is not None:
                if excerpt.pop("truncated") and "max_retrieval_chars" not in result.truncation_reasons:
                    result.truncation_reasons.append("max_retrieval_chars")
                if excerpt["text"]:
                    result.excerpts.append(excerpt)
                    chars += len(excerpt["text"])
            elif (self.text_resolver is not None or self.db is not None) and "excerpt_unavailable" not in result.truncation_reasons:
                result.truncation_reasons.append("excerpt_unavailable")
            result.refs.append({
                "chunk_id": hit["chunk_id"],
                "material_id": hit["material_id"],
                "page_no": hit["page_no"],
                "region_id": region_id,
                "source": "material",
                **{key: hit[key] for key in ("section_id", "content_hash", "offset", "end_offset",
                                             "unit", "line_start", "line_end", "source_start", "source_end")
                   if key in hit},
            })
            kept += 1
        result.chars_total = chars
        if result.excerpts and "text_resolver_unavailable" in result.truncation_reasons:
            result.truncation_reasons.remove("text_resolver_unavailable")
        return result

    def _attachment_hits(self, owner_user_id, material_ids, query):
        if self.db is None:
            return []
        from ..materials.corpus import search_corpus
        return search_corpus(self.db, owner_user_id, material_ids, query)[0]

    def _excerpt(self, hit: dict, owner_user_id: str, max_chars: int) -> Optional[dict]:
        """经注入的 resolver 取受限正文片段（不落库）；无 resolver 时不取正文。"""
        if self.text_resolver is None:
            if self.db is None:
                return None
            from ..materials.models import TextChunkRow, MaterialRow
            with self.db.session() as session:
                text = session.scalar(select(TextChunkRow.private_text).join(MaterialRow).where(
                    TextChunkRow.chunk_id == hit["chunk_id"], MaterialRow.owner_user_id == owner_user_id,
                    MaterialRow.deleted_at.is_(None)))
        else:
            text = self.text_resolver(hit)
        if not text:
            return None
        limit = max(0, max_chars)
        return {"chunk_id": hit["chunk_id"], "text": text[:limit], "truncated": len(text) > limit}

    def save_manifest(
        self,
        *,
        task_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        retrieval: RetrievalResult,
        history_range: Optional[dict] = None,
        extra_refs: Optional[list[dict]] = None,
    ) -> dict:
        refs = list(retrieval.refs) + list(extra_refs or [])
        payload = json.dumps(
            {
                "budget": retrieval.budget,
                "refs": refs,
                "history_range": history_range or {},
                "truncation_reasons": retrieval.truncation_reasons,
            },
            ensure_ascii=False, sort_keys=True,
        )
        manifest_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        manifest_id = (
            f"mf_{utcnow().strftime('%Y%m%d%H%M%S')}_"
            f"{uuid.uuid4().hex[:12]}"
        )
        with self.db.session() as sess:
            sess.add(
                ContextManifestRow(
                    manifest_id=manifest_id,
                    task_id=task_id,
                    conversation_id=conversation_id,
                    manifest_hash=manifest_hash,
                    budget_json=json.dumps(retrieval.budget, ensure_ascii=False),
                    retrieval_refs_json=json.dumps(refs, ensure_ascii=False),
                    history_range_json=json.dumps(history_range or {}, ensure_ascii=False),
                    truncation_reasons_json=json.dumps(retrieval.truncation_reasons, ensure_ascii=False),
                    summary_version="slice3-v1",
                )
            )
            sess.commit()
        return {
            "manifest_id": manifest_id,
            "manifest_hash": manifest_hash,
            "ref_count": len(refs),
            "truncation_reasons": list(retrieval.truncation_reasons),
        }


def build_history_range(messages: list[dict], max_history_messages: int) -> tuple[list[dict], dict]:
    """按预算截取历史消息范围（旧消息优先丢弃），返回（保留范围, 范围描述）。"""
    if len(messages) <= max_history_messages:
        return messages, {"kept": len(messages), "dropped": 0}
    kept = messages[-max_history_messages:]
    return kept, {"kept": len(kept), "dropped": len(messages) - len(kept), "reason": "max_history_messages"}


def validate_material_ownership(db: Database, owner_user_id: str, material_ids: list[str]) -> list[str]:
    """校验材料所有权与未删除状态；返回实际可用的 material_ids。"""
    from ..materials.models import MaterialRow

    if not material_ids:
        return []
    with db.session() as sess:
        rows = (
            sess.execute(
                select(MaterialRow).where(
                    MaterialRow.owner_user_id == owner_user_id,
                    MaterialRow.material_id.in_(list(material_ids)),
                    MaterialRow.deleted_at.is_(None),
                )
            )
            .scalars()
            .all()
        )
        from ..materials.models import DocumentAttachmentRow
        attachments = set(sess.scalars(select(DocumentAttachmentRow.material_id).where(
            DocumentAttachmentRow.material_id.in_(material_ids),
            DocumentAttachmentRow.owner_user_id == owner_user_id)))
        from ..materials.source_models import ImportedSourceRow
        attachments.update(sess.scalars(select(ImportedSourceRow.material_id).where(
            ImportedSourceRow.material_id.in_(material_ids), ImportedSourceRow.owner_user_id == owner_user_id)))
        if any(row.material_id in attachments and row.status != "parsed" for row in rows):
            raise error("invalid_field", "请等待附件识别完成，或移除失败附件。", status=409)
        return [r.material_id for r in rows]


__all__ = [
    "PlanningRetrievalService", "RetrievalResult", "build_history_range",
    "validate_material_ownership",
]
