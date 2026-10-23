"""适配器基类：统一错误类型、超时、响应大小、凭据脱敏与能力探测契约。

- 能力探测只发固定非私人样例（`PROBE_TEXT` / 1x1 像素样例）。
- 供应商原始异常映射到受控错误码，不写入日志、不返回给用户。
- 内容模型允许 HTTP/HTTPS 和非公网目标；媒体等出口保留独立限制。
"""

from __future__ import annotations

import base64
import ipaddress
import socket
from contextlib import contextmanager
from time import monotonic
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from ..errors import AdapterError
from ...net_policy import allowed_address, is_literal_address

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


def is_metadata_address(host: str) -> bool:
    normalized = host.lower().rstrip(".")
    if normalized in _METADATA_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address) == "169.254.169.254"

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
    from_hostname = not is_literal_address(host)
    for info in infos:
        if not allowed_address(info[4][0], from_hostname=from_hostname):
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
    if is_metadata_address(parsed.hostname):
        raise AdapterError("invalid_endpoint", status=400, detail="metadata_address")
    if not allow_private and _is_private_or_loopback(parsed.hostname):
        raise AdapterError("invalid_endpoint", status=400, detail="private_address")


def validate_model_endpoint(endpoint: str, *, allow_http: bool = True) -> None:
    """Content models allow nonpublic destinations, but never metadata services."""
    validate_endpoint(endpoint, require_https=not allow_http, allow_private=True)
    parsed = urlparse(endpoint)
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise AdapterError("invalid_endpoint", status=400)
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, 0, socket.SOCK_STREAM)
    except socket.gaierror:
        return
    if any(is_metadata_address(info[4][0]) for info in infos):
        raise AdapterError("invalid_endpoint", status=400, detail="metadata_address")


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
        reasoning_settings: Optional[dict] = None,
    ) -> None:
        self.reasoning_settings = dict(reasoning_settings or {"mode": "default", "show_summary": True})
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
        self._client = httpx.Client(timeout=self.timeout, follow_redirects=False)
        return self._client

    @contextmanager
    def _http_client(self):
        key = getattr(self, "http_pool_key", None)
        if key and self._client is None:
            from .http_pool import HTTP_CLIENTS
            with HTTP_CLIENTS.borrow(key) as client:
                yield client
        else:
            yield self._client_or_new()

    @contextmanager
    def _stream_request(self, method, url, **kwargs):
        from .request_metrics import observe
        started = monotonic()
        first = None
        failure = None
        try:
            with self._http_client() as client:
                with client.stream(method, url, timeout=self.timeout, **kwargs) as response:
                    if not 200 <= response.status_code < 300:
                        self._map_status(response.status_code, {"raw": response.read()[:256]})
                    def lines():
                        nonlocal first
                        size = 0
                        for line in response.iter_lines():
                            size += len(line.encode("utf8"))
                            if size > self.max_response_bytes:
                                raise AdapterError("response_too_large", status=502)
                            if first is None and line.startswith("data:"):
                                first = max(0, int((monotonic()-started)*1000))
                            yield line
                    yield lines()
        except httpx.TimeoutException as exc:
            failure = AdapterError("timeout", status=504)
            raise failure from exc
        except httpx.HTTPError as exc:
            failure = AdapterError("connection_error", status=502)
            raise failure from exc
        except Exception as exc:
            failure = exc
            raise
        finally:
            observe(self, started, first_chunk_ms=first, exc=failure)

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
        from .request_metrics import observe
        started = monotonic()
        failure = None
        try:
            with self._http_client() as client:
                response = client.request(method, url, json=json_body, params=params,
                    headers={**self.headers, "Accept": "application/json"}, timeout=self.timeout)
            if len(response.content) > self.max_response_bytes:
                raise AdapterError("response_too_large", status=502)
            try:
                payload = response.json()
            except ValueError:
                payload = {"raw": response.content[:512].decode("utf8", errors="replace")}
            # Keep the response contract for callers, but classify HTTP failures here too.
            try:
                self._map_status(response.status_code, payload)
            except AdapterError as exc:
                failure = exc
            return response.status_code, payload, dict(response.headers)
        except httpx.TimeoutException as exc:
            failure = AdapterError("timeout", status=504)
            raise failure from exc
        except httpx.HTTPError as exc:
            failure = AdapterError("connection_error", status=502)
            raise failure from exc
        except Exception as exc:
            failure = exc
            raise
        finally:
            observe(self, started, exc=failure)

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
    def _apply_probe_reasoning(self, payload: dict) -> None:
        from ..reasoning import probe_settings_for

        fields = probe_settings_for(self.protocol, self.endpoint, self.model_name, self.reasoning_settings)
        if "generationConfig" in fields:
            fields["generationConfig"] = {**payload.get("generationConfig", {}), **fields["generationConfig"]}
        payload.update(fields)

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
            return ProbeResult(capability, "unknown", exc.code)
        except Exception:  # noqa: BLE001 - 供应商异常不外泄
            return ProbeResult(capability, "unknown", "error")

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
