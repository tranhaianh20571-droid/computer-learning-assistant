"""适配器工厂与能力探测服务（切片 1 T03/T04）。

`build_adapter` 根据配置协议构造对应适配器；
`CapabilityTestService` 用非私人样例探测能力并写回 `capability_status`。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from ..config_service import ConfigService
from ..models import ANYSEARCH_ENDPOINT
from ...logging_audit.audit import AuditWriteError
from ..errors import Slice1Error, error
from .anthropic import AnthropicAdapter
from .imagegen import ImageGenerationAdapter
from .base import AdapterBase, validate_endpoint, validate_model_endpoint
from .gemini import GeminiAdapter
from .minimax import MiniMaxTTSAdapter
from .openai import OpenAIAdapter
from .responses import ResponsesAdapter
from .paddleocr import PaddleOCRAdapter
from .anysearch import AnySearchAdapter
from .somark import SomarkAdapter
from .paddle_vl import PaddleVLAdapter
from .video import MiniMaxVideoAdapter, SeedanceVideoAdapter

ADAPTERS = {
    "openai_responses": ResponsesAdapter,
    "paddleocr_vl": PaddleVLAdapter,
    "somark": SomarkAdapter,
    "minimax_video": MiniMaxVideoAdapter,
    "seedance": SeedanceVideoAdapter,
    "openai_image": ImageGenerationAdapter,
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
    "gemini": GeminiAdapter,
    "minimax": MiniMaxTTSAdapter,
    "paddleocr": PaddleOCRAdapter,
    "anysearch": AnySearchAdapter,
}

# 允许内网/回环端点的协议（本机连接器与本地部署 TTS/OCR 网关）
ALLOW_PRIVATE_PROTOCOLS = frozenset({"minimax", "paddleocr"})


def build_adapter(
    protocol: str,
    *,
    endpoint: str,
    model_name: str = "",
    credentials: Optional[dict] = None,
    client: Any = None,
    allow_private: Optional[bool] = None,
    **extra: Any,
) -> AdapterBase:
    if protocol not in ADAPTERS:
        raise error("protocol_mismatch", f"unknown protocol {protocol}")
    if protocol in ("somark", "paddleocr_vl"):
        from ..models import DOCUMENT_DESTINATIONS
        if (endpoint, model_name) != DOCUMENT_DESTINATIONS[protocol]:
            raise error("invalid_field", "invalid_document_parser_destination")
    if protocol == "anysearch":
        if endpoint != ANYSEARCH_ENDPOINT or model_name != "" or allow_private is True:
            raise error("invalid_field", "invalid_search_destination")
        allow_private = False
    if allow_private is None:
        allow_private = protocol in ALLOW_PRIVATE_PROTOCOLS
    try:
        if protocol in ("openai", "openai_responses", "anthropic", "gemini"):
            validate_model_endpoint(endpoint)
        else:
            validate_endpoint(endpoint, require_https=False, allow_private=allow_private)
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", "invalid_field")
        raise error("invalid_field", f"endpoint rejected: {code}") from exc
    cls = ADAPTERS[protocol]
    return cls(
        endpoint=endpoint,
        model_name=model_name,
        credentials=credentials or {},
        client=client,
        **extra,
    )


class CapabilityTestService:
    """运行能力探测并把结果写回配置；只发非私人样例。"""

    def __init__(
        self,
        configs: ConfigService,
        *,
        adapter_factory: Callable[..., AdapterBase] = build_adapter,
    ) -> None:
        self.configs = configs
        self.adapter_factory = adapter_factory

    def run(
        self,
        config_id: str,
        *,
        actor_id: str,
        is_admin: bool,
        capabilities: Optional[list[str]] = None,
    ) -> dict:
        view = self.configs.get(config_id, actor_id=actor_id, is_admin=is_admin)
        if view["kind"] == "document_parser":
            self.configs.audit.append("config.changed", actor_id=actor_id, object_type="service_config",
                object_id=config_id, result="probe_started")
        if view["kind"] == "search":
            return self._run_search(view, actor_id=actor_id, is_admin=is_admin, capabilities=capabilities)
        credentials = (self.configs.resolve_document_credentials(config_id,
            owner_user_id=actor_id, expected_version=view["config_version"])
            if view["kind"] == "document_parser" else self.configs.resolve_credentials(config_id))
        extra: dict[str, Any] = {}
        if view["kind"] == "content_model":
            extra["reasoning_settings"] = view["reasoning_settings"]
        if view["kind"] == "tts":
            extra["voice_id"] = credentials.get("voice_id", "")
        adapter = self.adapter_factory(
            view["protocol"],
            endpoint=view["endpoint"],
            model_name=view["model_name"],
            credentials=credentials,
            **extra,
        )
        try:
            if capabilities:
                # 只允许探测已声明的能力子集；未知能力一律不可用。
                status = {cap: adapter.probe(cap).as_dict() for cap in capabilities}
            else:
                status = adapter.probe_all()
        except Exception as exc:  # noqa: BLE001 - 供应商异常不外泄
            if view["kind"] == "document_parser":
                self.configs.audit.append("config.changed", actor_id=actor_id, object_type="service_config",
                    object_id=config_id, result="probe_failed")
            raise error("capability_test_failed", "probe_failed") from None
        finally:
            try:
                adapter.close()
            except Exception:  # noqa: BLE001
                pass
        # 探测结果与既有状态合并，避免部分探测丢掉其它能力状态。
        merged = dict(view["capability_status"])
        merged.update(status)
        from ..capability_refresh import stamp_status
        updated = self.configs.set_capability_status(config_id,
            stamp_status(status, view["config_version"], view["capability_status"]),
            expected_version=view["config_version"], merge=True)
        if view["kind"] == "document_parser":
            self.configs.audit.append("config.changed", actor_id=actor_id, object_type="service_config",
                object_id=config_id, result="probe_completed" if all(item["state"] == "available" for item in status.values()) else "probe_failed")
        return {"config_id": config_id, "capability_status": updated["capability_status"]}


    def _run_search(self, view: dict, *, actor_id: str, is_admin: bool,
                    capabilities: Optional[list[str]]) -> dict:
        if capabilities is not None and capabilities != ["text"]:
            raise Slice1Error("invalid_field", "invalid_search_probe_capabilities", 422)
        config_id, version = view["config_id"], view["config_version"]
        self.configs.reserve_search_probe(config_id, actor_id=actor_id,
            is_admin=is_admin, expected_version=version)
        adapter = None
        try:
            def guard():
                self.configs.check_search_config(config_id, owner_user_id=actor_id, expected_version=version)
            credentials = self.configs.resolve_search_credentials(config_id,
                owner_user_id=actor_id, expected_version=version)
            adapter = self.adapter_factory("anysearch", endpoint=ANYSEARCH_ENDPOINT,
                model_name="", credentials=credentials, probe_guard=guard)
            guard()
            raw = adapter.probe("text").as_dict()
            # Probe adapters cannot return provider text/unknown detail fields to the API.
            allowed_details = {"", "unsupported", "not_applicable", "credential_invalid", "timeout",
                "connection_error", "rate_limited", "error", "capability_test_failed",
                "SEARCH_TIMEOUT", "SEARCH_CONNECTION_ERROR", "SEARCH_RATE_LIMITED",
                "SEARCH_CREDENTIAL_INVALID", "SEARCH_QUOTA_EXCEEDED", "SEARCH_PROVIDER_UNAVAILABLE",
                "SEARCH_OUTPUT_INVALID", "SEARCH_RESPONSE_TOO_LARGE", "SEARCH_REQUEST_REJECTED"}
            state = raw.get("state") if raw.get("state") in ("available", "unavailable", "unknown") else "unavailable"
            detail = raw.get("detail", "")
            if not isinstance(detail, str) or detail not in allowed_details:
                state, detail = "unavailable", "capability_test_failed"
            status = {"text": {"state": state, "detail": detail}}
        except AuditWriteError:
            raise
        except Slice1Error:
            self.configs.record_search_probe_failure(config_id, actor_id=actor_id, is_admin=is_admin)
            raise
        except Exception:
            self.configs.record_search_probe_failure(config_id, actor_id=actor_id, is_admin=is_admin)
            raise error("capability_test_failed", "search_probe_failed") from None
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:
                    pass
        from ..capability_refresh import stamp_status
        stamped = stamp_status(status, version)
        if stamped["text"]["state"] != "available":
            from datetime import datetime, timedelta, timezone
            stamped["text"]["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
        updated = self.configs.set_capability_status(config_id,
            stamped,
            expected_version=version, merge=True, actor_id=actor_id, is_admin=is_admin)
        return {"config_id": config_id, "config_version": updated["config_version"],
                "capability_status": updated["capability_status"]}


def require_capability(status: dict, capability: str) -> None:
    """任务调度前检查能力；不支持时抛受控错误码，避免无限重试。"""
    state = (status or {}).get(capability)
    if isinstance(state, dict):
        state = state.get("state")
    if state != "available":
        raise Slice1Error(
            "capability_unavailable",
            f"model_capability_unavailable: {capability}",
            409,
        )
