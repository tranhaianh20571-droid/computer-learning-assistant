"""Private structural passages and resumable, owner-bound embedding indexes."""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import timedelta

from sqlalchemy import (DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
                        bindparam, delete, select, text as sql_text)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from ..logging_audit.models import Base
from ..memory.models import now
from ..memory.personal import PersonalModels, latest, finite_vector, profile_for
from ..memory.providers import ProviderClient
from ..planning.errors import error
from .models import DocumentAttachmentRow, MaterialRow
from .study_sources import block_text, build_study_index, digest, namespace

CHUNK_VERSION = "structure-1"


class CorpusDocument(Base):
    __tablename__ = "corpus_documents"
    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), index=True)
    material_id: Mapped[str] = mapped_column(String(64), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    blocks_json: Mapped[str] = mapped_column(Text)


class CorpusIndex(Base):
    __tablename__ = "corpus_indexes"
    __table_args__ = (UniqueConstraint("document_id", "space", name="uq_corpus_document_space"),)
    index_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("corpus_documents.document_id", ondelete="CASCADE"))
    owner_user_id: Mapped[str] = mapped_column(String(64), index=True)
    space: Mapped[str] = mapped_column(String(64))
    config_revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    completed: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    vectors_json: Mapped[str] = mapped_column(Text, default="[]")
    lease_until: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)


def segments(state, material_id):
    """Read normalized OCR only; retain exact offsets in the study-index text."""
    index = state.get("study_index") or build_study_index(state)
    sections = index.get("sections", [])
    output, cursor = [], 0
    pages = state.get("page_results") or [
        {"page_no": c["page_no"], "blocks": [{"type": c.get("kind", "text"), "content": c["text"]}]}
        for c in state.get("chunks", [])]
    for page in pages:
        local = 0
        for number, block in enumerate(page.get("blocks", [])):
            text = block_text(block)
            if not text:
                continue
            offset = cursor + local
            section = next((s for s in reversed(sections)
                            if s["offset"] <= offset < s["end_offset"]
                            and not s.get("reference_only")), None)
            parent_id = digest([material_id, page["page_no"], number, text, CHUNK_VERSION])
            start = 0
            while start < len(text):
                stop = min(len(text), start + 1200)
                if stop < len(text):
                    boundaries = list(re.finditer(r"\n\n|[。！？.!?]\s", text[start + 600:stop]))
                    if boundaries:
                        stop = start + 600 + boundaries[-1].end()
                body = text[start:stop]
                output.append({"id": digest([parent_id, start, stop]), "parent_id": parent_id,
                    "page_no": page["page_no"], "block_no": number, "text": body,
                    "parent_text": text[:8000], "offset": offset + start,
                    "end_offset": offset + stop,
                    "section_id": namespace(material_id, section["id"]) if section else "",
                    "section_title": section["title"] if section else "",
                    "kind": block.get("type", "text"),
                    **({"source_url": block.get("source_url") or state["source_url"]}
                       if block.get("source_url") or state.get("source_url") else {}),
                    **({"unit": state["unit"]} if state.get("unit") else {}),
                    **{k: block[k] for k in ("line_start", "line_end", "source_start", "source_end", "start_ms", "end_ms", "locator") if k in block}})
                if stop == len(text):
                    break
                start = max(start + 1, stop - 120)
            local += len(text) + 1
        cursor += max(0, local - 1) + 1
    for chunk in state.get("chunks", []):
        if chunk.get("kind") not in ("image_understanding", "speaker_notes") or not chunk.get("text"):
            continue
        section = next((s for s in reversed(sections)
                        if s["page_no"] <= chunk["page_no"] <= s["end_page"]
                        and not s.get("reference_only")), None)
        text = chunk["text"]
        identity = digest([material_id, chunk["page_no"], "auxiliary", text])
        output.append({"id": identity, "parent_id": identity, "page_no": chunk["page_no"],
            "block_no": -1, "text": text, "parent_text": text,
            "offset": None, "end_offset": None, "auxiliary": True,
            "section_id": namespace(material_id, section["id"]) if section else "",
            "section_title": section["title"] if section else "",
            "unit": state.get("unit", "page"), "kind": chunk["kind"],
            **({"source_url": state["source_url"]} if state.get("source_url") else {}),
            **{k: chunk[k] for k in ("start_ms", "end_ms", "asset_index") if k in chunk}})
    return output


