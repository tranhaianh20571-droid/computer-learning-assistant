"""Synthetic providers, genuine SDK dialogue, and atomic full-card delivery."""
import base64
import uuid

import pytest
from backend.characters.creation_delivery import SCENES, select_voice
from backend.characters.distillation_models import BuildRequest, CharacterBuildRow
from backend.characters.voice_policy import profile_gender
from backend.slice1.errors import AdapterError, Slice1Error
from backend.tasks.service import LearningTaskRow
from test_character_cards import generated_image
from test_persona_workflows import SKILL, builder

pytest_plugins = ("test_character_cards",)

PROFILE = {
    "name": "合成学妹", "identity": "原作中的女性学妹",
    "background": "来自合成作品，与用户是同校同学；授课只是共同学习的活动。",
    "personality": "温柔细致，遇到不确定的事实直说未知。",
    "appearance": "沿用约定的短外套、衬衣和深色裙装，不换成教师职业服装。",
    "tags": ["学妹", "中文", "温柔"],
}


def setup_creation(env, monkeypatch):
    import test_persona_workflows

    cards, owner, service, calls = builder(env, monkeypatch, pipeline_version=7, streaming=True)
    _, media = env
    service.media_service = media
    configs = {}
    for role, kind, protocol, model in (
        ("image", "image_generation", "openai_image", "synthetic-image"),
        ("voice", "tts", "minimax", "speech-2.8-hd"),
    ):
        configs[role] = cards.configs.create(
            actor_id=owner, is_admin=False, kind=kind, protocol=protocol,
            endpoint=f"https://{role}.example.test/v1", model_name=model,
            credentials={"api_key": "synthetic-key"}, owner_scope="user")
    issued = []
    controls = {"failure_at": None, "on_image": None, "ready": True, "errors": [],
                "confirmation": None, "profile": dict(PROFILE)}
    normal_code = service._code

    def capture_error(exc):
        import traceback
        controls["errors"].append("".join(traceback.format_exception(exc)))
        return normal_code(exc)

    monkeypatch.setattr(service, "_code", capture_error)
    normal = test_persona_workflows.synthetic_reply

    def reply(stage, payload, props):
        if stage == "dialogue_plan":
            return {"topic": PROFILE["name"], "subject_kind": "fictional", "work": "合成原作", "queries": [],
                    "user_confirmation": controls["confirmation"] or (
                        "confirm" if payload.get("pending_character") and payload["message"] == "确认创作此人物。"
                        else "revise" if "改成" in payload["message"] else "unclear")}
        if stage == "dialogue_reply":
            profile = controls["profile"]
            return {
                "reply": f"{profile['name']}：{profile['background']} 外观：{profile['appearance']} 性格：{profile['personality']}"
                    if controls["ready"] else "只核对一个尚未明确的外观细节。",
                "question": "" if controls["ready"] else "保留原作服装吗？",
                "profile": profile, "skill": SKILL,
                "thinking_mechanism": "先看条件。", "decision_method": "核对依据。",
                "expression_style": "清楚温柔。", "knowledge_boundary": "不编造原作事实。",
                "ready": controls["ready"],
            }
        return normal(stage, payload, props)

    monkeypatch.setattr(test_persona_workflows, "synthetic_reply", reply)
    png = generated_image()

    class Adapter:
        model_name = "speech-2.8-hd"

        def list_voices(self):
            return [
                {"voice_id": "Spanish_female", "name": "Spanish female", "description": "女性温柔"},
                {"voice_id": "Chinese_male", "name": "中文男声", "description": "男性沉稳"},
                {"voice_id": "Chinese_female", "name": "中文女声", "description": "女性温柔"},
            ]

        def _image_request(self, body, reference=None):
            self.authorize_request()
            issued.append({"kind": "image", "body": body, "reference": reference})
            index = sum(item["kind"] == "image" for item in issued)
            if controls["on_image"]:
                controls["on_image"](index)
            if controls["failure_at"] == index:
                raise AdapterError("capability_test_failed")
            return {"data": [{"b64_json": base64.b64encode(png).decode()}]}

        def synthesize(self, text, **options):
            issued.append({"kind": "voice", "voice": options["voice_id"]})
            return {"audio_hex": b"synthetic-audio".hex()}

        def close(self):
            pass

    media.adapter_factory = lambda *args, **kwargs: Adapter()
    return cards, owner, service, configs, issued, controls, calls


