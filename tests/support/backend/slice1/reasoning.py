"""Versioned, local reasoning rules and strict protocol settings (no discovery I/O)."""
from __future__ import annotations

import json
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .errors import Slice1Error

RULES_VERSION = "reasoning-2026-10-20.1"
DEFAULT_SETTINGS = {"mode": "default", "show_summary": True}
DEFAULT_SETTINGS_JSON = json.dumps(DEFAULT_SETTINGS, separators=(",", ":"))


class ReasoningSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    mode: Literal["default", "effort", "budget", "auto", "off"] = "default"
    effort: str | None = Field(default=None, min_length=1, max_length=24, pattern=r"^[a-z][a-z0-9_]*$")
    budget_tokens: int | None = Field(default=None, ge=1, le=131072)
    show_summary: bool = True
    manual: bool = False
    discovered: bool = False

    @model_validator(mode="before")
    @classmethod
    def no_null_overrides(cls, value):
        if isinstance(value, dict) and any(v is None for v in value.values()):
            raise ValueError("reasoning_null_field")
        return value

    @model_validator(mode="after")
    def coherent_mode(self):
        if self.manual and self.discovered:
            raise ValueError("reasoning_override_conflict")
        if (self.mode == "effort") != (self.effort is not None):
            raise ValueError("reasoning_effort_mode_mismatch")
        if (self.mode == "budget") != (self.budget_tokens is not None):
            raise ValueError("reasoning_budget_mode_mismatch")
        return self

    def model_dump(self, **kwargs):
        # Legacy JSON and verification snapshots do not gain a false override.
        value = super().model_dump(**kwargs)
        if not self.manual:
            value.pop("manual", None)
        if not self.discovered:
            value.pop("discovered", None)
        return value


class ReasoningDescribeIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    protocol: Literal["openai", "openai_responses", "anthropic", "gemini"]
    endpoint: str = Field(min_length=1, max_length=500)
    model_name: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def safe_shape(self):
        # Parsing only: describe must never resolve DNS or contact the endpoint.
        parsed = urlsplit(self.endpoint)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or any(c.isspace() or ord(c) < 32 for c in self.endpoint)
                or self.model_name != self.model_name.strip()
                or any(ord(c) < 32 for c in self.model_name)):
            raise ValueError("invalid_reasoning_destination")
        _ = parsed.port
        return self


class ReasoningTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    config_version: int = Field(ge=1)


def _invalid(message="invalid_reasoning_settings"):
    return Slice1Error("invalid_field", message, 422)


def parse_settings(value=None) -> dict:
    if value is None:
        return dict(DEFAULT_SETTINGS)
    try:
        if isinstance(value, ReasoningSettings):
            return value.model_dump(exclude_none=True)
        return ReasoningSettings.model_validate(value).model_dump(exclude_none=True)
    except ValidationError:
        raise _invalid() from None


def stored_settings(raw) -> dict:
    """Legacy rows omit settings; invalid persisted overrides never silently run."""
    try:
        value = json.loads(raw or DEFAULT_SETTINGS_JSON) if isinstance(raw, (str, type(None))) else raw
    except (ValueError, TypeError):
        raise _invalid("stored_reasoning_settings_invalid") from None
    return parse_settings(value)


def _manual_controls(protocol: str) -> dict:
    """Protocol candidates only; never claim an unknown model supports them."""
    if protocol in ("openai","openai_responses"):
        return dict(control_kind="effort", supported_modes=["default", "effort", "off"],
            effort_values=["none", "minimal", "low", "medium", "high", "xhigh", "max"],
            budget=None, summary_supported=False)
    if protocol == "anthropic":
        return dict(control_kind="manual_anthropic", supported_modes=["default", "effort", "budget", "auto", "off"],
            effort_values=["low", "medium", "high", "max"],
            budget={"min": 1024, "max": 32000, "output_reserve": 4096}, summary_supported=True)
    return dict(control_kind="manual_gemini", supported_modes=["default", "effort", "budget", "auto", "off"],
        effort_values=["minimal", "low", "medium", "high"],
        budget={"min": 128, "max": 32768}, summary_supported=True)


