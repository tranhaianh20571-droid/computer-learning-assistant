"""MiniMax TTS 适配器（切片 1 T04）。

- `POST /v1/t2a_v2`，非流式 `hex` 音频。
- 固定保存 `model`、`voice_id`、`subtitle_enable=true`、`subtitle_type`、
  音频格式与请求文本哈希。
- 支持自定义端点以适配 `api.minimax.cn` / `api.minimax.io`。
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Any, Optional

from .base import AdapterBase


class MiniMaxTTSAdapter(AdapterBase):
    kind = "tts"
    protocol = "minimax"
    capabilities = ("text",)
    default_timeout = 60.0

    def __init__(
        self,
        *,
        endpoint: str,
        model_name: str = "speech-2.8-hd",
        credentials: Optional[dict] = None,
        voice_id: str = "",
        subtitle_type: str = "sentence",
        audio_format: str = "mp3",
        sample_rate: int = 32000,
        bitrate: int = 128000,
        **kwargs: Any,
    ) -> None:
        super().__init__(endpoint=endpoint, model_name=model_name, credentials=credentials, **kwargs)
        self.voice_id = voice_id
        self.subtitle_type = subtitle_type
        self.audio_format = audio_format
        self.sample_rate = sample_rate
        self.bitrate = bitrate

    @property
    def headers(self) -> dict[str, str]:
        key = self.credentials.get("api_key", "")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _tts_url(self) -> str:
        return f"{self.endpoint.removesuffix('/v1')}/v1/t2a_v2"

    def list_voices(self) -> list[dict]:
        """Read only the account catalog; never synthesize or return vendor payloads."""
        from .base import AdapterError

        base = self.endpoint.removesuffix("/v1")
        status, body, _ = self._request("POST", f"{base}/v1/get_voice", json_body={"voice_type": "all"})
        self._map_status(status, body)
        if not isinstance(body, dict):
            raise AdapterError("capability_test_failed")
        result = body.get("base_resp")
        if result is not None:
            if not isinstance(result, dict) or "status_code" not in result:
                raise AdapterError("capability_test_failed")
            code = str(result["status_code"])
            if code != "0":
                if code in ("1004", "2049"):
                    raise AdapterError("credential_invalid", status=401)
                if code == "1002":
                    raise AdapterError("rate_limited", status=429)
                raise AdapterError("capability_test_failed")
        groups = ("system_voice", "voice_cloning", "voice_generation")
        if not any(isinstance(body.get(group), list) for group in groups):
            raise AdapterError("capability_test_failed")

        def clean(value, limit):
            if not isinstance(value, str):
                return ""
            return "".join(c for c in value[:limit] if unicodedata.category(c) not in ("Cc", "Cf")).strip()

        voices, seen = [], set()
        for group in groups:
            entries = body.get(group, [])
            if entries is None:
                entries = []
            if not isinstance(entries, list):
                raise AdapterError("capability_test_failed")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise AdapterError("capability_test_failed")
                raw_id = entry.get("voice_id")
                if not isinstance(raw_id, str) or not raw_id.strip() or len(raw_id) > 200:
                    raise AdapterError("capability_test_failed")
                identifier = raw_id.strip()
                if not identifier or any(unicodedata.category(c) in ("Cc", "Cf") for c in raw_id):
                    raise AdapterError("capability_test_failed")
                if identifier in seen:
                    continue
                seen.add(identifier)
                if len(seen) > 2000:
                    raise AdapterError("response_too_large")
                description = entry.get("description", [])
                if isinstance(description, list):
                    description = "；".join(clean(part, 160) for part in description[:4] if isinstance(part, str))
                voices.append({"voice_id": identifier, "name": clean(entry.get("voice_name"), 120) or identifier,
                    "category": group, "description": clean(description, 400)})
        return voices

    def _probe(self, capability: str) -> None:
        # 文本能力探测：用固定非私人样例请求一条极短音频。
        self.synthesize("OK", voice_id=self.voice_id or "male-qn-qingse")

    def build_request(self, text: str, *, voice_id: Optional[str] = None) -> dict:
        voice = voice_id or self.voice_id
        if not voice:
            from .base import AdapterError

            raise AdapterError("invalid_field", status=400)
        if len(text) > 10000:
            from .base import AdapterError

            raise AdapterError("text_too_long", status=400)
        return {
            "model": self.model_name,
            "text": text,
            "stream": False,
            "voice_setting": {"voice_id": voice, "speed": 1.0, "vol": 1.0, "pitch": 0},
            "audio_setting": {
                "format": self.audio_format,
                "sample_rate": self.sample_rate,
                "bitrate": self.bitrate,
                "channel": 1,
            },
            "subtitle_enable": True,
            "subtitle_type": self.subtitle_type,
        }

    def synthesize(self, text: str, *, voice_id: Optional[str] = None) -> dict:
        """返回含音频 hex、字幕与文本哈希的受控结果（不含原始供应商异常）。"""
        payload = self.build_request(text, voice_id=voice_id)
        status, body, headers = self._request("POST", self._tts_url(), json_body=payload)
        self._map_status(status, body)
        if isinstance(body,dict) and isinstance(body.get("base_resp"),dict) and str(body["base_resp"].get("status_code",0))!="0":
            from .base import AdapterError
            code=str(body["base_resp"]["status_code"])
            raise AdapterError("credential_invalid" if code in ("1004","2049") else "rate_limited" if code=="1002" else "capability_test_failed")
        data = body.get("data") if isinstance(body, dict) else None
        audio_hex = ""
        if isinstance(data, dict):
            audio_hex = data.get("audio") or ""
        elif isinstance(body, dict):
            audio_hex = body.get("audio") or ""
        trace_id = ""
        if isinstance(body, dict):
            trace_id = str(body.get("trace_id") or body.get("traceId") or "")
        # 字幕时标：MiniMax 经 subtitle_file URL 提供 JSON 时标（临时 URL，
        # 须在合成时抓取解析并持久化，不依赖链接时效）；抓取失败 → 无字幕
        subtitle = None
        if isinstance(data, dict):
            subtitle = _normalize_subtitle(data.get("subtitle"))
            if subtitle is None and data.get("subtitle_file"):
                subtitle = _fetch_subtitle_file(str(data["subtitle_file"]))
        return {
            "model": self.model_name,
            "voice_id": payload["voice_setting"]["voice_id"],
            "audio_format": self.audio_format,
            "subtitle_enable": True,
            "subtitle_type": self.subtitle_type,
            "audio_hex": audio_hex,
            "subtitle": subtitle,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "vendor_trace_id": trace_id,
            "endpoint": self.endpoint,
        }


def _fetch_subtitle_file(url: str) -> Optional[list[dict]]:
    """抓取并解析 subtitle_file（JSON 数组，time_begin/end 毫秒浮点）。"""
    import httpx

    try:
        from .base import validate_endpoint
        validate_endpoint(url,require_https=True,allow_private=False)
        r = httpx.get(url, timeout=20.0, follow_redirects=False,trust_env=False)
        r.raise_for_status()
        if len(r.content)>1024*1024:return None
        entries = r.json()
    except Exception:  # noqa: BLE001 - 抓取失败按无字幕处理（无音频模式）
        return None
    if not isinstance(entries, list):
        return None
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        out.append({
            "text": str(e.get("text") or "")[:200],
            "begin_ms": int(float(e.get("time_begin") or 0)),
            "end_ms": int(float(e.get("time_end") or 0)),
        })
    return out or None


def _normalize_subtitle(value: Any) -> Optional[list[dict]]:
    """内联字幕（若供应商直接返回数组）归一化。"""
    if not isinstance(value, list) or not value:
        return None
    out = []
    for e in value:
        if isinstance(e, dict):
            out.append({
                "text": str(e.get("text") or "")[:200],
                "begin_ms": int(float(e.get("begin_ms") or 0)),
                "end_ms": int(float(e.get("end_ms") or 0)),
            })
    return out or None
