"""适配器工厂与能力探测服务（切片 1 T03/T04）。

`build_adapter` 根据配置协议构造对应适配器；
`CapabilityTestService` 用非私人样例探测能力并写回 `capability_status`。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from ..config_service import ConfigService
from ..errors import Slice1Error, error
from .anthropic import AnthropicAdapter
from .base import AdapterBase, validate_endpoint
from .gemini import GeminiAdapter
from .minimax import MiniMaxTTSAdapter
from .openai import OpenAIAdapter
from .paddleocr import PaddleOCRAdapter
from .tavily_hikari import TavilyHikariAdapter

ADAPTERS = {
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
    "gemini": GeminiAdapter,
    "minimax": MiniMaxTTSAdapter,
    "paddleocr": PaddleOCRAdapter,
    "tavily_hikari": TavilyHikariAdapter,
}

# 允许内网/回环端点的协议（本机连接器与本地部署 TTS/OCR 网关）
ALLOW_PRIVATE_PROTOCOLS = frozenset({"minimax", "paddleocr", "tavily_hikari"})


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
    if allow_private is None:
        allow_private = protocol in ALLOW_PRIVATE_PROTOCOLS
    try:
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
        credentials = self.configs.resolve_credentials(config_id)
        extra: dict[str, Any] = {}
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
            raise error("capability_test_failed", type(exc).__name__) from exc
        finally:
            try:
                adapter.close()
            except Exception:  # noqa: BLE001
                pass
        # 探测结果与既有状态合并，避免部分探测丢掉其它能力状态。
        merged = dict(view["capability_status"])
        merged.update(status)
        updated = self.configs.set_capability_status(config_id, merged)
        return {"config_id": config_id, "capability_status": updated["capability_status"]}


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
