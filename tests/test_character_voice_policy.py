"""Synthetic profile-aware recommendation, derivation and live TTS boundaries."""
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.characters.router import create_character_router
from backend.characters.schemas import Profile
from backend.characters.voice_policy import (
    automatic_speaking_style, profile_gender, recommend_voices, resolve_voice, voice_gender,
)
from backend.slice1.errors import AdapterError, Slice1Error, as_detail
from test_character_cards import env  # noqa: F401
from test_native_teaching_authorization import isolated_scheduler  # noqa: F401
from test_slice1_tts_config import bound, database, TEXT  # noqa: F401


VOICES = [
    {"voice_id": "female-warm", "name": "合成温柔女声", "description": "女性，温柔、沉稳、清晰",
     "category": "system_voice"},
    {"voice_id": "male-calm", "name": "合成沉稳男声", "description": "男性，沉稳、清晰",
     "category": "system_voice"},
    {"voice_id": "male-bright", "name": "合成轻快男声", "description": "男性，活泼、轻快",
     "category": "system_voice"},
    {"voice_id": "custom-unknown", "name": "合成自定义", "description": "温柔", "category": "voice_cloning"},
]


@pytest.mark.parametrize("profile,gender", [
    ({"identity": "男性数学导师", "personality": "温柔耐心，轻甜"}, "male"),
    ({"identity": "导师", "appearance": "成年男性，长发"}, "male"),
    ({"identity": "成年少女人设", "personality": "沉稳严谨"}, "female"),
    ({"identity": "male mentor"}, "male"),
    ({"identity": "female mentor"}, "female"),
    ({"identity": "男", "personality": "温柔"}, "male"),
    ({"identity": "导师", "tags": ["温柔", "男性"]}, "male"),
    ({"identity": "女"}, "female"),
    ({"identity": "温柔数学导师", "appearance": "长发", "name": "合成女士姓名"}, "unknown"),
    ({"background": "受一位女性老师启发"}, "unknown"),
    ({"background": "角色是成年男性数学导师。"}, "male"),
    ({"background": "合成导师喜欢黑板。他相信每个问题都值得认真对待。"}, "male"),
    ({"background": "她喜欢用日常故事帮助理解。"}, "female"),
    ({"background": "其他擅长数学的同学也会来上课。"}, "unknown"),
    ({"identity": "男性导师，不使用女声"}, "male"),
])
def test_gender_comes_from_explicit_profile_not_personality_or_names(profile, gender):
    assert profile_gender(profile) == gender


def test_male_hard_filter_overrides_warmer_female_score_and_preserves_manual_male():
    profile = {"identity": "男性导师", "personality": "温柔沉稳清晰", "tags": ["温柔"]}
    result = recommend_voices(profile, VOICES)
    assert result["recommended_voice_id"] == "male-calm"
    assert set(result["compatible_voice_ids"]) == {"male-calm", "male-bright"}
    assert len(result["data"]) == len(VOICES)
    assert resolve_voice(profile, VOICES, "male-bright") == "male-bright"
    assert resolve_voice(profile, VOICES, "female-warm") == "male-calm"
    assert resolve_voice(profile, VOICES, "custom-unknown") == "male-calm"
    assert voice_gender({"voice_id": "female-soft"}) == "female"


def test_no_compatible_male_does_not_fall_back_to_female_or_unknown():
    result = recommend_voices({"identity": "男性导师"}, [VOICES[0], VOICES[3]])
    assert result["recommended_voice_id"] is None and result["compatible_voice_ids"] == []
    assert result["match_status"] == "no_compatible_voice"
    assert resolve_voice({"identity": "男性导师"}, [VOICES[0]], "female-warm") is None
    assert recommend_voices({"identity": "女性导师"}, VOICES)["recommended_voice_id"] == "female-warm"


def test_style_generated_without_editable_style_feedback_or_vendor_grammar():
    lively = {"identity": "成年少女导师", "personality": "活泼俏皮，温柔撒娇", "background": "爱讲故事比喻"}
    calm = {"identity": "男性导师", "personality": "严谨沉稳，简洁直接", "background": "数学工程"}
    generated = automatic_speaking_style(lively)
    assert "语气词" in generated and "嗯嗯" in generated and "类比" in generated
    assert "条理清楚" in automatic_speaking_style(calm)
    assert generated == automatic_speaking_style({**lively, "speaking_style": "严厉冷漠"})
    assert generated != automatic_speaking_style(calm)
    assert "<#" not in generated and "(chuckle)" not in generated


