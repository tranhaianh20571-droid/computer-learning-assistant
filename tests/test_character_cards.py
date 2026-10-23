"""Synthetic private drafts, immutable publishing, assets and deletion fences."""
import io
import json
import uuid
from pathlib import Path

import pytest
from PIL import Image,ImageDraw
from backend.logging_audit.db import Database
from backend.logging_audit.repositories import PgAuditLog, PgTaskEventStore
from backend.slice1.config_service import ConfigService
from backend.slice1.disclosure_service import DisclosureService
from backend.slice1.errors import Slice1Error
from backend.materials.storage import MaterialStorage
from backend.tasks.service import TaskService
from backend.characters.service import CharacterService
from backend.characters.schemas import Profile, MediaRequest
from backend.characters.media import CharacterMediaService, normalize_media, isolated_pointing_part
from backend.characters.models import CharacterRevisionRow, CharacterMediaJobRow


@pytest.fixture
def env(tmp_path):
    db = Database(); db.create_all()
    audit = PgAuditLog(db)
    configs = ConfigService(db, encryption_key="synthetic-character-key", audit=audit)
    cards = CharacterService(db, configs, audit, MaterialStorage(tmp_path))
    media = CharacterMediaService(cards, TaskService(db, PgTaskEventStore(db)), DisclosureService(db, audit))
    try:
        yield cards, media
    finally:
        media.close(); db.dispose()


def test_one_card_immutable_revisions_public_snapshot_collection_and_delete(env):
    cards, _ = env
    owner = "synthetic-character-" + uuid.uuid4().hex
    card = cards.create(owner, Profile(name="Synthetic tutor", personality="Calm"))
    with pytest.raises(Slice1Error): cards.get("synthetic-other", card["card_id"])
    cards.publish(owner, card["card_id"], 1)
    cards.collect("synthetic-other", card["card_id"])
    cards.collect("synthetic-other", card["card_id"])
    draft = cards.edit(owner, card["card_id"], 1, Profile(name="Private edited tutor"))
    assert draft["card_id"] == card["card_id"] and draft["revision"] == 2
    assert cards.get("synthetic-other", card["card_id"])["profile"]["name"] == "Synthetic tutor"
    assert len(cards.list("synthetic-other")) == 1
    with pytest.raises(Slice1Error) as conflict:
        cards.edit(owner, card["card_id"], 1, Profile(name="Stale"))
    assert conflict.value.status == 409
    with cards.db.session() as sess:
        assert sess.get(CharacterRevisionRow, (card["card_id"], 1)).profile_json != sess.get(CharacterRevisionRow, (card["card_id"], 2)).profile_json
    cards.delete(owner, card["card_id"])
    assert not cards.list("synthetic-other")
    with pytest.raises(Slice1Error): cards.get(owner, card["card_id"])


def image():
    output = io.BytesIO(); Image.new("RGBA", (100, 200), (20, 40, 30, 80)).save(output, format="PNG")
    return output.getvalue()


def generated_image(part="portrait"):
    output=io.BytesIO()
    canvas=Image.new("RGBA",(100,200) if part=="portrait" else (200,100),(0,0,0,0))
    ImageDraw.Draw(canvas).rounded_rectangle(
        (20,10,80,190) if part=="portrait" else (10,30,190,70),radius=8,fill=(20,40,30,255))
    canvas.save(output,format="PNG")
    return output.getvalue()


