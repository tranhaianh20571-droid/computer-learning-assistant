"""切片 2 资料 API 路由（T02）。

统一会话认证 + ownership；响应不含原始文件名/路径。
OCR 写路径（T03/T04）以 T01 门禁为准，本路由只暴露契约与状态。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from pydantic import BaseModel, Field

from ..logging_audit.models import Base  # noqa: F401 - ensure metadata import order
from .chunk_index import ChunkIndexService, SOURCE_VERSION
from .errors import MaterialsError, error
from .models import MaterialRow, utcnow
from .upload import MaterialService, QuotaService, validate_batch


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    material_ids: list[str] = Field(default_factory=list)
    page_no: Optional[int] = Field(default=None, ge=1)
    source: str = "material"


class RetryIn(BaseModel):
    reason: str = Field(default="user_retry", max_length=40)


def create_materials_router(
    *,
    materials: MaterialService,
    quotas: QuotaService,
    index: ChunkIndexService,
    resolve_user: Callable[[Request], Any],
) -> APIRouter:
    router = APIRouter(prefix="/api/materials", tags=["slice2"])

    def _user(request: Request):
        return resolve_user(request)

    @router.post("", status_code=201)
    async def upload_materials(
        request: Request,
        files: list[UploadFile] = File(...),
    ) -> dict:
        user = _user(request)
        batch: list[tuple[str, Optional[str], bytes]] = []
        for f in files:
            payload = await f.read()
            batch.append((f.filename or "", f.content_type, payload))
        validated = validate_batch(batch)
        created = []
        for v in validated:
            created.append(materials.create_material(owner_user_id=user.user_id, validated=v))
        return {"data": created, "quota": quotas.snapshot(user.user_id)}

    @router.get("")
    def list_materials(request: Request) -> dict:
        user = _user(request)
        return {"data": materials.list_for_owner(user.user_id), "quota": quotas.snapshot(user.user_id)}

    @router.get("/{material_id}")
    def get_material(material_id: str, request: Request) -> dict:
        user = _user(request)
        return materials.get(material_id, actor_id=user.user_id)

    @router.get("/{material_id}/pages/{page_no}")
    def get_page(material_id: str, page_no: int, request: Request) -> dict:
        user = _user(request)
        materials.get(material_id, actor_id=user.user_id)
        from sqlalchemy import select

        from .models import MaterialPageRow

        with materials.db.session() as sess:
            row = (
                sess.execute(
                    select(MaterialPageRow).where(
                        MaterialPageRow.material_id == material_id,
                        MaterialPageRow.page_no == page_no,
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                raise error("index_out_of_range", "page not found")
            return {
                "page_id": row.page_id,
                "material_id": row.material_id,
                "page_no": row.page_no,
                "width_px": row.width_px,
                "height_px": row.height_px,
                "rotation_deg": row.rotation_deg,
                "status": row.status,
                "failure_reason": row.failure_reason,
                "text_trustworthiness": row.text_trustworthiness,
            }

    @router.post("/{material_id}/pages/{page_no}/retry")
    def retry_page(material_id: str, page_no: int, body: RetryIn, request: Request) -> dict:
        """把失败页重置为 pending；实际重解析由 OCR worker（T03）领取。"""
        user = _user(request)
        materials.get(material_id, actor_id=user.user_id)
        from sqlalchemy import select

        from .models import MaterialPageRow

        with materials.db.session() as sess:
            row = (
                sess.execute(
                    select(MaterialPageRow).where(
                        MaterialPageRow.material_id == material_id,
                        MaterialPageRow.page_no == page_no,
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                raise error("index_out_of_range", "page not found")
            if row.status != "failed":
                raise error("page_state_conflict", "only failed pages can be retried")
            row.status = "pending"
            row.failure_reason = None
            row.updated_at = utcnow()
            sess.flush()
            result = {"material_id": material_id, "page_no": page_no, "status": row.status}
            sess.commit()
        return result

    @router.delete("/{material_id}")
    def delete_material(material_id: str, request: Request) -> dict:
        user = _user(request)
        result = materials.delete(material_id, actor_id=user.user_id)
        # 立即从检索命中集移除并写索引删除账本
        index.delete_for_material(material_id, owner_user_id=user.user_id, reason="material_deleted")
        return result

    @router.post("/search")
    def search_materials(body: SearchIn, request: Request) -> dict:
        user = _user(request)
        if body.source != "material":
            raise error("invalid_field", "only source=material supported in slice 2")
        results = index.search(
            owner_user_id=user.user_id,
            query=body.query,
            material_ids=body.material_ids,
            page_no=body.page_no,
        )
        return {"data": results, "source": "material", "source_version": SOURCE_VERSION}

    return router


def install_materials_error_handlers(app) -> None:
    from fastapi.responses import JSONResponse

    @app.exception_handler(MaterialsError)
    async def _materials_error(_: Request, exc: MaterialsError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"error_code": exc.code, "message": exc.message}
        )
