"""Anthropic 原生内容模型适配器（切片 1 T03）。

消息格式、`tool_use` 与 `x-api-key` 头与 OpenAI 兼容协议不同。
"""

from __future__ import annotations

from typing import Any

from .base import CAPABILITIES, PROBE_TEXT, AdapterBase, AdapterError, image_data_url


class AnthropicAdapter(AdapterBase):
    supports_text_stream = True
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
        endpoint = self.endpoint.rstrip("/").removesuffix("/v1")
        return f"{endpoint}/v1/messages"

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
        try:
            with self._stream_request(
                "POST",
                self._messages_url(),
                json=payload,
                headers={**self.headers, "Accept": "text/event-stream"},
            ) as lines:
                for _ in lines:
                    return
        except AdapterError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AdapterError("connection_error", status=502) from exc

    def generate(self, messages: list[dict], **options: Any) -> dict:
        payload = self._generation_payload(messages, **options)
        status, body, _ = self._request("POST", self._messages_url(), json_body=payload)
        self._map_status(status, body)
        calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": b.get("input", {})}}
                 for b in body.get("content", []) if b.get("type") == "tool_use"]
        text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
        return {"content": text, "tool_arguments": calls[0]["function"]["arguments"] if calls else None, "tool_calls": calls}

    def stream(self, messages: list[dict], **options: Any):
        import json
        payload = self._generation_payload(messages, **options)
        payload["stream"] = True
        with self._stream_request("POST", self._messages_url(), json=payload,
                headers={**self.headers, "Accept": "text/event-stream"}) as lines:
            for line in lines:
                if not line.startswith("data:"):
                    continue
                try:
                    chunk = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if chunk.get("type") == "error":
                    from .base import AdapterError
                    kind = (chunk.get("error") or {}).get("type")
                    raise AdapterError("rate_limited" if kind == "rate_limit_error" else "capability_test_failed", status=502)
                delta = chunk.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    yield delta["text"]

    def _generation_payload(self, messages: list[dict], **options: Any) -> dict:
        import json
        systems, converted = [], []
        for message in messages:
            content = message.get("content", "") or ""
            role = message.get("role", "user")
            if role == "system":
                systems.append(str(content))
                continue
            blocks = []
            if role == "tool":
                blocks.append({"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": str(content)})
                role = "user"
            elif isinstance(content, list):
                for item in content:
                    if item.get("type") == "text":
                        blocks.append({"type": "text", "text": item["text"]})
                    elif item.get("type") == "image_url":
                        url = item["image_url"]["url"]
                        if not url.startswith("data:"):
                            from .base import AdapterError
                            raise AdapterError("unsupported_capability", status=422)
                        header, data = url.split(",", 1)
                        blocks.append({"type": "image", "source": {"type": "base64",
                            "media_type": header[5:].split(";")[0], "data": data}})
            elif content:
                blocks.append({"type": "text", "text": str(content)})
            for call in message.get("tool_calls", []):
                fn = call["function"]; args = fn.get("arguments", {})
                blocks.append({"type": "tool_use", "id": call["id"], "name": fn["name"],
                               "input": json.loads(args) if isinstance(args, str) else args})
            if blocks:
                if converted and converted[-1]["role"] == role:
                    converted[-1]["content"].extend(blocks)
                else:
                    converted.append({"role": role, "content": blocks})
        payload = {"model": self.model_name, "max_tokens": 16000, "messages": converted}
        if systems:
            payload["system"] = "\n".join(systems)
        if options.get("tools"):
            payload["tools"] = [{"name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": t["function"]["parameters"]} for t in options["tools"]]
            choice = options.get("tool_choice", "auto")
            payload["tool_choice"] = ({"type": "tool", "name": choice["function"]["name"]}
                                      if isinstance(choice, dict) else {"type": "auto" if choice == "auto" else "any"})
        return payload