def _selected_controls(capabilities: dict, settings: dict, protocol: str) -> dict:
    if settings.get("discovered"):
        if capabilities.get("endpoint_scope") == "unverified":
            raise _invalid("reasoning_protocol_mismatch")
        controls = _manual_controls(protocol)
        if settings["mode"] == "effort":
            controls["effort_values"] = [settings["effort"]]
        if settings["mode"] == "budget":
            controls["budget"] = {"min": 1, "max": 131072}
        return controls
    if not settings.get("manual"):
        return capabilities
    manual = capabilities.get("manual_controls")
    if capabilities["source"] != "unknown" or manual is None:
        raise _invalid("reasoning_manual_not_allowed")
    return manual


def describe_reasoning(protocol: str, endpoint: str, model_name: str) -> dict:
    try:
        ReasoningDescribeIn(protocol=protocol, endpoint=endpoint, model_name=model_name)
    except (ValidationError, ValueError):
        raise _invalid("invalid_reasoning_description") from None
    host = urlsplit(endpoint).hostname.lower()
    result = dict(schema_version=1, rules_version=RULES_VERSION, source="unknown",
        control_kind="unknown", supported_modes=["default"], effort_values=[], budget=None,
        summary_supported=False, endpoint_scope="unverified", default_behavior="provider_default")
    canonical_hosts = {"openai": "api.openai.com", "openai_responses":"api.openai.com", "anthropic": "api.anthropic.com",
                       "gemini": "generativelanguage.googleapis.com"}
    known_hosts = set(canonical_hosts.values()) | {"api.deepseek.com"}
    # A known provider with the wrong protocol is not treated as a compatible relay.
    if host in known_hosts and host != canonical_hosts[protocol] and not (protocol == "openai" and host == "api.deepseek.com"):
        return result
    result["endpoint_scope"] = "official" if host in known_hosts else "compatible_unverified"
    name = model_name.lower()
    base = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", name)

    def known(kind, modes, efforts=(), budget=None, summary=True):
        result.update(source="rule", control_kind=kind, supported_modes=["default", *modes],
            effort_values=list(efforts), budget=budget, summary_supported=summary)
        return result

    if protocol in ("openai","openai_responses"):
        if host == "api.deepseek.com" and base in {"deepseek-v4-flash", "deepseek-v4-pro", "deepseek-flash"}:
            return known("deepseek_effort", ["effort", "off"], ["low", "high", "max"])
        if base in {"deepseek-reasoner"} and host == "api.deepseek.com":
            return known("fixed", [], summary=True)
        efforts = None
        if base in {"o1", "o3", "o3-mini", "o4-mini"}:
            efforts = ["low", "medium", "high"]
        elif base in {"gpt-5", "gpt-5-mini", "gpt-5-nano"}:
            efforts = ["minimal", "low", "medium", "high"]
        elif base == "gpt-5.1":
            efforts = ["none", "low", "medium", "high"]
        elif base in {"gpt-5.2", "gpt-5.4", "gpt-5.5"}:
            efforts = ["none", "low", "medium", "high", "xhigh"]
        if efforts:
            return known("effort", ["effort", *(["off"] if "none" in efforts else [])], efforts, summary=protocol=="openai_responses")
    elif protocol == "anthropic":
        # Explicit model families, not a blanket claude-* capability guess.
        if base in {"claude-sonnet-4-6", "claude-opus-4-6"}:
            efforts = ["low", "medium", "high", *(["max"] if base == "claude-opus-4-6" else [])]
            return known("adaptive", ["effort", "auto", "off"], efforts)
        if base in {"claude-sonnet-4-20250514", "claude-opus-4-20250514", "claude-sonnet-4-5-20250929",
                    "claude-haiku-4-5-20251001", "claude-3-7-sonnet-20250219"}:
            return known("budget", ["budget", "off"], budget={"min": 1024, "max": 32000, "output_reserve": 4096})
    elif protocol == "gemini":
        if base in {"gemini-2.5-flash", "gemini-2.5-flash-preview-05-20"}:
            return known("budget", ["budget", "auto", "off"], budget={"min": 128, "max": 24576})
        if base in {"gemini-2.5-pro", "gemini-2.5-pro-preview-06-05"}:
            return known("budget", ["budget", "auto"], budget={"min": 128, "max": 32768})
        if base in {"gemini-3-pro-preview", "gemini-3.1-pro-preview"}:
            return known("level", ["effort"], ["low", "high"])
        if base == "gemini-3-flash-preview":
            return known("level", ["effort"], ["minimal", "low", "medium", "high"])
    result["manual_controls"] = _manual_controls(protocol)
    return result