def tts_config(cards, owner):
    return cards.configs.create(actor_id=owner, is_admin=False, kind="tts", protocol="minimax",
        endpoint="https://voice.example.test", model_name="speech-2.8-hd",
        credentials={"api_key": "synthetic-key", "voice_id": "female-warm"})


def test_profile_save_regenerates_style_without_rewriting_revision(env):
    cards, _ = env
    card = cards.create("synthetic-owner", Profile(name="Synthetic", personality="活泼温柔撒娇",
        speaking_style="Client cannot set the effective policy"))
    original = card["profile"]["speaking_style"]
    changed = cards.edit("synthetic-owner", card["card_id"], 1,
        Profile(name="Synthetic", personality="严谨沉稳，简洁直接", speaking_style=original))
    assert changed["profile"]["speaking_style"] != original
    assert "条理清楚" in changed["profile"]["speaking_style"]
    from backend.characters.models import CharacterRevisionRow
    with cards.db.session() as sess:
        assert json.loads(sess.get(CharacterRevisionRow, (card["card_id"], 1)).profile_json)["speaking_style"] == original


def test_catalog_cache_is_owner_version_scoped_and_rechecks_ownership(env, monkeypatch):
    cards, media = env
    config = tts_config(cards, "synthetic-owner")
    calls = []
    class Adapter:
        def list_voices(self):
            calls.append("list")
            return VOICES
        def close(self):
            calls.append("close")
    media.adapter_factory = lambda *args, **kwargs: Adapter()
    first = media.expression("synthetic-owner", {"identity": "男性导师"}, config["config_id"])
    second = media.expression("synthetic-owner", {"identity": "女性导师"}, config["config_id"])
    assert first["recommended_voice_id"].startswith("male-")
    assert second["recommended_voice_id"].startswith("female-") and calls == ["list", "close"]
    with pytest.raises(Slice1Error):
        media.expression("synthetic-stranger", {}, config["config_id"])
    cards.configs.update(config["config_id"], actor_id="synthetic-owner", is_admin=False, model_name="speech-2.8-turbo")
    media.expression("synthetic-owner", {}, config["config_id"])
    assert calls == ["list", "close"]*2
    monkeypatch.setattr("backend.characters.media.monotonic", lambda: float("inf"))
    media.expression("synthetic-owner", {}, config["config_id"])
    assert calls == ["list", "close"]*3


def test_failed_catalog_still_returns_style_without_wiping_saved_voice(env):
    cards, media = env
    config = tts_config(cards, "synthetic-owner")
    closed = []
    class Adapter:
        def list_voices(self):
            raise AdapterError("timeout", detail="private provider detail")
        def close(self):
            closed.append(True)
    media.adapter_factory = lambda *args, **kwargs: Adapter()
    result = media.expression("synthetic-owner", {"personality": "活泼温柔"}, config["config_id"])
    assert result["match_status"] == "catalog_unavailable" and result["speaking_style"]
    assert "private provider detail" not in str(result) and closed == [True]