def request():
    return BuildRequest(mode="ai", dialogue=True, complete_card=True,
        topic="待识别人物", message="按合成原作的学妹身份完整生成，用来共同学习，不要改成职业老师。",
        confirmed=True, request_key=uuid.uuid4().hex)


def confirmed_creation(service, owner):
    introduction = service.create(owner, request())
    assert service.run(introduction["build_id"])["status"] == "succeeded"
    return service.create(owner, request().model_copy(update={
        "parent_build_id": introduction["build_id"], "message": "确认创作此人物。"}))


def test_full_dialogue_waits_for_chat_confirmation_then_saves_complete_card(env, monkeypatch):
    cards, owner, service, configs, issued, controls, calls = setup_creation(env, monkeypatch)
    req = request()
    created = service.create(owner, req)
    assert service.create(owner, req)["build_id"] == created["build_id"]
    with cards.db.session() as sess:
        task = sess.get(LearningTaskRow, created["task_id"])
        assert (task.deadline_at - task.created_at).total_seconds() >= 2399
    receipt = service.run(created["build_id"])
    assert receipt["status"] == "succeeded", (receipt, controls["errors"])
    introduction = service.get(owner, created["build_id"])
    assert introduction["accepted_revision"] is None and issued == []
    assert "persona" not in introduction["result"]
    assert introduction["result"]["dialogue"]["awaiting_confirmation"] is True
    assert PROFILE["name"] in introduction["result"]["dialogue"]["reply"]
    assert "是否确认创作此人物" in introduction["result"]["dialogue"]["question"]
    assert [name for name, _ in calls] == ["dialogue_plan", "dialogue_reply"]
    with pytest.raises(Slice1Error):
        service.accept(owner, created["build_id"])
    confirmation = request().model_copy(update={
        "parent_build_id": created["build_id"], "message": "确认创作此人物。"})
    created = service.create(owner, confirmation)
    assert service.create(owner, confirmation)["build_id"] == created["build_id"]
    assert service.run(created["build_id"])["status"] == "succeeded", controls["errors"]
    result = service.get(owner, created["build_id"])
    assert result["result"]["dialogue"]["creation_confirmed"] is True
    assert result["result"]["dialogue"]["awaiting_confirmation"] is False
    assert result["result"]["dialogue"]["profile"] == introduction["result"]["dialogue"]["profile"]
    assert result["accepted_revision"] == 2
    card = cards.get(owner, result["card_id"])
    assert card["revision"] == 2 and card["published_revision"] is None
    assert card["profile"]["identity"] == PROFILE["identity"]
    assert card["profile"]["appearance"] == PROFILE["appearance"]
    assert card["profile"]["voice_id"] == "Chinese_female"
    assert card["profile"]["voice_config_id"] == configs["voice"]["config_id"]
    assert card["profile"]["voice_selection"] == "auto"
    assert len(card["profile"]["media_ids"]) == 6
    assert all(asset["confirmed"] for asset in card["assets"])
    assert {asset["scene"] for asset in card["assets"] if asset["kind"] == "image"} == set(SCENES)
    assert sum(asset["kind"] == "voice" for asset in card["assets"]) == 1
    images = [item for item in issued if item["kind"] == "image"]
    assert len(images) == 5 and images[0]["reference"] is None
    assert all(item["reference"] == generated_image() for item in images[1:])
    assert all("female tutor" not in item["body"]["prompt"] for item in images)
    assert all(PROFILE["identity"] in item["body"]["prompt"] for item in images)
    assert all("not a requirement to change" in item["body"]["prompt"] for item in images)
    assert card["persona"]["quality"]["passed"]
    assert result["result"]["completion"]["status"] == "complete"
    assert [name for name, _ in calls] == [
        "dialogue_plan", "dialogue_reply", "dialogue_plan", "fast_research", "fast_replies", "fast_verify"]
    before = len(issued)
    assert service.run(created["build_id"])["status"] == "succeeded"
    assert service.accept(owner, created["build_id"])["revision"] == 2
    assert len(issued) == before