def validate_settings(value, *, protocol: str, endpoint: str, model_name: str) -> dict:
    settings = parse_settings(value)
    # Default remains usable for legacy model names and endpoints without rewriting them.
    if settings["mode"] == "default" and not settings.get("manual"):
        return settings
    capabilities = _selected_controls(describe_reasoning(protocol, endpoint, model_name), settings, protocol)
    if settings["mode"] not in capabilities["supported_modes"]:
        raise _invalid("reasoning_mode_not_supported")
    if settings["mode"] == "effort" and settings["effort"] not in capabilities["effort_values"]:
        raise _invalid("reasoning_effort_not_supported")
    if settings["mode"] == "budget":
        boundary = capabilities["budget"]
        if boundary is None or not boundary["min"] <= settings["budget_tokens"] <= boundary["max"]:
            raise _invalid("reasoning_budget_out_of_range")
    return settings


def model_settings_for(protocol: str, endpoint: str, model_name: str, value=None) -> dict:
    settings = validate_settings(value, protocol=protocol, endpoint=endpoint, model_name=model_name)
    try:
        capabilities = describe_reasoning(protocol, endpoint, model_name)
    except Slice1Error:
        # Legacy default configurations continue to be validated by native transport.
        if settings["mode"] == "default":
            return {}
        raise
    capabilities = _selected_controls(capabilities, settings, protocol)
    mode, kind = settings["mode"], capabilities["control_kind"]
    mapped = {}
    if protocol in ("openai","openai_responses"):
        if kind == "deepseek_effort" and mode != "default":
            mapped["extra_body"] = {"thinking": {"type": "disabled" if mode == "off" else "enabled"}}
        if mode == "effort":
            mapped["openai_reasoning_effort"] = settings["effort"]
        elif mode == "off" and kind != "deepseek_effort":
            mapped["openai_reasoning_effort"] = "none"
        if protocol=="openai_responses" and settings["show_summary"] and capabilities["summary_supported"]:
            mapped["openai_reasoning_summary"]="auto"
    elif protocol == "anthropic":
        if mode == "effort":
            mapped.update(anthropic_thinking={"type": "adaptive"}, anthropic_effort=settings["effort"])
        elif mode == "auto":
            mapped["anthropic_thinking"] = {"type": "adaptive"}
        elif mode == "budget":
            budget = settings["budget_tokens"]
            mapped.update(anthropic_thinking={"type": "enabled", "budget_tokens": budget}, max_tokens=budget + 4096)
        elif mode == "off":
            mapped["anthropic_thinking"] = {"type": "disabled"}
    elif protocol == "gemini":
        thinking = {}
        if mode == "effort":
            thinking["thinking_level"] = settings["effort"].upper()
        elif mode in {"budget", "auto", "off"}:
            thinking["thinking_budget"] = settings.get("budget_tokens") if mode == "budget" else (-1 if mode == "auto" else 0)
        if capabilities["summary_supported"]:
            thinking["include_thoughts"] = settings["show_summary"]
        if thinking:
            mapped["google_thinking_config"] = thinking
    return mapped


def probe_settings_for(protocol: str, endpoint: str, model_name: str, value=None) -> dict:
    """Translate the same validated SDK controls into capability probe wire fields."""
    mapped = model_settings_for(protocol, endpoint, model_name, value)
    if not mapped:
        return {}
    if protocol=="openai_responses":
        reasoning={}
        if "openai_reasoning_effort" in mapped:
            reasoning["effort"]=mapped["openai_reasoning_effort"]
        if "openai_reasoning_summary" in mapped:
            reasoning["summary"]=mapped["openai_reasoning_summary"]
        return {"reasoning":reasoning,"max_output_tokens":4096} if reasoning else {}
    if protocol == "openai":
        fields = dict(mapped.get("extra_body", {}))
        if "openai_reasoning_effort" in mapped:
            fields["reasoning_effort"] = mapped["openai_reasoning_effort"]
        return {**fields, "max_tokens": 4096} if fields else {}
    if protocol == "anthropic":
        fields = {"max_tokens": mapped.get("max_tokens", 4096)}
        if "anthropic_thinking" in mapped:
            fields["thinking"] = mapped["anthropic_thinking"]
        if "anthropic_effort" in mapped:
            fields["output_config"] = {"effort": mapped["anthropic_effort"]}
        return fields
    if protocol == "gemini":
        names = {"thinking_level": "thinkingLevel", "thinking_budget": "thinkingBudget",
                 "include_thoughts": "includeThoughts"}
        thinking = {names[key]: item for key, item in mapped.get("google_thinking_config", {}).items()}
        limit = max(4096, thinking.get("thinkingBudget", 0) + 4096)
        return {"generationConfig": {"thinkingConfig": thinking, "maxOutputTokens": limit}} if thinking else {}
    return {}