def test_preview_auth_strict_schema_no_write_and_no_paid_synthesis(env):
    cards, media = env
    config = tts_config(cards, "synthetic-owner")
    class Adapter:
        def list_voices(self):
            return VOICES
        def close(self):
            pass
    media.adapter_factory = lambda *args, **kwargs: Adapter()
    app = FastAPI()
    def owner(request):
        if request.headers.get("Authorization") != "Bearer synthetic-session":
            raise HTTPException(401)
        return SimpleNamespace(user_id="synthetic-owner")
    app.include_router(create_character_router(cards, media, owner))
    @app.exception_handler(Slice1Error)
    async def handled(request, exc):
        return JSONResponse(status_code=exc.status, content=as_detail(exc))
    client = TestClient(app)
    body = {"profile": {"name": "Synthetic", "identity": "男性导师"}, "config_id": config["config_id"]}
    before = cards.list("synthetic-owner")
    assert client.post("/api/character-expression", json=body).status_code == 401
    headers = {"Authorization": "Bearer synthetic-session"}
    response = client.post("/api/character-expression", json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["recommended_voice_id"] == "male-calm"
    assert cards.list("synthetic-owner") == before
    bad = client.post("/api/character-expression", json={**body, "temperature": .9}, headers=headers)
    assert bad.status_code == 422 and "temperature" not in bad.text
    bad = client.post("/api/character-expression", json={**body, "config_id": "foreign-config"}, headers=headers)
    assert bad.status_code == 404
    local = client.post("/api/character-expression", json={"profile": body["profile"]}, headers=headers)
    assert local.status_code == 200 and local.json()["speaking_style"]


@pytest.mark.parametrize("selected,expected", [
    ("", "male-calm"), ("female-warm", "male-calm"), ("male-bright", "male-bright"),
])
def test_actual_tts_request_never_uses_female_fallback_for_male(bound, monkeypatch, selected, expected):
    from backend.characters.models import CharacterCardRow, CharacterSegmentRow
    from backend.tasks.service import LearningTaskRow
    from backend.teaching import tts_config as module
    state = bound()
    with state.db.session() as sess:
        conv_id = sess.get(LearningTaskRow, state.task).subject_id.split(":")[0]
        sess.add(CharacterCardRow(card_id=state.owner, owner_user_id=state.owner, current_revision=1))
        sess.flush()
        sess.add(CharacterSegmentRow(conversation_id=conv_id, step_no=2, card_id=state.owner,
            revision=1, profile_json=Profile(name="Synthetic", identity="男性数学导师",
                personality="温柔耐心", voice_id=selected,
                voice_config_id=state.view["config_id"] if selected else "").model_dump_json()))
    original = module.build_adapter
    def build(*args, **kwargs):
        adapter = original(*args, **kwargs)
        adapter.list_voices = lambda: VOICES
        return adapter
    monkeypatch.setattr(module, "build_adapter", build)
    adapter = state.factory(state.owner, state.task)
    try:
        adapter.synthesize(TEXT)
    finally:
        adapter.close()
    assert state.sent[0][1]["voice_id"] == expected


def test_tts_without_compatible_male_fails_before_synthesis_and_closes_client(bound, monkeypatch):
    from backend.characters.models import CharacterCardRow, CharacterSegmentRow
    from backend.tasks.service import LearningTaskRow
    from backend.teaching import tts_config as module
    state = bound()
    with state.db.session() as sess:
        conv_id = sess.get(LearningTaskRow, state.task).subject_id.split(":")[0]
        sess.add(CharacterCardRow(card_id=state.owner, owner_user_id=state.owner, current_revision=1))
        sess.flush()
        sess.add(CharacterSegmentRow(conversation_id=conv_id, step_no=2, card_id=state.owner,
            revision=1, profile_json=Profile(name="Synthetic", identity="男性导师").model_dump_json()))
    original = module.build_adapter
    def build(*args, **kwargs):
        adapter = original(*args, **kwargs)
        adapter.list_voices = lambda: [VOICES[0], VOICES[3]]
        return adapter
    monkeypatch.setattr(module, "build_adapter", build)
    with pytest.raises(Slice1Error) as caught:
        state.factory(state.owner, state.task)
    assert caught.value.code == "capability_test_failed" and state.sent == []
    assert state.built[-1][1]["client"].is_closed


@pytest.fixture
def isolated_voice_sample_env(isolated_scheduler,tmp_path):
    fixture=env.__wrapped__(tmp_path)
    try:
        yield next(fixture)
    finally:
        fixture.close()


@pytest.mark.parametrize("compatible", [True, False])
def test_voice_sample_checks_gender_before_request_and_publication(isolated_voice_sample_env, compatible):
    from backend.characters.schemas import MediaRequest
    cards, media = isolated_voice_sample_env
    owner = "synthetic-sample-" + uuid.uuid4().hex[:12]
    config = tts_config(cards, owner)
    card = cards.create(owner, Profile(name="Synthetic", identity="男性数学导师",
        personality="温柔耐心", voice_id="female-warm", voice_config_id=config["config_id"]))
    sent, closed = [], []
    class Adapter:
        model_name = "speech-2.8-hd"
        def list_voices(self):
            return VOICES if compatible else [VOICES[0]]
        def synthesize(self, text, **options):
            sent.append(options)
            return {"audio_hex": b"synthetic-audio".hex()}
        def close(self):
            closed.append(True)
    media.adapter_factory = lambda *args, **kwargs: Adapter()
    task = media.create(owner, card["card_id"], MediaRequest(kind="voice", revision=1,
        config_id=config["config_id"], config_version=1), "synthetic-sample")
    media.run(task["task_id"])
    assert media.tasks.get_task(task["task_id"])["status"] == ("succeeded" if compatible else "failed")
    assert closed == [True]
    if compatible:
        assert len(sent) == 1 and sent[0]["voice_id"] == "male-calm"
        assert len(cards.get(owner, card["card_id"])["assets"]) == 1
    else:
        assert sent == [] and cards.get(owner, card["card_id"])["assets"] == []