def ensure_document(session, owner, material, attachment):
    state = json.loads(attachment.result_json)
    content_hash = digest(state)
    identity = digest([owner, material.material_id, content_hash, CHUNK_VERSION])
    row = session.get(CorpusDocument, identity)
    if row is None:
        row = CorpusDocument(document_id=identity, owner_user_id=owner,
            material_id=material.material_id, content_hash=content_hash,
            blocks_json=json.dumps(segments(state, material.material_id), ensure_ascii=False))
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            row = session.get(CorpusDocument, identity)
            if row is None:
                raise
    config = latest(session, owner)
    if config and config.enabled:
        profile = profile_for(session, owner, config)
        space = profile.embedding_space if profile else None
        if space:
            exists = session.scalar(select(CorpusIndex).where(
                CorpusIndex.document_id == identity, CorpusIndex.space == space))
            if exists is None:
                try:
                    with session.begin_nested():
                        session.add(CorpusIndex(index_id=uuid.uuid4().hex, document_id=identity,
                            owner_user_id=owner, space=space,
                            config_revision=config.revision, status="pending"))
                        session.flush()
                except IntegrityError:
                    pass  # Concurrent readers scheduled the same immutable index.
    return row


def remove_corpus(session, owner, material_id):
    ids = select(CorpusDocument.document_id).where(
        CorpusDocument.owner_user_id == owner, CorpusDocument.material_id == material_id)
    session.execute(delete(CorpusIndex).where(CorpusIndex.document_id.in_(ids)))
    session.execute(delete(CorpusDocument).where(CorpusDocument.document_id.in_(ids)))


def pgvector_available(session):
    if session.bind.dialect.name != "postgresql":
        return False
    return bool(session.scalar(sql_text("SELECT to_regclass('corpus_vectors') IS NOT NULL")))


def pgvector_index_complete(session, index, owner, material_id, dimensions, chunks):
    """Choose the SQL backend only for the complete current immutable index."""
    if not dimensions or not chunks or not pgvector_available(session):
        return False
    try:
        with session.begin_nested():
            row = session.execute(sql_text("""
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE owner_user_id=:owner AND material_id=:material
                           AND space=:space AND dimensions=:dimensions) AS matching,
                       min(position) AS first_position, max(position) AS last_position
                  FROM corpus_vectors WHERE index_id=:index_id
            """), {"owner": owner, "material": material_id, "space": index.space,
                   "dimensions": dimensions, "index_id": index.index_id}).mappings().one()
    except SQLAlchemyError:
        return False
    return (row["total"] == row["matching"] == chunks
            and row["first_position"] == 0 and row["last_position"] == chunks - 1)


def write_pg_vectors(session, job, document, blocks, start, vectors, spec):
    """Mirror the JSON checkpoint into the exact-search pgvector index."""
    if not vectors or not pgvector_available(session):
        return
    rows = []
    for offset, vector in enumerate(vectors):
        position = start + offset
        if position >= len(blocks):
            break
        block = blocks[position]
        rows.append({"index_id": job.index_id, "position": position,
            "chunk_id": block["id"], "owner_user_id": job.owner_user_id,
            "material_id": document.material_id, "space": spec["embedding_space"],
            "dimensions": len(vector), "page_no": block.get("page_no"),
            "section_id": (block.get("section_id") or "")[:200],
            "embedding": json.dumps(vector)})
    if not rows:
        return
    session.execute(sql_text("""
        INSERT INTO corpus_vectors
        (index_id, position, chunk_id, owner_user_id, material_id, space,
         dimensions, page_no, section_id, embedding)
        VALUES (:index_id, :position, :chunk_id, :owner_user_id, :material_id, :space,
                :dimensions, :page_no, :section_id, CAST(:embedding AS vector))
        ON CONFLICT (index_id, position) DO UPDATE SET
          chunk_id=EXCLUDED.chunk_id, owner_user_id=EXCLUDED.owner_user_id,
          material_id=EXCLUDED.material_id, space=EXCLUDED.space,
          dimensions=EXCLUDED.dimensions, page_no=EXCLUDED.page_no,
          section_id=EXCLUDED.section_id, embedding=EXCLUDED.embedding
    """), rows)


