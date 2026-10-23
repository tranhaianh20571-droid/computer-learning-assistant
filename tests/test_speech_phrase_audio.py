"""Real FFmpeg over synthetic PCM; no external synthesis or private content."""
import io
import wave
import pytest
from backend.characters.speech import compile_speech, synthesize_plan
from backend.slice1.adapters.base import AdapterError


def synthetic_wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(32000)
        stream.writeframes(b"\0\0"*32000)
    return output.getvalue()


def test_phrase_audio_is_bounded_and_cues_are_retimed(monkeypatch):
    profile = {"personality": "活泼", "speaking_style": "关键词强调"}
    text = "先观察。注意唯一键。再验证。"
    plan = compile_speech(text, profile, "speech-2.8-hd", "synthetic")
    from backend.characters import speech
    monkeypatch.setattr(speech, "compile_speech", lambda *args, **kwargs: pytest.fail("frozen plans must not recompile"))
    calls, authorized = [], []
    def synthesize(value, **options):
        calls.append(options)
        return {"audio_hex": synthetic_wav().hex(), "subtitle": [
            {"text": value, "begin_ms": 0, "end_ms": 1000}]}
    result = synthesize_plan(plan, profile, "speech-2.8-hd", "synthetic", synthesize, authorized.append)
    assert len(calls) == result["synthesis_requests"] == 3
    assert any(item["volume"] > 1 for item in calls)
    assert len(bytes.fromhex(result["audio_hex"])) > 100
    assert "".join(cue["text"] for cue in result["subtitle"]) == text
    assert [cue["begin_ms"] for cue in result["subtitle"]] == [0, 1000, 2000]
    assert result["subtitle"][-1]["display_end"] == len(text)
    assert authorized == [text]*4


def test_phrase_failure_does_not_return_partial_publishable_audio():
    profile = {"personality": "活泼"}
    plan = compile_speech("先观察。注意唯一键。再验证。", profile, "speech-2.8-hd", "synthetic")
    with pytest.raises(AdapterError):
        synthesize_plan(plan, profile, "speech-2.8-hd", "synthetic",
            lambda *args, **kwargs: {"audio_hex": synthetic_wav().hex(), "subtitle": [
                {"text": "unmatched", "begin_ms": 0, "end_ms": 1000}]}, lambda _: None)
