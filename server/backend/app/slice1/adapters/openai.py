"""OpenAI 兼容内容模型适配器（切片 1 T03）。

支持自定义 endpoint/model/api_key；能力探测覆盖文本、图像、工具调用、
JSON Schema、流式与取消。探测只发非私人样例。
"""

from __future__ import annotations

from typing import Any, Optional

from .base import CAPABILITIES, PROBE_TEXT, AdapterBase, image_data_url


class OpenAIAdapter(AdapterBase):
    kind = "content_model"
    protocol = "openai"
    capabilities = CAPABILITIES

    @property
    def headers(self) -> dict[str, str]:
        key = self.credentials.get("api_key", "")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _chat_url(self) -> str:
        return f"{self.endpoint}/chat/completions"

    def _probe(self, capability: str) -> None:
        if capability == "streaming":
            self._probe_streaming()
            return
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": PROBE_TEXT}],
            "max_tokens": 8,
        }
        if capability == "image":
            payload["messages"] = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROBE_TEXT},
                        {"type": "image_url", "image_url": {"url": image_data_url()}},
                    ],
                }
            ]
        elif capability == "tool_call":
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": "probe_echo",
                        "description": "probe",
                        "parameters": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    },
                }
            ]
            payload["tool_choice"] = "auto"
        elif capability == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "probe",
                    "schema": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                        "additionalProperties": False,
                    },
                },
            }
        elif capability == "cancel":
            # 取消能力：能拿到响应即可，具体中断由切片 3 的流式实现负责。
            pass
        status, body, _ = self._request("POST", self._chat_url(), json_body=payload)
        self._map_status(status, body)

    def _probe_streaming(self) -> None:
        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": PROBE_TEXT}],
            "max_tokens": 8,
            "stream": True,
        }
        client = self._client_or_new()
        try:
            with client.stream(
                "POST",
                self._chat_url(),
                json=payload,
                headers={**self.headers, "Accept": "text/event-stream"},
            ) as response:
                if not (200 <= response.status_code < 300):
                    self._map_status(response.status_code, {"raw": response.read()[:256]})
                # 读取首个数据块确认确实是流式
                for _ in response.iter_lines():
                    return
        except Exception as exc:  # noqa: BLE001
            from .base import AdapterError

            raise AdapterError("connection_error", status=502) from exc
