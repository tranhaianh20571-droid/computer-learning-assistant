"""切片 1 API 路由：能力配置、外发确认、连接器配对与请求围栏。

统一会话认证与所有权校验；普通用户不可读写管理员 OCR/搜索配置。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from ..logging_audit.models import Base  # noqa: F401 - ensure metadata import order
from .adapters import CapabilityTestService
from .config_service import ConfigService
from .connector_service import ConnectorService
from .disclosure_service import DisclosureService
from .errors import Slice1Error, as_detail, error
from .models import ADMIN_KINDS


class ConfigCreateIn(BaseModel):
    kind: str
    protocol: str
    endpoint: str = Field(min_length=1, max_length=500)
    model_name: str = Field(default="", max_length=200)
    credentials: dict[str, Any] = Field(default_factory=dict)
    owner_scope: str = "user"


class ConfigTestIn(BaseModel):
    """能力测试请求：只允许探测能力集合，不接受用户自由文本。"""

    capabilities: Optional[list[str]] = None


class ConfigUpdateIn(BaseModel):
    endpoint: Optional[str] = Field(default=None, max_length=500)
    model_name: Optional[str] = Field(default=None, max_length=200)
    credentials: Optional[dict[str, Any]] = None


class DisclosureCreateIn(BaseModel):
    task_id: str = Field(min_length=1, max_length=64)
    config_id: str = Field(min_length=1, max_length=64)
    content_category: str
    scope_snapshot: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: int = Field(default=86400, ge=1, le=7 * 24 * 3600)


class PairingIn(BaseModel):
    device_name: str = Field(default="", max_length=120)


class BindIn(BaseModel):
    pairing_code: str = Field(min_length=1)
    device_name: str = Field(default="", max_length=120)


class EnqueueRequestIn(BaseModel):
    binding_id: str
    task_id: str
    call_type: str = "model.generate"
    payload: Optional[dict[str, Any]] = None
    ttl_seconds: int = Field(default=120, ge=1, le=600)


class AcceptRequestIn(BaseModel):
    binding_id: str
    task_id: str
    nonce: str
    owner_user_id: Optional[str] = None


class CompleteRequestIn(BaseModel):
    result_ok: bool = True


class RevokeIn(BaseModel):
    reason: str = ""


def create_slice1_router(
    *,
    configs: ConfigService,
    capabilities: CapabilityTestService,
    disclosures: DisclosureService,
    connectors: ConnectorService,
    resolve_user: Callable[[Request], Any],
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["slice1"])

    def _user(request: Request):
        return resolve_user(request)

    @router.get("/capabilities/configs")
    def list_configs(request: Request, kind: Optional[str] = None) -> dict:
        user = _user(request)
        return {"data": configs.list(actor_id=user.user_id, is_admin=bool(user.is_admin), kind=kind)}

    @router.post("/capabilities/configs", status_code=201)
    def create_config(body: ConfigCreateIn, request: Request) -> dict:
        user = _user(request)
        if not isinstance(body.credentials, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in body.credentials.items()
        ):
            raise error("credential_invalid", "credentials must be a flat string map")
        if len(body.credentials) > 20:
            raise error("credential_invalid", "too many credential fields")
        if not user.is_admin and body.kind in ADMIN_KINDS:
            raise error("admin_only")
        return configs.create(
            actor_id=user.user_id,
            is_admin=bool(user.is_admin),
            kind=body.kind,
            protocol=body.protocol,
            endpoint=body.endpoint,
            model_name=body.model_name,
            credentials=body.credentials,
            owner_scope=body.owner_scope,
        )

    @router.get("/capabilities/configs/{config_id}")
    def get_config(config_id: str, request: Request) -> dict:
        user = _user(request)
        return configs.get(config_id, actor_id=user.user_id, is_admin=bool(user.is_admin))

    @router.patch("/capabilities/configs/{config_id}")
    def update_config(config_id: str, body: ConfigUpdateIn, request: Request) -> dict:
        user = _user(request)
        if body.credentials is not None and (
            not isinstance(body.credentials, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in body.credentials.items())
        ):
            raise error("credential_invalid", "credentials must be a flat string map")
        return configs.update(
            config_id,
            actor_id=user.user_id,
            is_admin=bool(user.is_admin),
            endpoint=body.endpoint,
            model_name=body.model_name,
            credentials=body.credentials,
        )

    @router.delete("/capabilities/configs/{config_id}")
    def deactivate_config(config_id: str, request: Request) -> dict:
        user = _user(request)
        return configs.deactivate(config_id, actor_id=user.user_id, is_admin=bool(user.is_admin))

    @router.post("/capabilities/configs/{config_id}/test")
    def test_config(config_id: str, request: Request, body: Optional[ConfigTestIn] = None) -> dict:
        user = _user(request)
        return capabilities.run(
            config_id,
            actor_id=user.user_id,
            is_admin=bool(user.is_admin),
            capabilities=body.capabilities if body else None,
        )

    @router.post("/disclosures", status_code=201)
    def create_disclosure(body: DisclosureCreateIn, request: Request) -> dict:
        user = _user(request)
        return disclosures.create(
            actor_id=user.user_id,
            task_id=body.task_id,
            config_id=body.config_id,
            content_category=body.content_category,
            scope_snapshot=body.scope_snapshot,
            ttl_seconds=body.ttl_seconds,
        )

    @router.get("/disclosures")
    def list_disclosures(request: Request, task_id: str) -> dict:
        user = _user(request)
        return {"data": disclosures.list_for_task(task_id, actor_id=user.user_id, is_admin=bool(user.is_admin))}

    @router.post("/disclosures/{grant_id}/revoke")
    def revoke_disclosure(grant_id: str, request: Request) -> dict:
        user = _user(request)
        return disclosures.revoke(grant_id, actor_id=user.user_id, is_admin=bool(user.is_admin))

    # ---- 连接器 ----
    @router.post("/connectors/pairing", status_code=201)
    def create_pairing(body: PairingIn, request: Request) -> dict:
        user = _user(request)
        return connectors.create_pairing(actor_id=user.user_id, device_name=body.device_name)

    @router.post("/connectors/bind", status_code=201)
    def bind(body: BindIn) -> dict:
        return connectors.bind(pairing_code=body.pairing_code, device_name=body.device_name)

    @router.get("/connectors")
    def list_connectors(request: Request) -> dict:
        user = _user(request)
        return {"data": connectors.list_bindings(actor_id=user.user_id, is_admin=bool(user.is_admin))}

    @router.post("/connectors/{binding_id}/revoke")
    def revoke_connector(binding_id: str, request: Request) -> dict:
        user = _user(request)
        return connectors.revoke(binding_id, actor_id=user.user_id, is_admin=bool(user.is_admin))

    @router.post("/connectors/requests", status_code=201)
    def enqueue_request(body: EnqueueRequestIn, request: Request) -> dict:
        _user(request)
        return connectors.enqueue_request(
            binding_id=body.binding_id,
            task_id=body.task_id,
            call_type=body.call_type,
            payload=body.payload,
            ttl_seconds=body.ttl_seconds,
        )

    @router.post("/connectors/requests/accept")
    def accept_request(
        body: AcceptRequestIn,
        x_connector_token: str = Header(default=""),
    ) -> dict:
        if not x_connector_token:
            raise HTTPException(status_code=401, detail={"error_code": "SESSION_INVALID", "message": "connector token required"})
        binding = connectors.resolve_binding(x_connector_token)
        owner = body.owner_user_id or binding["owner_user_id"]
        return connectors.accept_request(
            binding_id=body.binding_id,
            task_id=body.task_id,
            nonce=body.nonce,
            owner_user_id=owner,
        )

    @router.post("/connectors/requests/{request_id}/complete")
    def complete_request(request_id: str, body: CompleteRequestIn, request: Request) -> dict:
        _user(request)
        return connectors.complete_request(request_id, result_ok=body.result_ok)

    return router


def install_slice1_error_handlers(app, error_cls=Slice1Error) -> None:
    from fastapi.responses import JSONResponse

    @app.exception_handler(error_cls)
    async def _slice1_error(_: Request, exc) -> JSONResponse:  # noqa: ANN001
        return JSONResponse(status_code=getattr(exc, "status", 400), content=as_detail(exc))
