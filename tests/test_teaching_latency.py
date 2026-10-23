"""Synthetic checks for real incrementality, cache isolation and private snapshots."""
import json
from datetime import datetime, timezone
from threading import Event
from concurrent.futures import ThreadPoolExecutor
import httpx
import pytest
from sqlalchemy import select
from backend.agents.runner import run_streaming_structured_agent
from backend.planning.model_runner import ModelCapabilityGate
from backend.slice1.adapters.openai import OpenAIAdapter
from backend.slice1.capability_refresh import stamp_status
from backend.teaching.content_stream import partial_step, PreviewWriter, read_snapshot
from backend.teaching.outputs import LessonPlanCandidate
from backend.teaching.learning_models import LessonBlueprintRow, PreparedLessonPlanRow
from backend.tasks.service import LeaseError
from test_teaching_assessments import env, database, lesson, BOARD  # noqa: F401

TEXT = "先想象一排贴着不同标签的抽屉，通过标签找到对应物品。" + "这帮助我们理解键和值之间的映射，每个键唯一，通过一个已知的键找到对应值。"*3
PAYLOAD = {"steps":[{"text":TEXT,"title":"键值查找","board":BOARD,"phase":"intuition",
    "covered_goal_ids":["g1"],"lesson_finished":False}]}


def test_gate_uses_nested_saved_capabilities_and_skips_unused_native():
    class Adapter:
        capability_status={"tool_call":{"state":"available"},"text":{"state":"available"}}
        def probe(self,cap): raise AssertionError("saved capabilities must not be probed")
    assert ModelCapabilityGate(Adapter()).choose_modes(("tool","prompted")) == ["tool","prompted"]


def test_transient_probe_preserves_known_state_and_refresh_timestamp():
    saved={"text":{"state":"available","detail":""}}
    result=stamp_status({"text":{"state":"unknown","detail":"timeout"}},3,saved)["text"]
    assert result["state"]=="available" and result["detail"]=="timeout" and result["config_version"]==3
    assert datetime.fromisoformat(result["expires_at"]) > datetime.now(timezone.utc)


def test_adapter_reuses_owned_client_and_does_not_close_injected_client(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                 "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    own=OpenAIAdapter(endpoint="https://synthetic.invalid",model_name="synthetic")
    assert own._client_or_new() is own._client_or_new()
    client=own._client_or_new();own.close();assert client.is_closed
    supplied=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={"choices":[]})))
    adapter=OpenAIAdapter(endpoint="https://synthetic.invalid",client=supplied)
    adapter.generate([]);adapter.close();assert not supplied.is_closed
    supplied.close()


def test_http_timeout_has_controlled_classification_and_no_wire_data():
    from backend.slice1.adapters.request_metrics import failure_code
    def fail(request): raise httpx.ReadTimeout("synthetic vendor body",request=request)
    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        adapter=OpenAIAdapter(endpoint="https://synthetic.invalid",client=client)
        with pytest.raises(Exception) as caught:adapter.generate([])
        assert failure_code(caught.value)=="MODEL_TIMEOUT"
        assert "vendor body" not in str(caught.value)


def test_partial_text_arrives_before_board_and_only_complete_board_items_appear():
    first=partial_step('{"steps":[{"text":"先想象抽屉')
    assert first["text"]=="先想象抽屉" and first["board"] is None
    point=partial_step('{"steps":[{"text":"说明","title":"查找","board":{"title":"标签","points":["完整要点","半截')
    assert [e["text"] for e in point["board"]["elements"]]==["标签","1. 完整要点"]
    invalid=partial_step('{"steps":[{"text":"说明","board":{"title":"标签","nodes":[{"id":"n1"}],"edges":[{"from_id":"n1","to_id":"missing"}]}}]}')
    assert len(invalid["board"]["elements"])==1


def test_pydanticai_stream_produces_draft_before_provider_finishes_and_validates_final():
    finished=False;seen=[]
    class Adapter:
        supports_text_stream=True
        model_name="synthetic-stream"
        def generate(self,*args,**kwargs):raise AssertionError("must stream")
        def stream(self,messages):
            nonlocal finished
            value=json.dumps(PAYLOAD,ensure_ascii=False)
            for start in range(0,len(value),19):yield value[start:start+19]
            finished=True
    def preview(delta,reset=False):
        if delta:seen.append((delta,finished))
    result=run_streaming_structured_agent(Adapter(),LessonPlanCandidate,[{"role":"user","content":"合成目标"}],
        on_delta=preview,agent_name="teaching_agent")
    assert result.payload.steps[0].text==TEXT and result.mode=="prompted"
    assert len(seen)>3 and seen[0][1] is False


def test_cold_start_has_a_frozen_lightweight_plan_without_waiting_for_planner(env):
    _,_,start=lesson(env)
    with env[1].db.session() as sess:
        row=sess.get(LessonBlueprintRow,start["conversation_id"])
        stages=json.loads(row.plan_json)["stages"]
    assert [s["phase"] for s in stages]==["intuition","concept","example","summary"]


def test_snapshots_are_replaceable_reconnectable_private_and_cancel_fenced(env):
    _,_,start=lesson(env);service=env[1].teaching;owner=env[2];task=start["task_id"]
    lease=service.tasks.claim(task,lease_owner="synthetic-stream",lease_seconds=120,allow_reclaim_active=False)
    conv=service._conversation(start["conversation_id"],owner)
    writer=PreviewWriter(service,lease,conv,1)
    writer('{"steps":[{"text":"第一句')
    first=read_snapshot(service,task,owner)
    assert first["text"]=="第一句" and first["event"]=="snapshot"
    assert read_snapshot(service,task,owner)["id"]==first["id"]
    with pytest.raises(Exception) as caught:read_snapshot(service,task,"other-synthetic-owner")
    assert caught.value.status==404
    response=env[0].get(f"/api/teaching/tasks/{task}/content-events",headers={})
    assert response.status_code in (401,404)
    service.tasks.request_cancel(task,actor_id=owner)
    with pytest.raises(LeaseError):writer("",reset=True)
    assert read_snapshot(service,task,owner)["event"]=="error"


