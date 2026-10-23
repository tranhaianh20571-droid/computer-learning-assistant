"""Strict search contracts and pure evidence policy; no implicit provider fallback."""
from .contracts import (
    AnySearchCredentials, AnySearchRequest, ClosedModel, Empty, ExternalEvidence, Failed,
    RawSearchData, RawSearchEnvelope, RawSearchItem, RawSearchMetadata, SearchArgs,
    SearchContinuation, SearchErrorCode, SearchOutcome, SearchProviderError,
    SearchRequirement, Skipped, Succeeded, SEARCH_OUTCOME_ADAPTER,
)
from .policy import (
    DEFAULT_POLICY, SearchPolicy, decide_continuation, derive_query, evidence_payload,
    external_refs, normalize_envelope, normalize_url, requirement_for,
)

__all__ = [
    "AnySearchCredentials", "AnySearchRequest", "ClosedModel", "Empty", "ExternalEvidence",
    "Failed", "RawSearchData", "RawSearchEnvelope", "RawSearchItem", "RawSearchMetadata",
    "SearchArgs", "SearchContinuation", "SearchErrorCode", "SearchOutcome", "SearchProviderError",
    "SearchRequirement", "Skipped", "Succeeded", "SEARCH_OUTCOME_ADAPTER", "DEFAULT_POLICY",
    "SearchPolicy", "decide_continuation", "derive_query", "evidence_payload", "external_refs",
    "normalize_envelope", "normalize_url", "requirement_for",
]