class CorpusWorker:
    def __init__(self, db, audit=None):
        self.db, self.models = db, PersonalModels(db, audit)

    def _valid(self, session, job):
        document = session.get(CorpusDocument, job.document_id)
        material = session.get(MaterialRow, document.material_id) if document else None
        from .source_models import ImportedSourceRow
        attachment = (session.get(DocumentAttachmentRow, document.material_id)
                      or session.get(ImportedSourceRow, document.material_id)) if document else None
        if (not document or not material or material.owner_user_id != job.owner_user_id
                or material.deleted_at or material.status != "parsed" or not attachment
                or attachment.owner_user_id != job.owner_user_id
                or digest(json.loads(attachment.result_json)) != document.content_hash):
            raise error("course_not_found", status=404)
        return document

    def tick(self):
        from sqlalchemy import or_
        with self.db.session() as session:
            job = session.scalar(select(CorpusIndex).where(
                CorpusIndex.status.in_(("pending", "building")),
                CorpusIndex.attempts < 3,
                or_(CorpusIndex.lease_until.is_(None), CorpusIndex.lease_until < now()))
                .order_by(CorpusIndex.created_at).with_for_update(skip_locked=True).limit(1))
            if not job:
                return False
            try:
                document = self._valid(session, job)
                blocks = json.loads(document.blocks_json)
                identity, owner, revision = job.index_id, job.owner_user_id, job.config_revision
                job.status, job.lease_until = "building", now() + timedelta(seconds=120)
                job.attempts += 1
                session.commit()
                def guard():
                    self.models.check(owner, revision)
                    with self.db.session() as check_session:
                        current = check_session.get(CorpusIndex, identity)
                        if not current or current.status != "building":
                            raise error("disclosure_revoked")
                        self._valid(check_session, current)
                guard()
                spec = self.models.resolve(owner, revision)
                if spec["embedding_status"] == "pending":
                    spec = self.models.detect(owner, revision)
                job = session.get(CorpusIndex, identity, with_for_update=True, populate_existing=True)
                self._valid(session, job)
                if spec["embedding_status"] != "detected":
                    job.status, job.lease_until = "blocked", None
                    return True
                if job.space != spec["embedding_space"]:
                    # The owner moved to a new embedding space; this job is obsolete.
                    job.status, job.lease_until = "cancelled", None
                    return True
                start = job.completed
                batch = blocks[start:start + 16]
                vectors = ProviderClient(spec, guard=guard).embed([
                    b["section_title"] + "\n" + b["text"] for b in batch]) if batch else []
                if vectors and spec["dimensions"] and len(vectors[0]) != spec["dimensions"]:
                    raise error("capability_test_failed", "Embedding 维度与索引空间不一致。")
                job = session.get(CorpusIndex, identity, with_for_update=True, populate_existing=True)
                self._valid(session, job)
                values = json.loads(job.vectors_json) + vectors
                job.vectors_json = json.dumps(values)
                job.completed = len(values)
                write_pg_vectors(session, job, document, blocks, start, vectors, spec)
                job.status = "ready" if len(values) == len(blocks) else "pending"
                job.attempts, job.lease_until = 0, None
            except Exception as exc:
                if job:
                    job.lease_until = None
                    if getattr(exc, "code", "") in ("course_not_found", "access_denied"):
                        job.status = "cancelled"
                    elif getattr(exc, "code", "") == "config_not_found":
                        job.status = "blocked"
                    elif job.attempts >= 3:
                        job.status = "failed"
            return True


def lexical_score(tokens, text):
    lowered = text.lower()
    return sum(1 for token in tokens if token.lower() in lowered)


