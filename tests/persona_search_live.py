"""Explicit paired direct-retrieval control. Never persists credentials or search bodies."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from time import monotonic
import uuid

ROOT=Path(__file__).resolve().parents[1]


async def run(supplied):
    os.environ.update(CONTENT_TRACE_ENABLED="0",OTEL_ENABLED="0")
    sys.path.insert(0,str(ROOT/"server/backend"))
    from app.assembler import create_app
    from app.slice1.capability_refresh import CapabilityRefresher
    from app.slice1.adapters.anysearch import AnySearchAdapter
    from app.search.contracts import AnySearchRequest
    from app.search.policy import normalize_envelope
    from app.characters.evidence import read_public_source,page_status,canonical_source
    from app.agents.contracts import AgentDeps
    CapabilityRefresher.register=lambda *args:None
    app=create_app()
    learning=next(route.app for route in app.routes
        if getattr(route,"path",None) in ("","/") and hasattr(route,"app"))
    configs=learning.state.slice1.configs
    view=configs.active_search_for_owner(supplied["owner"])
    credentials=configs.resolve_search_credentials(view["config_id"],owner_user_id=supplied["owner"],
        expected_version=view["config_version"])
    rows=[]
    for case in supplied["cases"]:
        started=monotonic()
        adapter=AnySearchAdapter(endpoint=view["endpoint"],credentials=credentials)
        row={"id":case["id"],"status":"failed"}
        try:
            def check(*args):
                current=configs.get(view["config_id"],actor_id=supplied["owner"],is_admin=False)
                if not current["is_active"] or current["config_version"]!=view["config_version"]:
                    raise ValueError("configuration changed")
            raw=await adapter.search(AnySearchRequest(query=case["query"],max_results=10),
                check_outbound=check,before_wire=check,remaining_seconds=lambda:60-(monotonic()-started))
            outcome=normalize_envelope(raw,query_id=uuid.uuid4())
            row.update(status=outcome.state,duration_ms=int((monotonic()-started)*1000),
                candidate_count=len(getattr(outcome,"results",[])),sources=[])
            for item in getattr(outcome,"results",[]):
                url=canonical_source(item.url)
                if not url:continue
                try:
                    body=await read_public_source(item.url,AgentDeps())
                    status=page_status(body)
                except Exception:
                    body="";status="unavailable"
                row["sources"].append({"url":url,"title":item.title,"read_status":status,"chars":len(body)})
        except Exception as exc:
            row["error_code"]=getattr(exc,"code","SEARCH_OUTPUT_INVALID")
        finally:await adapter.aclose()
        rows.append(row)
        print(json.dumps({key:value for key,value in row.items() if key!="sources"}),flush=True)
        if row.get("error_code") in ("SEARCH_CREDENTIAL_INVALID","SEARCH_QUOTA_EXCEEDED","SEARCH_RATE_LIMITED"):break
    output=ROOT/".backup/persona-evidence"
    output.mkdir(parents=True,exist_ok=True)
    (output/"anysearch-control.json").write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding="utf8")
    return 0 if len(rows)==3 and all(row["status"]=="succeeded" for row in rows) else 1


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--live",action="store_true")
    if not parser.parse_args().live:parser.error("real calls require --live")
    return asyncio.run(run(json.load(sys.stdin)))


if __name__=="__main__":
    sys.exit(main())
