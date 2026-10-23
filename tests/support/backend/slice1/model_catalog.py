"""Bounded authenticated model discovery; never expose provider bodies."""
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .adapters.base import validate_model_endpoint
from .errors import AdapterError, error


def canonical_endpoint(endpoint: str, protocol="openai"):
    url = urlsplit(endpoint.strip().rstrip("/"))
    path = re.sub(r"/(?:chat/completions|responses|embeddings|rerank|models|messages|systemone|images/(?:generations|edits))$", "", url.path)
    if protocol == "jev":
        path = re.sub(r"/v1$", "", path)
    elif protocol == "gemini":
        # /v1 is also accepted as a base-address shorthand by Gemini generation.
        path = re.sub(r"/v1$", "/v1beta", path)
        if not re.search(r"/v\d+(?:beta\d*)?$", path):
            path += "/v1beta"
    elif not re.search(r"/v\d+(?:beta\d*)?$", path):
        path += "/v1"
    return urlunsplit((url.scheme, url.netloc, path, url.query, url.fragment)).rstrip("/")


def normalize_base(endpoint: str, protocol="openai", *, allow_local=False):
    value = (endpoint or "").strip().rstrip("/")
    if any(ord(c) < 32 for c in value):
        raise error("invalid_field", "服务地址无效。")
    try:
        url = urlsplit(value)
        url.port
    except ValueError:
        raise error("invalid_field", "服务地址无效。") from None
    if not url.hostname or url.username or url.password or url.query or url.fragment:
        raise error("invalid_field", "请填写无凭据、参数的基础地址。")
    local = url.hostname in ("localhost", "127.0.0.1", "::1")
    try:
        local = local or ipaddress.ip_address(url.hostname).is_loopback
    except ValueError:
        pass
    content_model = protocol in ("openai", "openai_responses", "anthropic", "gemini")
    if content_model or not (allow_local and local and url.scheme == "http"):
        try:
            validate_model_endpoint(value, allow_http=content_model)
        except AdapterError:
            raise error("invalid_field", "服务地址不符合该协议的要求。") from None
    return canonical_endpoint(value, protocol)


class ModelsIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    endpoint: str = Field(default="", max_length=500)
    protocol: str = Field(default="openai", max_length=40)
    api_key: SecretStr | None = Field(default=None, repr=False)
    config_id: str | None = Field(default=None, max_length=64)


def discover_models(endpoint, protocol, key, *, allow_local=False):
    if protocol not in ("openai", "openai_responses", "openai_image", "embedding", "rerank", "jev", "anthropic", "gemini", "seedance"):
        raise error("invalid_field", "该协议暂不提供模型目录。")
    base = normalize_base(endpoint, protocol, allow_local=allow_local)
    catalog_base = base + "/v1" if protocol == "jev" else base
    headers = {"Accept": "application/json"}
    if protocol == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    elif protocol == "gemini":
        headers["x-goog-api-key"] = key
    else:
        headers["Authorization"] = "Bearer " + key
    try:
        with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", catalog_base + "/models", headers=headers) as response:
                if response.status_code != 200:
                    raise error("capability_test_failed", "未能获取模型目录，可手动填写模型名。")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 1024 * 1024:
                        raise ValueError("catalog_limit")
        import json
        payload = json.loads(body)
        rows = payload.get("data", payload.get("models", []))
        result = set()
        for row in rows[:2000]:
            name = row.get("id", row.get("name", "")) if isinstance(row, dict) else ""
            if protocol == "gemini":
                name = name.removeprefix("models/")
            if isinstance(name, str) and 0 < len(name) <= 200 and name.isprintable():
                result.add(name)
        return {"models": sorted(result)[:500]}
    except Exception:
        raise error("capability_test_failed", "获取模型目录失败，可手动填写模型名。") from None
