"""切片 1 测试应用：在切片 0 应用上挂载能力配置/外发确认/连接器路由。"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, Request

from .logging_audit.repositories import PgAuditLog
from .slice0_app import Slice0State, create_slice0_app
from .slice1.adapters import CapabilityTestService
from .slice1.config_service import ConfigService
from .slice1.connector.gateway import ConnectorGateway
from .slice1.connector_service import ConnectorService
from .slice1.disclosure_service import DisclosureService
from .slice1.errors import Slice1Error, as_detail
from .slice1.router import create_slice1_router


class Slice1State:
    def __init__(self, db, audit: PgAuditLog, encryption_key: Optional[str] = None) -> None:
        self.db = db
        self.audit = audit
        self.disclosures = DisclosureService(db, audit)
        self.configs = ConfigService(
            db,
            encryption_key=encryption_key,
            on_config_changed=self._on_config_changed,
        )
        self.capabilities = CapabilityTestService(self.configs)
        self.connectors = ConnectorService(db, audit)
        self.gateway = ConnectorGateway(
            self.connectors,
            task_owner_resolver=self._task_owner,
        )

    def _on_config_changed(self, config_id: str, actor_id: str, action: str) -> None:
        # 配置端点/凭据变更或停用后，旧外发授权立即失效。
        self.disclosures.revoke_for_config(config_id, reason=action)
        try:
            self.audit.append(
                "config.changed",
                actor_id=actor_id,
                object_type="service_config",
                object_id=config_id,
                result=action,
                config_id=config_id,
            )
        except Exception:  # noqa: BLE001 - 审计失败必须可见，但不阻断配置变更
            pass

    def _task_owner(self, task_id: str) -> Optional[str]:
        from .tasks.service import LearningTaskRow

        with self.db.session() as sess:
            row = sess.get(LearningTaskRow, task_id)
            return row.owner_user_id if row else None


def create_slice1_app(state: Optional[Slice1State] = None, slice0_state: Optional[Slice0State] = None) -> FastAPI:
    if slice0_state is None:
        from .db import Database
        from .logging_audit.repositories import PgAuditLog as _Audit

        db = Database()
        audit = _Audit(db)
        slice0_state = Slice0State(db, audit)
    app = create_slice0_app(slice0_state)
    if state is None:
        state = Slice1State(slice0_state.db, slice0_state.audit)
    app.state.slice1 = state

    def resolve_user(request: Request):
        raw = request.headers.get("Authorization", "")
        token = raw[7:] if raw.startswith("Bearer ") else ""
        if not token:
            for part in request.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "session_token":
                    token = v
        from .auth.service import AuthError

        try:
            return slice0_state.auth.resolve_session(token)
        except AuthError as exc:
            from fastapi import HTTPException

            raise HTTPException(status_code=exc.status, detail={"error_code": exc.code, "message": exc.message})

    app.include_router(
        create_slice1_router(
            configs=state.configs,
            capabilities=state.capabilities,
            disclosures=state.disclosures,
            connectors=state.connectors,
            resolve_user=resolve_user,
        )
    )

    from fastapi.responses import JSONResponse

    @app.exception_handler(Slice1Error)
    async def slice1_error(_: Request, exc: Slice1Error) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=as_detail(exc))

    return app
