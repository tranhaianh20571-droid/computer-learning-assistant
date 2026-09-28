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

    def generate(self, messages: list[dict], **options: Any) -> dict:
        """切片 3/4 结构化与文字生成（OpenAI 兼容 chat.completions）。

        - native：response_format 透传（网关/模型支持时强制 schema）；
        - tool：tools + tool_choice 透传，返回 tool_arguments；
        - plain_text：纯文字（即时对话路径）。
        供应商异常经 _map_status 映射为受控错误码。
        """
        payload: dict[str, Any] = {"model": self.model_name, "messages": messages}
        if options.get("response_format"):
            payload["response_format"] = options["response_format"]
        if options.get("tools"):
            payload["tools"] = options["tools"]
            payload["tool_choice"] = options.get("tool_choice") or "auto"
        status, body, _ = self._request("POST", self._chat_url(), json_body=payload)
        self._map_status(status, body)
        msg = (body.get("choices") or [{}])[0].get("message", {}) or {}
        tool_calls = msg.get("tool_calls") or []
        tool_arguments = None
        if tool_calls:
            fn = tool_calls[0].get("function") or {}
            tool_arguments = fn.get("arguments")
        return {"content": msg.get("content") or "", "tool_arguments": tool_arguments}

    def generate_stream_text(self, messages: list[dict], **options: Any) -> str:
        """流式收集为完整文本（v2）：供结构化生成的大 JSON 输出使用。

        与 generate 同参（响应体 stream=True），生成期间连接持续有数据，
        规避非流式长生成在网关/上游的读超时与截断；完成后整体解析。
        """
        return "".join(delta for delta in self.stream(messages, **options) if delta)

    def stream(self, messages: list[dict], **options: Any):
        """SSE 流式纯文本生成（切片 4 补充）：逐段 yield 文本增量。

        - 解析 chat.completions SSE（delta.content 与 [DONE] 终止符）；
        - 超时/HTTP 错误映射与 generate 一致（AdapterError 受控错误码）；
        - 仅用于纯文字路径（即时对话/讲次追问文字）；结构化输出不走流式。
        """
        import json as _json

        payload: dict[str, Any] = {"model": self.model_name, "messages": messages, "stream": True}
        client = self._client_or_new()
        try:
            with client.stream(
                "POST", self._chat_url(), json=payload,
                headers={**self.headers, "Accept": "text/event-stream"},
            ) as response:
                if not (200 <= response.status_code < 300):
                    self._map_status(response.status_code, {"raw": response.read()[:256]})
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = _json.loads(data)
                    except ValueError:
                        continue
                    choices = chunk.get("choices") or [{}]
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        yield delta
        except httpx.TimeoutException as exc:
            from .base import AdapterError

            raise AdapterError("timeout", status=504) from exc
        except httpx.HTTPError as exc:
            from .base import AdapterError

            raise AdapterError("connection_error", status=502) from exc

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
