"""内置检索分词与 tsvector 写入（切片 2 T02/T05）。

契约（交接文档 §3.1「内置检索契约」）：
- jieba 中文分词在应用层执行，**锁定版本**；PostgreSQL 不自动执行 jieba；
- 英文标识符、代码符号、版本号、页码作为独立词元显式写入 tsvector；
- 分词版本变化 → 重建索引并记录版本。

T02 只落地确定性分词与版本常量；查询/删除账本属 T05（无 OCR 依赖，可继续）。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Iterable, Optional

from sqlalchemy import select

from ..logging_audit.db import Database
from .errors import error
from .models import MaterialRegionRow, TextChunkRow, load_json

# ---- 版本锁定（T02 契约冻结） ----
# jieba 版本在 server/backend/pyproject.toml 锁定为 0.42.1；
# 变更词典/分词实现必须递增 INDEX_PIPELINE_VERSION 并触发重建。
JIEBA_VERSION = "0.42.1"
INDEX_PIPELINE_VERSION = "slice2-index-v1"
SOURCE_VERSION = f"jieba-{JIEBA_VERSION}+{INDEX_PIPELINE_VERSION}"

# 英文标识符/代码符号/版本号/页码作为独立词元。
# 注意：标识符与版本号必须整体保留（PostgreSQL 不自动执行 jieba，
# 而 simple 配置不做词干化，拆分会在检索时丢召回）。
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*")
_VERSION_RE = re.compile(r"v?\d+(?:\.\d+){1,4}")
_CODE_RUN_RE = re.compile(r"[A-Za-z0-9_+#*\-/.@]+")
# 中文/日韩统一表意文字片段
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")

_PAGE_TOKEN_PREFIX = "p"


@dataclass
class Tokenized:
    tokens: list[str]
    source_version: str


def jieba_tokens(text: str) -> list[str]:
    """中文分词 + 独立英文/代码/数字词元。确定性、可复现。

    按 CJK / 非 CJK 分段：中文片段交给 jieba；ASCII 片段整体保留，
    因此英文标识符/版本号不会被 jieba 拆碎。
    """
    import jieba

    if jieba.__version__ != JIEBA_VERSION:
        # 版本漂移必须显式暴露，不能静默继续
        raise RuntimeError(
            f"jieba version drift: expected {JIEBA_VERSION}, got {jieba.__version__}"
        )
    tokens: list[str] = []
    for segment in _split_cjk(text or ""):
        if _CJK_RUN_RE.fullmatch(segment):
            tokens.extend(piece for piece in jieba.cut(segment) if piece.strip())
        else:
            tokens.extend(_ascii_tokens(segment))
    return [t for t in tokens if t]


def _split_cjk(text: str) -> list[str]:
    """把字符串拆成 CJK 与非 CJK 连续片段。"""
    segments: list[str] = []
    buf: list[str] = []
    buf_is_cjk = False
    for ch in text:
        is_cjk = bool(_CJK_RUN_RE.fullmatch(ch))
        if buf and is_cjk != buf_is_cjk:
            segments.append("".join(buf))
            buf = []
        buf.append(ch)
        buf_is_cjk = is_cjk
    if buf:
        segments.append("".join(buf))
    return segments


def _ascii_tokens(segment: str) -> list[str]:
    """从非 CJK 片段中抽取整体标识符/版本号/数字/代码串。"""
    out: list[str] = []
    for raw in segment.split():
        version = _VERSION_RE.fullmatch(raw)
        if version:
            out.append(raw)
            continue
        ident = _IDENTIFIER_RE.fullmatch(raw)
        if ident:
            out.append(raw)
            continue
        found = _CODE_RUN_RE.findall(raw)
        out.extend(found if found else [raw])
    return out


def _contains_cjk(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def build_tokens(
    *,
    text: str,
    page_no: int,
    caption: Optional[str] = None,
    identifiers: Optional[Iterable[str]] = None,
) -> Tokenized:
    tokens = list(jieba_tokens(text))
    if caption:
        tokens.extend(jieba_tokens(caption))
    for ident in identifiers or ():
        if _VERSION_RE.fullmatch(ident) or _IDENTIFIER_RE.fullmatch(ident):
            tokens.append(ident)
        else:
            tokens.extend(_CODE_RUN_RE.findall(ident) or [ident])
    # 页码作为独立词元
    tokens.append(f"{_PAGE_TOKEN_PREFIX}{page_no}")
    # 去重但保持稳定顺序
    seen: set[str] = set()
    ordered: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    return Tokenized(tokens=ordered, source_version=SOURCE_VERSION)


def to_tsvector_expr(tokens: list[str]) -> str:
    """生成 `to_tsvector('simple', ...)` 可用的词元串（simple 配置不做语言处理）。

    `simple` 配置不做词干化，保留标识符/版本号原样（§4 统一 6）。
    注意：PostgreSQL 会把 `conv2d_block_7` 再拆成 3 个 lexeme，
    查询必须同样由 lexeme 构建，不能直接用原始标识符拼 tsquery。
    """
    safe = [t.replace("'", "''") for t in tokens if t]
    return " ".join(safe)


def to_tsquery_expr(tokens: list[str]) -> str:
    """把词元列表转为 OR 连接的 tsquery 字符串。

    解析器行为（`_` 拆开、`.` 保留）不适合用正则复现，因此词元拼接成串后
    交由数据库 `plainto_tsquery('simple', ...)` 解析；调用方用
    `ts_query_params()` 取对应的 SQL 参数。
    """
    return to_tsvector_expr(tokens)


def ts_query_params(tokens: list[str]) -> dict:
    """查询侧 SQL 参数：用数据库函数解析词元串，避免与写入侧不一致。"""
    return {"q": to_tsvector_expr(tokens)}


class ChunkIndexService:
    """文本段写入与按 owner/材料/区域过滤的检索（T02 地基 + T05 查询）。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def index_chunk(
        self,
        *,
        material_id: str,
        page_no: int,
        text: str,
        chunk_index: int = 0,
        material_region_id: Optional[str] = None,
        caption: Optional[str] = None,
        identifiers: Optional[Iterable[str]] = None,
    ) -> dict:
        tokenized = build_tokens(
            text=text, page_no=page_no, caption=caption, identifiers=identifiers
        )
        # tsvector 列必须用数据库函数生成：直接绑定字符串会按字面量存整词，
        # 与查询侧 `to_tsquery` 拆分出的 lexeme 不一致（导致英文标识符漏召回）。
        from sqlalchemy import func as sa_func

        ts_expr = sa_func.to_tsvector("simple", to_tsvector_expr(tokenized.tokens))
        chunk_id = f"chk_{uuid.uuid4().hex[:12]}"
        with self.db.session() as sess:
            row = TextChunkRow(
                chunk_id=chunk_id,
                material_id=material_id,
                material_region_id=material_region_id,
                page_no=page_no,
                chunk_index=chunk_index,
                tsvector=ts_expr,  # type: ignore[arg-type]
                source_version=tokenized.source_version,
            )
            sess.add(row)
            sess.flush()
            sess.commit()
        return {
            "chunk_id": chunk_id,
            "material_id": material_id,
            "page_no": page_no,
            "chunk_index": chunk_index,
            "token_count": len(tokenized.tokens),
            "source_version": tokenized.source_version,
        }

    def search(
        self,
        *,
        owner_user_id: str,
        query: str,
        material_ids: list[str],
        page_no: Optional[int] = None,
    ) -> list[dict]:
        """仅在本 owner 选定材料、且区域可用的范围内检索。"""
        from .models import MaterialRow, FigureAssetRow

        if not query.strip():
            raise error("invalid_field", "empty query")
        if not material_ids:
            return []
        tokens = jieba_tokens(query)
        if not tokens:
            return []
        ts_query = to_tsquery_expr(tokens)
        if not ts_query:
            return []
        with self.db.session() as sess:
            owned = set(
                sess.execute(
                    select(MaterialRow.material_id).where(
                        MaterialRow.owner_user_id == owner_user_id,
                        MaterialRow.material_id.in_(material_ids),
                        MaterialRow.deleted_at.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            if not owned:
                return []
            from sqlalchemy import text as sa_text

            sql = sa_text(
                """
                SELECT c.chunk_id, c.material_id, c.page_no, c.chunk_index,
                       c.material_region_id, c.source_version
                FROM text_chunks c
                JOIN materials m ON m.material_id = c.material_id
                WHERE m.owner_user_id = :owner
                  AND m.deleted_at IS NULL
                  AND c.material_id = ANY(:mids)
                  AND (CAST(:page_no AS INTEGER) IS NULL OR c.page_no = CAST(:page_no AS INTEGER))
                  AND c.tsvector @@ plainto_tsquery('simple', :q)
                ORDER BY c.page_no, c.chunk_index
                """
            )
            rows = sess.execute(
                sql,
                {
                    "owner": owner_user_id,
                    "mids": list(owned),
                    "page_no": page_no,
                    "q": ts_query,
                },
            ).all()
            results: list[dict] = []
            for row in rows:
                figure = (
                    sess.execute(
                        select(FigureAssetRow).where(
                            FigureAssetRow.material_region_id == row.material_region_id
                        )
                    )
                    .scalars()
                    .first()
                    if row.material_region_id
                    else None
                )
                results.append(
                    {
                        "chunk_id": row.chunk_id,
                        "material_id": row.material_id,
                        "page_no": row.page_no,
                        "chunk_index": row.chunk_index,
                        "region_id": row.material_region_id,
                        "figure_asset_id": figure.figure_id if figure else None,
                        "source": "material",
                        "source_version": row.source_version,
                    }
                )
            return results

    def region_is_available(self, region_id: str) -> bool:
        with self.db.session() as sess:
            row = sess.get(MaterialRegionRow, region_id)
            return row is not None and row.status == "success"

    def delete_for_material(self, material_id: str, *, owner_user_id: str, reason: str) -> int:
        """删除资料/区域失效：同事务移除命中集并写索引删除账本。"""
        from .models import AssetUsageLedgerRow

        with self.db.session() as sess:
            rows = (
                sess.execute(select(TextChunkRow).where(TextChunkRow.material_id == material_id))
                .scalars()
                .all()
            )
            for row in rows:
                sess.delete(row)
            sess.add(
                AssetUsageLedgerRow(
                    ledger_id=f"led_{uuid.uuid4().hex[:12]}",
                    object_type="index",
                    object_id=material_id,
                    owner_user_id=owner_user_id,
                    tombstoned_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                    deletion_reason=reason,
                )
            )
            sess.commit()
            return len(rows)


def load_region_meta(row: MaterialRegionRow) -> dict:
    return load_json(None) | {"kind": row.kind, "status": row.status}
