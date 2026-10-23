"""Responses web-search connector with the existing authorized HTTP exit."""
import asyncio
from collections import Counter
from datetime import datetime, timezone

import httpx2

from ..agents.native import SafeModelTransport
from ..planning.errors import error
from .evidence import canonical_source


def parse_search_response(data, max_results=10):
    provider_error=data.get("error") if isinstance(data,dict) else None
    if isinstance(provider_error,dict):
        code=provider_error.get("code") or provider_error.get("type")
        if code in ("insufficient_quota","billing_error","quota_exceeded"):
            raise error("SEARCH_QUOTA_EXCEEDED")
        if code in ("invalid_api_key","authentication_error"):
            raise error("MODEL_CREDENTIAL_INVALID")
    if not isinstance(data,dict) or not isinstance(data.get("output"),list):
        raise error("SEARCH_OUTPUT_INVALID")
    output=[item for item in data["output"] if isinstance(item,dict)]
    calls=[item for item in output if item.get("type")=="web_search_call"
           and item.get("status")=="completed"]
    if data.get("status")!="completed" or not calls:
        raise error("SEARCH_OUTPUT_INVALID","search not proven")
    found={}
    def add(item):
        if not isinstance(item,dict):return
        url=canonical_source(item.get("url",""))
        if url and url not in found:
            found[url]={"url":item.get("url"),"title":str(item.get("title",""))[:300],"excerpt":""}
    # Prefer the answer's selected citations over the much larger raw result pool.
    for item in output:
        if item.get("type")=="message":
            for content in item.get("content") or []:
                if not isinstance(content,dict):continue
                for annotation in content.get("annotations") or []:
                    add(annotation)
    for call in calls:
        action=call.get("action")
        if isinstance(action,dict):add(action)
    for call in calls:
        action=call.get("action")
        if not isinstance(action,dict):continue
        for item in action.get("sources") or []:
            add(item)
    if not found:
        raise error("SEARCH_OUTPUT_INVALID","search URLs unavailable")
    return {"results":list(found.values())[:max_results],
        "metrics":{"completed_actions":len(calls),
                   "actions":dict(Counter(item["action"].get("type","unknown")
                       for item in calls if isinstance(item.get("action"),dict))),
                   "visible_urls":len(found)},
        "fetched_at":datetime.now(timezone.utc).isoformat()}


async def search_responses(endpoint, model, key, query, deps, *, max_results=10, max_tool_calls=3,
                           timeout=600, transport=None):
    """One submitted request; no automatic resubmission after an ambiguous failure."""
    from ..slice1.model_catalog import canonical_endpoint
    endpoint=canonical_endpoint(endpoint)
    payload={"model":model,"input":query+"\nUse web_search. Prefer original full texts that are readable by an anonymous HTTPS GET. "
             "Return at most "+str(max_results)+" distinct sources with URL citations. Prefer official HTML/PDF transcripts, "
             "signed articles, public letters, and complete interview transcripts. Exclude video landing pages, social media, "
             "login/paywall pages, book catalog pages, search result pages, and dynamic JavaScript-only pages. "
             "Do not invent quotations. No X search or code execution.",
             "tools":[{"type":"web_search"}],"max_tool_calls":max_tool_calls,
             "max_output_tokens":3500,"store":False}
    deps.check()
    deps.budget.consume("logical")
    try:
        async with asyncio.timeout(timeout):
            async with httpx2.AsyncClient(
                transport=transport or SafeModelTransport(endpoint,deps,max_bytes=2_000_000),
                timeout=httpx2.Timeout(timeout,connect=15,write=60,pool=15),
                follow_redirects=False,trust_env=False) as client:
                response=await client.post(endpoint+"/responses",json=payload,
                    headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"})
                if response.status_code!=200:
                    raise error("SEARCH_OUTPUT_INVALID")
                result=parse_search_response(response.json(),max_results)
                for _ in range(result["metrics"]["completed_actions"]):
                    deps.budget.consume("tool")
                deps.check()
                return result
    except (TimeoutError,httpx2.TimeoutException):
        raise error("MODEL_TIMEOUT") from None
    except (ValueError,httpx2.HTTPError):
        raise error("SEARCH_OUTPUT_INVALID") from None


def configured_grok_search(service, root, task_id, query, *, max_tool_calls=3):
    from types import SimpleNamespace
    from ..agents.model_routing import TaskModelRoleRow
    from ..agents.task_binding import bind_task_deps
    with service.db.session() as sess:
        pinned=sess.get(TaskModelRoleRow,(task_id,"research_search"))
        if not pinned:
            raise error("MODEL_CAPABILITY_UNAVAILABLE")
        view=service.cards.configs.get(pinned.config_id,actor_id=root.owner_user_id,is_admin=False)
        source=SimpleNamespace(config_id=pinned.config_id,config_version=pinned.config_version,
            endpoint=view["endpoint"],disclosure_grant_id=pinned.grant_id)
        if view["protocol"] not in ("openai","openai_responses") or view["config_version"]!=pinned.config_version:
            raise error("DISCLOSURE_REVOKED")
        key=service.cards.configs.resolve_credentials(pinned.config_id).get("api_key","")
    deps=bind_task_deps(service.db,source,actor_id=root.owner_user_id,task_id=task_id,disclosures=service.disclosures)
    return asyncio.run(search_responses(view["endpoint"],view["model_name"],key,query,deps,
                                        max_tool_calls=max_tool_calls))
