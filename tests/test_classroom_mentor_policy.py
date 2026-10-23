"""Mentor preferences, expression cache and native speech parameter boundaries."""
from types import SimpleNamespace
import pytest
from backend.characters.schemas import Profile, UseMentor
from backend.characters.service import CharacterService
from backend.characters.expression import derive_expression, expression_snapshot
from backend.characters.speech import compile_speech, map_subtitles
from backend.materials.storage import MaterialStorage
from backend.slice1.config_service import ConfigService
from backend.slice1.errors import Slice1Error
from test_teaching_assessments import env, database, lesson  # noqa: F401


def cards(env, tmp_path):
    state = env[1]
    return CharacterService(state.db, ConfigService(state.db, encryption_key="synthetic-policy", audit=state.audit),
        state.audit, MaterialStorage(tmp_path))


def test_preference_conflict_default_and_delete(env, tmp_path):
    service = cards(env, tmp_path)
    owner = env[2]
    card = service.create(owner, Profile(name="Synthetic mentor"))
    chosen = service.use(owner, UseMentor(expected_version=0, card_id=card["card_id"], revision=1))
    assert chosen["version"] == 1 and service.preference(owner)["card_id"] == card["card_id"]
    with pytest.raises(Slice1Error):
        service.use(owner, UseMentor(expected_version=0, card_id=None))
    service.delete(owner, card["card_id"])
    assert service.preference(owner) == {"version": 2, "card_id": None, "revision": None}


def test_preference_applies_to_new_lesson_and_freezes_old_segments(env, tmp_path):
    service = cards(env, tmp_path)
    owner = env[2]
    card = service.create(owner, Profile(name="Synthetic mentor"))
    service.use(owner, UseMentor(expected_version=0, card_id=card["card_id"], revision=1))
    _, _, start = lesson(env)
    from backend.characters.service import segment_character
    with env[1].db.session() as sess:
        assert segment_character(sess, start["conversation_id"], 1)["card_id"] == card["card_id"]
    value = service.use(owner, UseMentor(expected_version=1, conversation_id=start["conversation_id"]))
    assert value["effective_step_no"] == 2
    with env[1].db.session() as sess:
        assert segment_character(sess, start["conversation_id"], 1)["card_id"] == card["card_id"]
        assert segment_character(sess, start["conversation_id"], 2) is None


def test_private_preference_is_owned(env, tmp_path):
    service = cards(env, tmp_path)
    card = service.create(env[2], Profile(name="Private"))
    with pytest.raises(Slice1Error):
        service.use("stranger", UseMentor(expected_version=0, card_id=card["card_id"], revision=1))


def test_expression_uses_background_style_and_is_cached(env):
    calm = derive_expression({"personality": "严谨沉稳", "background": "数学教授", "speaking_style": "简洁直接"})
    lively = derive_expression({"background": "喜欢故事与比喻的活泼伙伴", "speaking_style": "轻快幽默，亲切引导"})
    assert lively.liveliness > calm.liveliness and lively.temperature > calm.temperature
    one = expression_snapshot(env[1].db, {"background": "温柔陪伴", "identity": "导师"})
    two = expression_snapshot(env[1].db, {"background": "温柔陪伴", "identity": "导师", "base_asset_id": "ignored"})
    assert one == two and one.evidence == ["identity", "background"]


def test_native_controls_do_not_leak_and_unknown_model_is_plain():
    profile = {"background": "活泼伙伴", "speaking_style": "轻快，声音柔和明亮"}
    plan = compile_speech("先观察。然后比较。", profile, "speech-2.8-hd", "voice", event="correct")
    assert "(chuckle)" in plan.speech_text and plan.speed > 1
    assert dict(plan.voice_modify) == {"intensity": -10, "timbre": 10}
    mapped = map_subtitles(plan, [{"text": "先观察。<#0.20#>(chuckle)", "begin_ms": 0, "end_ms": 500},
        {"text": "然后比较。", "begin_ms": 700, "end_ms": 1000}])
    assert "".join(item["text"] for item in mapped) == plan.display_text
    plain = compile_speech("先观察。然后比较。", profile, "unknown", "voice")
    assert plain.speech_text == plain.display_text and not plain.capabilities


def test_minimax_parameter_ranges_and_effect_allowlist():
    from backend.slice1.adapters.minimax import MiniMaxTTSAdapter
    from backend.slice1.adapters.base import AdapterError
    adapter = MiniMaxTTSAdapter(endpoint="https://voice.example.test", voice_id="synthetic")
    try:
        request = adapter.build_request("Sample", speed=1.1, pitch=1, voice_modify={"sound_effects": "lofi_telephone"})
        assert request["voice_setting"]["speed"] == 1.1
        with pytest.raises(AdapterError):
            adapter.build_request("Sample", voice_modify={"sound_effects": "telephone"})
        with pytest.raises(AdapterError):
            adapter.build_request("Sample", pitch=999)
    finally:
        adapter.close()


def test_segment_expression_is_frozen_across_policy_changes(env, tmp_path, monkeypatch):
    from backend.characters import expression
    from backend.characters.expression import content_expression
    service = cards(env, tmp_path)
    card = service.create(env[2], Profile(name="Frozen", personality="活泼亲切"))
    service.use(env[2], UseMentor(expected_version=0, card_id=card["card_id"], revision=1))
    _, _, start = lesson(env)
    from backend.characters.service import segment_character
    binding = (start["conversation_id"], 1)
    with env[1].db.session() as sess:
        segment_character(sess, *binding, freeze=True)
    adapter = SimpleNamespace(protocol="openai", model_name="synthetic", reasoning_settings={})
    first = content_expression(env[1].db, adapter, card["profile"], binding=binding)
    monkeypatch.setattr(expression, "POLICY_VERSION", "synthetic-new-policy")
    again = content_expression(env[1].db, adapter, {"personality": "严肃克制"}, binding=binding)
    assert first == again and adapter.expression_temperature == first["effective_temperature"]


def test_native_phrase_budget_pronunciation_and_display_integrity():
    profile = {"personality": "活泼亲切", "speaking_style": "关键句强调，语尾延长"}
    text = "先说明重载。注意线程和缓存的区别。这样就清楚啦。"
    plan = compile_speech(text, profile, "speech-2.8-hd", "voice")
    assert 1 <= len(plan.segments) <= 3
    assert "".join(text[a:b] for a, b, _, _ in plan.segments) == text
    assert any(volume > 1 for _, _, _, volume in plan.segments)
    assert any("重载/" in item for item in plan.pronunciation)
    assert plan.display_text == text


def test_native_candidate_traits_refine_once_and_remain_internal(env):
    from backend.characters.expression import learn_expression, PersonaTraits
    profile = {"background": "陪伴用户从生活例子理解关系", "speaking_style": "多用类比"}
    traits = PersonaTraits(liveliness=.8, warmth=.9, restraint=.3, variety=.8,
        professional_need=.6, confidence=.85, evidence=["background", "speaking_style"])
    with env[1].db.session() as sess:
        learn_expression(sess, profile, "synthetic-model", traits)
    first = expression_snapshot(env[1].db, profile, "synthetic-model")
    assert first.director_source == "semantic" and first.liveliness == .8
    with env[1].db.session() as sess:
        learn_expression(sess, profile, "synthetic-model", traits.model_copy(update={"liveliness": .2}))
    assert expression_snapshot(env[1].db, profile, "synthetic-model") == first
    public = Profile(name="Synthetic", background=profile["background"]).model_dump()
    assert "temperature" not in public and "persona_traits" not in public
