"""Bounded fixed-sample parameter discovery; provider text never leaves this module."""
from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
import secrets
from time import monotonic
from urllib.parse import quote

import httpx2 as httpx
from pydantic import Field, SecretStr

from ..agents.contracts import AgentDeps, RunBudget
from ..agents.native import SafeModelTransport
from .errors import error
from .model_catalog import canonical_endpoint
from .adapters.base import is_metadata_address
from .reasoning import ReasoningDescribeIn

DISCOVERY_VERSION = "reasoning-discovery-1"
MAX_REQUESTS = 12
DEADLINE_SECONDS = 50
EFFORTS = {
    "openai": ("none", "minimal", "low", "medium", "high", "xhigh", "max"),
    "openai_responses": ("none", "minimal", "low", "medium", "high", "xhigh", "max"),
    "anthropic": ("low", "medium", "high", "max"),
    "gemini": ("minimal", "low", "medium", "high"),
}
_VALUE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


class ReasoningDiscoverIn(ReasoningDescribeIn):
    api_key: SecretStr | None = Field(default=None, repr=False, max_length=8192)
    config_id: str | None = Field(default=None, max_length=64)
    config_version: int | None = Field(default=None, ge=1)
    refresh: bool = False


def _enums(payload, *, error_response=False):
    """Accept only parameter-scoped, bounded enum data, never model prose."""
    found = []
    nodes = 0

    def walk(node, path="", depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > 80 or depth > 6 or not isinstance(node, dict):
            return
        for name, value in list(node.items())[:40]:
            if not isinstance(name, str):
                continue
            location = (path + "." + name).lower().replace("_", "")
            relevant = ("reasoning" in location or "thinking" in location or "effort" in location)
            enum_field = name in ("enum", "values", "supported_values", "supported_levels", "effort_values",
                                  "supported_effort_values", "supported_thinking_levels")
            if relevant and isinstance(value, list) and (enum_field or name in ("effort", "reasoning_effort", "thinkingLevel", "thinking_level")):
                for item in value[:16]:
                    if isinstance(item, str) and _VALUE.fullmatch(item.lower()):
                        found.append(item.lower())
            if isinstance(value, dict):
                walk(value, location, depth + 1)
    walk(payload)
    if error_response and isinstance(payload, dict):
        detail = payload.get("error", payload)
        message = detail.get("message", "") if isinstance(detail, dict) else ""
        if isinstance(message, str) and len(message) <= 4096:
            if re.search(r"(reasoning[_ .]?effort|thinking[_ .]?level|output_config[. ]effort|\beffort\b)", message, re.I):
                segment = re.search(r"(?:supported (?:values|levels)|allowed (?:values|levels)|must be (?:one of|in)|expected one of)[: ]*(.{1,500})", message, re.I)
                if segment:
                    found.extend(re.findall(r"""['"]([a-z][a-z0-9_]{0,23})['"]""", segment[1].lower()))
    return list(dict.fromkeys(found))[:8]


def _parameter_rejected(payload):
    if not isinstance(payload, dict):
        return False
    detail = payload.get("error", payload)
    if not isinstance(detail, dict):
        return False
    param = detail.get("param", "")
    if param in ("reasoning_effort", "reasoning.effort", "thinking", "thinking.type",
                 "thinking.budget_tokens", "output_config.effort", "generationConfig.thinkingConfig"):
        return True
    message = detail.get("message", "")
    return (isinstance(message, str) and len(message) <= 4096
            and bool(re.search(r"reasoning|thinking|budget_tokens|output_config|\beffort\b", message, re.I))
            and bool(re.search(r"unsupported|not support|invalid|must|only|unknown|unrecognized|cannot|not allowed", message, re.I)))


def probe_payload(protocol, model, setting=None, *, invalid=False):
    """Protocol vocabulary only; no model-name conditions or model defaults."""
    setting = setting or {"mode": "default"}
    mode = setting["mode"]
    if protocol in ("openai", "openai_responses"):
        extra = {}
        if mode == "effort" or invalid:
            effort = "invalid_probe_value" if invalid else setting["effort"]
            extra = ({"reasoning": {"effort": effort}} if protocol == "openai_responses"
                     else {"reasoning_effort": effort})
        if protocol == "openai_responses":
            return "/responses", {"model": model, "input": "Reply OK.", "max_output_tokens": 128,
                                  "store": False, **extra}
        return "/chat/completions", {"model": model, "messages": [{"role": "user", "content": "Reply OK."}],
                                    "max_completion_tokens": 128, **extra}
    if protocol == "anthropic":
        body = {"model": model, "messages": [{"role": "user", "content": "Reply OK."}], "max_tokens": 128}
        if mode == "effort" or invalid:
            body.update(thinking={"type": "adaptive"}, output_config={"effort": "invalid_probe_value" if invalid else setting["effort"]})
        elif mode == "budget":
            body.update(thinking={"type": "enabled", "budget_tokens": setting["budget_tokens"]},
                        max_tokens=setting["budget_tokens"] + 128)
        elif mode == "auto":
            body["thinking"] = {"type": "adaptive"}
        elif mode == "off":
            body["thinking"] = {"type": "disabled"}
        return "/messages", body
    thinking = {"includeThoughts": False}
    if mode == "effort" or invalid:
        thinking["thinkingLevel"] = "INVALID_PROBE_VALUE" if invalid else setting["effort"].upper()
    elif mode in ("budget", "off", "auto"):
        thinking["thinkingBudget"] = setting.get("budget_tokens", 0 if mode == "off" else -1)
    generation = {"maxOutputTokens": max(128, setting.get("budget_tokens", 0) + 128)}
    if mode != "default" or invalid:
        generation["thinkingConfig"] = thinking
    return "/models/" + quote(model.removeprefix("models/"), safe="-_.") + ":generateContent", {
        "contents": [{"role": "user", "parts": [{"text": "Reply OK."}]}], "generationConfig": generation}


def _option(setting, evidence):
    mode = setting["mode"]
    value = mode + (":" + str(setting["effort"]) if mode == "effort" else
                    ":" + str(setting["budget_tokens"]) if mode == "budget" else "")
    return {"value": value, "settings": {**setting, "show_summary": False, "discovered": True},
            "evidence": evidence}


def _generation_ok(protocol, data):
    if not isinstance(data, dict) or data.get("error") is not None:
        return False
    if protocol == "openai":
        return isinstance(data.get("choices"), list) and bool(data["choices"])
    if protocol == "openai_responses":
        return data.get("object") == "response" and isinstance(data.get("output"), list)
    if protocol == "anthropic":
        return data.get("type") == "message" and isinstance(data.get("content"), list)
    return isinstance(data.get("candidates"), list) and bool(data["candidates"])


class ReasoningDiscovery:
    def __init__(self, *, transport_factory=SafeModelTransport, clock=monotonic):
        self.transport_factory, self.clock = transport_factory, clock
        self._salt = secrets.token_bytes(32)
        self._cache = OrderedDict()
        self._inflight = {}
        self._recent = OrderedDict()

    async def detect(self, *, actor_id, protocol, endpoint, model, key, check=lambda: None,
                     audit=lambda result: None, refresh=False):
        try:
            target = ReasoningDescribeIn(protocol=protocol, endpoint=endpoint, model_name=model)
        except ValueError:
            raise error("invalid_field", "invalid_reasoning_destination") from None
        # DNS and metadata-alias checks belong to the timed, pinned transport.
        from urllib.parse import urlsplit
        if is_metadata_address(urlsplit(target.endpoint).hostname):
            raise error("invalid_field", "invalid_reasoning_destination")
        base = canonical_endpoint(target.endpoint, protocol)
        fingerprint = hmac.new(self._salt, key.encode(), hashlib.sha256).hexdigest()
        identity = (actor_id, protocol, base, model, fingerprint)
        try:
            check()
        except Exception:
            audit("probe_failed")
            raise
        cached = self._cache.get(identity)
        if not refresh and cached and cached[0] > self.clock():
            self._cache.move_to_end(identity)
            return {**deepcopy(cached[1]), "cached": True}
        running = self._inflight.get(identity)
        if running and running.get_loop() is asyncio.get_running_loop():
            return deepcopy(await asyncio.shield(running))
        history = self._recent.setdefault(actor_id, deque())
        while history and history[0] < self.clock() - 60:
            history.popleft()
        if len(history) >= 4:
            audit("probe_failed")
            return self._empty("rate_limited", "MODEL_RATE_LIMITED")
        history.append(self.clock())
        self._recent.move_to_end(actor_id)
        while len(self._recent) > 512:
            self._recent.popitem(last=False)

        async def run():
            audit("probe_started")
            try:
                result = await self._explore(protocol, base, model, key, check)
                check()
            except asyncio.CancelledError:
                audit("probe_failed")
                raise
            except Exception:
                audit("probe_failed")
                raise
            audit("probe_completed" if result["state"] == "completed" else "probe_failed")
            ttl = 21600 if result["state"] == "completed" and not result["error_code"] else 60
            self._cache[identity] = (self.clock() + ttl, deepcopy(result))
            self._cache.move_to_end(identity)
            while len(self._cache) > 512:
                self._cache.popitem(last=False)
            return result
        task = asyncio.create_task(run())
        self._inflight[identity] = task
        try:
            return deepcopy(await task)
        finally:
            if self._inflight.get(identity) is task:
                del self._inflight[identity]

    @staticmethod
    def _empty(state="unknown", code=None):
        return {"version": DISCOVERY_VERSION, "state": state, "options": [],
                "parameter_validation": "unknown", "requests": 0, "cached": False,
                "error_code": code, "provider_status": None}

    async def _explore(self, protocol, base, model, key, check):
        result = self._empty()
        deps = AgentDeps(omit_content_trace=True, check_state=check,
            budget=RunBudget(wire_limit=MAX_REQUESTS, deadline=datetime.now(timezone.utc) + timedelta(seconds=DEADLINE_SECONDS)))
        transport = self.transport_factory(base, deps, max_bytes=128 * 1024, inspect_parameter_errors=True)
        headers = {"Accept": "application/json"}
        if protocol == "anthropic":
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        elif protocol == "gemini":
            headers["x-goog-api-key"] = key
        else:
            headers["Authorization"] = "Bearer " + key
        semaphore = asyncio.Semaphore(2)
        failures = []

        async def send(client, method, path, payload=None):
            async with semaphore:
                deps.check()
                response = await client.request(method, base + path, json=payload, headers=headers)
                try:
                    data = response.json()
                except (ValueError, UnicodeDecodeError):
                    data = {}
                return response.status_code, data

        async def candidate(client, setting):
            try:
                path, payload = probe_payload(protocol, model, setting)
                status, data = await send(client, "POST", path, payload)
                if status == 200 and _generation_ok(protocol, data):
                    # The invalid-value control determines how trustworthy acceptance is.
                    return _option(setting, "request_accepted")
                if status in (400, 422) and _parameter_rejected(data):
                    return None
                failures.append("MODEL_PROVIDER_ERROR")
            except Exception as exc:
                failure = getattr(exc, "code", "MODEL_PROVIDER_ERROR")
                if failure in ("MODEL_CREDENTIAL_INVALID", "MODEL_RATE_LIMITED"):
                    raise
                failures.append(failure)
            return None

        try:
            async with asyncio.timeout(DEADLINE_SECONDS):
                async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                            timeout=httpx.Timeout(12)) as client:
                    # Details are optional, same destination only; no metadata-provided URLs are fetched.
                    declared = []
                    try:
                        status, details = await send(client, "GET", "/models/" + quote(model.removeprefix("models/"), safe="-_."))
                        declared = _enums(details) if status == 200 else []
                    except Exception as exc:
                        if getattr(exc, "code", "") in ("MODEL_CREDENTIAL_INVALID", "MODEL_RATE_LIMITED"):
                            raise
                    path, payload = probe_payload(protocol, model)
                    status, answer = await send(client, "POST", path, payload)
                    if status != 200 or not _generation_ok(protocol, answer):
                        result["error_code"] = "MODEL_PROVIDER_ERROR"
                        result["provider_status"] = status if 400 <= status <= 599 else None
                        return {**result, "requests": deps.budget.wire_requests}
                    path, payload = probe_payload(protocol, model, invalid=True)
                    status, detail = 0, {}
                    try:
                        status, detail = await send(client, "POST", path, payload)
                    except Exception as exc:
                        if getattr(exc, "code", "") in ("MODEL_CREDENTIAL_INVALID", "MODEL_RATE_LIMITED"):
                            raise
                    result["parameter_validation"] = ("rejected_invalid" if status in (400, 422) and _parameter_rejected(detail)
                                                       else "unconfirmed")
                    advertised = list(dict.fromkeys([*declared, *_enums(detail, error_response=status in (400, 422))]))
                    efforts = advertised or list(EFFORTS[protocol])
                    candidates = [{"mode": "effort", "effort": effort} for effort in efforts[:7]]
                    if protocol in ("anthropic", "gemini"):
                        candidates += [{"mode": "budget", "budget_tokens": 1024 if protocol == "anthropic" else 128},
                                       {"mode": "auto"}, {"mode": "off"}]
                    candidates = candidates[:MAX_REQUESTS - deps.budget.wire_requests]
                    for offset in range(0, len(candidates), 2):
                        items = await asyncio.gather(*(candidate(client, setting) for setting in candidates[offset:offset + 2]))
                        result["options"].extend(item for item in items if item is not None)
                    for option in result["options"]:
                        if option["settings"].get("effort") in declared:
                            option["evidence"] = "declared"
                    result["state"] = "completed" if result["options"] or not failures else "unknown"
                    if failures:
                        result["error_code"] = failures[0] if failures[0] in (
                            "MODEL_TIMEOUT", "MODEL_CONNECTION_ERROR", "MODEL_RESPONSE_TOO_LARGE") else "MODEL_PROVIDER_ERROR"
        except (TimeoutError, asyncio.TimeoutError):
            result["error_code"] = "MODEL_TIMEOUT"
        except Exception as exc:
            from ..agents.limits import to_controlled_error
            code = to_controlled_error(exc).code
            status = getattr(exc, "provider_status", None)
            if type(status) is int and 400 <= status <= 599:
                result["provider_status"] = status
            result["error_code"] = code if code in ("MODEL_CREDENTIAL_INVALID", "MODEL_RATE_LIMITED",
                "MODEL_TIMEOUT", "MODEL_CONNECTION_ERROR", "MODEL_RESPONSE_TOO_LARGE") else "MODEL_PROVIDER_ERROR"
        finally:
            result["requests"] = deps.budget.wire_requests
        return result
