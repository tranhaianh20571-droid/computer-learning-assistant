"""原图裁切与坐标映射（切片 2 T04）。

- 坐标换模：OCR 返回的 bbox 按页面尺寸、rotation、coordinate_system 换算为
  200 DPI 渲染像素坐标（top_left）；旋转页的逆变换参数可复现并落
  `figure_assets.inverse_transform`；
- 裁切在受控渲染结果上执行，图块写受控存储（对象键不含用户内容）；
- 无法稳定裁切 → 区域 `partial`、不新增 figure_assets，保留页码支持按需重绘；
- 结构图（structure_diagram）必须命中图块才算完整（trustworthy 语义）。
"""

from __future__ import annotations

import io
import uuid
from typing import Optional

from sqlalchemy import select

from ..logging_audit.db import Database
from .models import FigureAssetRow, MaterialPageRow, MaterialRegionRow, MaterialRow
from .storage import MaterialStorage, StorageError


class CropError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def map_bbox_to_pixels(
    *,
    bbox: dict,
    page_width: int,
    page_height: int,
    rotation: int,
    coordinate_system: str = "top_left",
) -> dict:
    """把 OCR 报告的 bbox 逆变换为未旋转页面的 top_left 坐标。

    - `page_width/height` 为**未旋转**页面尺寸（渲染像素）；
    - `bbox` 为 OCR 在旋转后视图（rotation=90/180/270）里报告的坐标；
    - 逆变换（连续坐标，round-trip 可复现）：
      * 90° CW：rx = H − oy, ry = ox ⇒ ox = ry, oy = H − rx
      * 270° CW：rx = oy, ry = W − ox ⇒ ox = W − ry, oy = rx
      * 180°：ox = W − rx, oy = H − ry
    - bottom_left（PDF 原生）先翻转为 top_left。
    纯函数：同输入同输出（故障注入「旋转/不同坐标系」的可复现核心）。
    """
    left = float(bbox.get("left", 0))
    top = float(bbox.get("top", 0))
    right = float(bbox.get("right", 0))
    bottom = float(bbox.get("bottom", 0))
    if coordinate_system == "bottom_left" and page_height:
        top, bottom = page_height - bottom, page_height - top
    rotation = int(rotation) % 360
    if rotation == 90:
        left, top, right, bottom = top, page_height - right, bottom, page_height - left
    elif rotation == 270:
        left, top, right, bottom = page_width - bottom, left, page_width - top, right
    elif rotation == 180:
        left, right = page_width - right, page_width - left
        top, bottom = page_height - bottom, page_height - top
    return {
        "left": round(max(0.0, min(left, page_width or left)), 2),
        "top": round(max(0.0, min(top, page_height or top)), 2),
        "right": round(max(0.0, min(right, page_width or right)), 2),
        "bottom": round(max(0.0, min(bottom, page_height or bottom)), 2),
    }


class CropService:
    def __init__(self, db: Database, storage: Optional[MaterialStorage] = None) -> None:
        self.db = db
        self.storage = storage or MaterialStorage()

    def crop_region(self, *, owner_user_id: str, region_id: str) -> dict:
        """把一个 success 区域裁成图块资产；失败抛受控错误（调用方标 partial）。"""
        with self.db.session() as sess:
            region = sess.get(MaterialRegionRow, region_id)
            if region is None:
                raise CropError("region_not_found")
            material = sess.get(MaterialRow, region.material_id)
            if material is None or material.owner_user_id != owner_user_id:
                raise CropError("region_not_found")
            page = (
                sess.execute(
                    select(MaterialPageRow).where(
                        MaterialPageRow.material_id == region.material_id,
                        MaterialPageRow.page_no == region.page_no,
                    )
                )
                .scalars()
                .first()
            )
            if page is None or not page.width_px or not page.height_px:
                raise CropError("page_not_rendered")

            payload = self.storage.get(MaterialStorage.original_key(region.material_id))
            rendered = None
            from .parse import render_page, RenderError

            try:
                rendered = render_page(payload, region.page_no)
            except RenderError as exc:
                raise CropError("render_failed") from exc

            # 渲染像素坐标已在入库时换算；PNG 尺寸 = 渲染像素（scale=1 语义）
            box = (
                max(0, int(region.bbox_left)),
                max(0, int(region.bbox_top)),
                min(rendered.width_px, int(region.bbox_right) or 1),
                min(rendered.height_px, int(region.bbox_bottom) or 1),
            )
            if box[2] - box[0] < 2 or box[3] - box[1] < 2:
                raise CropError("bbox_too_small")

            figure_id = f"fig_{uuid.uuid4().hex[:12]}"
            buffer = io.BytesIO()
            rendered.image.crop(box).save(buffer, format="PNG")
            key = MaterialStorage.figure_key(region.material_id, figure_id)
            try:
                self.storage.put(key, buffer.getvalue())
            except StorageError as exc:
                raise CropError("store_failed") from exc

            inverse = {
                "rotation": int(page.rotation_deg or 0),
                "coordinate_system": region.coordinate_system,
                "page_width": page.width_px,
                "page_height": page.height_px,
                "bbox": {
                    "left": region.bbox_left, "top": region.bbox_top,
                    "right": region.bbox_right, "bottom": region.bbox_bottom,
                },
            }
            figure = FigureAssetRow(
                figure_id=figure_id,
                material_id=region.material_id,
                material_region_id=region.region_id,
                storage_key=key,
                source_page_no=region.page_no,
                bbox=f'{{"left": {box[0]}, "top": {box[1]}, "right": {box[2]}, "bottom": {box[3]}}}',
                inverse_transform=__import__("json").dumps(inverse, ensure_ascii=False),
            )
            sess.add(figure)
            sess.commit()
            return {
                "figure_id": figure_id,
                "storage_key": key,
                "page_no": region.page_no,
                "bbox": inverse["bbox"],
            }

    def crop_page_figures(self, *, owner_user_id: str, material_id: str, page_no: int) -> dict:
        """对一页的全部 success 区域执行裁切；失败区域降级 partial。"""
        results = {"cropped": 0, "partial": 0}
        with self.db.session() as sess:
            region_ids = (
                sess.execute(
                    select(MaterialRegionRow.region_id).where(
                        MaterialRegionRow.material_id == material_id,
                        MaterialRegionRow.page_no == page_no,
                        MaterialRegionRow.status == "success",
                        MaterialRegionRow.kind.in_(("figure", "structure_diagram", "table", "formula")),
                    )
                )
                .scalars()
                .all()
            )
        for rid in region_ids:
            try:
                self.crop_region(owner_user_id=owner_user_id, region_id=rid)
                results["cropped"] += 1
            except CropError:
                with self.db.session() as sess:
                    region = sess.get(MaterialRegionRow, rid)
                    if region is not None:
                        region.status = "partial"
                        region.trustworthy = False
                        sess.commit()
                results["partial"] += 1
        return results


__all__ = ["CropService", "CropError", "map_bbox_to_pixels"]
