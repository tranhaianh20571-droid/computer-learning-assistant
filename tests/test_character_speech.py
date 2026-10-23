from backend.characters.speech import compile_speech, map_subtitles


def test_compiler_separates_pause_tokens_and_clean_text():
    source = "先看分母。然后决定通分。"
    plan = compile_speech(source, {"personality": "成熟温柔"}, "speech-2.8-hd", "synthetic-voice")
    assert plan.display_text == source
    assert plan.speech_text == "先看分母。<#0.35#>然后决定通分。"
    assert len(plan.fingerprint) == 64
    cues = map_subtitles(plan, [{"text": "先看分母。", "begin_ms": 0, "end_ms": 800},
        {"text": "然后决定通分。", "begin_ms": 1100, "end_ms": 2000}])
    assert "".join(cue["text"] for cue in cues) == source
    assert cues[1]["display_start"] == 5


def test_unmatched_subtitles_do_not_claim_precise_alignment():
    plan = compile_speech("观察。继续。", {}, "speech-2.8-hd", "synthetic-voice")
    assert map_subtitles(plan, [{"text": "unmatched", "begin_ms": 0, "end_ms": 500}]) is None


def test_user_tokens_do_not_become_control_operations_or_modify_display():
    text = "符号<#3#>和(chuckle)是什么？"
    plan = compile_speech(text, {}, "speech-2.8-hd", "synthetic-voice")
    assert plan.display_text == text and "<#" not in plan.speech_text and "(chuckle)" not in plan.speech_text
    assert map_subtitles(plan, [{"text": "unmatched", "begin_ms": 0, "end_ms": 500}]) is None


def test_voice_model_style_change_fingerprint_but_image_does_not():
    text = "第一句。第二句。"
    one = compile_speech(text, {"personality": "温柔", "base_asset_id": "a"}, "speech-2.8-hd", "a")
    other_image = compile_speech(text, {"personality": "温柔", "base_asset_id": "b"}, "speech-2.8-hd", "a")
    other_voice = compile_speech(text, {"personality": "温柔"}, "speech-2.8-hd", "b")
    assert one.fingerprint == other_image.fingerprint and one.fingerprint != other_voice.fingerprint


def test_nonverbal_is_model_scoped_and_not_displayed():
    source="先观察。再想一想。"
    rich=compile_speech(source,{"personality":"活泼"},"speech-2.8-hd","synthetic",event="correct")
    plain=compile_speech(source,{"personality":"活泼"},"speech-2.6-hd","synthetic")
    assert "(chuckle)" in rich.speech_text and "(chuckle)" not in plain.speech_text
    assert rich.display_text==plain.display_text==source and rich.emotion=="happy"


def test_feedback_tone_follows_evaluation_even_without_error_keywords():
    source = "先检查这一步。再试一次。"
    profile = {"personality": "活泼开朗，喜欢叹息"}
    for event in ("partial", "incorrect", "unsure", "frustrated", "feedback"):
        plan = compile_speech(source, profile, "speech-2.8-hd", "synthetic", event=event)
        assert plan.emotion == "calm"
        assert "(chuckle)" not in plan.speech_text and "(sighs)" not in plan.speech_text
        assert plan.display_text == source
    explaining = compile_speech(source, profile, "speech-2.8-hd", "synthetic")
    assert "(chuckle)" not in explaining.speech_text
    assert plan.speed < explaining.speed