def test_dialogue_keeps_clarifying_until_ready_without_starting_media(env, monkeypatch):
    _, owner, service, _, issued, controls, _ = setup_creation(env, monkeypatch)
    controls["ready"] = False
    created = service.create(owner, request())
    assert service.run(created["build_id"])["status"] == "succeeded"
    result = service.get(owner, created["build_id"])
    assert result["accepted_revision"] is None and "persona" not in result["result"]
    assert issued == []
    controls["ready"] = True
    continued = request().model_copy(update={
        "parent_build_id": created["build_id"], "message": "保留原作服装，就这样直接全部生成。"})
    next_turn = service.create(owner, continued)
    assert next_turn["card_id"] == created["card_id"]
    assert service.run(next_turn["build_id"])["status"] == "succeeded"
    assert service.get(owner, next_turn["build_id"])["accepted_revision"] is None
    assert issued == []
    confirmed = service.create(owner, request().model_copy(update={
        "parent_build_id": next_turn["build_id"], "message": "确认创作此人物。"}))
    assert service.run(confirmed["build_id"])["status"] == "succeeded"
    assert service.get(owner, confirmed["build_id"])["accepted_revision"] == 2


def test_failed_image_preserves_private_progress_and_explicit_retry_skips_completed_media(env, monkeypatch):
    cards, owner, service, _, issued, controls, _ = setup_creation(env, monkeypatch)
    controls["failure_at"] = 3
    created = confirmed_creation(service, owner)
    assert service.run(created["build_id"])["status"] == "failed"
    partial = cards.get(owner, created["card_id"])
    assert partial["revision"] == 1 and not partial["profile"]["media_ids"]
    assert len(partial["assets"]) == 3 and not any(asset["confirmed"] for asset in partial["assets"])
    assert sum(item["kind"] == "voice" for item in issued) == 1
    assert sum(item["kind"] == "image" for item in issued) == 3
    controls["failure_at"] = None
    service.action(owner, created["build_id"], "retry")
    receipt = service.run(created["build_id"])
    assert receipt["status"] == "succeeded", "\n".join(controls["errors"])
    assert sum(item["kind"] == "voice" for item in issued) == 1
    assert sum(item["kind"] == "image" for item in issued) == 6
    assert cards.get(owner, created["card_id"])["revision"] == 2


def test_successful_media_survives_failure_before_parent_checkpoint_commit(env, monkeypatch):
    from backend.characters.distillation_models import CharacterBuildStageRow

    cards, owner, service, _, issued, _, _ = setup_creation(env, monkeypatch)
    submit = service.tasks.submit_result
    interrupted = []

    def interrupt_once(lease, **options):
        with cards.db.session() as sess:
            stage = sess.get(CharacterBuildStageRow, lease.task_id)
            target = stage and stage.stage_key == "delivery_image_thinking"
        if target and options["status"] == "succeeded" and not interrupted:
            interrupted.append(True)
            raise RuntimeError("Synthetic checkpoint interruption after successful provider result.")
        return submit(lease, **options)

    monkeypatch.setattr(service.tasks, "submit_result", interrupt_once)
    created = confirmed_creation(service, owner)
    assert service.run(created["build_id"])["status"] == "failed" and interrupted
    assert sum(item["kind"] == "image" for item in issued) == 3
    service.action(owner, created["build_id"], "retry")
    assert service.run(created["build_id"])["status"] == "succeeded"
    assert sum(item["kind"] == "image" for item in issued) == 5
    assert sum(item["kind"] == "voice" for item in issued) == 1
    assert cards.get(owner, created["card_id"])["revision"] == 2


