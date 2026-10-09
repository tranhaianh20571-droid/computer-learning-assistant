"""Synthetic account catalog, controlled errors and route boundaries; no vendor calls."""
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.slice1.adapters.minimax import MiniMaxTTSAdapter
from backend.slice1.errors import AdapterError, Slice1Error, as_detail
from backend.slice1.router import create_slice1_router
from backend.slice1 import voice_catalog


def catalog(payload, status=200, endpoint="https://voice.example.test"):
    def respond(request):
        assert request.url.path == "/v1/get_voice"
        assert json.loads(request.content) == {"voice_type": "all"}
        assert request.headers["Authorization"] == "Bearer synthetic-key"
        return httpx.Response(status, json=payload)
    return MiniMaxTTSAdapter(endpoint=endpoint,credentials={"api_key":"synthetic-key"},
        client=httpx.Client(transport=httpx.MockTransport(respond)))


def test_normalize_catalog_preserves_words_trims_ids_deduplicates_and_limits_metadata():
    adapter=catalog({"system_voice":[{"voice_id":" voice-one ","voice_name":"合成\x00音色",
        "description":["合成\n介绍","x"*500],"secret":"must-not-return"},
        {"voice_id":"voice-one","voice_name":"重复"}],"voice_cloning":[{"voice_id":"clone-one"}],
        "voice_generation":[{"voice_id":"generated-one"}],"music_generation":[{"voice_id":"music-only"}],"base_resp":{"status_code":0}},
        endpoint="https://voice.example.test/v1")
    try:
        result=adapter.list_voices()
    finally:
        adapter._client.close()
    assert [v["voice_id"] for v in result]==["voice-one","clone-one","generated-one"]
    assert result[0]["name"]=="合成音色" and "\n" not in result[0]["description"]
    assert all(set(v)=={"voice_id","name","category","description"} for v in result)
    assert result[1]["name"]=="clone-one"


@pytest.mark.parametrize("payload,status,code",[
    ({"base_resp":{"status_code":1004,"status_msg":"private-vendor-message"}},200,"credential_invalid"),
    ({"base_resp":{"status_code":1002}},200,"rate_limited"),
    ({"base_resp":{"status_code":1000}},200,"capability_test_failed"),
    ({},401,"credential_invalid"),
    ({"system_voice":{}},200,"capability_test_failed"),
    ({"system_voice":[{"voice_id":"bad\n"}]},200,"capability_test_failed"),
    ({"system_voice":[{"voice_id":"a"*201}]},200,"capability_test_failed"),
    ({"system_voice":[{"voice_id":f"voice-{n}"} for n in range(2001)]},200,"response_too_large"),
])
def test_catalog_failures_do_not_publish_vendor_messages(payload,status,code):
    adapter=catalog(payload,status)
    try:
        with pytest.raises(AdapterError) as failure:adapter.list_voices()
        assert failure.value.code==code and "private-vendor-message" not in str(failure.value)
    finally:adapter._client.close()


def test_empty_catalog_is_distinct_from_bad_response():
    adapter=catalog({"system_voice":[],"voice_cloning":[],"voice_generation":[],"base_resp":{"status_code":0}})
    try:assert adapter.list_voices()==[]
    finally:adapter._client.close()


@pytest.mark.parametrize("endpoint,key",[
    ("https://user:pass@voice.example.test","synthetic-key"),
    ("https://voice.example.test?secret=value","synthetic-key"),
    ("https://voice.example.test#fragment","synthetic-key"),
    ("https://voice.example.test",""),
    ("https://voice.example.test","invalid\nkey"),
])
def test_invalid_query_does_not_open_network(monkeypatch,endpoint,key):
    monkeypatch.setattr(voice_catalog,"build_adapter",lambda *a,**k:pytest.fail("network must not be opened"))
    with pytest.raises(Slice1Error):voice_catalog.list_tts_voices(endpoint=endpoint,api_key=key)


def test_query_uses_bounded_direct_client_and_cleans_up(monkeypatch):
    seen={}
    class Client:
        def __init__(self,**options):seen.update(options)
        def __enter__(self):return self
        def __exit__(self,*args):seen["closed"]=True
    class Adapter:
        def list_voices(self):raise AdapterError("timeout")
    def build(*args,**kwargs):
        assert kwargs["max_response_bytes"]==1024*1024
        return Adapter()
    monkeypatch.setattr(voice_catalog.httpx,"Client",Client)
    monkeypatch.setattr(voice_catalog,"build_adapter",build)
    with pytest.raises(Slice1Error) as failure:
        voice_catalog.list_tts_voices(endpoint="https://voice.example.test",api_key="synthetic-key")
    assert failure.value.code=="capability_test_failed"
    assert seen=={"timeout":20.0,"follow_redirects":False,"trust_env":False,"closed":True}


def test_route_auth_runs_before_provider_and_never_echoes_key(monkeypatch):
    app=FastAPI()
    def user(request):
        if request.headers.get("Authorization")!="Bearer synthetic-session":raise HTTPException(401)
        return SimpleNamespace(user_id="synthetic-owner",is_admin=False)
    app.include_router(create_slice1_router(configs=None,capabilities=None,disclosures=None,connectors=None,resolve_user=user))
    @app.exception_handler(Slice1Error)
    async def handled(request,exc):return JSONResponse(status_code=exc.status,content=as_detail(exc))
    calls=[]
    def read(**args):
        calls.append(args)
        return {"data":[{"voice_id":"synthetic-voice","name":"合成音色","category":"system_voice","description":""}]}
    monkeypatch.setattr(voice_catalog,"list_tts_voices",read)
    client=TestClient(app)
    body={"endpoint":"https://voice.example.test","api_key":"synthetic-key"}
    assert client.post("/api/capabilities/tts/voices",json=body).status_code==401 and calls==[]
    response=client.post("/api/capabilities/tts/voices",json=body,headers={"Authorization":"Bearer synthetic-session"})
    assert response.status_code==200 and len(calls)==1 and "synthetic-key" not in response.text
