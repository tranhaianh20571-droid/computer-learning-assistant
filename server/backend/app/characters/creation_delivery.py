"""One private, checkpointed character delivery using existing media workers."""
import json
import re
from datetime import UTC, datetime

from sqlalchemy import select

from ..planning.errors import error
from ..tasks.service import LearningTaskRow
from .distillation_models import CharacterBuildRow
from .models import CharacterAssetRow, CharacterMediaJobRow
from .schemas import MediaRequest, Profile
from .service import reject
from .voice_policy import recommend_voices

SCENES = ("neutral", "explaining", "thinking", "encouraging", "celebrating")
CONFIG_KINDS = {"image": "image_generation", "voice": "tts"}


def freeze_configs(service, owner):
    frozen = {}
    for role, kind in CONFIG_KINDS.items():
        config = service.cards.configs.latest_active_for_owner(owner, kind)
        if not config or config["owner_scope"] != "user":
            reject("config_not_found", 409, message="完整创作需要先配置本人的图片生成与语音服务。")
        frozen[role] = {"config_id": config["config_id"], "config_version": config["config_version"]}
    return frozen


def check_configs(configs, owner, frozen, *, session=None):
    for role, kind in CONFIG_KINDS.items():
        if session is None:
            config = configs.get(frozen[role]["config_id"], actor_id=owner, is_admin=False)
        else:
            from ..slice1.models import ServiceConfigRow
            row = session.get(ServiceConfigRow, frozen[role]["config_id"], with_for_update=True)
            if not row or row.owner_user_id != owner:
                raise error("DISCLOSURE_REVOKED")
            config = {key: getattr(row, key) for key in ("kind", "owner_scope", "is_active", "config_version")}
        if (not config["is_active"] or config["owner_scope"] != "user" or config["kind"] != kind
            or config["config_version"] != frozen[role]["config_version"]):
            raise error("DISCLOSURE_REVOKED")


def guard_media_creation(media, owner, card_id, revision, creation):
    from .distillation import build_domain, utc_deadline

    with media.db.session() as sess:
        row = sess.get(CharacterBuildRow, creation["build_id"])
        root = sess.get(LearningTaskRow, row.task_id) if row else None
        if (not row or row.owner_user_id != owner or row.card_id != card_id
            or row.card_revision != revision or not root or root.cancel_requested
            or root.generation != creation["generation"] or root.attempt_id != creation["attempt_id"]
            or root.status not in ("leased", "running") or not root.lease_until
            or utc_deadline(root.lease_until) <= datetime.now(UTC)):
            raise error("TASK_CANCELLED")
        build_domain(sess, root)
        request = json.loads(row.request_json)
        if not request.get("complete_card"):
            raise error("TASK_CANCELLED")
    check_configs(media.configs, owner, request["delivery_configs"])


def select_voice(profile, voices):
    text = " ".join(str(profile.get(key, "")) for key in (
        "identity", "background", "personality", "speaking_style"))
    english = bool(re.search(
        r"(?:英语|英文)(?:授课|讲解|交流|对话)|(?:说|使用|讲)英语|"
        r"English[- ]speaking|speak English|(?:语言|language)\s*[:：]?\s*(?:英语|英文|English)",
        text, re.I))
    wanted = r"English|英语|英文" if english else r"Chinese|Mandarin|中文|汉语|普通话"
    foreign = r"Spanish|Japanese|Korean|French|German|Russian|Arabic|Portuguese|Italian"
    candidates = []
    for voice in voices:
        label = " ".join(str(voice.get(key, "")) for key in ("voice_id", "name", "description"))
        if re.search(wanted, label, re.I) and not re.search(foreign, str(voice.get("voice_id", "")), re.I):
            candidates.append(voice)
    voice = recommend_voices(profile, candidates)["recommended_voice_id"]
    if not voice:
        reject("capability_unavailable", 409, message="当前语音服务没有与人物语言和性别相符的音色。")
    return voice