@pytest.mark.parametrize("fence", ["cancel", "delete", "edit", "config"])
def test_parent_changes_prevent_late_media_and_card_publication(env, monkeypatch, fence):
    cards, owner, service, configs, _, controls, _ = setup_creation(env, monkeypatch)
    created = confirmed_creation(service, owner)

    changed = []

    def change(index):
        if index != 1:
            return
        changed.append(index)
        if fence == "cancel":
            service.action(owner, created["build_id"], "cancel")
        elif fence == "delete":
            cards.delete(owner, created["card_id"])
        elif fence == "edit":
            from backend.characters.schemas import Profile
            cards.edit(owner, created["card_id"], 1, Profile(name="用户另行修改的人物"))
        else:
            from backend.slice1.models import ServiceConfigRow
            with cards.db.session() as sess:
                sess.get(ServiceConfigRow, configs["image"]["config_id"]).config_version += 1

    controls["on_image"] = change
    receipt = service.run(created["build_id"])
    assert changed == [1], controls["errors"]
    assert receipt["status"] != "succeeded"
    with cards.db.session() as sess:
        assert sess.get(CharacterBuildRow, created["build_id"]).accepted_revision is None
    if fence != "delete":
        card = cards.get(owner, created["card_id"])
        assert not card["profile"]["media_ids"]
        assert not any(asset["kind"] == "image" for asset in card["assets"])


def test_complete_creation_never_borrows_another_owners_media_config(env, monkeypatch):
    _, owner, service, _, _, _, _ = setup_creation(env, monkeypatch)
    other = "synthetic-other-" + uuid.uuid4().hex
    from backend.agents.model_routing import set_role
    from test_persona_workflows import config
    research = config(service.cards, other, "research")
    set_role(service.cards.configs, other, "research", research["config_id"], 0)
    with pytest.raises(Slice1Error) as failure:
        service.create(other, request())
    assert failure.value.status == 409
    assert owner != other


@pytest.mark.parametrize("message,source", [
    ("好的", ""), ("继续", ""), ("先别生成", ""),
    ("确认，但先把服装改成白衬衫。", ""), ("这样会生成几张图？", ""),
    ("确认创作此人物。", "新的人物材料，必须重新介绍并确认。"),
])
def test_pending_character_does_not_generate_for_ambiguity_revision_or_new_material(
    env, monkeypatch, message, source,
):
    _, owner, service, _, issued, _, calls = setup_creation(env, monkeypatch)
    intro = service.create(owner, request())
    assert service.run(intro["build_id"])["status"] == "succeeded"
    continued = service.create(owner, request().model_copy(update={
        "parent_build_id": intro["build_id"], "message": message, "source_text": source}))
    assert service.run(continued["build_id"])["status"] == "succeeded"
    saved = service.get(owner, continued["build_id"])
    assert saved["result"]["dialogue"]["awaiting_confirmation"] is True
    assert saved["accepted_revision"] is None and "persona" not in saved["result"]
    assert issued == []
    assert not any(name.startswith("fast_") for name, _ in calls)
    assert bool(saved["request"].get("pending_character")) == (not source)


@pytest.mark.parametrize("complete_card,finish", [(True, False), (True, True), (False, True)])
def test_first_turn_cannot_skip_introduction_even_if_model_claims_confirmation(
    env, monkeypatch, complete_card, finish,
):
    _, owner, service, _, issued, controls, calls = setup_creation(env, monkeypatch)
    controls["confirmation"] = "confirm"
    created = service.create(owner, request().model_copy(update={
        "message": "确认创作此人物。", "complete_card": complete_card, "finish": finish}))
    assert service.run(created["build_id"])["status"] == "succeeded"
    saved = service.get(owner, created["build_id"])
    assert saved["result"]["dialogue"]["awaiting_confirmation"] is True
    assert saved["accepted_revision"] is None and "persona" not in saved["result"]
    assert issued == [] and [name for name, _ in calls] == ["dialogue_plan", "dialogue_reply"]


