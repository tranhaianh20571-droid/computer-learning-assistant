"""Strict search boundary types; raw provider messages are intentionally absent."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel, ConfigDict, Field, SecretStr, StrictInt, StrictStr, TypeAdapter,
    UUID4, field_validator, model_validator,
)

QUERY_CHAR_LIMIT = 1024
QUERY_BYTE_LIMIT = 4096
MAX_RESULTS = 10
DEFAULT_RESULTS = 10
EVIDENCE_ITEM_CHARS = 2000
EVIDENCE_TOTAL_CHARS = 8000
RESPONSE_BYTE_LIMIT = 1024 * 1024
SearchRequirement = Literal["optional", "fresh_evidence"]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class AnySearchCredentials(ClosedModel):
    api_key: SecretStr = Field(repr=False)

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, value: SecretStr) -> SecretStr:
        text = value.get_secret_value()
        if not text or text != text.strip() or len(text) > 4096:
            raise ValueError("invalid_api_key")
        if "*" in text or "\u2022" in text or any(unicodedata.category(c) in ("Cc", "Cs") for c in text):
            raise ValueError("invalid_api_key")
        return value


class SearchArgs(ClosedModel):
    query: StrictStr = Field(min_length=1, max_length=QUERY_CHAR_LIMIT)
    max_results: StrictInt = Field(default=DEFAULT_RESULTS, ge=1, le=MAX_RESULTS)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if any(unicodedata.category(c) == "Cc" for c in value):
            raise ValueError("invalid_search_query")
        value = value.strip()
        if not value:
            raise ValueError("invalid_search_query")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            raise ValueError("invalid_search_query") from None
        if size > QUERY_BYTE_LIMIT:
            raise ValueError("search_query_too_large")
        return value


class AnySearchRequest(SearchArgs):
    format: Literal["json"] = "json"
    zone: Literal["cn", "intl"] | None = None
    language: Literal["zh-CN", "en"] | None = None


class RawModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)


class RawSearchItem(RawModel):
    title: StrictStr
    url: StrictStr
    snippet: StrictStr | None = None
    content: StrictStr | None = None


class RawSearchMetadata(RawModel):
    total_results: StrictInt = Field(ge=0, le=MAX_RESULTS)
    search_time_ms: StrictInt = Field(ge=0)


class RawSearchData(RawModel):
    results: list[dict] = Field(max_length=MAX_RESULTS)
    metadata: RawSearchMetadata

    @model_validator(mode="after")
    def consistent_count(self) -> RawSearchData:
        if self.metadata.total_results != len(self.results):
            raise ValueError("invalid_search_metadata")
        return self


class RawSearchEnvelope(RawModel):
    code: StrictInt
    request_id: UUID4
    data: RawSearchData

    @field_validator("code")
    @classmethod
    def success_code(cls, value: int) -> int:
        if value != 0:
            raise ValueError("invalid_search_business_code")
        return value


class ExternalEvidence(ClosedModel):
    evidence_id: StrictStr = Field(min_length=1, max_length=64)
    query_id: UUID
    result_index: StrictInt = Field(ge=0, le=MAX_RESULTS - 1)
    source: Literal["external"] = "external"
    protocol: Literal["anysearch"] = "anysearch"
    title: StrictStr = Field(max_length=300)
    url: StrictStr = Field(min_length=1, max_length=2048)
    excerpt: StrictStr = Field(min_length=1, max_length=EVIDENCE_ITEM_CHARS)
    fetched_at: StrictStr = Field(min_length=1, max_length=40)
    domain: StrictStr = Field(min_length=1, max_length=253)
    truncated: bool = False

    @field_validator("fetched_at")
    @classmethod
    def utc_timestamp(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", value):
            raise ValueError("invalid_evidence_timestamp")
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("invalid_evidence_timestamp") from None
        if stamp.utcoffset() != timezone.utc.utcoffset(stamp):
            raise ValueError("invalid_evidence_timestamp")
        return value

    @model_validator(mode="after")
    def normalized_reference(self) -> ExternalEvidence:
        from .policy import normalize_url
        normalized = normalize_url(self.url)
        if normalized is None or normalized != (self.url, self.domain):
            raise ValueError("invalid_evidence_reference")
        if self.evidence_id != f"ev_{self.query_id.hex}_{self.result_index}":
            raise ValueError("invalid_evidence_id")
        if not self.excerpt.strip() or any(
            unicodedata.category(c) == "Cc" for c in self.title + self.excerpt
        ):
            raise ValueError("invalid_evidence_text")
        return self


class Succeeded(ClosedModel):
    state: Literal["succeeded"] = "succeeded"
    query_id: UUID
    external_request_id: UUID4
    results: tuple[ExternalEvidence, ...] = Field(min_length=1, max_length=MAX_RESULTS)
    dropped_count: StrictInt = Field(default=0, ge=0, le=MAX_RESULTS)

    @model_validator(mode="after")
    def consistent_evidence(self) -> Succeeded:
        if any(item.query_id != self.query_id for item in self.results):
            raise ValueError("invalid_evidence_query_id")
        if len({item.result_index for item in self.results}) != len(self.results):
            raise ValueError("duplicate_evidence_index")
        if len({item.url for item in self.results}) != len(self.results):
            raise ValueError("duplicate_evidence_url")
        if sum(len(item.excerpt) for item in self.results) > EVIDENCE_TOTAL_CHARS:
            raise ValueError("evidence_budget_exceeded")
        if len(self.results) + self.dropped_count > MAX_RESULTS:
            raise ValueError("invalid_evidence_count")
        return self


class Empty(ClosedModel):
    state: Literal["empty"] = "empty"
    query_id: UUID
    external_request_id: UUID4
    reason: Literal["no_hits", "no_usable_results"]


class Skipped(ClosedModel):
    state: Literal["skipped"] = "skipped"
    reason: Literal["disabled", "no_config", "offline_continue", "offline_policy", "query_unavailable"]
    prior_query_id: UUID | None = None


SearchErrorCode = Literal[
    "SEARCH_TIMEOUT", "SEARCH_CONNECTION_ERROR", "SEARCH_RATE_LIMITED",
    "SEARCH_CREDENTIAL_INVALID", "SEARCH_QUOTA_EXCEEDED",
    "SEARCH_PROVIDER_UNAVAILABLE", "SEARCH_OUTPUT_INVALID",
    "SEARCH_RESPONSE_TOO_LARGE", "SEARCH_REQUEST_REJECTED",
    "SEARCH_CONFIG_UNAVAILABLE", "SEARCH_BUDGET_EXCEEDED", "SEARCH_INTERRUPTED",
]
_SEARCH_ERROR_ADAPTER = TypeAdapter(SearchErrorCode)


class Failed(ClosedModel):
    state: Literal["failed"] = "failed"
    query_id: UUID
    error_code: SearchErrorCode
    retryable: bool
    external_request_id: UUID4 | None = None


SearchOutcome = Annotated[Succeeded | Empty | Skipped | Failed, Field(discriminator="state")]
SEARCH_OUTCOME_ADAPTER = TypeAdapter(SearchOutcome)


class SearchContinuation(ClosedModel):
    mode: Literal["with_evidence", "offline", "limited"]
    reason: Literal[
        "evidence_available", "no_hits", "no_usable_results", "disabled", "no_config",
        "offline_policy", "query_unavailable", "provider_failure", "budget_exhausted", "interrupted",
    ]
    evidence_required: bool

    @model_validator(mode="after")
    def consistent_mode(self) -> SearchContinuation:
        if self.mode == "limited" and not self.evidence_required:
            raise ValueError("invalid_search_continuation")
        if self.mode == "with_evidence" and self.reason != "evidence_available":
            raise ValueError("invalid_search_continuation")
        return self


class SearchProviderError(Exception):
    """Only controlled metadata survives the provider boundary (never raw errors)."""

    def __init__(self, code: SearchErrorCode, *, retryable: bool = False,
                 unsent: bool = False, status: int | None = None,
                 external_request_id: UUID | None = None,
                 retry_after: datetime | None = None) -> None:
        self.code = _SEARCH_ERROR_ADAPTER.validate_python(code)
        self.error_code = self.code
        self.retryable = bool(retryable)
        self.unsent = bool(unsent)
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        self.external_request_id = (
            external_request_id if isinstance(external_request_id, UUID)
            and external_request_id.version == 4 else None
        )
        self.retry_after = (
            retry_after.astimezone(timezone.utc) if isinstance(retry_after, datetime)
            and retry_after.tzinfo is not None else None
        )
        super().__init__(self.code)