def test_config_probe_cannot_overwrite_changed_version(env):
    from backend.slice1.config_service import ConfigService
    from backend.slice1.models import ServiceConfigRow
    from sqlalchemy import update
    configs=ConfigService(env[1].db,encryption_key="synthetic-not-used")
    # Synthetic metadata only: no encrypted credentials are needed for this CAS.
    with env[1].db.session() as sess:
        sess.add(ServiceConfigRow(config_id="cfg_latency_synthetic",owner_user_id=env[2],owner_scope="user",
            kind="content_model",protocol="openai",endpoint="https://synthetic.invalid",model_name="synthetic",
            encrypted_credentials="",credential_mask="",capability_status="{}",config_version=2,is_active=True))
        sess.commit()
    view=configs.set_capability_status("cfg_latency_synthetic",{"text":{"state":"available"}},expected_version=1,merge=True)
    assert view["config_version"]==2 and view["capability_status"]=={}
    with env[1].db.session() as sess:
        sess.delete(sess.get(ServiceConfigRow,"cfg_latency_synthetic"));sess.commit()


def test_stream_validation_retry_resets_draft_and_never_commits_invalid_phase():
    from pydantic_ai.exceptions import ModelRetry
    resets=[]
    class Adapter:
        supports_text_stream=True
        model_name="synthetic-retry"
        requests=0
        def stream(self,messages):
            self.requests+=1
            payload=json.loads(json.dumps(PAYLOAD))
            if self.requests==1:payload["steps"][0]["phase"]="concept"
            yield json.dumps(payload,ensure_ascii=False)
    adapter=Adapter()
    def validate(ctx, output):
        if output.steps[0].phase!="intuition":raise ModelRetry("合成阶段必须为intuition")
        return output
    result=run_streaming_structured_agent(adapter,LessonPlanCandidate,[{"role":"user","content":"合成目标"}],
        on_delta=lambda delta,reset=False:resets.append(reset),output_validator=validate)
    assert result.payload.steps[0].phase=="intuition" and adapter.requests==2 and resets.count(True)==2


def test_followup_stream_is_paused_and_final_answer_is_fenced(env):
    from backend.planning.models import ConversationRow
    _,_,start=lesson(env);client,state,user,headers=env
    class Adapter:
        supports_text_stream=True
        capability_status={"streaming":{"state":"available"}}
        model_name="synthetic-followup"
        def stream(self,messages):
            value=json.dumps({"alignment_version":1,"answer":"可以把键理解成抽屉的唯一标签。",
                "beats":[{"id":"answer","text":"可以把键理解成抽屉的唯一标签。",
                    "blocks":[{"id":"label","type":"text","text":"唯一标签"}],
                    "cues":[{"block_id":"label","target":"text","quote":"唯一标签"}]}]},ensure_ascii=False)
            for n in range(0,len(value),12):yield value[n:n+12]
    state.teaching.model_adapter_factory=lambda *args:Adapter()
    reply=client.post(f"/api/teaching/{start['conversation_id']}/followup",headers=headers,
        json={"content":"合成问题","idempotency_key":"latency-followup"}).json()
    assert state.teaching.get_workspace(start["conversation_id"],actor_id=user)["session_state"]=="paused"
    response=client.post(f"/api/teaching/tasks/{reply['task_id']}/run",headers=headers)
    assert response.json()["status"]=="succeeded",response.text
    workspace=state.teaching.get_workspace(start["conversation_id"],actor_id=user)
    assert workspace["session_state"]=="awaiting_resume" and workspace["followups"][-1]["role"]=="assistant"
    assert read_snapshot(state.teaching,reply["task_id"],user)["event"]=="done"


def test_each_http_request_emits_safe_duration_and_stream_first_chunk(monkeypatch):
    from backend.slice1.adapters import request_metrics
    from backend.logging_audit.logger import StructuredLogger, MemoryLogStream
    stream=MemoryLogStream()
    monkeypatch.setattr(request_metrics,"_logger",StructuredLogger(service="api",stream=stream))
    payload='data: {"choices":[{"delta":{"content":"合成增量"}}]}\n\ndata: [DONE]\n\n'
    with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,text=payload))) as client:
        adapter=OpenAIAdapter(endpoint="https://synthetic.invalid",credentials={"api_key":"synthetic-secret-marker"},client=client)
        assert list(adapter.stream([]))==["合成增量"]
    line=stream.lines()[-1];record=json.loads(line)
    assert record["event"]=="model.request.completed" and record["duration_ms"]>=0 and record["first_chunk_ms"]>=0
    assert "synthetic-secret-marker" not in line and "合成增量" not in line and "synthetic.invalid" not in line


def test_connection_pool_reuses_config_version_and_never_evicts_active_clients(monkeypatch):
    from backend.slice1.adapters import http_pool
    class Client:
        closed=False
        def __init__(self,**kwargs):pass
        def close(self):self.closed=True
    monkeypatch.setattr(http_pool.httpx,"Client",Client)
    pool=http_pool.HttpClientPool(capacity=1)
    with pool.borrow(("synthetic-config",1)) as original:
        with pool.borrow(("synthetic-config",1)) as reused:assert reused is original
        with pool.borrow(("synthetic-config",2)) as replacement:
            assert replacement is not original and not original.closed
        assert not original.closed
    assert len(pool._entries)<=1
    pool.close()