def test_pointing_part_isolates_arm_from_repeated_full_reference():
    canvas=Image.new("RGBA",(900,600),(0,0,0,0));draw=ImageDraw.Draw(canvas)
    draw.rectangle((30,20,280,580),fill=(200,40,30,255))
    draw.rectangle((450,210,850,310),fill=(30,100,140,255))
    result=isolated_pointing_part(canvas)
    assert result.width>3*result.height
    assert result.getpixel((result.width//2,result.height//2))==(30,100,140,255)
    assert all(red!=200 for red,green,blue,alpha in result.getdata() if alpha)


@pytest.mark.parametrize("case",["empty","only_portrait","ambiguous"])
def test_pointing_part_rejects_missing_or_ambiguous_components(case):
    from backend.slice1.errors import AdapterError
    canvas=Image.new("RGBA",(900,600),(0,0,0,0));draw=ImageDraw.Draw(canvas)
    if case=="only_portrait":
        draw.rectangle((30,20,280,580),fill="red")
    if case=="ambiguous":
        draw.rectangle((50,50,450,150),fill="red")
        draw.rectangle((50,350,450,450),fill="blue")
    with pytest.raises(AdapterError):
        isolated_pointing_part(canvas)


@pytest.mark.parametrize("changes,expected", [
    ({"tags": ["synthetic"] * 13}, "人物标签最多 12 个"),
    ({"identity": "x" * 241}, "人物身份最多 240 字"),
    ({"name": " "}, "人物名字不能为空"),
    ({"private_unknown_field": "synthetic-private-value"}, "提交的字段不符合要求"),
])
def test_edit_validation_identifies_fields_without_echoing_private_input(env, changes, expected):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from backend.characters.router import create_character_router
    from backend.slice1.errors import as_detail
    cards, media = env
    owner = "synthetic-validation-" + uuid.uuid4().hex
    card = cards.create(owner, Profile(name="Synthetic tutor"))
    app = FastAPI()
    app.include_router(create_character_router(cards, media, lambda request: SimpleNamespace(user_id=owner)))
    @app.exception_handler(Slice1Error)
    async def handled(request, exc):
        return JSONResponse(status_code=exc.status, content=as_detail(exc))
    response = TestClient(app).patch("/api/cards/" + card["card_id"], json={
        "expected_revision": card["revision"], "profile": {**card["profile"], **changes},
    })
    assert response.status_code == 422
    assert expected in response.json()["message"]
    assert response.json()["error_code"] == "invalid_field"
    assert "synthetic-private-value" not in response.text
    assert "private_unknown_field" not in response.text
    assert cards.get(owner, card["card_id"])["revision"] == card["revision"]


def test_preserve_alpha_aspect_private_candidates_confirm_and_publish(env):
    cards, _ = env
    owner = "synthetic-assets-" + uuid.uuid4().hex
    card = cards.create(owner, Profile(name="Synthetic tutor"))
    payload, mime = normalize_media(image(), "image")
    with Image.open(io.BytesIO(payload)) as decoded:
        assert decoded.size == (100, 200) and decoded.getpixel((0, 0))[3] == 80
    asset = cards.add_asset(owner, card["card_id"], 1, payload, "image", mime)
    with pytest.raises(Slice1Error):
        cards.edit(owner, card["card_id"], 1, Profile(name="Synthetic tutor", media_ids=[asset], base_asset_id=asset))
    cards.confirm_asset(owner, card["card_id"], asset)
    updated = cards.edit(owner, card["card_id"], 1, Profile(name="Synthetic tutor", media_ids=[asset], base_asset_id=asset))
    cards.publish(owner, card["card_id"], updated["revision"])
    assert cards.asset("synthetic-other", card["card_id"], asset)[0] == payload
    cards.delete(owner, card["card_id"])
    with pytest.raises(Slice1Error): cards.asset("synthetic-other", card["card_id"], asset)


@pytest.mark.parametrize("kind,data", [("image", b"not an image"), ("video", b"not a video"), ("image", b"<svg/>"), ("video", b"")])
def test_invalid_upload_rejected(kind, data):
    with pytest.raises(Slice1Error): normalize_media(data, kind)


def test_video_decode_accepts_short_mp4_and_rejects_truncated_file(tmp_path):
    import subprocess
    import imageio_ffmpeg
    path = tmp_path / "synthetic.mp4"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=green:s=80x120:d=1", "-an", "-c:v", "libx264", str(path)],
        check=True, capture_output=True, timeout=15)
    payload = path.read_bytes()
    assert normalize_media(payload, "video") == (payload, "video/mp4")
    with pytest.raises(Slice1Error): normalize_media(payload[:80], "video")


def test_media_idempotency_owner_revision_and_cancel_no_provider_calls(env):
    cards, media = env
    owner = "synthetic-media-" + uuid.uuid4().hex
    card = cards.create(owner, Profile(name="Synthetic tutor"))
    config = cards.configs.create(actor_id=owner, is_admin=False, kind="image_generation", protocol="openai_image",
        endpoint="https://image.example.test/v1", model_name="synthetic-image", credentials={"api_key": "synthetic-key"})
    request = MediaRequest(kind="image", revision=1, config_id=config["config_id"], config_version=1, count=2)
    result = media.create(owner, card["card_id"], request, "synthetic-idempotency")
    again = media.create(owner, card["card_id"], request, "synthetic-idempotency")
    assert again["deduped"] and again["task_id"] == result["task_id"]
    with pytest.raises(Slice1Error): media.create(owner, card["card_id"], request.model_copy(update={"count": 1}), "synthetic-idempotency")
    with pytest.raises(Slice1Error): media.cancel("synthetic-other", card["card_id"], result["task_id"])
    media.cancel(owner, card["card_id"], result["task_id"])
    assert media.tasks.get_task(result["task_id"])["cancel_requested"]


def test_tombstone_prevents_media_late_publication(env):
    cards, media = env
    owner = "synthetic-fence-" + uuid.uuid4().hex
    card = cards.create(owner, Profile(name="Synthetic tutor"))
    config = cards.configs.create(actor_id=owner, is_admin=False, kind="image_generation", protocol="openai_image",
        endpoint="https://image.example.test/v1", model_name="synthetic-image", credentials={"api_key": "synthetic-key"})
    request = MediaRequest(kind="image", revision=1, config_id=config["config_id"], config_version=1)
    result = media.create(owner, card["card_id"], request, "synthetic-fence")
    lease = media.tasks.claim(result["task_id"], lease_owner="synthetic-worker")
    with cards.db.session() as sess:
        job = sess.get(CharacterMediaJobRow, result["task_id"]); sess.expunge(job)
    cards.delete(owner, card["card_id"])
    with pytest.raises(Exception): media._guard(job, lease)
    media.tasks.recover_worker_failure(lease, stage="generate", error_code="LEASE_LOST")


@pytest.mark.parametrize("request_seconds", [0, 120, 299])
def test_image_job_publishes_private_candidates_without_cover_crop(env, monkeypatch, request_seconds):
    import base64
    from datetime import timedelta
    import backend.tasks.service as task_service
    clock = task_service.utcnow()
    monkeypatch.setattr(task_service, "utcnow", lambda: clock)
    cards, media = env
    owner = "synthetic-publish-" + uuid.uuid4().hex
    card = cards.create(owner, Profile(name="Synthetic tutor"))
    config = cards.configs.create(actor_id=owner, is_admin=False, kind="image_generation", protocol="openai_image",
        endpoint="https://image.example.test/v1", model_name="synthetic-image", credentials={"api_key": "synthetic-key"})
    class Adapter:
        def _image_request(self, body):
            nonlocal clock
            clock += timedelta(seconds=request_seconds)
            self.authorize_request()
            return {"data": [{"b64_json": base64.b64encode(generated_image()).decode()}]}
        def close(self):
            pass
    def adapter_factory(*args, **kwargs):
        assert kwargs["timeout"] == 300
        return Adapter()
    media.adapter_factory = adapter_factory
    job = media.create(owner, card["card_id"], MediaRequest(kind="image", revision=1,
        config_id=config["config_id"], config_version=1, count=2), "synthetic-publish")
    media.run(job["task_id"])
    assert media.tasks.get_task(job["task_id"])["status"] == "succeeded"
    result = cards.get(owner, card["card_id"])
    assert len(result["assets"]) == 2 and not any(a["confirmed"] for a in result["assets"])
    for asset in result["assets"]:
        with Image.open(io.BytesIO(cards.asset(owner, card["card_id"], asset["asset_id"])[0])) as decoded:
            assert decoded.size == (100, 200) and decoded.mode == "RGBA"


@pytest.mark.parametrize("part", ["upper_arm", "forearm", "pointing_body"])
def test_new_pointing_part_requests_are_rejected_even_with_confirmed_reference(env, part):
    cards, media = env
    owner = "synthetic-pointing-" + uuid.uuid4().hex
    card = cards.create(owner, Profile(name="Synthetic tutor"))
    config = cards.configs.create(actor_id=owner, is_admin=False, kind="image_generation", protocol="openai_image",
        endpoint="https://image.example.test/v1", model_name="synthetic-image", credentials={"api_key": "synthetic-key"})
    request = MediaRequest(kind="image", image_part=part, revision=1,
        config_id=config["config_id"], config_version=1)
    with pytest.raises(Slice1Error) as error:
        media.create(owner, card["card_id"], request, "synthetic-missing-base")
    assert error.value.status == 422
    asset = cards.add_asset(owner, card["card_id"], 1, image(), "image", "image/png", confirmed=True)
    card = cards.edit(owner, card["card_id"], 1, Profile(name="Synthetic tutor",
        media_ids=[asset], base_asset_id=asset))
    request = request.model_copy(update={"revision":card["revision"]})
    with pytest.raises(Slice1Error) as error:
        media.create(owner, card["card_id"], request, "synthetic-part")
    assert error.value.status == 422 and error.value.code == "invalid_field"


def test_pointing_body_matches_base_and_is_private_frozen_asset(env):
    cards, media = env
    owner = "synthetic-body-" + uuid.uuid4().hex
    card = cards.create(owner, Profile(name="Synthetic tutor"))
    ids = [cards.add_asset(owner,card["card_id"],1,image(),"image","image/png",confirmed=True) for _ in range(4)]
    profile=Profile.model_validate({"name":"Synthetic tutor","media_ids":ids,"base_asset_id":ids[0],
        "pointing_rig":{"upper_arm_asset_id":ids[1],"forearm_asset_id":ids[2],
            "body_asset_id":ids[3],"reference_asset_id":ids[0]}})
    saved=cards.edit(owner,card["card_id"],1,profile)
    assert next(a for a in saved["assets"] if a["asset_id"]==ids[3])["image_part"]=="pointing_body"
    prompt=media._prompt(saved["profile"],{"scene":"neutral","image_part":"pointing_body"})
    assert "REMOVE ENTIRELY" in prompt and "Do not add an arm" in prompt
    assert "tea tray" not in prompt and "her RIGHT" not in prompt
    arm=media._prompt(saved["profile"],{"scene":"neutral","image_part":"upper_arm"})
    assert "maid" not in arm and "puff sleeve" not in arm
    changed=profile.model_copy(update={"base_asset_id":ids[1]})
    with pytest.raises(Slice1Error):
        cards.edit(owner,card["card_id"],saved["revision"],changed)
    cards.publish(owner,card["card_id"],saved["revision"])
    copied=cards.copy("synthetic-copy-"+uuid.uuid4().hex,card["card_id"])
    rig=copied["profile"]["pointing_rig"]
    assert rig["body_asset_id"] in copied["profile"]["media_ids"]
    assert rig["reference_asset_id"]==copied["profile"]["base_asset_id"]
    assert rig["body_asset_id"]!=ids[3]


@pytest.mark.parametrize("scene,fail", [("neutral",False),("explaining",False),("neutral",True)])
def test_single_image_replacement_publishes_complete_portrait_atomically(env, scene, fail):
    import base64
    from backend.slice1.errors import AdapterError
    cards,media=env
    owner="synthetic-replace-"+uuid.uuid4().hex
    card=cards.create(owner,Profile(name="Synthetic tutor"))
    old=cards.add_asset(owner,card["card_id"],1,image(),"image","image/png",confirmed=True)
    parts=[cards.add_asset(owner,card["card_id"],1,image(),"image","image/png",confirmed=True) for _ in range(3)]
    card=cards.edit(owner,card["card_id"],1,Profile.model_validate({
        "name":"Synthetic tutor","media_ids":[old,*parts],"base_asset_id":old,
        "pointing_rig":{"body_asset_id":parts[0],"upper_arm_asset_id":parts[1],
            "forearm_asset_id":parts[2],"reference_asset_id":old}}))
    from backend.characters.persona import PersonaPackage
    card=cards.edit_persona(owner,card["card_id"],card["revision"],
        PersonaPackage(skill="# Synthetic complete persona\nPreserve causal explanations and evidence.",
                       references=[{"name":"01-writings.md","body":"Synthetic frozen report."}]))
    config=cards.configs.create(actor_id=owner,is_admin=False,kind="image_generation",protocol="openai_image",
        endpoint="https://image.example.test/v1",model_name="synthetic-image",credentials={"api_key":"synthetic-key"})
    calls=[]
    buffer=io.BytesIO()
    sample=Image.new("RGBA",(100,200),(20,40,30,255))
    sample.putpixel((0,0),(0,0,0,0))
    sample.save(buffer,format="PNG")
    payload=buffer.getvalue()
    class Adapter:
        def _image_request(self,body,reference=None):
            self.authorize_request()
            calls.append(body)
            if fail:
                raise AdapterError("capability_test_failed")
            assert reference==image()
            return {"data":[{"b64_json":base64.b64encode(payload).decode()}]}
        def close(self):
            pass
    media.adapter_factory=lambda *args,**kwargs:Adapter()
    job=media.create(owner,card["card_id"],MediaRequest(kind="image",replace_current=True,revision=card["revision"],
        config_id=config["config_id"],config_version=1,scene=scene),"synthetic-replacement")
    media.run(job["task_id"])
    saved=cards.get(owner,card["card_id"])
    assert saved["persona"]==card["persona"]
    if fail:
        assert saved["revision"]==card["revision"] and saved["profile"]==card["profile"]
        assert len(saved["assets"])==4
        assert media.tasks.get_task(job["task_id"])["status"]=="failed"
    else:
        assert saved["revision"]==card["revision"]+1
        assert len(calls)==1
        assert "naturally attached" in calls[0]["prompt"]
        assert "attach at the shoulder" not in calls[0]["prompt"]
        assert all(asset["confirmed"] for asset in saved["assets"])
        if scene=="neutral":
            assert saved["profile"]["base_asset_id"]!=old
            assert saved["profile"]["pointing_rig"] is None
            assert len(saved["profile"]["media_ids"])==1 and old not in saved["profile"]["media_ids"]
            assert not set(parts)&set(saved["profile"]["media_ids"])
            assert len(saved["assets"])==5
            with cards.db.session() as session:
                from backend.characters.models import CharacterRevisionRow
                previous=json.loads(session.get(CharacterRevisionRow,(card["card_id"],card["revision"])).profile_json)
                assert previous["pointing_rig"]==card["profile"]["pointing_rig"]
        else:
            assert saved["profile"]["base_asset_id"]==old
