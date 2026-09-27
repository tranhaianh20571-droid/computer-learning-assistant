"""Anthropic 原生内容模型适配器（切片 1 T03）。

消息格式、`tool_use` 与 `x-api-key` 头与 OpenAI 兼容协议不同。
"""

from __future__ import annotations

from typing import Any

from .base import CAPABILITIES, PROBE_TEXT, AdapterBase, image_data_url


class AnthropicAdapter(AdapterBase):
    kind = "content_model"
    protocol = "anthropic"
    capabilities = CAPABILITIES

    @property
    def headers(self) -> dict[str, str]:
        key = self.credentials.get("api_key", "")
        headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        if key:
            headers["x-api-key"] = key
        return headers

    def _messages_url(self) -> str:
        return f"{self.endpoint}/v1/messages"

    def _probe(self, capability: str) -> None:
        if capability == "streaming":
            self._probe_streaming()
            return
        content: Any = PROBE_TEXT
        payload: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": content}],
        }
        if capability == "image":
            payload["messages"] = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROBE_TEXT},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": image_data_url().split(",", 1)[1],
                            },
                        },
                    ],
                }
            ]
        elif capability == "tool_call":
            payload["tools"] = [
                {
                    "name": "probe_echo",
                    "description": "probe",
                    "input_schema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                }
            ]
        elif capability == "json_schema":
            # Anthropic 用工具约束结构化输出；探测其 tool 能力即可。
            payload["tools"] = [
                {
                    "name": "probe_json",
                    "description": "probe",
                    "input_schema": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                    },
                }
            ]
            payload["tool_choice"] = {"type": "tool", "name": "probe_json"}
        elif capability == "cancel":
            pass
        status, body, _ = self._request("POST", self._messages_url(), json_body=payload)
        self._map_status(status, body)

    def _probe_streaming(self) -> None:
        payload = {
            "model": self.model_name,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": PROBE_TEXT}],
            "stream": True,
        }
        client = self._client_or_new()
        try:
            with client.stream(
                "POST",
                self._messages_url(),
                json=payload,
                headers={**self.headers, "Accept": "text/event-stream"},
            ) as response:
                if not (200 <= response.status_code < 300):
                    self._map_status(response.status_code, {"raw": response.read()[:256]})
                for _ in response.iter_lines():
                    return
        except Exception as exc:  # noqa: BLE001
            from .base import AdapterError

            raise AdapterError("connection_error", status=502) from exc
