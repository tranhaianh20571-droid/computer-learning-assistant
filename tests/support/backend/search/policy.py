"""Pure query policy and temporary evidence normalization (no network or logging)."""
from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit
from uuid import UUID

from pydantic import ValidationError

from .contracts import (
    EVIDENCE_ITEM_CHARS, EVIDENCE_TOTAL_CHARS, RESPONSE_BYTE_LIMIT,
    Empty, ExternalEvidence, Failed, RawSearchEnvelope, RawSearchItem,
    SearchArgs, SearchContinuation, SearchOutcome, SearchRequirement, Skipped, Succeeded,
)


@dataclass(frozen=True)
class SearchPolicy:
    title_chars: int = 300
    excerpt_chars: int = EVIDENCE_ITEM_CHARS
    total_excerpt_chars: int = EVIDENCE_TOTAL_CHARS
    response_bytes: int = RESPONSE_BYTE_LIMIT
    hard_timeout_seconds: float = 20.0
    window_seconds: float = 120.0
    logical_limit: int = 3
    wire_limit: int = 6
    retry_limit: int = 5
    json_depth: int = 32
    json_nodes: int = 20000
    topic_chars: int = 256

    def __post_init__(self) -> None:
        limits = ((self.title_chars, 300), (self.excerpt_chars, EVIDENCE_ITEM_CHARS),
                  (self.total_excerpt_chars, EVIDENCE_TOTAL_CHARS),
                  (self.response_bytes, RESPONSE_BYTE_LIMIT), (self.json_depth, 32),
                  (self.json_nodes, 20000), (self.topic_chars, 256))
        if any(type(value) is not int or not 1 <= value <= maximum for value, maximum in limits):
            raise ValueError("invalid_search_policy")
        if not 0 < self.hard_timeout_seconds <= 20 or not 0 < self.window_seconds <= 120:
            raise ValueError("invalid_search_policy")
        if any(type(value) is not int or not 1 <= value <= maximum for value, maximum in
               ((self.logical_limit, 3), (self.wire_limit, 6), (self.retry_limit, 5))):
            raise ValueError("invalid_search_policy")


DEFAULT_POLICY = SearchPolicy()
_CREDENTIAL_PARAMS = {
    "key", "apikey", "token", "accesstoken", "refreshtoken", "idtoken", "password",
    "passwd", "pwd", "secret", "clientsecret", "authorization", "auth", "credential",
    "credentials", "xamzcredential", "xamzsecuritytoken", "xgoogcredential", "sessionid",
}
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".onion", ".invalid")


def normalize_url(value: str) -> tuple[str, str] | None:
    """Validate an evidence reference without resolving or fetching its host."""
    if not isinstance(value, str) or not value or len(value) > 2048 or value != value.strip():
        return None
    decoded = unquote(value)
    if any(unicodedata.category(c).startswith("C") or c.isspace() for c in value):
        return None
    if any(unicodedata.category(c).startswith("C") for c in decoded) or "\\" in decoded:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            return None
        host = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if not host or len(host) > 253 or "%" in host:
            return None
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host or host.endswith(_LOCAL_SUFFIXES):
                return None
            if re.fullmatch(r"[\d.]+", host) or re.match(r"^0x[0-9a-f]+(?:\.|$)", host):
                return None
            labels = host.split(".")
            if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
                return None
        else:
            if (not ip.is_global or ip.is_multicast or ip.is_reserved
                    or (ip.version == 6 and ip.ipv4_mapped is not None and not ip.ipv4_mapped.is_global)
                    or (ip.version == 6 and ip.sixtofour is not None and not ip.sixtofour.is_global)):
                return None
            host = ip.compressed
        for key, _ in parse_qsl(parts.query, keep_blank_values=True, max_num_fields=128):
            key = re.sub(r"[^a-z0-9]", "", key.lower())
            if key in _CREDENTIAL_PARAMS or key.endswith(("apikey", "accesstoken", "password", "clientsecret")):
                return None
        scheme = parts.scheme.lower()
        authority = f"[{host}]" if ":" in host else host
        if port is not None and (scheme, port) not in (("https", 443), ("http", 80)):
            authority += f":{port}"
        normalized = urlunsplit((scheme, authority, parts.path, parts.query, ""))
        if len(normalized) > 2048:
            return None
        return normalized, host
    except (ValueError, UnicodeError):
        return None


def _clean(text: str) -> str:
    return "".join(" " if unicodedata.category(c).startswith("C") else c for c in text).strip()


