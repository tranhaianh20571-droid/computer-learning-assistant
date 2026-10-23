"""切片 1 API 路由：能力配置、外发确认、连接器配对与请求围栏。

统一会话认证与所有权校验；普通用户不可读写管理员 OCR 配置；搜索配置只能由本人管理。
"""

from __future__ import annotations

from typing import Any, Callable, Literal, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

from ..logging_audit.models import Base  # noqa: F401 - ensure metadata import order
from .adapters import CapabilityTestService
from .config_service import ConfigService, SearchConfigCreateIn, SearchConfigUpdateIn
from .connector_service import ConnectorService
from .disclosure_service import DisclosureService
from .errors import Slice1Error, as_detail, error
from .models import ADMIN_KINDS
from .reasoning import (ReasoningSettings, ReasoningDescribeIn, ReasoningTestIn,
                        describe_reasoning, probe_saved_reasoning)
from .model_catalog import ModelsIn, discover_models
from .reasoning_discovery import ReasoningDiscoverIn, ReasoningDiscovery


class ConfigCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    kind: str
    protocol: str
    endpoint: str = Field(min_length=1, max_length=500)
    model_name: str = Field(default="", max_length=200)
    credentials: dict[str, Any] = Field(default_factory=dict)
    owner_scope: str = "user"
    reasoning_settings: ReasoningSettings | None = None

    @field_validator("reasoning_settings")
    @classmethod
    def reasoning_not_null(cls, value):
        if value is None:
            raise ValueError("invalid_reasoning_settings")
        return value


class ConfigTestIn(BaseModel):
    """能力测试请求：只允许探测能力集合，不接受用户自由文本。"""

    capabilities: Optional[list[str]] = None


class RoleBindingIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    config_id: str | None = Field(default=None, max_length=64)
    expected_revision: int = Field(ge=0)


class RoleConfigurationIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_revision: int = Field(ge=0)
    config_id: str | None = Field(default=None, max_length=64)
    config_version: int | None = Field(default=None, ge=1)
    protocol: Literal["openai", "openai_responses", "anthropic", "gemini"]
    endpoint: str = Field(min_length=1, max_length=500)
    model_name: str = Field(min_length=1, max_length=200)
    api_key: SecretStr | None = None
    reasoning_settings: ReasoningSettings = Field(default_factory=ReasoningSettings)


class SearchConfigTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    capabilities: list[Literal["text"]] = Field(default_factory=lambda: ["text"], min_length=1, max_length=1)


class TtsVoicesIn(BaseModel):
    endpoint: str = Field(min_length=1, max_length=500)
    api_key: SecretStr


class ConfigUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    endpoint: Optional[str] = Field(default=None, max_length=500)
    model_name: Optional[str] = Field(default=None, max_length=200)
    credentials: Optional[dict[str, Any]] = None
    reasoning_settings: ReasoningSettings | None = None

    @field_validator("reasoning_settings")
    @classmethod
    def reasoning_not_null(cls, value):
        if value is None:
            raise ValueError("invalid_reasoning_settings")
        return value


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
    reasoning_discovery = ReasoningDiscovery()

    def _user(request: Request):
        return resolve_user(request)

    async def _config_payload(request: Request, *, allow_empty=False) -> dict:
        raw = await request.body()
        if allow_empty and not raw:
            return {}
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, detail={"error_code": "invalid_field", "message": "invalid_config_payload"}) from None
        if not isinstance(payload, dict):
            raise HTTPException(422, detail={"error_code": "invalid_field", "message": "invalid_config_payload"})
        return payload

    def _validate_config(model, payload):
        try:
            return model.model_validate(payload)
        except ValidationError:
            # Never return ValidationError.errors/input/ctx: they can contain a Key.
            raise HTTPException(422, detail={"error_code": "invalid_field", "message": "invalid_config_fields"}) from None

    @router.get("/capabilities/configs")
    def list_configs(request: Request, kind: Optional[str] = None) -> dict:
        user = _user(request)
        return {"data": configs.list(actor_id=user.user_id, is_admin=bool(user.is_admin), kind=kind)}

    @router.get("/capabilities/model-roles")
    def model_roles(request: Request):
        from ..agents.model_routing import role_views
        return {"data": role_views(configs, _user(request).user_id)}

    @router.put("/capabilities/model-roles/{role}")
    def update_model_role(role: str, body: RoleBindingIn, request: Request):
        from ..agents.model_routing import set_role
        return {"data": set_role(configs, _user(request).user_id, role,
                                body.config_id, body.expected_revision)}

    @router.put("/capabilities/model-roles/{role}/configuration")
    async def save_role_config(role: str, request: Request):
        from ..agents.model_routing import save_role_configuration
        user = _user(request)
        body = _validate_config(RoleConfigurationIn, await _config_payload(request))
        return {"data": await run_in_threadpool(save_role_configuration, configs, user.user_id, role, body)}

    @router.post("/capabilities/configs/{config_id}/credential/reveal")
    def reveal_config_credential(config_id: str, request: Request):
        from ..agents.model_routing import reveal_content_key
        key = reveal_content_key(configs, _user(request).user_id, config_id)
        return JSONResponse({"api_key": key}, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @router.post("/capabilities/models")
    def model_catalog(body: ModelsIn, request: Request):
        user = _user(request)
        endpoint, protocol = body.endpoint, body.protocol
        key = body.api_key.get_secret_value().strip() if body.api_key else ""
        if body.config_id:
            view, credentials = configs.catalog_snapshot(body.config_id, actor_id=user.user_id, is_admin=bool(user.is_admin))
            endpoint, protocol = endpoint or view["endpoint"], view["protocol"]
            if not key:
                if endpoint != view["endpoint"]:
                    raise error("credential_invalid", "地址已变化，请重新填写 API Key。")
                key = credentials.get("api_key", "")
        if not key:
            raise error("credential_invalid", "请先填写 API Key。")
        try:
            result = discover_models(endpoint, protocol, key, allow_local=bool(user.is_admin))
        except Exception:
            configs.audit.append("config.changed", actor_id=user.user_id, object_type="model_catalog",
                object_id="discovery", result="probe_failed")
            raise
        configs.audit.append("config.changed", actor_id=user.user_id, object_type="model_catalog",
            object_id="discovery", result="probe_completed")
        return result

    @router.post("/capabilities/tts/voices")
    def tts_voices(body: TtsVoicesIn, request: Request) -> dict:
        _user(request)
        from .voice_catalog import list_tts_voices

        return list_tts_voices(endpoint=body.endpoint, api_key=body.api_key.get_secret_value())

    @router.post("/capabilities/configs", status_code=201)
    async def create_config(request: Request) -> dict:
        user = _user(request)
        payload = await _config_payload(request)
        if payload.get("kind") == "search" or payload.get("protocol") == "anysearch":
            body = _validate_config(SearchConfigCreateIn, payload)
            credentials = {"api_key": body.credentials.api_key.get_secret_value()}
        else:
            body = _validate_config(ConfigCreateIn, payload)
            credentials = body.credentials
        if not isinstance(credentials, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in credentials.items()
        ):
            raise error("credential_invalid", "credentials must be a flat string map")
        if len(credentials) > 20:
            raise error("credential_invalid", "too many credential fields")
        if not user.is_admin and body.kind in ADMIN_KINDS:
            raise error("admin_only")
        return await run_in_threadpool(configs.create,
            actor_id=user.user_id, is_admin=bool(user.is_admin), kind=body.kind,
            protocol=body.protocol, endpoint=body.endpoint, model_name=body.model_name,
            credentials=credentials, owner_scope=body.owner_scope,
            **({"reasoning_settings": body.reasoning_settings.model_dump(exclude_none=True)}
               if getattr(body, "reasoning_settings", None) is not None else {}))

    @router.get("/capabilities/configs/{config_id}")
    def get_config(config_id: str, request: Request) -> dict:
        user = _user(request)
        return configs.get(config_id, actor_id=user.user_id, is_admin=bool(user.is_admin))

    @router.patch("/capabilities/configs/{config_id}")
    async def update_config(config_id: str, request: Request) -> dict:
        user = _user(request)
        view = await run_in_threadpool(configs.get, config_id, actor_id=user.user_id, is_admin=bool(user.is_admin))
        payload = await _config_payload(request)
        body = _validate_config(SearchConfigUpdateIn if view["kind"] == "search" else ConfigUpdateIn, payload)
        values = body.model_dump(exclude_unset=True)
        if getattr(body, "reasoning_settings", None) is not None:
            values["reasoning_settings"] = body.reasoning_settings.model_dump(exclude_none=True)
        if view["kind"] == "search" and body.credentials is not None:
            values["credentials"] = {"api_key": body.credentials.api_key.get_secret_value()}
        elif body.credentials is not None and not all(
            isinstance(k, str) and isinstance(v, str) for k, v in body.credentials.items()
        ):
            raise error("credential_invalid", "credentials must be a flat string map")
        return await run_in_threadpool(configs.update, config_id,
            actor_id=user.user_id, is_admin=bool(user.is_admin), **values)

    @router.delete("/capabilities/configs/{config_id}")
    def deactivate_config(config_id: str, request: Request) -> dict:
        user = _user(request)
        return configs.deactivate(config_id, actor_id=user.user_id, is_admin=bool(user.is_admin))

    @router.post("/capabilities/configs/{config_id}/test")
    async def test_config(config_id: str, request: Request) -> dict:
        user = _user(request)
        view = await run_in_threadpool(configs.get, config_id, actor_id=user.user_id, is_admin=bool(user.is_admin))
        payload = await _config_payload(request, allow_empty=True)
        body = _validate_config(SearchConfigTestIn if view["kind"] == "search" else ConfigTestIn, payload)
        return await run_in_threadpool(capabilities.run, config_id,
            actor_id=user.user_id, is_admin=bool(user.is_admin), capabilities=body.capabilities)

    @router.post("/capabilities/reasoning/describe")
    async def describe_reasoning_config(request: Request) -> dict:
        _user(request)
        body = _validate_config(ReasoningDescribeIn, await _config_payload(request))
        return describe_reasoning(body.protocol, body.endpoint, body.model_name)

    @router.post("/capabilities/reasoning/discover")
    async def discover_reasoning_config(request: Request) -> dict:
        from .model_catalog import canonical_endpoint
        user = _user(request)
        body = _validate_config(ReasoningDiscoverIn, await _config_payload(request))
        def audit(result):
            configs.audit.append("config.changed", actor_id=user.user_id, object_type="reasoning_discovery",
                                 object_id="discovery", result=result)
        key = body.api_key.get_secret_value().strip() if body.api_key else ""
        check = lambda: None
        try:
            if body.config_id:
                view = configs.get(body.config_id, actor_id=user.user_id, is_admin=False)
                version = body.config_version or view["config_version"]
                configs.check_reasoning_probe(body.config_id, actor_id=user.user_id, expected_version=version)
                if not key:
                    if (body.protocol != view["protocol"] or
                            canonical_endpoint(body.endpoint, body.protocol) != canonical_endpoint(view["endpoint"], view["protocol"])):
                        raise error("credential_invalid", "地址或协议已变化，请重新填写 API Key。")
                    key = configs.resolve_reasoning_credentials(body.config_id, actor_id=user.user_id,
                                                                expected_version=version).get("api_key", "")
                    check = lambda: configs.check_reasoning_probe(body.config_id, actor_id=user.user_id, expected_version=version)
            if not key:
                raise error("credential_invalid", "请先填写 API Key。")
        except Exception:
            audit("probe_failed")
            raise
        result = await reasoning_discovery.detect(actor_id=user.user_id, protocol=body.protocol,
            endpoint=body.endpoint, model=body.model_name, key=key, check=check, audit=audit,
            refresh=body.refresh)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post("/capabilities/configs/{config_id}/reasoning/test")
    async def test_reasoning_config(config_id: str, request: Request) -> dict:
        user = _user(request)
        body = _validate_config(ReasoningTestIn, await _config_payload(request))
        return await probe_saved_reasoning(configs, config_id, actor_id=user.user_id,
            is_admin=bool(user.is_admin), config_version=body.config_version)

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
