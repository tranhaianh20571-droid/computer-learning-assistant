"""切片 2 集成应用：在切片 1 应用上挂载资料上传、状态、删除与检索路由。

OCR 写路径（T03/T04）以 T01 真实样本 spike 为门禁；本应用只装配 T02 已冻结的
数据模型、上传校验、配额账本、PDF 渲染契约与内置检索。
"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException, Request

from .logging_audit.repositories import PgAuditLog
from .materials.chunk_index import ChunkIndexService
from .materials.router import create_materials_router, install_materials_error_handlers
from .materials.upload import MaterialService, QuotaService
from .slice1_app import Slice1State, create_slice1_app


class Slice2State:
    def __init__(self, db, audit: Optional[PgAuditLog] = None) -> None:
        self.db = db
        self.audit = audit
        self.quotas = QuotaService(db)
        self.materials = MaterialService(db, self.quotas)
        self.index = ChunkIndexService(db)


def create_slice2_app(
    state: Optional[Slice2State] = None,
    slice1_state: Optional[Slice1State] = None,
) -> FastAPI:
    app = create_slice1_app(state=slice1_state)
    if state is None:
        base = app.state.slice1
        state = Slice2State(base.db, base.audit)
    app.state.slice2 = state

    def _resolve_user(request: Request):
        raw = request.headers.get("Authorization", "")
        token = raw[7:] if raw.startswith("Bearer ") else ""
        if not token:
            for part in request.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "session_token":
                    token = v
        from .auth.service import AuthError

        try:
            return app.state.slice0.auth.resolve_session(token)
        except AuthError as exc:
            raise HTTPException(
                status_code=exc.status, detail={"error_code": exc.code, "message": exc.message}
            )

    app.include_router(
        create_materials_router(
            materials=state.materials,
            quotas=state.quotas,
            index=state.index,
            resolve_user=_resolve_user,
        )
    )
    install_materials_error_handlers(app)
    return app
