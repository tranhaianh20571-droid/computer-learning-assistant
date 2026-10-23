"""Explicitly accept a completed persona and reuse verified images, without generation."""
import argparse
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
RIG_KEYS=("body_asset_id","upper_arm_asset_id","forearm_asset_id","reference_asset_id")


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--live",action="store_true")
    args=parser.parse_args()
    if not args.live:
        parser.error("card mutations require --live and explicit stdin parameters")
    supplied=json.load(sys.stdin)
    os.environ.update(CONTENT_TRACE_ENABLED="0",OTEL_ENABLED="0")
    sys.path.insert(0,str(ROOT/"server/backend"))
    from PIL import Image
    from app.assembler import create_app
    from app.slice1.capability_refresh import CapabilityRefresher
    from app.characters.schemas import Profile
    from app.characters.persona import PersonaPackage
    from app.characters.defaults import install_system_default
    CapabilityRefresher.register=lambda *args:None
    app=create_app()
    service=next(route.app for route in app.routes
        if getattr(route,"path",None) in ("","/") and hasattr(route,"app")).state.slice3.distillation
    service.kick=lambda ident:None
    cards=service.cards
    owner=supplied["owner"]
    build=service.get(owner,supplied["build_id"])
    if build["status"]!="succeeded" or "persona" not in build["result"]:
        raise ValueError("a complete persisted persona is required")
    baseline=cards.get(owner,supplied["baseline_card"])
    baseline_profile=baseline["profile"]
    rig=baseline_profile.get("pointing_rig") or {}
    selected=baseline_profile["media_ids"]
    assets=[item for item in baseline["assets"] if item["asset_id"] in selected]
    if (len(assets)!=len(selected) or not assets or
            any(item["kind"]!="image" or not item["confirmed"] for item in assets) or
            not {"neutral","explaining","thinking","encouraging","celebrating"}<=
                {item["scene"] for item in assets} or
            any(rig.get(key) not in selected for key in RIG_KEYS)):
        raise ValueError("baseline approved images and pointing rig are incomplete")
    verified={}
    for item in assets:
        payload,mime=cards.asset(owner,baseline["card_id"],item["asset_id"])
        with Image.open(BytesIO(payload)) as image:
            if mime!="image/png" or image.format!="PNG":
                raise ValueError("baseline image format is invalid")
            image.verify()
        with Image.open(BytesIO(payload)) as image:
            if image.mode!="RGBA" or min(image.size)<16 or image.getchannel("A").getextrema()!=(0,255):
                raise ValueError("baseline image dimensions or alpha are invalid")
        verified[item["asset_id"]]=(payload,mime)
    card=service.accept(owner,build["build_id"])
    if supplied.get("update_baseline") is True:
        if (not card["persona"]["quality"].get("passed") or
                supplied.get("install_default") is True and not card["persona"]["quality"].get("default_eligible")):
            raise ValueError("baseline promotion requires persona and requested mentor acceptance")
        if baseline.get("persona")!=card["persona"]:
            profile={**card["profile"],**{key:baseline_profile[key] for key in (
                "appearance","media_ids","base_asset_id","pointing_rig","voice_id","voice_config_id","voice_selection")}}
            updated=cards.edit(owner,baseline["card_id"],baseline["revision"],Profile.model_validate(profile))
            card=cards.edit_persona(owner,updated["card_id"],updated["revision"],
                PersonaPackage.model_validate(card["persona"]))
        else:
            card=baseline
    if not card["profile"]["media_ids"]:
        mapping={}
        for item in assets:
            payload,mime=verified[item["asset_id"]]
            mapping[item["asset_id"]]=cards.add_asset(owner,card["card_id"],card["revision"],
                payload,"image",mime,item["scene"],confirmed=True)
        profile={**card["profile"],"appearance":baseline_profile["appearance"],
            "media_ids":[mapping[ident] for ident in selected],
            "base_asset_id":mapping[baseline_profile["base_asset_id"]],
            "pointing_rig":{**rig,**{key:mapping[rig[key]] for key in RIG_KEYS}}}
        card=cards.edit(owner,card["card_id"],card["revision"],Profile.model_validate(profile))
    current_rig=card["profile"].get("pointing_rig") or {}
    current_ids=card["profile"]["media_ids"]
    if len(current_ids)!=len(selected) or any(current_rig.get(key) not in current_ids for key in RIG_KEYS):
        raise ValueError("accepted card image mapping is incomplete")
    for old,new in zip(selected,current_ids,strict=True):
        payload,_=cards.asset(owner,card["card_id"],new)
        if sha256(payload).digest()!=sha256(verified[old][0]).digest():
            raise ValueError("reused image bytes differ")
    quality=card["persona"]["quality"]
    published=False
    installed=False
    if quality["passed"] and card["can_publish"] and supplied.get("publish") is True:
        if supplied.get("install_default") is True and (
                quality.get("schema_version",1)<2 or quality.get("default_eligible") is True):
            install_system_default(cards,owner,card["card_id"],card["revision"],publish=True)
            card=cards.get(owner,card["card_id"])
            published=True
            installed=True
        else:
            card=cards.publish(owner,card["card_id"],card["revision"])
            published=True
    result={"build_id":build["build_id"],"card_id":card["card_id"],"revision":card["revision"],
        "search_provider":build["request"].get("search_provider"),"quality_passed":quality["passed"],
        "failed_checks":[check["name"] for check in quality["report"]["checks"] if not check["passed"]],
        "images_reused":len(current_ids),"skill_characters":len(card["persona"]["skill"]),
        "grade":quality.get("grade"),"default_eligible":quality.get("default_eligible"),
        "research_reports":len(card["persona"]["references"]),"evidence":build["evidence"],
        "published":published,"installed_default":installed}
    output=ROOT/".backup/persona-evidence"
    output.mkdir(parents=True,exist_ok=True)
    (output/"accepted-card.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf8")
    print(json.dumps(result,ensure_ascii=True))
    return 0


if __name__=="__main__":
    sys.exit(main())
