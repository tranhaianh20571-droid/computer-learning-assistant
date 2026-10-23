"""Adversarial contracts and normalization using synthetic, body-free fixtures."""
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from backend.search.contracts import (
    AnySearchCredentials, AnySearchRequest, Empty, ExternalEvidence, Failed,
    RawSearchEnvelope, SearchArgs, SearchContinuation, SearchProviderError, Skipped,
    Succeeded, SEARCH_OUTCOME_ADAPTER,
)
from backend.search.policy import (
    SearchPolicy, decide_continuation, derive_query, evidence_payload,
    external_refs, normalize_envelope, normalize_url, requirement_for,
)


def envelope(results, **metadata):
    return RawSearchEnvelope.model_validate_json(json.dumps({
        "code": 0, "request_id": str(uuid4()), "message": "ignored synthetic message",
        "data": {"results": results, "metadata": {
            "total_results": len(results), "search_time_ms": 1, **metadata,
        }},
    }))


def hit(index=0, **updates):
    return {"title": "Synthetic title", "url": f"https://example.org/item/{index}",
            "snippet": "Synthetic excerpt", **updates}


@pytest.mark.parametrize("value", [True, False, 0, 11, 1.0, 1.5, "1", "5", None])
def test_max_results_requires_exact_bounded_integer(value):
    with pytest.raises(ValidationError):
        SearchArgs(query="topic", max_results=value)


@pytest.mark.parametrize("value", [None, 1, True, {}, [], "", "   ", "\n", "a\nb", "a\x00b", "x" * 1025, "\ud800"])
def test_query_rejects_non_string_empty_control_and_surrogate(value):
    with pytest.raises(ValidationError):
        SearchArgs(query=value)


def test_closed_frozen_requests_and_schema_types():
    args = SearchArgs(query=" topic ")
    assert args.query == "topic" and args.max_results == 10
    schema = SearchArgs.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["max_results"]["type"] == "integer"
    assert schema["properties"]["max_results"]["maximum"] == 10
    with pytest.raises(ValidationError):
        SearchArgs(query="topic", arbitrary="rejected")
    with pytest.raises(ValidationError):
        args.query = "changed"
    assert AnySearchRequest(query="topic").format == "json"
    with pytest.raises(ValidationError):
        AnySearchRequest(query="topic", language="zh")


@pytest.mark.parametrize("value", ["", " ", " key", "key ", "a\nb", "x" * 4097])
def test_credentials_reject_invalid_secret_values(value):
    with pytest.raises(ValidationError):
        AnySearchCredentials(api_key=SecretStr(value))


def test_credentials_and_provider_error_hide_secrets():
    credentials = AnySearchCredentials(api_key=SecretStr("synthetic-test-key"))
    assert "synthetic-test-key" not in repr(credentials)
    assert "synthetic-test-key" not in credentials.model_dump_json()
    error = SearchProviderError("SEARCH_RATE_LIMITED", status=429, retry_after=datetime.now(timezone.utc),
                                external_request_id=uuid4())
    assert str(error) == "SEARCH_RATE_LIMITED"
    assert error.status == 429 and error.retry_after.tzinfo == timezone.utc
    with pytest.raises(ValidationError):
        SearchProviderError("raw vendor message")


@pytest.mark.parametrize("url", [
    "file:///tmp/test", "javascript:alert(1)", "ftp://example.org/x", "https://localhost/x",
    "https://127.0.0.1/x", "http://10.0.0.1/x", "http://169.254.169.254/x", "http://[::1]/x",
    "http://[::ffff:127.0.0.1]/x", "http://224.0.0.1/x", "http://[ff0e::1]/x", "https://example.local/x", "http://2130706433/x",
    "http://0177.0.0.1/x", "http://0x7f000001/x", "https://user:pass@example.org/x",
    "https://example.org/x?api_key=synthetic", "https://example.org/x?access_token=synthetic",
    "https://example.org/x?X-Amz-Credential=synthetic", "https://example.org/\nx",
    "https://example.org/%0ax", "https://example.org/%5Cx", "https://exa_mple.org/x",
    " https://example.org/x", "https://example.org:99999/x", "https://example.org:0/x",
    "https://example.org/x?" + "&".join("x=1" for _ in range(129)),
])
def test_unsafe_reference_is_dropped(url):
    assert normalize_url(url) is None


def test_reference_preserves_public_query_and_removes_fragment():
    assert normalize_url("HTTPS://EXAMPLE.ORG:443/path?a=one%20two#section") == (
        "https://example.org/path?a=one%20two", "example.org")
    assert normalize_url("https://example.org/path?signature=synthetic-public") is not None
    assert normalize_url("https://8.8.8.8/path") == ("https://8.8.8.8/path", "8.8.8.8")


def test_normalization_mixed_hits_preserves_original_indices_and_boundaries():
    query_id = uuid4()
    outcome = normalize_envelope(envelope([
        {"title": "wrong", "url": "https://example.org/x", "snippet": 3},
        hit(1, title="T\n" + "x" * 400, snippet="S\x00" + "x" * 3000, ignored="discarded"),
        hit(1, url="https://example.org/item/1#duplicate"),
        hit(3, url="http://127.0.0.1/private"),
        hit(4, snippet=None, content=" fallback content "),
    ]), query_id=query_id, fetched_at=datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8))))
    assert isinstance(outcome, Succeeded)
    assert [item.result_index for item in outcome.results] == [1, 4]
    assert outcome.dropped_count == 3
    assert len(outcome.results[0].excerpt) == 2000 and outcome.results[0].truncated
    assert len(outcome.results[0].title) == 300
    assert outcome.results[1].excerpt == "fallback content"
    assert outcome.results[0].fetched_at == "2025-12-31T16:00:00.000Z"
    assert outcome.results[0].evidence_id == f"ev_{query_id.hex}_1"
    assert isinstance(outcome.results, tuple)
    assert all(item.query_id == query_id for item in outcome.results)