async def probe_saved_reasoning(configs, config_id: str, *, actor_id: str, is_admin: bool,
                                config_version: int) -> dict:
    """One bounded synthetic SDK request; acceptance never proves effective effort."""
    import asyncio
    from datetime import datetime, timedelta, timezone
    from pydantic_ai import Agent
    from ..agents.contracts import AgentDeps, RunBudget
    from ..agents.native import native_model_session, ReasoningParameterError
    from .adapters.base import AdapterBase, PROBE_TEXT

    snapshot = configs.reserve_reasoning_probe(config_id, actor_id=actor_id, is_admin=is_admin,
                                              expected_version=config_version)
    source = AdapterBase(endpoint=snapshot["endpoint"], model_name=snapshot["model_name"],
        credentials={},
        reasoning_settings=snapshot["reasoning_settings"], timeout=15.0, max_response_bytes=128 * 1024)
    source.protocol = snapshot["protocol"]
    def check():
        configs.check_reasoning_probe(config_id, actor_id=actor_id, expected_version=config_version)
    deps = AgentDeps(actor_id=actor_id, config_id=config_id, config_version=config_version,
        budget=RunBudget(request_limit=1, wire_limit=1, deadline=datetime.now(timezone.utc) + timedelta(seconds=20)),
        check_state=check, omit_content_trace=True)
    outcome = {"evidence": "unknown", "error_code": None}
    try:
        source.credentials = configs.resolve_reasoning_credentials(config_id, actor_id=actor_id, expected_version=config_version)
        async with asyncio.timeout(20):
            async with native_model_session(source, deps) as model:
                agent = Agent(model, output_type=str, retries=0)
                agent.instrument = False
                # Budget profiles retain their reserved answer space; other modes get a finite cap.
                await agent.run(PROBE_TEXT, model_settings={"max_tokens": model.settings.get("max_tokens", 4096)})
        outcome = {"evidence": "request_accepted", "error_code": None}
    except ReasoningParameterError:
        outcome = {"evidence": "unsupported", "error_code": "MODEL_CAPABILITY_UNAVAILABLE"}
    except (TimeoutError, asyncio.TimeoutError):
        outcome = {"evidence": "unknown", "error_code": "MODEL_TIMEOUT"}
    except Slice1Error:
        configs.finish_reasoning_probe(config_id, actor_id=actor_id, expected_version=config_version,
            result={"evidence": "unknown", "error_code": "MODEL_PROVIDER_ERROR"})
        raise
    except Exception as exc:
        # Anthropic wraps transport exceptions; preserve the controlled cause.
        from ..agents.limits import to_controlled_error
        failure = to_controlled_error(exc)
        controlled = failure.code
        allowed = {"MODEL_TIMEOUT", "MODEL_CONNECTION_ERROR", "MODEL_RATE_LIMITED", "MODEL_CREDENTIAL_INVALID",
                   "MODEL_PROVIDER_ERROR", "MODEL_RESPONSE_TOO_LARGE"}
        outcome = ({"evidence": "unsupported", "error_code": "MODEL_CAPABILITY_UNAVAILABLE"}
                   if isinstance(failure, ReasoningParameterError) else
                   {"evidence": "unknown", "error_code": controlled if controlled in allowed else "MODEL_PROVIDER_ERROR"})
    return configs.finish_reasoning_probe(config_id, actor_id=actor_id, expected_version=config_version, result=outcome)
