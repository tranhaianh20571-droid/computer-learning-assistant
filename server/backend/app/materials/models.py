"""切片 2 数据模型：资料、页、区域、图块、文本段、配额账本与删除账本。

对应交接文档 §3.1 核心契约。所有个人资源含 `owner_user_id`；
文件名/路径只存散列，不存正文。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from ..logging_audit.models import Base

MATERIAL_KINDS = ("pdf", "md", "txt")
MATERIAL_STATUSES = ("uploaded", "parsing", "parsed", "partial", "failed")
PAGE_STATUSES = ("pending", "success", "partial", "failed")
REGION_KINDS = ("text", "figure", "table", "formula", "structure_diagram")
REGION_STATUSES = ("pending", "success", "partial", "failed")
COORDINATE_SYSTEMS = ("top_left", "bottom_left")
TOMBSTONE_OBJECT_TYPES = ("material", "page", "region", "figure", "chunk")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MaterialRow(Base):
    __tablename__ = "materials"
    __table_args__ = (
        Index("idx_materials_owner", "owner_user_id"),
        Index("idx_materials_status", "owner_user_id", "status"),
    )

    material_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    filename_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_bytes_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    quota_reserved_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="uploaded")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MaterialPageRow(Base):
    __tablename__ = "material_pages"
    __table_args__ = (
        UniqueConstraint("material_id", "page_no", name="uq_material_pages_material_page"),
        Index("idx_material_pages_material", "material_id", "page_no"),
        Index("idx_material_pages_status", "material_id", "status"),
    )

    page_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    material_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("materials.material_id", ondelete="CASCADE"), nullable=False
    )
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    width_px: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    height_px: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rotation_deg: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text_trustworthiness: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    failure_reason: Mapped[str | None] = mapped_column(String(64))
    ocr_job_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MaterialRegionRow(Base):
    __tablename__ = "material_regions"
    __table_args__ = (
        Index("idx_material_regions_material", "material_id", "page_no"),
        Index("idx_material_regions_kind", "material_id", "kind"),
    )

    region_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    material_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("materials.material_id", ondelete="CASCADE"), nullable=False
    )
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    bbox_left: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    bbox_top: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    bbox_right: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    bbox_bottom: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    coordinate_system: Mapped[str] = mapped_column(String(20), nullable=False, default="top_left")
    rotation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reading_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kind: Mapped[str] = mapped_column(String(30), nullable=False, default="text")
    trustworthy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class FigureAssetRow(Base):
    __tablename__ = "figure_assets"
    __table_args__ = (
        Index("idx_figure_assets_material", "material_id", "source_page_no"),
        Index("idx_figure_assets_region", "material_region_id"),
    )

    figure_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    material_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("materials.material_id", ondelete="CASCADE"), nullable=False
    )
    material_region_id: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(128), nullable=False)
    source_page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    bbox: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    caption_text: Mapped[str | None] = mapped_column(String(200))
    adjacent_text_ref: Mapped[str | None] = mapped_column(String(64))
    inverse_transform: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class TextChunkRow(Base):
    __tablename__ = "text_chunks"
    __table_args__ = (
        Index("idx_text_chunks_material", "material_id", "page_no"),
        Index("idx_text_chunks_region", "material_region_id"),
        Index("idx_text_chunks_tsvector", "tsvector", postgresql_using="gin"),
    )

    chunk_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    material_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("materials.material_id", ondelete="CASCADE"), nullable=False
    )
    material_region_id: Mapped[str | None] = mapped_column(String(64))
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tsvector: Mapped[str | None] = mapped_column(TSVECTOR)
    source_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MaterialUploadQuotaRow(Base):
    __tablename__ = "material_upload_quotas"

    owner_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    quota_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class DeletionTombstoneRow(Base):
    """删除墓碑占位：本切片只落钩子，恢复流程归切片 5。"""

    __tablename__ = "deletion_tombstones"
    __table_args__ = (
        Index("idx_deletion_tombstones_object", "object_type", "object_id"),
        Index("idx_deletion_tombstones_owner", "owner_user_id"),
    )

    tombstone_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    object_type: Mapped[str] = mapped_column(String(20), nullable=False)
    object_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    cleanup_stage: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AssetUsageLedgerRow(Base):
    """资产引用/索引删除账本：删除或失效时同事务写入。"""

    __tablename__ = "asset_usage_ledger"
    __table_args__ = (
        Index("idx_asset_usage_ledger_object", "object_type", "object_id"),
        Index("idx_asset_usage_ledger_owner", "owner_user_id"),
    )

    ledger_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    object_type: Mapped[str] = mapped_column(String(20), nullable=False)
    object_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    last_referenced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_task_id: Mapped[str | None] = mapped_column(String(64))
    tombstoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletion_reason: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


def parse_bbox(row: MaterialRegionRow) -> dict:
    return {
        "left": row.bbox_left,
        "top": row.bbox_top,
        "right": row.bbox_right,
        "bottom": row.bbox_bottom,
    }


def load_json(text: Optional[str]) -> dict:
    try:
        value = json.loads(text or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}
