"""Explicit opt-in live check; connection secrets are read from stdin only."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server/backend"))

from app.agents.contracts import AgentDeps, RunBudget
from app.agents.native import SafeModelTransport, native_model_session
from app.slice1.adapters.base import AdapterBase
from app.slice1.model_catalog import normalize_base
from app.slice1.reasoning_discovery import ReasoningDiscovery, probe_payload
import httpx2 as httpx


async def run(payload):
    results = []
    for case in payload["cases"]:
        protocol, endpoint, key = (case[name] for name in ("protocol", "endpoint", "key"))
        result = {"case": case["name"], "protocol": protocol}
        try:
            base = normalize_base(endpoint, protocol)
            headers = ({"x-api-key": key, "anthropic-version": "2023-06-01"} if protocol == "anthropic"
                       else {"x-goog-api-key": key} if protocol == "gemini"
                       else {"Authorization": "Bearer " + key})
            deps = AgentDeps(omit_content_trace=True, budget=RunBudget(wire_limit=2))
            async with httpx.AsyncClient(transport=SafeModelTransport(base, deps, max_bytes=1024 * 1024),
                                        trust_env=False, timeout=15) as client:
                response = await client.get(base + "/models", headers=headers)
                data = response.json()
            rows = data.get("data", data.get("models", []))
            models = [item.get("id", item.get("name", "")).removeprefix("models/") for item in rows if isinstance(item, dict)]
            models = [name for name in models if re.fullmatch(r"[a-zA-Z0-9_./:-]{1,120}", name) and not name.startswith("sk-")]
            result["catalog_count"] = len(models)
            if payload.get("diagnostic"):
                path, body = probe_payload(protocol, case["model"])
                deps = AgentDeps(omit_content_trace=True, budget=RunBudget(wire_limit=2))
                async with httpx.AsyncClient(transport=SafeModelTransport(base, deps,
                    max_bytes=128 * 1024, inspect_parameter_errors=True), trust_env=False, timeout=30) as client:
                    response = await client.post(base + path, headers=headers, json=body)
                result["http_status"] = response.status_code
                result["content_type"] = response.headers.get("content-type", "").split(";")[0]
                try:
                    reply = response.json()
                except ValueError:
                    reply = {}
                result["response_fields"] = [name for name in reply if re.fullmatch(r"[A-Za-z_]{1,30}", name)][:20] if isinstance(reply, dict) else []
                detail = reply.get("error", {}) if isinstance(reply, dict) else {}
                detail = detail if isinstance(detail, dict) else {}
                for field in ("code", "param", "type"):
                    item = detail.get(field)
                    if isinstance(item, str) and re.fullmatch(r"[a-zA-Z0-9_.]{1,60}", item):
                        result["provider_error_" + field] = item
                message = detail.get("message", "")
                if isinstance(message, str):
                    result["error_markers"] = [word for word in ("instructions", "stream", "store", "max_output_tokens", "max_tokens", "unsupported", "required", "model", "quota", "budget") if word in message.lower()]
            elif payload.get("catalog_only"):
                result["models"] = models[:80]
            else:
                model = case.get("model")
                if not model or model not in models:
                    raise ValueError("requested_model_not_in_catalog")
                result["model"] = model
                discovery = ReasoningDiscovery()
                arguments = dict(actor_id="authorized-live-check", protocol=protocol, endpoint=endpoint, model=model, key=key)
                result["discovery"] = await discovery.detect(**arguments)
                repeat = await discovery.detect(**arguments)
                result["cache_reused"] = repeat["cached"]
                options = result["discovery"]["options"]
                preferred = next((item for item in options if item["value"] == "effort:low"), options[0] if options else None)
                if preferred:
                    from pydantic_ai import Agent
                    source = AdapterBase(endpoint=endpoint, model_name=model, credentials={"api_key": key},
                                         reasoning_settings=preferred["settings"], timeout=25, max_response_bytes=128 * 1024)
                    source.protocol = protocol
                    deps = AgentDeps(omit_content_trace=True, budget=RunBudget(wire_limit=2, request_limit=1,
                        deadline=datetime.now(timezone.utc) + timedelta(seconds=30)))
                    async with asyncio.timeout(30):
                        async with native_model_session(source, deps) as sdk_model:
                            agent = Agent(sdk_model, output_type=str, retries=0)
                            agent.instrument = False
                            answer = await agent.run("Reply OK.", model_settings={"max_tokens": max(256, preferred["settings"].get("budget_tokens", 0) + 128)})
                    result["native_selected"] = preferred["value"]
                    result["native_ok"] = bool(answer.output.strip())
        except Exception as exc:
            result["error_code"] = getattr(exc, "code", "LIVE_CHECK_FAILED")
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    output = ROOT / ".backup/reasoning-discovery"
    output.mkdir(parents=True, exist_ok=True)
    (output / ("diagnostic.json" if payload.get("diagnostic") else "catalog.json" if payload.get("catalog_only") else "live-results.json")).write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf8")


if __name__ == "__main__":
    asyncio.run(run(json.loads(sys.stdin.readline())))
