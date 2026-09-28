"""Gemini 原生内容模型适配器（切片 1 T03）。

使用 `generateContent` / `streamGenerateContent`，API key 走查询参数 `key`。
v2：补齐生成实现——native 档走原生 `responseSchema` 结构化输出（协议级保证），
tool 档走 `function_declarations`，prompted/对话走流式文本收集。
"""

from __future__ import annotations

import json as _json
from typing import Any

import httpx

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

    # ------------------------------------------------------------------
    # 生成实现（v2）：messages → contents/systemInstruction；schema 翻译
    # ------------------------------------------------------------------
    _STRIP_SCHEMA_KEYS = frozenset(
        {"$schema", "$defs", "$ref", "additionalProperties", "title", "default", "examples", "prefixItems"}
    )

    def _translate_schema(self, schema: Any, defs: Any = None, seen: frozenset = frozenset()) -> Any:
        """Pydantic/OpenAPI schema → Gemini OpenAPI 子集：$ref 内联、剥离不支持键。"""
        if not isinstance(schema, dict):
            return schema
        if "$ref" in schema:
            name = str(schema["$ref"]).rsplit("/", 1)[-1]
            if name in seen:
                return {"type": "OBJECT"}  # 递归引用：退化为空对象
            defs = defs if defs is not None else {}
            return self._translate_schema(defs.get(name, {"type": "OBJECT"}), defs, seen | {name})
        if "anyOf" in schema and isinstance(schema["anyOf"], list):
            branches = [b for b in schema["anyOf"] if isinstance(b, dict) and b.get("type") != "null"]
            out = self._translate_schema(branches[0], defs, seen) if branches else {"type": "STRING"}
            if isinstance(out, dict) and len(branches) != len(schema["anyOf"]):
                out["nullable"] = True
            return out
        out: dict[str, Any] = {}
        for key, value in schema.items():
            if key in self._STRIP_SCHEMA_KEYS:
                continue
            if key == "type" and isinstance(value, str):
                out["type"] = value.upper()
            elif key == "properties" and isinstance(value, dict):
                out["properties"] = {
                    pk: self._translate_schema(pv, defs, seen) for pk, pv in value.items()
                }
            elif key == "items":
                out["items"] = self._translate_schema(value, defs, seen)
            elif key in ("enum", "required", "format", "description", "nullable"):
                out[key] = value
        return out

    def _messages_to_contents(self, messages: list[dict]) -> tuple[list[dict], Optional[dict]]:
        """system → systemInstruction；assistant → model；返回 (contents, system_instruction)。"""
        contents: list[dict] = []
        system_parts: list[dict] = []
        for m in messages:
            text = str(m.get("content", "") or "")
            if not text:
                continue
            if m.get("role") == "system":
                system_parts.append({"text": text})
                continue
            contents.append(
                {"role": "model" if m.get("role") == "assistant" else "user", "parts": [{"text": text}]}
            )
        return contents, ({"parts": system_parts} if system_parts else None)

    def generate(self, messages: list[dict], **options: Any) -> dict:
        """切片 3/4 结构化与文字生成（Gemini 原生 generateContent）。

        - native：response_format(json_schema) → 原生 responseMimeType+responseSchema；
        - tool：tools(function_declarations) 透传，返回 function_call.args；
        - finishReason 非 STOP（MAX_TOKENS/SAFETY 等）时内容不完整，
          照常返回交由上层校验失败降级（不静默伪造成功）。
        """
        contents, system_instruction = self._messages_to_contents(messages)
        generation_config: dict[str, Any] = {}
        if options.get("response_format"):
            schema = (options["response_format"].get("json_schema") or {}).get("schema")
            if schema:
                generation_config["response_mime_type"] = "application/json"
                generation_config["response_schema"] = self._translate_schema(schema)
        payload: dict[str, Any] = {"contents": contents, "generationConfig": generation_config}
        if system_instruction:
            payload["systemInstruction"] = system_instruction
        if options.get("tools"):
            declarations = []
            for t in options["tools"]:
                fn = t.get("function") or t
                declarations.append(
                    {
                        "name": fn.get("name"),
                        "description": fn.get("description", ""),
                        "parameters": self._translate_schema(fn.get("parameters") or {"type": "OBJECT"}),
                    }
                )
            payload["tools"] = [{"function_declarations": declarations}]
            choice = options.get("tool_choice")
            if isinstance(choice, dict) and choice.get("function"):
                payload["tool_config"] = {
                    "function_calling_config": {
                        "mode": "ANY",
                        "allowed_function_names": [choice["function"]["name"]],
                    }
                }
            elif choice == "required":
                payload["tool_config"] = {"function_calling_config": {"mode": "ANY"}}
            else:
                payload["tool_config"] = {"function_calling_config": {"mode": "AUTO"}}
        status, body, _ = self._request(
            "POST", self._model_url("generateContent"), json_body=payload, params=self._params()
        )
        self._map_status(status, body)
        return self._parse_generate_body(body)

    def _parse_generate_body(self, body: dict) -> dict:
        candidates = body.get("candidates") or [{}]
        cand = candidates[0] or {}
        parts = ((cand.get("content") or {}).get("parts")) or []
        text = "".join(str(p.get("text", "")) for p in parts if "text" in p)
        tool_arguments = None
        for p in parts:
            fc = p.get("function_call")
            if fc and isinstance(fc, dict):
                tool_arguments = fc.get("args") or {}
                break
        return {"content": text, "tool_arguments": tool_arguments}

    def stream(self, messages: list[dict], **options: Any):
        """SSE 流式纯文本生成：streamGenerateContent?alt=sse，逐段 yield 文本增量。"""
        contents, system_instruction = self._messages_to_contents(messages)
        payload: dict[str, Any] = {"contents": contents}
        if system_instruction:
            payload["systemInstruction"] = system_instruction
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
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data:
                        continue
                    try:
                        chunk = _json.loads(data)
                    except ValueError:
                        continue
                    parts = ((chunk.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                    for p in parts:
                        t = p.get("text")
                        if t:
                            yield t
        except httpx.TimeoutException as exc:
            from .base import AdapterError

            raise AdapterError("timeout", status=504) from exc
        except httpx.HTTPError as exc:
            from .base import AdapterError

            raise AdapterError("connection_error", status=502) from exc

    def generate_stream_text(self, messages: list[dict], **options: Any) -> str:
        """流式收集为完整文本（v2）：供结构化生成的大 JSON 输出使用。"""
        return "".join(delta for delta in self.stream(messages, **options) if delta)
