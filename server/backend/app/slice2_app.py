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
    def __init__(self, db, audit: Optional[PgAuditLog] = None, storage=None) -> None:
        self.db = db
        self.audit = audit
        self.quotas = QuotaService(db)
        self.materials = MaterialService(db, self.quotas, storage=storage)
        self.index = ChunkIndexService(db)


def create_slice2_app(
    state: Optional[Slice2State] = None,
    slice1_state: Optional[Slice1State] = None,
    *,
    storage=None,
    pipeline_holder: Optional[dict] = None,
) -> FastAPI:
    app = create_slice1_app(state=slice1_state)
    if state is None:
        base = app.state.slice1
        state = Slice2State(base.db, base.audit, storage=storage)
    app.state.slice2 = state

    def _resolve_user(request: Request):
        # 登录态优先；无有效会话时以访客身份继续（auth/guest.py）
        from .auth.guest import resolve_user_or_guest

        return resolve_user_or_guest(request, app.state.slice0.auth)

    app.include_router(
        create_materials_router(
            materials=state.materials,
            quotas=state.quotas,
            index=state.index,
            resolve_user=_resolve_user,
            pipeline_holder=pipeline_holder,
        )
    )
    install_materials_error_handlers(app)
    return app