def test_confirmation_is_owner_scoped_latest_only_and_reuses_agreed_draft(env, monkeypatch):
    _, owner, service, _, issued, controls, _ = setup_creation(env, monkeypatch)
    intro = service.create(owner, request())
    assert service.run(intro["build_id"])["status"] == "succeeded"
    original = service.get(owner, intro["build_id"])["result"]["dialogue"]
    confirmation = request().model_copy(update={
        "parent_build_id": intro["build_id"], "message": "确认创作此人物。"})
    with pytest.raises(Slice1Error) as foreign:
        service.create("synthetic-foreign-owner", confirmation)
    assert foreign.value.status in (404, 409)
    with pytest.raises(Slice1Error) as foreign_read:
        service.get("synthetic-foreign-owner", intro["build_id"])
    assert foreign_read.value.status == 404
    controls["profile"] = {**PROFILE, "appearance": "白色衬衫与深色裙装。"}
    revision = service.create(owner, request().model_copy(update={
        "parent_build_id": intro["build_id"], "message": "把衣服改成白色。"}))
    assert service.run(revision["build_id"])["status"] == "succeeded"
    revised = service.get(owner, revision["build_id"])["result"]["dialogue"]
    assert revised["profile"]["appearance"] != original["profile"]["appearance"]
    with pytest.raises(Slice1Error) as stale:
        service.create(owner, confirmation)
    assert stale.value.status == 409 and issued == []
    confirmed = service.create(owner, confirmation.model_copy(update={
        "parent_build_id": revision["build_id"], "request_key": uuid.uuid4().hex}))
    assert confirmed["request"]["pending_character"]["profile"] == revised["profile"]
    with pytest.raises(Slice1Error):
        service.create(owner, confirmation.model_copy(update={
            "parent_build_id": revision["build_id"], "request_key": uuid.uuid4().hex}))
    controls["profile"] = dict(PROFILE)
    assert service.run(confirmed["build_id"])["status"] == "succeeded"
    assert service.get(owner, confirmed["build_id"])["result"]["profile"]["appearance"] == "白色衬衫与深色裙装。"


def test_material_question_limit_and_finish_still_require_later_chat_confirmation(env, monkeypatch):
    import json

    cards, owner, service, _, issued, _, _ = setup_creation(env, monkeypatch)
    first = service.create(owner, request().model_copy(update={"source_text": "合成人物资料。"}))
    assert service.run(first["build_id"])["status"] == "succeeded"
    with cards.db.session() as sess:
        row = sess.get(CharacterBuildRow, first["build_id"])
        saved = json.loads(row.result_json)
        saved["dialogue"]["question_count"] = 8
        row.result_json = json.dumps(saved)
    final = service.create(owner, request().model_copy(update={
        "parent_build_id": first["build_id"], "message": "请整理最终介绍。", "finish": True}))
    assert service.run(final["build_id"])["status"] == "succeeded"
    dialogue = service.get(owner, final["build_id"])["result"]["dialogue"]
    assert dialogue["question_count"] == 8 and dialogue["awaiting_confirmation"]
    assert "是否确认创作此人物" in dialogue["question"]
    assert issued == []
    confirmed = service.create(owner, request().model_copy(update={
        "parent_build_id": final["build_id"], "message": "确认创作此人物。"}))
    assert service.run(confirmed["build_id"])["status"] == "succeeded"
    assert service.get(owner, confirmed["build_id"])["accepted_revision"] == 2


@pytest.mark.parametrize("identity,gender", [
    ("女讲师", "female"), ("学妹", "female"), ("学弟", "male"), ("男性，不是学妹", "male"),
])
def test_explicit_identity_selects_matching_language_and_gender(identity, gender):
    assert profile_gender({"identity": identity}) == gender
    voices = [
        {"voice_id": "Spanish_female", "name": "Spanish female", "description": "女性温柔"},
        {"voice_id": "Chinese_female", "name": "中文女声", "description": "女性"},
        {"voice_id": "Chinese_male", "name": "中文男声", "description": "男性"},
    ]
    assert select_voice({"identity": identity}, voices) == "Chinese_" + gender


def test_voice_language_follows_explicit_dialogue_preference_instead_of_original_work_locale():
    voices = [
        {"voice_id": "English_female", "name": "English female", "description": "female"},
        {"voice_id": "Chinese_female", "name": "中文女声", "description": "女性"},
    ]
    assert select_voice({"identity": "学妹", "background": "喜欢 English 文学"}, voices) == "Chinese_female"
    assert select_voice({"identity": "学妹", "speaking_style": "使用英语授课"}, voices) == "English_female"
