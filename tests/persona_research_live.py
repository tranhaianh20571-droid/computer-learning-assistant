"""Explicit Nuwa experiment. Credentials arrive on stdin and are encrypted by ConfigService."""
import argparse
import json
import os
from pathlib import Path
import sys
import uuid
from time import monotonic

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--live",action="store_true")
    args=parser.parse_args()
    if not args.live:
        parser.error("real provider calls require --live")
    supplied=json.load(sys.stdin)
    os.environ.update(CONTENT_TRACE_ENABLED="0",OTEL_ENABLED="0")
    sys.path.insert(0,str(ROOT/"server/backend"))
    from app.assembler import create_app
    from app.slice1.capability_refresh import CapabilityRefresher
    from app.agents.model_routing import role_views,save_role_configuration
    from app.slice1.router import RoleConfigurationIn
    from app.characters.distillation_models import BuildRequest
    CapabilityRefresher.register=lambda *args:None
    app=create_app()
    learning=next(route.app for route in app.routes
        if getattr(route,"path",None) in ("","/") and hasattr(route,"app"))
    service=learning.state.slice3.distillation
    owner=supplied["owner"]
    baseline=service.cards.get(owner,supplied["baseline_card"])
    if supplied.get("key"):
        binding=next(row for row in role_views(service.cards.configs,owner) if row["role"]=="research_search")
        current=binding["configuration"]
        save_role_configuration(service.cards.configs,owner,"research_search",RoleConfigurationIn(
            expected_revision=binding["revision"],config_id=binding["config_id"],
            config_version=current["config_version"] if current else None,protocol="openai",
            endpoint=supplied["endpoint"],model_name=supplied["model"],api_key=supplied.pop("key")))
    service.kick=lambda ident:None
    if supplied.get("diagnose"):
        original_code=service._code
        def code(exc):
            mapped=original_code(exc)
            print(json.dumps({"diagnostic_type":type(exc).__name__,"mapped_error":mapped}),flush=True)
            return mapped
        service._code=code
    original_stage=service._stage
    def stage(root,key,*args,**kwargs):
        started=monotonic()
        try:
            result=original_stage(root,key,*args,**kwargs)
            print(json.dumps({"stage":key,"status":"succeeded",
                "duration_ms":int((monotonic()-started)*1000)}),flush=True)
            return result
        except Exception as exc:
            print(json.dumps({"stage":key,"status":"failed","error_code":service._code(exc)}),flush=True)
            raise
    service._stage=stage
    provider=supplied.get("search_provider","anysearch")
    if provider not in ("anysearch","grok"):
        raise ValueError("unsupported search provider")
    if supplied.get("retry_build_id"):
        created=service.action(owner,supplied["retry_build_id"],"retry")
    else:
        created=service.create(owner,BuildRequest(mode="ai",topic=supplied.get("topic","Sal Khan"),
            purpose=supplied.get("purpose","公开资料启发的掌握学习人物导师；保留本人原始教学证据与完整六维原稿。"),
            search_provider=provider,research_policy=supplied.get("research_policy",{"max_searches":8,"max_reads":40}),
            reuse_build_id=supplied.get("reuse_build_id",""),
            subject=supplied.get("subject",{"kind":"real"}),
            mentor_evaluation=supplied.get("mentor_evaluation",False),
            confirmed=True,request_key=uuid.uuid4().hex))
    print(json.dumps({"build_id":created["build_id"],"status":"started"}),flush=True)
    outcome=service.run(created["build_id"])
    saved=service.get(owner,created["build_id"])
    result={"build_id":created["build_id"],"status":saved["status"],"error_code":saved["error_code"],
        "search_provider":saved["request"].get("search_provider"),
        "evidence":saved["evidence"],"quality_passed":saved.get("result",{}).get(
            "persona",{}).get("quality",{}).get("passed",False),
        "baseline_revision":baseline["revision"],"stages":len(saved["stages"])}
    output=ROOT/".backup/persona-evidence"
    output.mkdir(parents=True,exist_ok=True)
    (output/"live-build.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf8")
    print(json.dumps(result,ensure_ascii=True),flush=True)
    return 0 if outcome.get("accepted") else 1


if __name__=="__main__":
    sys.exit(main())
