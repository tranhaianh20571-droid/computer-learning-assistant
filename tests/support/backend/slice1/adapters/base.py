"""适配器基类：统一错误类型、超时、响应大小、凭据脱敏与能力探测契约。

- 能力探测只发固定非私人样例（`PROBE_TEXT` / 1x1 像素样例）。
- 供应商原始异常映射到受控错误码，不写入日志、不返回给用户。
- 公网模型地址要求 HTTPS，拒绝回环/私网/链路本地/云元数据地址。
"""

from __future__ import annotations

import base64
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from ..errors import AdapterError

PROBE_TEXT = "你好，请回复 OK"
PROBE_IMAGE_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 90.0

CAPABILITIES = ("text", "image", "tool_call", "json_schema", "streaming", "cancel")

# 云元数据地址（必须拒绝）
_METADATA_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.google.internal.",
}

# 探测时被拒绝能力对应的供应商信号
_UNSUPPORTED_MARKERS = (
    "unsupported",
    "not support",
    "not_supported",
    "invalid_request",
    "does not support",
)


@dataclass
class ProbeResult:
    capability: str
    state: str  # available | unavailable
    detail: str = ""

    def as_dict(self) -> dict:
        return {"state": self.state, "detail": self.detail}


def _is_private_or_loopback(host: str) -> bool:
    if host in _METADATA_HOSTS:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
            return True
    return False


def validate_endpoint(endpoint: str, *, require_https: bool = True, allow_private: bool = False) -> None:
    """校验配置端点；非法地址抛受控错误码。"""
    parsed = urlparse(endpoint)
    if parsed.scheme not in ("http", "https"):
        raise AdapterError("invalid_endpoint", status=400)
    if not parsed.hostname:
        raise AdapterError("invalid_endpoint", status=400)
    if require_https and parsed.scheme != "https":
        raise AdapterError("invalid_endpoint", status=400, detail="https_required")
    if parsed.hostname in _METADATA_HOSTS:
        raise AdapterError("invalid_endpoint", status=400, detail="metadata_address")
    if not allow_private and _is_private_or_loopback(parsed.hostname):
        raise AdapterError("invalid_endpoint", status=400, detail="private_address")


def _looks_unsupported(payload: Any) -> bool:
    text = str(payload).lower()
    return any(marker in text for marker in _UNSUPPORTED_MARKERS)


class AdapterBase:
    """所有外部适配器的公共行为。"""

    kind = "content_model"
    protocol = "base"
    capabilities: tuple[str, ...] = CAPABILITIES
    default_timeout = DEFAULT_TIMEOUT

    def __init__(
        self,
        *,
        endpoint: str,
        model_name: str = "",
        credentials: Optional[dict] = None,
        client: Optional[httpx.Client] = None,
        timeout: Optional[float] = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model_name = model_name
        self.credentials = credentials or {}
        self.timeout = timeout if timeout is not None else self.default_timeout
        self.max_response_bytes = max_response_bytes
        self._client = client
        self._owns_client = client is None

    # ---- HTTP ----
    @property
    def headers(self) -> dict[str, str]:
        return {}

    def _client_or_new(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(timeout=self.timeout, follow_redirects=False)

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> tuple[int, Any, dict]:
        client = self._client_or_new()
        try:
            response = client.request(
                method,
                url,
                json=json_body,
                params=params,
                headers={**self.headers, "Accept": "application/json"},
            )
        except httpx.TimeoutException as exc:
            raise AdapterError("timeout", status=504) from exc
        except httpx.HTTPError as exc:
            raise AdapterError("connection_error", status=502) from exc
        content = response.content
        if len(content) > self.max_response_bytes:
            raise AdapterError("response_too_large", status=502)
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": content[:512].decode("utf-8", errors="replace")}
        return response.status_code, payload, dict(response.headers)

    def _map_status(self, status: int, payload: Any) -> None:
        if 200 <= status < 300:
            return
        if status in (401, 403):
            raise AdapterError("credential_invalid", status=401)
        if status == 429:
            raise AdapterError("rate_limited", status=429)
        if status >= 500:
            raise AdapterError("capability_test_failed", status=502)
        if _looks_unsupported(payload):
            raise AdapterError("unsupported_capability", status=400)
        raise AdapterError("capability_test_failed", status=400)

    # ---- 能力探测 ----
    def _probe(self, capability: str) -> None:
        """子类实现：成功返回 None，不支持抛 AdapterError('unsupported_capability')。"""
        raise AdapterError("unsupported_capability", status=400)

    def probe(self, capability: str) -> ProbeResult:
        if capability not in self.capabilities:
            return ProbeResult(capability, "unavailable", "not_applicable")
        try:
            self._probe(capability)
            return ProbeResult(capability, "available")
        except AdapterError as exc:
            if exc.code in ("unsupported_capability",):
                return ProbeResult(capability, "unavailable", "unsupported")
            if exc.code in ("credential_invalid",):
                return ProbeResult(capability, "unavailable", "credential_invalid")
            return ProbeResult(capability, "unavailable", exc.code)
        except Exception:  # noqa: BLE001 - 供应商异常不外泄
            return ProbeResult(capability, "unavailable", "error")

    def probe_all(self) -> dict[str, dict]:
        return {cap: self.probe(cap).as_dict() for cap in self.capabilities}

    def test_connection(self) -> ProbeResult:
        return self.probe("text")

    # ---- 切片 3 使用的调用骨架 ----
    def generate(self, messages: list[dict], **options: Any) -> dict:
        raise AdapterError("not_implemented", status=501)

    def stream(self, messages: list[dict], **options: Any) -> Any:
        raise AdapterError("not_implemented", status=501)

    def cancel(self, request_id: str) -> bool:
        return False


def image_data_url() -> str:
    return f"data:image/png;base64,{PROBE_IMAGE_BASE64}"