def complete_card(service, root, request, result):
    from . import distillation_prompts as prompts
    from .distillation import build_domain, encode

    profile = Profile.model_validate(result["profile"]).model_dump()
    if not all(profile[key].strip() for key in ("identity", "background", "personality", "appearance")):
        raise error("OUTPUT_INVALID")
    frozen = request["delivery_configs"]
    media = service.media_service
    check_configs(media.configs, root.owner_user_id, frozen)
    with service.db.session() as sess:
        row = sess.scalar(select(CharacterBuildRow).where(CharacterBuildRow.task_id == root.task_id))
        build_id, card_id, revision = row.build_id, row.card_id, row.card_revision
    assets = []

    def progress():
        result["completion"] = {
            "status": "running", "completed_images": sum(item["kind"] == "image" for item in assets),
            "total_images": len(SCENES), "voice_ready": any(item["kind"] == "voice" for item in assets),
        }
        with service.db.session() as sess:
            service.tasks.validate_lease(root, session=sess)
            build_domain(sess, sess.get(LearningTaskRow, root.task_id))
            sess.get(CharacterBuildRow, build_id).result_json = encode(result)

    progress()
    voice = service._stage(root, "delivery_voice_match", {"profile": profile, "config": frozen["voice"]},
        None, prompts.COMMON, work=lambda task_id: {
            "voice_id": select_voice(profile, media._voice_catalog(root.owner_user_id, frozen["voice"]["config_id"]))})
    profile.update(voice_id=voice["voice_id"], voice_config_id=frozen["voice"]["config_id"], voice_selection="auto")
    media.start()

    def prepare(kind, scene, reference=""):
        config = frozen["image" if kind == "image" else "voice"]
        payload = {"kind": kind, "scene": scene, "profile": profile,
                   "config": config, "reference_asset_id": reference}

        def generate(_task_id):
            check_configs(media.configs, root.owner_user_id, frozen)
            # A provider result can commit just before its parent checkpoint fails.
            with service.db.session() as sess:
                completed = sess.scalars(select(CharacterMediaJobRow).join(
                    LearningTaskRow, LearningTaskRow.task_id == CharacterMediaJobRow.task_id).where(
                        CharacterMediaJobRow.card_id == card_id,
                        CharacterMediaJobRow.owner_user_id == root.owner_user_id,
                        CharacterMediaJobRow.revision == revision,
                        CharacterMediaJobRow.config_id == config["config_id"],
                        CharacterMediaJobRow.config_version == config["config_version"],
                        LearningTaskRow.status == "succeeded").order_by(
                            LearningTaskRow.created_at.desc()).limit(50))
                for saved in completed:
                    original = json.loads(saved.request_json)
                    context = original.get("creation", {})
                    if (context.get("build_id") == build_id and context.get("profile") == profile
                        and context.get("reference_asset_id", "") == reference
                        and original["kind"] == kind and original["scene"] == scene):
                        return {"asset_ids": json.loads(saved.result_json), "kind": kind, "scene": scene,
                                "media_task_id": saved.task_id}
            created = media.create(root.owner_user_id, card_id, MediaRequest(
                kind=kind, scene=scene, revision=revision, **config),
                f"{build_id}:{kind}:{scene}:{root.generation}", creation={
                    "build_id": build_id, "generation": root.generation, "attempt_id": root.attempt_id,
                    "profile": profile, "reference_asset_id": reference,
                })
            while True:
                with service.db.session() as sess:
                    service.tasks.validate_lease(root, session=sess)
                    build_domain(sess, sess.get(LearningTaskRow, root.task_id))
                    job = sess.get(CharacterMediaJobRow, created["task_id"])
                    task = sess.get(LearningTaskRow, created["task_id"])
                    if task.status == "succeeded":
                        return {"asset_ids": json.loads(job.result_json), "kind": kind, "scene": scene,
                                "media_task_id": created["task_id"]}
                    if task.status in ("failed", "cancelled", "expired"):
                        code = service.tasks.get_task(task.task_id)["error_code"] or "OUTPUT_INVALID"
                        raise error(code)
                if service.stop_event.wait(.25):
                    raise error("TASK_CANCELLED")

        prepared = service._stage(root, "delivery_" + kind + "_" + scene, payload,
            None, prompts.COMMON, work=generate)
        assets.append(prepared)
        progress()
        return prepared["asset_ids"][0]

    prepare("voice", "neutral")
    base = prepare("image", "neutral")
    for scene in SCENES[1:]:
        prepare("image", scene, base)
    profile.update(media_ids=[ident for item in assets for ident in item["asset_ids"]],
                   base_asset_id=base, pointing_rig=None)
    result["profile"] = profile
    result["completion"] = {"status": "complete", "completed_images": len(SCENES),
                            "total_images": len(SCENES), "voice_ready": True, "assets": assets}
    return result


def confirm_assets(service, sess, root, request, result):
    check_configs(service.cards.configs, root.owner_user_id, request["delivery_configs"], session=sess)
    row = sess.scalar(select(CharacterBuildRow).where(CharacterBuildRow.task_id == root.task_id))
    for item in result["completion"]["assets"]:
        task = sess.get(LearningTaskRow, item["media_task_id"])
        job = sess.get(CharacterMediaJobRow, item["media_task_id"])
        if not task or task.status != "succeeded" or not job or job.card_id != row.card_id:
            raise error("STALE_RESULT_DISCARDED")
        for ident in item["asset_ids"]:
            asset = sess.get(CharacterAssetRow, ident, with_for_update=True)
            if (not asset or asset.owner_user_id != root.owner_user_id or asset.card_id != row.card_id
                or asset.source_revision != row.card_revision or asset.scene != item["scene"]
                or asset.kind != item["kind"]):
                raise error("STALE_RESULT_DISCARDED")
            asset.confirmed = True
    sess.flush()
