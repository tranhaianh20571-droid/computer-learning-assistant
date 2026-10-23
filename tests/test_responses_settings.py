"""Synthetic Responses wire contracts and optional capability-test behavior."""
import asyncio
import json
import socket

import httpx2 as httpx
import pytest
from pydantic import BaseModel

from backend.agents import native, runner
from backend.agents.contracts import AgentDeps, GenerationInput
from backend.slice1.adapters import build_adapter
from backend.slice1.model_catalog import normalize_base
from backend.slice1.reasoning import model_settings_for, probe_settings_for


@pytest.fixture(name="native_synthetic_model_session")
def native_protocol():
    yield


class Result(BaseModel):
    value: int


def response():
    return {"id":"resp_synthetic","object":"response","created_at":0,"status":"completed",
        "model":"gpt-5.4","output":[{"type":"message","id":"msg_synthetic","role":"assistant",
            "status":"completed","content":[{"type":"output_text","text":'{"value":23}',"annotations":[]}]}],
        "usage":{"input_tokens":3,"output_tokens":4,"total_tokens":7}}


def stream_response():
    complete=response()
    message=complete["output"][0]
    events=[
        ("response.created",{"response":{**complete,"status":"in_progress","output":[]}}),
        ("response.output_item.added",{"output_index":0,"item":{**message,"content":[],"status":"in_progress"}}),
        ("response.content_part.added",{"item_id":message["id"],"output_index":0,"content_index":0,
            "part":{"type":"output_text","text":"","annotations":[]}}),
        ("response.output_text.delta",{"item_id":message["id"],"output_index":0,"content_index":0,"delta":'{"value":23}'}),
        ("response.output_text.done",{"item_id":message["id"],"output_index":0,"content_index":0,"text":'{"value":23}'}),
        ("response.output_item.done",{"output_index":0,"item":message}),
        ("response.completed",{"response":complete}),
    ]
    return "".join("event: "+kind+"\ndata: "+json.dumps({"type":kind,"sequence_number":index,**item})+"\n\n"
                   for index,(kind,item) in enumerate(events)).encode()


@pytest.mark.parametrize("suffix", ["", "/v1", "/v1/", "/v1/responses"])
@pytest.mark.parametrize("streaming", [False, True])
def test_responses_uses_native_sdk_with_correct_reasoning_and_no_probe_gate(monkeypatch,suffix,streaming):
    seen=[]
    real_transport=native.SafeModelTransport
    def transport(endpoint,deps,**kwargs):
        def handle(request):
            seen.append(request)
            if streaming:
                return httpx.Response(200,headers={"content-type":"text/event-stream"},content=stream_response())
            return httpx.Response(200,json=response())
        return real_transport(endpoint,deps,**kwargs,inner=httpx.MockTransport(handle),
            resolver=lambda *args:[(socket.AF_INET,socket.SOCK_STREAM,6,"",("127.0.0.1",9999))])
    monkeypatch.setattr(native,"SafeModelTransport",transport)
    source=build_adapter("openai_responses",endpoint="http://127.0.0.1:9999"+suffix,
        model_name="gpt-5.4",credentials={"api_key":"synthetic-key"},
        reasoning_settings={"mode":"effort","effort":"low","show_summary":False,"discovered":True})
    source.defer_capability_probe=True
    source.capability_status={cap:{"state":"unavailable","detail":"timeout"} for cap in
        ("text","tool_call","json_schema","streaming")}
    chunks=[]
    result=asyncio.run(runner.execute_agent(source,Result,
        GenerationInput.from_messages([{"role":"user","content":"Synthetic: return value 23."}]),
        AgentDeps(),**({"on_delta":lambda value,**kwargs:chunks.append(value)} if streaming else {})))
    assert result.payload.value==23
    assert len(seen)==1 and seen[0].url.path=="/v1/responses"
    payload=json.loads(seen[0].content)
    assert payload["reasoning"]["effort"]=="low" and "reasoning_effort" not in payload
    assert payload["store"] is False
    assert bool(payload.get("stream"))==streaming
    if streaming:
        assert "".join(chunks)=='{"value":23}'
    else:
        assert payload["text"]["format"]["type"]=="json_schema"


def test_responses_base_and_reasoning_share_catalog_and_generation_contract():
    assert normalize_base("http://127.0.0.1:9999","openai_responses")=="http://127.0.0.1:9999/v1"
    settings={"mode":"effort","effort":"high","show_summary":False}
    assert model_settings_for("openai_responses","https://model.example.test","gpt-5.4",settings)=={
        "openai_reasoning_effort":"high"}
    assert probe_settings_for("openai_responses","https://model.example.test","gpt-5.4",settings)=={
        "reasoning":{"effort":"high"},"max_output_tokens":4096}


def test_default_configuration_does_not_require_capability_description():
    assert model_settings_for("openai_responses","http://127.0.0.1:9999","unknown-model",
        {"mode":"default","show_summary":False})=={}


def test_responses_business_tool_roundtrip_preserves_call_identity_and_shared_budget(monkeypatch):
    seen=[]
    real_transport=native.SafeModelTransport
    def transport(endpoint,deps,**kwargs):
        def handle(request):
            seen.append(json.loads(request.content))
            if len(seen)==1:
                return httpx.Response(200,json={**response(),"output":[{
                    "type":"function_call","id":"fc_synthetic","call_id":"call_synthetic",
                    "name":"lookup","arguments":"{}","status":"completed"}]})
            return httpx.Response(200,json=response())
        return real_transport(endpoint,deps,**kwargs,inner=httpx.MockTransport(handle),
            resolver=lambda *args:[(socket.AF_INET,socket.SOCK_STREAM,6,"",("127.0.0.1",9999))])
    monkeypatch.setattr(native,"SafeModelTransport",transport)
    source=build_adapter("openai_responses",endpoint="http://127.0.0.1:9999",
        model_name="gpt-5.4",credentials={"api_key":"synthetic-key"})
    def lookup() -> int:
        return 23
    deps=AgentDeps()
    result=asyncio.run(runner.execute_agent(source,Result,
        GenerationInput.from_messages([{"role":"user","content":"Use lookup for this synthetic result."}]),
        deps,tools=[lookup]))
    assert result.payload.value==23 and len(seen)==deps.budget.wire_requests==2
    assert seen[0]["tools"][0]["name"]=="lookup"
    returned=next(item for item in seen[1]["input"] if item.get("type")=="function_call_output")
    assert returned["call_id"]=="call_synthetic" and "23" in returned["output"]