def test_total_excerpt_budget_and_no_content_in_refs():
    outcome = normalize_envelope(envelope([hit(i, snippet="x" * 2500) for i in range(6)]), query_id=uuid4())
    assert isinstance(outcome, Succeeded)
    assert len(outcome.results) == 4 and outcome.dropped_count == 2
    assert sum(len(item.excerpt) for item in outcome.results) == 8000
    refs = external_refs(outcome)
    assert set(refs[0]) == {"query_id", "result_index", "evidence_id", "source", "protocol", "fetched_at", "domain"}
    assert not {"url", "title", "excerpt", "content"} & set(refs[0])
    payload = evidence_payload(outcome)
    assert len(payload["evidence"]) == 4 and payload["state"] == "succeeded"
    assert "excerpt" in payload["evidence"][0] and "ignored" not in json.dumps(payload)


@pytest.mark.parametrize("results,expected", [
    ([], "no_hits"), ([hit(snippet=None)], "no_usable_results"),
    ([hit(url="http://127.0.0.1/x")], "no_usable_results"),
])
def test_empty_distinctions(results, expected):
    outcome = normalize_envelope(envelope(results), query_id=uuid4())
    assert isinstance(outcome, Empty) and outcome.reason == expected
    assert evidence_payload(outcome)["evidence"] == [] and external_refs(outcome) == []


def test_all_structural_bad_is_failure_but_single_bad_does_not_poison_good():
    broken = [{"url": "https://example.org/x"}, {"title": 1, "url": "https://example.org/x"}]
    outcome = normalize_envelope(envelope(broken), query_id=uuid4())
    assert isinstance(outcome, Failed) and outcome.error_code == "SEARCH_OUTPUT_INVALID"
    assert isinstance(normalize_envelope(envelope(broken + [hit()]), query_id=uuid4()), Succeeded)


def test_raw_metadata_strictness_and_mutation_revalidation():
    with pytest.raises(ValidationError):
        envelope([hit()], total_results=0)
    with pytest.raises(ValidationError):
        envelope([hit()], search_time_ms=True)
    raw = envelope([hit()])
    raw.code = 400
    assert isinstance(normalize_envelope(raw, query_id=uuid4()), Failed)


@pytest.mark.parametrize("requirement", ["optional", "fresh_evidence"])
def test_success_uses_evidence_without_keyword_restriction(requirement):
    outcome = normalize_envelope(envelope([hit()]), query_id=uuid4())
    continuation = decide_continuation(outcome, requirement=requirement)
    assert continuation.mode == "with_evidence"
    assert continuation.evidence_required == (requirement == "fresh_evidence")


@pytest.mark.parametrize("outcome,reason", [
    (Skipped(reason="offline_continue"), "offline_policy"),
    (Skipped(reason="query_unavailable"), "query_unavailable"),
    (Failed(query_id=uuid4(), error_code="SEARCH_TIMEOUT", retryable=False), "provider_failure"),
    (Failed(query_id=uuid4(), error_code="SEARCH_BUDGET_EXCEEDED", retryable=False), "budget_exhausted"),
    (Failed(query_id=uuid4(), error_code="SEARCH_INTERRUPTED", retryable=False), "interrupted"),
])
def test_failure_continuation_preserves_requirement(outcome, reason):
    assert decide_continuation(outcome, requirement="optional").mode == "offline"
    fresh = decide_continuation(outcome, requirement="fresh_evidence")
    assert fresh.mode == "offline" and fresh.evidence_required
    assert decide_continuation(outcome, requirement="optional").reason == reason


def test_public_outcome_round_trip_and_cross_field_guards():
    outcome = normalize_envelope(envelope([hit()]), query_id=uuid4())
    assert SEARCH_OUTCOME_ADAPTER.validate_json(outcome.model_dump_json()) == outcome
    item = outcome.results[0].model_dump()
    for update in ({"domain": "other.example.org"}, {"evidence_id": "provider-id"},
                   {"fetched_at": "2026-01-01T00:00:00+08:00"}, {"truncated": 1}):
        with pytest.raises(ValidationError):
            ExternalEvidence.model_validate({**item, **update})
    assert SearchContinuation(mode="offline", reason="no_hits", evidence_required=True).mode == "offline"
    with pytest.raises(ValidationError):
        SearchContinuation(mode="limited", reason="no_hits", evidence_required=False)
    with pytest.raises(ValidationError):
        Succeeded(query_id=uuid4(), external_request_id=uuid4(), results=outcome.results)


@pytest.mark.parametrize("topic", [None, {}, ["topic"], "", "x" * 257, "two\nlines",
    "https://example.org/topic", "someone@example.org", "api_key=synthetic", "password: synthetic",
    "Long. " * 60, "a! b! c!"])
def test_unreliable_topics_skip_without_truncation(topic):
    assert derive_query(topic) is None


def test_query_derivation_and_requirement_are_deterministic():
    assert derive_query("  勾股定理  学习 ") == "勾股定理 学习"
    assert requirement_for("学习勾股定理") == "optional"
    assert requirement_for("查询最新课程") == "fresh_evidence"
    assert requirement_for("current learning resources") == "fresh_evidence"
    assert requirement_for(None) == "optional"
    with pytest.raises(ValueError):
        SearchPolicy(excerpt_chars=2001)