def search_corpus(db, owner, material_ids, query, sections=None):
    """Filter before rank; never return arbitrary zero-match passages."""
    from .chunk_index import jieba_tokens
    from ..memory.providers import ProviderClient
    if not material_ids:
        return [], []
    tokens = set(jieba_tokens(query)) if query.strip() else set()
    candidates, reasons = [], []
    content_versions = {}
    models = PersonalModels(db)
    with db.session() as probe:
        config = latest(probe, owner)
    if config and config.enabled:
        try:
            models.ensure_detected(owner, config.revision)
        except Exception as exc:
            if getattr(exc, "code", "") in ("access_denied", "disclosure_revoked",
                                            "config_not_found"):
                raise
    pg_indexes = set()
    pg_snapshots = {}
    with db.session() as session:
        from .source_models import source_records
        rows = source_records(session, owner, material_ids, parsed=True)
        config = latest(session, owner)
        spec = models.resolve(owner, config.revision) if config and config.enabled else None
        for material, attachment in rows:
            doc = ensure_document(session, owner, material, attachment)
            content_versions[material.material_id] = doc.content_hash
            blocks = json.loads(doc.blocks_json)
            index = session.scalar(select(CorpusIndex).where(
                CorpusIndex.document_id == doc.document_id, CorpusIndex.space == spec["embedding_space"],
                CorpusIndex.status == "ready")) if spec else None
            use_pg = bool(index and os.environ.get("CORPUS_VECTOR_BACKEND", "auto") != "json"
                and pgvector_index_complete(session, index, owner, material.material_id,
                    spec["dimensions"], len(blocks)))
            if use_pg:
                pg_indexes.add(index.index_id)
                pg_snapshots[index.index_id] = index.vectors_json
            vectors = json.loads(index.vectors_json) if index and not use_pg else []
            if spec and not index:
                reasons.append("embedding_index_pending")
            for i, block in enumerate(blocks):
                if sections is not None and not any(
                        s["id"].startswith(material.material_id + "/") and (
                            block["section_id"] == s["id"] or
                            "offset" in s and block["offset"] is not None
                            and s["offset"] <= block["offset"] < s["end_offset"] or
                            block.get("auxiliary") and s["page_no"] <= block["page_no"] <= s["end_page"])
                        for s in sections):
                    continue
                candidates.append({**block, "material_id": material.material_id,
                    "content_hash": doc.content_hash, "vector": vectors[i] if i < len(vectors) else None,
                    "pg_index_id": index.index_id if use_pg else None,
                    "pg_position": i if use_pg else None,
                    "lexical": lexical_score(tokens, block["text"])})
    lexical = sorted((c for c in candidates if c["lexical"] > 0),
                     key=lambda c: -c["lexical"])[:40]
    semantic = []
    def guard():
        if spec:
            models.check(owner, spec["revision"])
        with db.session() as session:
            for material, _ in rows:
                current = session.get(MaterialRow, material.material_id)
                from .source_models import ImportedSourceRow
                attachment = (session.get(DocumentAttachmentRow, material.material_id)
                              or session.get(ImportedSourceRow, material.material_id))
                if (not current or current.owner_user_id != owner or current.deleted_at
                        or current.status != "parsed" or not attachment
                        or attachment.owner_user_id != owner
                        or digest(json.loads(attachment.result_json)) != content_versions[material.material_id]):
                    raise error("course_not_found", status=404)
    if spec and query.strip():
        if spec["embedding_status"] != "detected":
            reasons.append("embedding_dimension_pending"
                           if spec["embedding_status"] == "pending" else "embedding_unavailable")
        else:
            try:
                vector = ProviderClient(spec, guard=guard).embed([query[:4000]])[0]
                if spec["dimensions"] and len(vector) != spec["dimensions"]:
                    raise error("capability_test_failed", "Embedding 维度与索引空间不一致。")
                similarities = {}
                for c in candidates:
                    if c["vector"]:
                        doc_vector = finite_vector(c["vector"], len(vector))
                        similarities[c["id"]] = sum(x * y for x, y in zip(vector, doc_vector, strict=True))
                pg_chunks = [c["id"] for c in candidates if c["pg_index_id"]]
                if pg_chunks:
                    try:
                        with db.session() as semantic_session:
                            found = semantic_session.execute(sql_text("""
                            SELECT chunk_id, 1 - (embedding <=> CAST(:q AS vector)) AS similarity
                            FROM (
                              SELECT chunk_id, embedding FROM corpus_vectors
                               WHERE owner_user_id = :owner AND space = :space
                                 AND dimensions = :dimensions AND material_id IN :materials
                                 AND index_id IN :indexes AND chunk_id IN :chunks
                            ) scoped
                            ORDER BY embedding <=> CAST(:q AS vector)
                            LIMIT 80
                            """).bindparams(bindparam("materials", expanding=True),
                                bindparam("indexes", expanding=True), bindparam("chunks", expanding=True)),
                                {"owner": owner, "space": spec["embedding_space"],
                                 "dimensions": len(vector), "materials": list(material_ids),
                                 "indexes": sorted(pg_indexes), "chunks": pg_chunks,
                                 "q": json.dumps(vector)}).all()
                        similarities.update({row.chunk_id: float(row.similarity) for row in found})
                    except Exception:
                        # A failed SQL read can still use the frozen ready JSON checkpoint.
                        reasons.append("embedding_unavailable")
                        for index_id, encoded in pg_snapshots.items():
                            try:
                                saved = json.loads(encoded)
                                for c in candidates:
                                    if c["pg_index_id"] == index_id:
                                        doc_vector = finite_vector(saved[c["pg_position"]], len(vector))
                                        similarities[c["id"]] = sum(x * y for x, y in zip(vector, doc_vector, strict=True))
                            except (ValueError, TypeError, IndexError):
                                continue
                by_id = {c["id"]: c for c in candidates}
                semantic = sorted(
                    ({**by_id[cid], "similarity": similarity}
                     for cid, similarity in similarities.items()
                     if cid in by_id and similarity > .15),
                    key=lambda c: -c["similarity"])[:40]
            except Exception as exc:
                if getattr(exc, "code", "") in ("config_not_found", "disclosure_revoked",
                                                "course_not_found", "access_denied"):
                    raise
                reasons.append("embedding_unavailable")
    fused = {}
    for route in (lexical, semantic):
        for rank, item in enumerate(route):
            entry = fused.setdefault(item["id"], {**item, "score": 0.0})
            entry["score"] += 1 / (60 + rank + 1)
    ranked = sorted(fused.values(), key=lambda c: -c["score"])[:40]
    if spec and ranked and query.strip():
        try:
            order = ProviderClient(spec, guard=guard).rerank(query,
                [c["section_title"] + "\n" + c["text"] for c in ranked])
            ranked = [ranked[i] for i, _ in order]
        except Exception as exc:
            if getattr(exc, "code", "") in ("config_not_found", "disclosure_revoked",
                                            "course_not_found", "access_denied"):
                raise
            reasons.append("rerank_unavailable")
    guard()
    result, parents = [], set()
    for c in ranked:
        if c["parent_id"] in parents:
            continue
        parents.add(c["parent_id"])
        text = c["parent_text"] if len(c["parent_text"]) <= 2400 else c["text"]
        if c.get("auxiliary"):
            text = ("[图像辅助解释，非逐字原文]\n" if c.get("kind") == "image_understanding"
                else "[演讲者备注，非幻灯片正文]\n") + text
        result.append({"chunk_id": c["id"], "material_id": c["material_id"],
            "page_no": c["page_no"], "region_id": None, "attachment_text": text,
            "section_id": c["section_id"], "content_hash": c["content_hash"],
            "title": c["section_title"], "offset": c["offset"], "end_offset": c["end_offset"],
            **{k: c[k] for k in ("unit", "line_start", "line_end", "source_start", "source_end", "start_ms", "end_ms", "locator", "source_url", "auxiliary", "kind", "asset_index") if k in c}})
        if len(result) >= 16:
            break
    return result, sorted(set(reasons))


def source_overview(db, owner, material_ids):
    """Bounded, balanced samples accompany the complete authoritative chapter list."""
    from .study_sources import documents, chapter_catalog
    with db.session() as session:
        docs = documents(session, owner, material_ids)
        catalog = chapter_catalog(docs)
        by_section = {}
        from .source_models import source_records
        rows = source_records(session, owner, material_ids, parsed=True)
        for material, attachment in rows:
            document = ensure_document(session, owner, material, attachment)
            for block in json.loads(document.blocks_json):
                if block["section_id"] and block["kind"] != "title" and not block.get("auxiliary"):
                    by_section.setdefault(block["section_id"], block["text"])
        allowance = min(500, 12000 // max(1, len(catalog)))
        return {"chapters": [{**s, "sample": by_section.get(s["id"], "")[:allowance],
                    "sample_is_summary": False} for s in catalog],
                "coverage_policy": "逐章决定纳入、合并、延期或排除并说明原因；片段样例不代表完整章节。"}
