"""Synthetic MiniMax subtitle parsing, proxy DNS and probe boundaries."""
import socket

import httpx
import pytest

from backend.slice1.adapters.base import AdapterError
from backend.slice1.adapters.minimax import (
    MiniMaxTTSAdapter, _fetch_subtitle_file, _normalize_subtitle, _validate_subtitle_endpoint)


def dns(monkeypatch, *addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
        for address in addresses])


@pytest.mark.parametrize("address", ["198.18.0.42", "198.19.255.254", "8.8.8.8", "2606:4700:4700::1111"])
def test_https_subtitle_name_accepts_public_or_proxy_dns(monkeypatch, address):
    dns(monkeypatch, address)
    _validate_subtitle_endpoint("https://subtitle.synthetic.test/cues?signature=synthetic")


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.0.1",
    "169.254.169.254", "::1", "::ffff:127.0.0.1", "fc00::1", "fe80::1"])
def test_other_nonpublic_subtitle_targets_stay_blocked(monkeypatch, address):
    dns(monkeypatch, address)
    with pytest.raises(AdapterError):
        _validate_subtitle_endpoint("https://subtitle.synthetic.test/cues")


@pytest.mark.parametrize("url", ["http://subtitle.synthetic.test/cues", "https://198.18.0.42/cues",
    "https://user:synthetic@subtitle.synthetic.test/cues", "https://metadata.google.internal/cues"])
def test_scheme_userinfo_and_literal_proxy_addresses_are_blocked(monkeypatch, url):
    dns(monkeypatch, "198.18.0.42")
    with pytest.raises(AdapterError):
        _validate_subtitle_endpoint(url)


def test_mixed_dns_does_not_hide_a_private_target(monkeypatch):
    dns(monkeypatch, "198.18.0.42", "127.0.0.1")
    with pytest.raises(AdapterError):
        _validate_subtitle_endpoint("https://subtitle.synthetic.test/cues")


@pytest.mark.parametrize("fields", [{"begin_ms": 0, "end_ms": 500}, {"time_begin": 0, "time_end": 500}])
def test_inline_and_downloaded_timestamp_formats_keep_full_text(fields):
    text = "Synthetic subtitle. " * 30
    assert _normalize_subtitle([{"text": text, **fields}]) == [{"text": text, "begin_ms": 0, "end_ms": 500}]


@pytest.mark.parametrize("cue", [None, {"text": "cue"}, {"text": "", "begin_ms": 0, "end_ms": 500},
    {"text": "cue", "begin_ms": "NaN", "end_ms": 500},
    {"text": "cue", "begin_ms": 0, "end_ms": float("inf")},
    {"text": "cue", "begin_ms": 10, "end_ms": 5},
    {"text": "cue", "begin_ms": 0, "end_ms": 120001}])
def test_invalid_subtitles_are_controlled_and_never_claim_timing(cue):
    assert _normalize_subtitle([cue]) is None


def test_download_preserves_https_limits_and_does_not_follow_redirects(monkeypatch):
    dns(monkeypatch, "198.18.0.42")
    def get(url, **kwargs):
        assert kwargs == {"timeout": 20.0, "follow_redirects": False, "trust_env": False}
        return httpx.Response(200, json=[{"text": "Synthetic cue", "time_begin": 0, "time_end": 500}],
            request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", get)
    assert _fetch_subtitle_file("https://subtitle.synthetic.test/cues")[0]["end_ms"] == 500


@pytest.mark.parametrize("status", [302, 503])
def test_failed_or_redirected_download_never_produces_subtitles(monkeypatch, status):
    dns(monkeypatch, "8.8.8.8")
    monkeypatch.setattr(httpx, "get", lambda url, **kwargs: httpx.Response(status,
        headers={"location": "https://other.synthetic.test/cues"}, request=httpx.Request("GET", url)))
    assert _fetch_subtitle_file("https://subtitle.synthetic.test/cues") is None


def test_oversized_subtitle_download_is_rejected(monkeypatch):
    dns(monkeypatch, "8.8.8.8")
    monkeypatch.setattr(httpx, "get", lambda url, **kwargs: httpx.Response(200,
        content=b" " * (1024 * 1024 + 1), request=httpx.Request("GET", url)))
    assert _fetch_subtitle_file("https://subtitle.synthetic.test/cues") is None


@pytest.mark.parametrize("result,state", [
    ({"audio_hex": "00", "subtitle": None}, "unknown"),
    ({"audio_hex": "", "subtitle": [{"text": "OK", "begin_ms": 0, "end_ms": 500}]}, "unknown"),
    ({"audio_hex": "00", "subtitle": [{"text": "OK", "begin_ms": 0, "end_ms": 500}]}, "available"),
])
def test_probe_requires_audio_and_subtitles(monkeypatch, result, state):
    adapter = MiniMaxTTSAdapter(endpoint="https://voice.synthetic.test", voice_id="synthetic-voice")
    monkeypatch.setattr(adapter, "synthesize", lambda *args, **kwargs: result)
    assert adapter.probe("text").state == state
    adapter.close()


def test_insufficient_balance_is_nonretryable_quota_error(monkeypatch):
    adapter=MiniMaxTTSAdapter(endpoint="https://voice.synthetic.test",voice_id="synthetic-voice")
    monkeypatch.setattr(adapter,"_request",lambda *args,**kwargs:(200,
        {"base_resp":{"status_code":1008,"status_msg":"insufficient balance"}},{}))
    with pytest.raises(AdapterError) as caught:
        adapter.synthesize("Synthetic source.")
    assert caught.value.code=="MODEL_QUOTA_EXCEEDED"
    adapter.close()
