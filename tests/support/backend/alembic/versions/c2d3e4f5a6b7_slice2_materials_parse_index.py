"""slice2_materials_parse_index

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-09-28

切片 2 T02：资料/页/区域/图块/文本段/配额账本/删除账本数据模型，
以及 `learning_tasks.lane` / `material_id` / `page_no` 字段。
OCR 写路径以 T01 真实样本 spike 为门禁；本迁移只落模型与索引。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "materials",
        sa.Column("material_id", sa.String(length=64), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.Column("filename_hash", sa.String(length=64), nullable=False),
        sa.Column("source_bytes_sha256", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("quota_reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_materials_owner", "materials", ["owner_user_id"], unique=False)
    op.create_index("idx_materials_status", "materials", ["owner_user_id", "status"], unique=False)

    op.create_table(
        "material_pages",
        sa.Column("page_id", sa.String(length=64), primary_key=True),
        sa.Column("material_id", sa.String(length=64), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("width_px", sa.Integer(), nullable=False),
        sa.Column("height_px", sa.Integer(), nullable=False),
        sa.Column("rotation_deg", sa.Integer(), nullable=False),
        sa.Column("text_trustworthiness", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("failure_reason", sa.String(length=64), nullable=True),
        sa.Column("ocr_job_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["material_id"], ["materials.material_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("material_id", "page_no", name="uq_material_pages_material_page"),
    )
    op.create_index("idx_material_pages_material", "material_pages", ["material_id", "page_no"], unique=False)
    op.create_index("idx_material_pages_status", "material_pages", ["material_id", "status"], unique=False)

    op.create_table(
        "material_regions",
        sa.Column("region_id", sa.String(length=64), primary_key=True),
        sa.Column("material_id", sa.String(length=64), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("bbox_left", sa.Float(), nullable=False),
        sa.Column("bbox_top", sa.Float(), nullable=False),
        sa.Column("bbox_right", sa.Float(), nullable=False),
        sa.Column("bbox_bottom", sa.Float(), nullable=False),
        sa.Column("coordinate_system", sa.String(length=20), nullable=False),
        sa.Column("rotation", sa.Integer(), nullable=False),
        sa.Column("reading_order", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("trustworthy", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["material_id"], ["materials.material_id"], ondelete="CASCADE"),
    )
    op.create_index("idx_material_regions_material", "material_regions", ["material_id", "page_no"], unique=False)
    op.create_index("idx_material_regions_kind", "material_regions", ["material_id", "kind"], unique=False)

    op.create_table(
        "figure_assets",
        sa.Column("figure_id", sa.String(length=64), primary_key=True),
        sa.Column("material_id", sa.String(length=64), nullable=False),
        sa.Column("material_region_id", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=128), nullable=False),
        sa.Column("source_page_no", sa.Integer(), nullable=False),
        sa.Column("bbox", sa.Text(), nullable=False),
        sa.Column("caption_text", sa.String(length=200), nullable=True),
        sa.Column("adjacent_text_ref", sa.String(length=64), nullable=True),
        sa.Column("inverse_transform", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["material_id"], ["materials.material_id"], ondelete="CASCADE"),
    )
    op.create_index("idx_figure_assets_material", "figure_assets", ["material_id", "source_page_no"], unique=False)
    op.create_index("idx_figure_assets_region", "figure_assets", ["material_region_id"], unique=False)

    op.create_table(
        "text_chunks",
        sa.Column("chunk_id", sa.String(length=64), primary_key=True),
        sa.Column("material_id", sa.String(length=64), nullable=False),
        sa.Column("material_region_id", sa.String(length=64), nullable=True),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("tsvector", postgresql.TSVECTOR(), nullable=True),
        sa.Column("source_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["material_id"], ["materials.material_id"], ondelete="CASCADE"),
    )
    op.create_index("idx_text_chunks_material", "text_chunks", ["material_id", "page_no"], unique=False)
    op.create_index("idx_text_chunks_region", "text_chunks", ["material_region_id"], unique=False)
    op.create_index(
        "idx_text_chunks_tsvector",
        "text_chunks",
        ["tsvector"],
        unique=False,
        postgresql_using="gin",
    )

    op.create_table(
        "material_upload_quotas",
        sa.Column("owner_user_id", sa.String(length=64), primary_key=True),
        sa.Column("quota_bytes", sa.BigInteger(), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "deletion_tombstones",
        sa.Column("tombstone_id", sa.String(length=64), primary_key=True),
        sa.Column("object_type", sa.String(length=20), nullable=False),
        sa.Column("object_id", sa.String(length=64), nullable=False),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cleanup_stage", sa.String(length=30), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_deletion_tombstones_object", "deletion_tombstones", ["object_type", "object_id"], unique=False)
    op.create_index("idx_deletion_tombstones_owner", "deletion_tombstones", ["owner_user_id"], unique=False)

    op.create_table(
        "asset_usage_ledger",
        sa.Column("ledger_id", sa.String(length=64), primary_key=True),
        sa.Column("object_type", sa.String(length=20), nullable=False),
        sa.Column("object_id", sa.String(length=64), nullable=False),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.Column("last_referenced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_task_id", sa.String(length=64), nullable=True),
        sa.Column("tombstoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deletion_reason", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_asset_usage_ledger_object", "asset_usage_ledger", ["object_type", "object_id"], unique=False)
    op.create_index("idx_asset_usage_ledger_owner", "asset_usage_ledger", ["owner_user_id"], unique=False)

    # 任务表扩展：lane + OCR 任务指针。默认 interactive，兼容切片 0 既有行。
    op.add_column(
        "learning_tasks",
        sa.Column("lane", sa.String(length=20), nullable=False, server_default="interactive"),
    )
    op.add_column("learning_tasks", sa.Column("material_id", sa.String(length=64), nullable=True))
    op.add_column("learning_tasks", sa.Column("page_no", sa.Integer(), nullable=True))
    op.create_index("idx_learning_tasks_lane_status", "learning_tasks", ["status", "lane"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_learning_tasks_lane_status", table_name="learning_tasks")
    op.drop_column("learning_tasks", "page_no")
    op.drop_column("learning_tasks", "material_id")
    op.drop_column("learning_tasks", "lane")

    op.drop_index("idx_asset_usage_ledger_owner", table_name="asset_usage_ledger")
    op.drop_index("idx_asset_usage_ledger_object", table_name="asset_usage_ledger")
    op.drop_table("asset_usage_ledger")
    op.drop_index("idx_deletion_tombstones_owner", table_name="deletion_tombstones")
    op.drop_index("idx_deletion_tombstones_object", table_name="deletion_tombstones")
    op.drop_table("deletion_tombstones")
    op.drop_table("material_upload_quotas")
    op.drop_index("idx_text_chunks_tsvector", table_name="text_chunks")
    op.drop_index("idx_text_chunks_region", table_name="text_chunks")
    op.drop_index("idx_text_chunks_material", table_name="text_chunks")
    op.drop_table("text_chunks")
    op.drop_index("idx_figure_assets_region", table_name="figure_assets")
    op.drop_index("idx_figure_assets_material", table_name="figure_assets")
    op.drop_table("figure_assets")
    op.drop_index("idx_material_regions_kind", table_name="material_regions")
    op.drop_index("idx_material_regions_material", table_name="material_regions")
    op.drop_table("material_regions")
    op.drop_index("idx_material_pages_status", table_name="material_pages")
    op.drop_index("idx_material_pages_material", table_name="material_pages")
    op.drop_table("material_pages")
    op.drop_index("idx_materials_status", table_name="materials")
    op.drop_index("idx_materials_owner", table_name="materials")
    op.drop_table("materials")