def normalize_envelope(envelope: RawSearchEnvelope, *, query_id: UUID,
                       fetched_at: datetime | None = None,
                       policy: SearchPolicy = DEFAULT_POLICY) -> SearchOutcome:
    """Preserve valid hits, original indices and controlled empty/invalid distinctions."""
    if not isinstance(query_id, UUID):
        raise TypeError("query_id must be a UUID")
    # Revalidate raw models too: unlike public contracts they are mutable.
    try:
        envelope = RawSearchEnvelope.model_validate(envelope.model_dump())
    except (ValidationError, AttributeError):
        return Failed(query_id=query_id, error_code="SEARCH_OUTPUT_INVALID", retryable=False)
    external_id = envelope.request_id
    if not envelope.data.results:
        return Empty(query_id=query_id, external_request_id=external_id, reason="no_hits")
    stamp = fetched_at or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise ValueError("fetched_at must be timezone aware")
    timestamp = stamp.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    evidence: list[ExternalEvidence] = []
    seen: set[str] = set()
    structural_bad = 0
    remaining = policy.total_excerpt_chars
    for index, raw in enumerate(envelope.data.results):
        try:
            item = RawSearchItem.model_validate(raw)
        except ValidationError:
            structural_bad += 1
            continue
        reference = normalize_url(item.url)
        if reference is None or reference[0] in seen:
            continue
        title = _clean(item.title)
        excerpt = _clean(item.snippet or "") or _clean(item.content or "")
        if not excerpt or remaining <= 0:
            continue
        limit = min(policy.excerpt_chars, remaining)
        clipped = excerpt[:limit].strip()
        if not clipped:
            continue
        evidence.append(ExternalEvidence(
            evidence_id=f"ev_{query_id.hex}_{index}", query_id=query_id, result_index=index,
            title=title[:policy.title_chars], url=reference[0], domain=reference[1],
            excerpt=clipped, fetched_at=timestamp,
            truncated=len(title) > policy.title_chars or len(excerpt) > limit,
        ))
        seen.add(reference[0])
        remaining -= len(clipped)
    if evidence:
        return Succeeded(query_id=query_id, external_request_id=external_id,
                         results=tuple(evidence), dropped_count=len(envelope.data.results) - len(evidence))
    if structural_bad == len(envelope.data.results):
        return Failed(query_id=query_id, external_request_id=external_id,
                      error_code="SEARCH_OUTPUT_INVALID", retryable=False)
    return Empty(query_id=query_id, external_request_id=external_id, reason="no_usable_results")


def evidence_payload(outcome: SearchOutcome) -> dict:
    """Sensitive, temporary model context. This projection must never be logged."""
    payload: dict = {"state": outcome.state, "evidence": []}
    if isinstance(outcome, Succeeded):
        payload["evidence"] = [item.model_dump(mode="json", include={
            "evidence_id", "source", "title", "url", "excerpt", "fetched_at", "truncated",
        }) for item in outcome.results]
    elif isinstance(outcome, Failed):
        payload["error_code"] = outcome.error_code
    else:
        payload["reason"] = outcome.reason
    return payload


def external_refs(outcome: SearchOutcome) -> list[dict]:
    """Body-free business manifest references; domains are not telemetry fields."""
    if not isinstance(outcome, Succeeded):
        return []
    return [item.model_dump(mode="json", include={
        "query_id", "result_index", "evidence_id", "source", "protocol", "fetched_at", "domain",
    }) for item in outcome.results]


def decide_continuation(outcome: SearchOutcome, *, requirement: SearchRequirement) -> SearchContinuation:
    if requirement not in ("optional", "fresh_evidence"):
        raise ValueError("invalid_search_requirement")
    required = requirement == "fresh_evidence"
    if isinstance(outcome, Succeeded):
        return SearchContinuation(mode="with_evidence",
                                  reason="evidence_available", evidence_required=required)
    if isinstance(outcome, Failed):
        reason = {"SEARCH_BUDGET_EXCEEDED": "budget_exhausted", "SEARCH_INTERRUPTED": "interrupted"}.get(
            outcome.error_code, "provider_failure")
    else:
        reason = "offline_policy" if outcome.reason == "offline_continue" else outcome.reason
    return SearchContinuation(mode="offline", reason=reason,
                              evidence_required=required)


def derive_query(topic: object) -> str | None:
    """Use one short explicit topic unchanged; never truncate arbitrary user prose."""
    if type(topic) is not str or any(unicodedata.category(c).startswith("C") for c in topic):
        return None
    candidate = " ".join(topic.split())
    if not candidate or len(candidate) > DEFAULT_POLICY.topic_chars or len(candidate.split()) > 40:
        return None
    if re.search(r"https?://|www\.|[\w.+-]+@[\w.-]+|(?:api[_ -]?key|password|bearer|token|密码|密钥)\s*[:=]", candidate, re.I):
        return None
    if len(re.findall(r"[。！？!?;；]", candidate)) > 2:
        return None
    try:
        return SearchArgs(query=candidate).query
    except ValidationError:
        return None


def requirement_for(text: object) -> SearchRequirement:
    if not isinstance(text, str):
        return "optional"
    if re.search(r"今天|今日|最新|目前|当前|现在|实时|截至|近期|本周|本月|今年|\b(?:today|latest|current|currently|now|recent|up.to.date|this\s+(?:week|month|year))\b", text, re.I):
        return "fresh_evidence"
    return "optional"
