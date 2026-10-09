"""Transient, authenticated catalog lookup; no configuration or capability writes."""
from urllib.parse import urlsplit

import httpx

from .adapters import build_adapter
from .errors import AdapterError, Slice1Error, error


def list_tts_voices(*, endpoint: str, api_key: str) -> dict:
    endpoint = endpoint.strip()
    api_key = api_key.strip()
    if not api_key or len(api_key) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in api_key):
        raise error("credential_invalid", "请填写有效的 MiniMax API Key。")
    try:
        address = urlsplit(endpoint)
        if address.username or address.password or address.query or address.fragment:
            raise ValueError("invalid_endpoint")
    except ValueError:
        raise error("invalid_field", "请填写不含凭据或查询参数的服务地址。") from None
    messages = {
        "credential_invalid": "MiniMax 凭据无效或没有音色查询权限。",
        "rate_limited": "音色查询暂时受到限流，请稍后再试。",
        "timeout": "音色查询超时，请稍后再试。",
        "connection_error": "无法连接语音服务，请检查服务地址。",
        "response_too_large": "音色列表超出可处理范围。",
    }
    try:
        with httpx.Client(timeout=20.0, follow_redirects=False, trust_env=False) as client:
            adapter = build_adapter("minimax", endpoint=endpoint, credentials={"api_key": api_key},
                client=client, timeout=20.0, max_response_bytes=1024*1024)
            return {"data": adapter.list_voices()}
    except Slice1Error:
        raise
    except Exception as exc:
        code = exc.code if isinstance(exc, AdapterError) else "capability_test_failed"
        message = messages.get(code, "获取音色失败，请检查服务地址与账号权限。")
        mapped = "credential_invalid" if code == "credential_invalid" else "capability_test_failed"
        raise error(mapped, message) from None
