"""切片 3 T01：模型能力探测与结构化输出 spike。

门禁目标（交接文档 T01 / AC-01）：
- 三种协议（OpenAI 兼容 / Anthropic / Gemini 原生）上文本、图像、工具调用、
  JSON Schema、流式、取消能力的分别探测；
- 同一规划 prompt 用 native / tool / prompted 三种输出模式发起，记录成功率、
  失败形态与降级结论；
- 不支持结构化输出的配置触发 `model_capability_unavailable` 边界。

边界与安全：
- 端点与凭据只能来自环境变量（人工前置，见证据文档）；缺失的协议记为 skipped。
- 请求只使用固定非私人样例；响应摘要脱敏（长度/状态/前 80 个非敏感字符）。
- 不打印、不落盘任何 API Key；证据文档中的端点只保留协议与主机名。
- 每类探测 max_tokens 上限 64；三类输出模式共享同一路线候选 prompt，
  单次 token 消耗很小。

用法（在提供凭据的环境中执行）：
    set SPIKE_OPENAI_ENDPOINT=https://.../v1
    set SPIKE_OPENAI_API_KEY=...
    set SPIKE_OPENAI_MODEL=...
    uv run python scripts/slice3_model_spike.py --out evidence-t01.json

输出：JSON 结果文件（脱敏）+ stdout 摘要矩阵；由执行者整理进
`文档/规范/tests/logging-audit/evidence/slice3-model-capability-spike.md`。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import re
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

# ---------------------------------------------------------------------------
# 固定样例（非私人；与切片 1 适配器保持同源语义）
# ---------------------------------------------------------------------------

PROBE_TEXT = "你好，请回复 OK"
PROBE_IMAGE_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# 路线候选输出模式共用 prompt（合成目标，不含任何真实教材/用户资料）
ROUTE_PROMPT = (
    "请为自学目标「入门数据库系统」生成一条学习路线，严格输出 JSON："
    '{"stages":[{"title":string,"courses":[{"title":string,"goal":string}]}],'
    '"prerequisites":[{"from":int,"to":int}]}，'
    "stages 恰好 2 个、每阶段恰好 2 门课程，prerequisites 用课程在列表中的下标引用，"
    "不得输出 JSON 以外的文字。"
)

TIMEOUT_SECONDS = 30.0
MAX_PROBE_TOKENS = 64

PROBE_CAPABILITIES = ("text", "image", "tool_call", "json_schema", "streaming", "cancel")
OUTPUT_MODES = ("native", "tool", "prompted")

_UNSAFE_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def redact_text(text: str, limit: int = 80) -> str:
    """响应摘要：去控制字符 + 限长。只用于证据，不进入日志系统。"""
    cleaned = _UNSAFE_CHARS.sub(" ", str(text or ""))
    return cleaned.strip()[:limit]


def host_of(url: str) -> str:
    try:
        return re.sub(r"^https?://", "", url or "").split("/")[0] or "<unset>"
    except Exception:  # noqa: BLE001
        return "<unset>"


class ProbeOutcome(dict):
    pass


def outcome(capability: str, state: str, detail: str = "", elapsed_ms: int = 0, **extra: Any) -> dict:
    d = {"capability": capability, "state": state, "detail": detail, "elapsed_ms": elapsed_ms}
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# 协议客户端
# ---------------------------------------------------------------------------


class ProtocolClient:
    """三种协议的最小探测客户端；不承担生产职责（生产走切片 1 适配器）。"""

    protocol = "base"

    def __init__(self, endpoint: str, api_key: str, model: str) -> None:
        self.endpoint = (endpoint or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.client = httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False)

    def close(self) -> None:
        self.client.close()

    # -- 各能力探测：返回 (ok, detail, elapsed_ms) --
    def probe(self, capability: str) -> dict:  # pragma: no cover - 真实网络
        raise NotImplementedError

    def generate_routed(self, mode: str) -> dict:  # pragma: no cover
        raise NotImplementedError


class OpenAIClient(ProtocolClient):
    protocol = "openai"

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _chat(self, payload: dict) -> tuple[int, Any]:
        r = self.client.post(f"{self.endpoint}/chat/completions", json=payload, headers=self._headers())
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:200]}
        return r.status_code, body

    def probe(self, capability: str) -> dict:
        start = time.monotonic()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": PROBE_TEXT}],
            "max_tokens": MAX_PROBE_TOKENS,
        }
        if capability == "image":
            payload["messages"] = [{
                "role": "user",
                "content": [
                    {"type": "text", "text": PROBE_TEXT},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,"
                     + base64.b64encode(PROBE_IMAGE_PNG_1PX).decode()}},
                ],
            }]
        elif capability == "tool_call":
            payload["tools"] = [{
                "type": "function",
                "function": {
                    "name": "probe_echo",
                    "description": "probe",
                    "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                },
            }]
            payload["tool_choice"] = "auto"
        elif capability == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "probe",
                    "strict": True,
                    "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}},
                               "required": ["ok"], "additionalProperties": False},
                },
            }
        elif capability == "streaming":
            payload["stream"] = True
        elif capability == "cancel":
            # OpenAI 兼容层无统一取消端点；以可中断流式读取近似验证。
            payload["stream"] = True
        try:
            if payload.get("stream"):
                with self.client.stream("POST", f"{self.endpoint}/chat/completions",
                                        json=payload, headers=self._headers()) as r:
                    first = ""
                    for line in r.iter_lines():
                        first = line
                        break
                    elapsed = int((time.monotonic() - start) * 1000)
                    if 200 <= r.status_code < 300:
                        detail = redact_text(first, 40)
                        state = "available"
                    else:
                        state = "unavailable"
                        detail = f"http_{r.status_code}"
                return outcome(capability, state, detail, elapsed)
            status, body = self._chat(payload)
            elapsed = int((time.monotonic() - start) * 1000)
            if capability == "tool_call" and 200 <= status < 300:
                msg = (body.get("choices") or [{}])[0].get("message", {})
                has_call = bool(msg.get("tool_calls"))
                return outcome(capability, "available" if has_call else "unavailable",
                               "tool_calls_present" if has_call else "no_tool_calls", elapsed)
            if capability == "json_schema" and 200 <= status < 300:
                content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
                try:
                    parsed = json.loads(content or "{}")
                    ok = parsed.get("ok") is True
                except ValueError:
                    ok = False
                return outcome(capability, "available" if ok else "unavailable",
                               "schema_respected" if ok else "schema_violated", elapsed)
            state = "available" if 200 <= status < 300 else "unavailable"
            return outcome(capability, state, f"http_{status}", elapsed)
        except (httpx.HTTPError, socket.gaierror, OSError) as exc:
            return outcome(capability, "unreachable", type(exc).__name__,
                           int((time.monotonic() - start) * 1000))

    def generate_routed(self, mode: str) -> dict:
        start = time.monotonic()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": ROUTE_PROMPT}],
            "max_tokens": 512,
        }
        schema = {
            "type": "object",
            "properties": {
                "stages": {"type": "array"},
                "prerequisites": {"type": "array"},
            },
            "required": ["stages"],
        }
        if mode == "native":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "route_candidate", "strict": False, "schema": schema},
            }
        elif mode == "tool":
            payload["tools"] = [{
                "type": "function",
                "function": {
                    "name": "submit_route_candidate",
                    "description": "submit the route candidate",
                    "parameters": schema,
                },
            }]
            payload["tool_choice"] = {"type": "function", "function": {"name": "submit_route_candidate"}}
        try:
            status, body = self._chat(payload)
            elapsed = int((time.monotonic() - start) * 1000)
            if not (200 <= status < 300):
                return {"mode": mode, "state": "failed", "detail": f"http_{status}",
                        "elapsed_ms": elapsed, "valid_json": False}
            msg = (body.get("choices") or [{}])[0].get("message", {})
            raw = ""
            if mode == "tool":
                calls = msg.get("tool_calls") or []
                if calls:
                    raw = calls[0].get("function", {}).get("arguments", "")
            else:
                raw = msg.get("content", "") or ""
            try:
                parsed = json.loads(raw)
                valid = isinstance(parsed, dict) and "stages" in parsed
            except ValueError:
                valid = False
            return {
                "mode": mode,
                "state": "ok" if valid else "parse_failed",
                "detail": redact_text(raw),
                "elapsed_ms": elapsed,
                "valid_json": valid,
            }
        except (httpx.HTTPError, socket.gaierror, OSError) as exc:
            return {"mode": mode, "state": "unreachable", "detail": type(exc).__name__,
                    "elapsed_ms": int((time.monotonic() - start) * 1000), "valid_json": False}


class AnthropicClient(ProtocolClient):
    protocol = "anthropic"

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

    def _messages(self, payload: dict) -> tuple[int, Any]:
        r = self.client.post(f"{self.endpoint}/v1/messages", json=payload, headers=self._headers())
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:200]}
        return r.status_code, body

    def probe(self, capability: str) -> dict:
        start = time.monotonic()
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_PROBE_TOKENS,
            "messages": [{"role": "user", "content": PROBE_TEXT}],
        }
        if capability == "image":
            payload["messages"] = [{
                "role": "user",
                "content": [
                    {"type": "text", "text": PROBE_TEXT},
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(PROBE_IMAGE_PNG_1PX).decode(),
                    }},
                ],
            }]
        elif capability == "tool_call":
            payload["tools"] = [{
                "name": "probe_echo",
                "description": "probe",
                "input_schema": {"type": "object", "properties": {"text": {"type": "string"}},
                                 "required": ["text"]},
            }]
        elif capability == "json_schema":
            # Anthropic 无独立 JSON Schema 端点；以工具调用强制 schema 近似。
            payload["tools"] = [{
                "name": "submit_probe",
                "description": "probe",
                "input_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}},
                                 "required": ["ok"]},
            }]
            payload["tool_choice"] = {"type": "tool", "name": "submit_probe"}
        elif capability == "streaming":
            payload["stream"] = True
        elif capability == "cancel":
            payload["stream"] = True
        try:
            if payload.get("stream"):
                with self.client.stream("POST", f"{self.endpoint}/v1/messages",
                                        json=payload, headers=self._headers()) as r:
                    for _ in r.iter_lines():
                        break
                    elapsed = int((time.monotonic() - start) * 1000)
                    state = "available" if 200 <= r.status_code < 300 else "unavailable"
                return outcome(capability, state, f"http_{r.status_code}", elapsed)
            status, body = self._messages(payload)
            elapsed = int((time.monotonic() - start) * 1000)
            if capability in ("tool_call", "json_schema") and 200 <= status < 300:
                blocks = body.get("content") or []
                has_tool = any(b.get("type") == "tool_use" for b in blocks if isinstance(b, dict))
                ok = has_tool
                if capability == "json_schema" and has_tool:
                    ok = any((b or {}).get("input", {}).get("ok") is True
                             for b in blocks if isinstance(b, dict))
                return outcome(capability, "available" if ok else "unavailable",
                               "tool_use_present" if has_tool else "no_tool_use", elapsed)
            state = "available" if 200 <= status < 300 else "unavailable"
            return outcome(capability, state, f"http_{status}", elapsed)
        except (httpx.HTTPError, socket.gaierror, OSError) as exc:
            return outcome(capability, "unreachable", type(exc).__name__,
                           int((time.monotonic() - start) * 1000))

    def generate_routed(self, mode: str) -> dict:
        start = time.monotonic()
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": ROUTE_PROMPT}],
        }
        if mode in ("native", "tool"):
            payload["tools"] = [{
                "name": "submit_route_candidate",
                "description": "submit the route candidate",
                "input_schema": {
                    "type": "object",
                    "properties": {"stages": {"type": "array"}, "prerequisites": {"type": "array"}},
                    "required": ["stages"],
                },
            }]
            payload["tool_choice"] = {"type": "tool", "name": "submit_route_candidate"}
        try:
            status, body = self._messages(payload)
            elapsed = int((time.monotonic() - start) * 1000)
            if not (200 <= status < 300):
                return {"mode": mode, "state": "failed", "detail": f"http_{status}",
                        "elapsed_ms": elapsed, "valid_json": False}
            blocks = body.get("content") or []
            raw = ""
            if mode in ("native", "tool"):
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        raw = json.dumps(b.get("input", {}), ensure_ascii=False)
                        break
            else:
                raw = "".join((b or {}).get("text", "") for b in blocks if isinstance(b, dict))
            try:
                parsed = json.loads(raw)
                valid = isinstance(parsed, dict) and "stages" in parsed
            except ValueError:
                valid = False
            return {
                "mode": mode,
                "state": "ok" if valid else "parse_failed",
                "detail": redact_text(raw),
                "elapsed_ms": elapsed,
                "valid_json": valid,
            }
        except (httpx.HTTPError, socket.gaierror, OSError) as exc:
            return {"mode": mode, "state": "unreachable", "detail": type(exc).__name__,
                    "elapsed_ms": int((time.monotonic() - start) * 1000), "valid_json": False}


class GeminiClient(ProtocolClient):
    protocol = "gemini"

    def _url(self, method: str) -> str:
        return f"{self.endpoint}/v1beta/models/{self.model}:{method}"

    def _post(self, url: str, payload: dict) -> tuple[int, Any]:
        r = self.client.post(url, json=payload, params={"key": self.api_key} if self.api_key else None)
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:200]}
        return r.status_code, body

    def probe(self, capability: str) -> dict:
        start = time.monotonic()
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": PROBE_TEXT}]}],
            "generationConfig": {"maxOutputTokens": MAX_PROBE_TOKENS},
        }
        if capability == "image":
            payload["contents"][0]["parts"].append({
                "inline_data": {
                    "mime_type": "image/png",
                    "data": base64.b64encode(PROBE_IMAGE_PNG_1PX).decode(),
                }
            })
        elif capability == "tool_call":
            payload["tools"] = [{
                "functionDeclarations": [{
                    "name": "probe_echo",
                    "description": "probe",
                    "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                                   "required": ["text"]},
                }]
            }]
        elif capability == "json_schema":
            payload["generationConfig"]["responseMimeType"] = "application/json"
            payload["generationConfig"]["responseSchema"] = {
                "type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
            }
        try:
            status, body = self._post(self._url("generateContent"), payload)
            elapsed = int((time.monotonic() - start) * 1000)
            if capability == "json_schema" and 200 <= status < 300:
                parts = (((body.get("candidates") or [{}])[0]).get("content") or {}).get("parts") or []
                text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
                try:
                    ok = json.loads(text).get("ok") is True
                except ValueError:
                    ok = False
                return outcome(capability, "available" if ok else "unavailable",
                               "schema_respected" if ok else "schema_violated", elapsed)
            state = "available" if 200 <= status < 300 else "unavailable"
            return outcome(capability, state, f"http_{status}", elapsed)
        except (httpx.HTTPError, socket.gaierror, OSError) as exc:
            return outcome(capability, "unreachable", type(exc).__name__,
                           int((time.monotonic() - start) * 1000))

    def generate_routed(self, mode: str) -> dict:
        start = time.monotonic()
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": ROUTE_PROMPT}]}],
            "generationConfig": {"maxOutputTokens": 1024},
        }
        if mode in ("native", "tool"):
            payload["generationConfig"]["responseMimeType"] = "application/json"
            payload["generationConfig"]["responseSchema"] = {
                "type": "object",
                "properties": {"stages": {"type": "array"}, "prerequisites": {"type": "array"}},
                "required": ["stages"],
            }
        try:
            status, body = self._post(self._url("generateContent"), payload)
            elapsed = int((time.monotonic() - start) * 1000)
            if not (200 <= status < 300):
                return {"mode": mode, "state": "failed", "detail": f"http_{status}",
                        "elapsed_ms": elapsed, "valid_json": False}
            parts = (((body.get("candidates") or [{}])[0]).get("content") or {}).get("parts") or []
            raw = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            try:
                parsed = json.loads(raw)
                valid = isinstance(parsed, dict) and "stages" in parsed
            except ValueError:
                valid = False
            return {
                "mode": mode,
                "state": "ok" if valid else "parse_failed",
                "detail": redact_text(raw),
                "elapsed_ms": elapsed,
                "valid_json": valid,
            }
        except (httpx.HTTPError, socket.gaierror, OSError) as exc:
            return {"mode": mode, "state": "unreachable", "detail": type(exc).__name__,
                    "elapsed_ms": int((time.monotonic() - start) * 1000), "valid_json": False}


def build_clients() -> list[ProtocolClient]:
    """按环境变量装配可测协议；缺变量 → skipped，不发请求。"""
    clients: list[ProtocolClient] = []
    spec = [
        ("SPIKE_OPENAI", OpenAIClient),
        ("SPIKE_ANTHROPIC", AnthropicClient),
        ("SPIKE_GEMINI", GeminiClient),
    ]
    for prefix, cls in spec:
        endpoint = os.environ.get(f"{prefix}_ENDPOINT", "")
        key = os.environ.get(f"{prefix}_API_KEY", "")
        model = os.environ.get(f"{prefix}_MODEL", "")
        if endpoint and key and model:
            clients.append(cls(endpoint, key, model))
    return clients


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="slice3-model-spike-result.json")
    args = parser.parse_args()

    clients = build_clients()
    result: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "environment": {
            "python": sys.version.split()[0],
            "os": platform.platform(),
            "httpx": httpx.__version__,
        },
        "protocols": [],
    }

    configured = {p: bool(os.environ.get(f"SPIKE_{p.upper()}_ENDPOINT")) for p in ("openai", "anthropic", "gemini")}
    result["configuration"] = {
        prefix: {"endpoint_configured": endpoint, "endpoint_host": host_of(os.environ.get(f"SPIKE_{prefix.upper()}_ENDPOINT", ""))}
        for prefix, endpoint in configured.items()
    }

    for client in clients:
        entry: dict[str, Any] = {
            "protocol": client.protocol,
            "endpoint_host": host_of(client.endpoint),
            "model": client.model,
            "capabilities": [client.probe(cap) for cap in PROBE_CAPABILITIES],
            "output_modes": [client.generate_routed(mode) for mode in OUTPUT_MODES],
        }
        result["protocols"].append(entry)
        client.close()

    # 未配置协议记为 skipped（不发请求、不猜测端点）
    seen = {e["protocol"] for e in result["protocols"]}
    for prefix, endpoint_configured in configured.items():
        if prefix not in seen:
            result["protocols"].append({
                "protocol": prefix,
                "state": "skipped",
                "reason": "credentials not provided via environment (manual prerequisite)",
            })

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    # stdout 摘要矩阵（不含任何响应正文）
    for entry in result["protocols"]:
        name = entry["protocol"]
        if entry.get("state") == "skipped":
            print(f"[{name}] SKIPPED: {entry['reason']}")
            continue
        caps = " ".join(f"{c['capability']}={c['state']}" for c in entry["capabilities"])
        modes = " ".join(f"{m['mode']}={m['state']}" for m in entry["output_modes"])
        print(f"[{name}] {caps} | modes: {modes}")
    print(f"result written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
