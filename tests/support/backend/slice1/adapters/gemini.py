"""Gemini 原生内容模型适配器（切片 1 T03）。

使用 `generateContent` / `streamGenerateContent`，API key 走查询参数 `key`。
"""

from __future__ import annotations

from typing import Any

from .base import CAPABILITIES, PROBE_TEXT, AdapterBase, image_data_url


class GeminiAdapter(AdapterBase):
    kind = "content_model"
    protocol = "gemini"
    capabilities = CAPABILITIES

    @property
    def headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def _model_url(self, method: str) -> str:
        return f"{self.endpoint}/v1beta/models/{self.model_name}:{method}"

    def _params(self) -> dict[str, str]:
        key = self.credentials.get("api_key", "")
        return {"key": key} if key else {}

    def _probe(self, capability: str) -> None:
        if capability == "streaming":
            self._probe_streaming()
            return
        parts: list[dict[str, Any]] = [{"text": PROBE_TEXT}]
        payload: dict[str, Any] = {"contents": [{"role": "user", "parts": parts}]}
        if capability == "image":
            parts.append(
                {
                    "inline_data": {
                        "mime_type": "image/png",
                        "data": image_data_url().split(",", 1)[1],
                    }
                }
            )
        elif capability == "tool_call":
            payload["tools"] = [
                {
                    "function_declarations": [
                        {
                            "name": "probe_echo",
                            "description": "probe",
                            "parameters": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                            },
                        }
                    ]
                }
            ]
        elif capability == "json_schema":
            payload["generationConfig"] = {
                "response_mime_type": "application/json",
                "response_schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
            }
        elif capability == "cancel":
            pass
        status, body, _ = self._request(
            "POST", self._model_url("generateContent"), json_body=payload, params=self._params()
        )
        self._map_status(status, body)

    def _probe_streaming(self) -> None:
        payload = {"contents": [{"role": "user", "parts": [{"text": PROBE_TEXT}]}]}
        client = self._client_or_new()
        try:
            with client.stream(
                "POST",
                self._model_url("streamGenerateContent"),
                json=payload,
                params={**self._params(), "alt": "sse"},
                headers={**self.headers, "Accept": "text/event-stream"},
            ) as response:
                if not (200 <= response.status_code < 300):
                    self._map_status(response.status_code, {"raw": response.read()[:256]})
                for _ in response.iter_lines():
                    return
        except Exception as exc:  # noqa: BLE001
            from .base import AdapterError

            raise AdapterError("connection_error", status=502) from exc
