"""ADR-028: actual PydanticAI tool loops and private, fenced feature workflows."""
from copy import deepcopy
import io
import json
import time
from PIL import Image
from pydantic import BaseModel
from pydantic_ai.messages import ModelMessagesTypeAdapter
from sqlalchemy import select
from common_fixtures import SyntheticModelAdapter, make_user, auth_headers, synthetic_output_schema, synthetic_output_response
from test_course_library import database, env, draft, confirm, PAYLOAD  # noqa: F401
from backend.agents.runner import run_structured_agent
from backend.planning.adjustments import ensure_refs, nodes, apply_operations
from backend.planning.feature_models import PlanningDialogueRow
from backend.planning.ink import InkRecipe, render_ink


class FeatureAdapter(SyntheticModelAdapter):
    def generate(self, messages, **options):
        props = synthetic_output_schema(options).get("properties", {})
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        if "recipe" in props:
            tool = next((m for m in reversed(messages) if m["role"] == "tool" and m.get("name") == "render_ink_cover"), None)
            recipe = {"composition": "grid", "density": 14, "ink_strength": .6, "accent": "blue", "whitespace": .35}
            if not tool:
                return {"tool_calls": [{"id":"draw_1","function":{"name":"render_ink_cover","arguments":{"recipe":recipe}}}]}
            value = {"recipe":recipe,"description":"合成网格封面","asset_id":json.loads(tool["content"])["asset_id"]}
        elif "action" in props:
            value = {"action":"clarification" if "先问" in user else "revision_request",
                     "answer":"希望保留基础课吗？" if "先问" in user else "将调整选中的标题。",
                     "options":["保留","压缩"] if "先问" in user else [],"instruction":"修改第一门课的标题"}
        elif "operations" in props:
            context, _ = json.JSONDecoder().raw_decode(user)
            ref = next(n["node_ref"] for n in context["nodes"] if n["role"] == "course")
            if context["kind"] != "route": ref = context["plan"]["node_ref"]
            value = {"summary":"调整课程知识主题标题","operations":[{"op":"update","node_ref":ref,"title":"数据库原理"}]}
        else:
            return super().generate(messages, **options)
        return synthetic_output_response(value, options)


def wait_turn(client, headers, ident):
    for _ in range(120):
        body=client.get(f"/api/planning/drafts/{ident}/dialogue",headers=headers).json()
        if body["turns"] and body["turns"][-1]["status"] in ("succeeded","failed"):
            assert body["turns"][-1]["status"] == "succeeded", body
            return body["turns"][-1]
        time.sleep(.05)
    raise AssertionError("turn timed out")


def test_actual_function_loop_preserves_result_and_call_id():
    class Answer(BaseModel): value: int
    class Adapter(SyntheticModelAdapter):
        def generate(self,messages,**options):
            result=next((m for m in messages if m["role"]=="tool"),None)
            if result:
                assert result["tool_call_id"] == "ordinary_1"
                assert json.loads(result["content"])["value"] == 42
                assert synthetic_output_schema(options)["properties"]["value"]["type"] == "integer"
                return synthetic_output_response({"value":42}, options)
            assert options["tool_choice"]=="auto"
            assert any(t["function"]["name"]=="multiply" for t in options["tools"])
            return {"tool_calls":[{"id":"ordinary_1","function":{"name":"multiply","arguments":{"a":6,"b":7}}}]}
    called=[]
    def multiply(a:int,b:int)->dict:
        """Multiply two values."""
        called.append((a,b)); return {"value":a*b}
    result=run_structured_agent(Adapter(),Answer,[{"role":"user","content":"multiply"}],tools=[multiply])
    assert result.payload.value==42 and called==[(6,7)]
    history=ModelMessagesTypeAdapter.validate_json(result.messages_json)
    assert any(getattr(p,"part_kind","")=="tool-return" for m in history for p in m.parts)


def test_dialogue_clarifies_then_candidate_requires_adoption(env):
    client,state,user,headers=env
    adapter=FeatureAdapter(); state.planning.model_adapter_factory=lambda owner,task:adapter
    rid,item=draft(env)
    ident=item["draft_id"]; url=f"/api/planning/drafts/{ident}/dialogue"
    assert client.post(url,headers=headers,json={"revision":1,"content":"先问清楚再改","idempotency_key":"q1"}).status_code==202
    first=wait_turn(client,headers,ident)
    assert first["result"]["action"]=="clarification" and "candidate" not in first["result"]
    response=client.post(url,headers=headers,json={"revision":1,"content":"改为数据库知识主题","idempotency_key":"q2"})
    assert response.status_code==202,response.text
    second=wait_turn(client,headers,ident)
    assert second["result"]["candidate"]["stages"][0]["courses"][0]["title"]=="数据库原理"
    assert state.drafts.get_draft(ident,actor_id=user)["content"]["stages"][0]["courses"][0]["title"]==PAYLOAD["stages"][0]["courses"][0]["title"]
    adopted=client.post(f"/api/planning/dialogue/{second['task_id']}/adopt",headers=headers,json={"revision":1})
    assert adopted.status_code==200 and adopted.json()["draft"]["revision"]==2
    assert adopted.json()["draft"]["content"]["stages"][0]["courses"][1]==second["result"]["candidate"]["stages"][0]["courses"][1]
    assert client.post(f"/api/planning/dialogue/{second['task_id']}/adopt",headers=headers,json={"revision":1}).json()["deduped"]


def test_stale_candidate_cannot_overwrite_manual_edit_and_other_owner(env):
    client,state,user,headers=env
    state.planning.model_adapter_factory=lambda owner,task:FeatureAdapter()
    _,item=draft(env); ident=item["draft_id"]
    client.post(f"/api/planning/drafts/{ident}/dialogue",headers=headers,json={"revision":1,"content":"修改标题","idempotency_key":"edit"})
    turn=wait_turn(client,headers,ident)
    current=state.drafts.get_draft(ident,actor_id=user); current["content"]["title"]="人工标题"
    state.drafts.save_draft(ident,actor_id=user,revision=1,content=current["content"])
    assert client.post(f"/api/planning/dialogue/{turn['task_id']}/adopt",headers=headers,json={"revision":2}).status_code==409
    _,token=make_user(client,state.db);other=auth_headers(token)
    assert client.get(f"/api/planning/drafts/{ident}/dialogue",headers=other).status_code==404
    assert client.post(f"/api/planning/dialogue/{turn['task_id']}/adopt",headers=other,json={"revision":2}).status_code==404


def test_move_preserves_identity_and_remaps_prerequisites():
    base=ensure_refs(deepcopy(PAYLOAD),"route")
    stage=base["stages"][0];a,b=stage["courses"]
    result=apply_operations(base,"route",[{"op":"move","node_ref":b["node_ref"],"parent_ref":stage["node_ref"],"index":0}])
    assert result["stages"][0]["courses"][0]["node_ref"]==b["node_ref"]
    assert result["prerequisites"]==[{"from_index":1,"to_index":0}]
    assert base["stages"][0]["courses"][0]["node_ref"]==a["node_ref"]


def test_single_course_revision(env):
    client,state,user,headers=env
    state.planning.model_adapter_factory=lambda owner,task:FeatureAdapter()
    _,item=draft(env,{"title":"合成单课","units":PAYLOAD["stages"][0]["courses"][0]["units"],"prerequisites":[]},kind="course_outline")
    ident=item["draft_id"]
    client.post(f"/api/planning/drafts/{ident}/dialogue",headers=headers,json={"revision":1,"content":"修改标题","idempotency_key":"single"})
    turn=wait_turn(client,headers,ident)
    assert turn["result"]["candidate"]["title"]=="数据库原理"
    assert "stages" not in turn["result"]["candidate"]


def test_dialogue_status_and_result_use_one_read_snapshot(env, monkeypatch):
    from sqlalchemy import event
    from backend.tasks.service import LearningTaskRow
    client, state, user, headers = env
    monkeypatch.setattr(state.features, "kick", lambda _: None)
    _, item = draft(env)
    queued = state.features.enqueue_dialogue(item["draft_id"], owner=user, revision=1,
        content="合成并发快照检查", key="snapshot")
    published = []
    def publish_between_reads(conn, cursor, statement, parameters, context, executemany):
        if (not published and statement.lstrip().upper().startswith("SELECT")
                and "FROM " + PlanningDialogueRow.__tablename__ in statement):
            published.append(True)
            with state.db.session() as sess:
                sess.get(PlanningDialogueRow, queued["task_id"]).result_json = json.dumps(
                    {"action": "clarification", "answer": "合成完整结果"})
                sess.get(LearningTaskRow, queued["task_id"]).status = "succeeded"
    event.listen(state.db.engine, "after_cursor_execute", publish_between_reads)
    try:
        first = state.features.dialogue(item["draft_id"], user)["turns"][0]
    finally:
        event.remove(state.db.engine, "after_cursor_execute", publish_between_reads)
    assert published
    assert first["status"] != "succeeded" or first["result"].get("answer") == "合成完整结果"
    second = state.features.dialogue(item["draft_id"], user)["turns"][0]
    assert second["status"] == "succeeded" and second["result"]["answer"] == "合成完整结果"


def test_cover_tool_generates_persistent_png_with_owner_and_delete_guards(env):
    client,state,user,headers=env
    def forbidden_model(*args):
        raise AssertionError("Local procedural cover must not call a content model")
    state.planning.model_adapter_factory=forbidden_model
    rid,item=draft(env)
    response=client.post(f"/api/planning/route/{rid}/cover",headers=headers,json={"idempotency_key":"cover1"})
    assert response.status_code==202,response.text
    task_id=response.json()["task_id"]
    pending=client.get(f"/api/planning/route/{rid}/cover",headers=headers).json()
    assert pending["task_id"]==task_id and pending["status"]=="queued" and pending["image_url"] is None
    state.features._run(task_id)
    state.features.cover_background.close()
    cover=client.get(f"/api/planning/route/{rid}/cover",headers=headers).json()
    assert cover["status"]=="succeeded" and cover.get("asset_id"),cover
    assert cover["requested_method"]==cover["actual_method"]=="procedural" and cover["fallback_reason"] is None
    png=client.get(cover["image_url"],headers=headers)
    assert png.status_code==200 and Image.open(io.BytesIO(png.content)).size==(960,720)
    again=client.post(f"/api/planning/route/{rid}/cover",headers=headers,json={"idempotency_key":"cover1"}).json()
    assert again["task_id"]==response.json()["task_id"]
    _,token=make_user(client,state.db)
    assert client.get(cover["image_url"],headers=auth_headers(token)).status_code==404
    assert client.delete(f"/api/planning/routes/{rid}",headers=headers).status_code==200
    assert client.get(cover["image_url"],headers=headers).status_code==404


def test_ink_recipes_are_seeded_and_visually_distinct():
    a=render_ink(InkRecipe(composition="grid"),42,320,240)
    assert a==render_ink(InkRecipe(composition="grid"),42,320,240)
    assert a!=render_ink(InkRecipe(composition="landscape"),42,320,240)


def test_confirmed_route_cannot_rename_and_keeps_completion(env):
    from backend.planning.models import LessonContentVersionRow, LessonProgressRow, LessonRow
    client,state,user,headers=env
    rid,item=draft(env);confirm(env,item)
    course=client.get(f"/api/routes/{rid}/overview",headers=headers).json()["stages"][0]["courses"][0]
    old_lesson=course["units"][0]["lessons"][0]["lesson_id"]
    with state.db.session() as sess:
        row=sess.get(LessonRow,old_lesson)
        version=LessonContentVersionRow(content_version_id="synthetic_"+old_lesson,
            lesson_id=old_lesson,content_version_no=1,goal_ref=row.goal_ref,scope_ref="合成范围")
        sess.add(version);sess.flush()
        sess.add(LessonProgressRow(progress_id="synthetic_progress_"+old_lesson,owner_user_id=user,
            lesson_id=old_lesson,lesson_content_version_id=version.content_version_id,learning_state="user_completed"))
    assert client.post(f"/api/planning/route/{rid}/edit-draft",headers=headers).status_code==409
    fresh=client.get(f"/api/routes/{rid}/overview",headers=headers).json()["stages"][0]["courses"][0]
    assert fresh["course_id"]==course["course_id"] and fresh["title"]==course["title"]
    assert fresh["lesson_completed"]==1


def test_task_publication_is_blocked_after_cancellation(env):
    state=env[1]
    result=state.tasks.create_task(owner_user_id=env[2],kind="planning_dialogue",subject_id="synthetic",
        idempotency_key="cancel_"+__import__("uuid").uuid4().hex,stage="generate")
    lease=state.tasks.claim(result["task_id"],lease_owner="synthetic-worker",lease_seconds=120)
    state.tasks.request_cancel(result["task_id"],actor_id=env[2])
    published=[]
    receipt=state.tasks.submit_result(lease,stage="generate",status="succeeded",publish=lambda sess:published.append(True))
    assert not receipt["accepted"] and not published


def test_image_provider_uses_separate_endpoint_and_returns_bounded_png(monkeypatch):
    monkeypatch.setattr("backend.slice1.adapters.imagegen.validate_endpoint", lambda endpoint: None)
    import base64
    import httpx
    from backend.slice1.adapters.imagegen import ImageGenerationAdapter
    sample=io.BytesIO();Image.new("RGB",(64,64),"white").save(sample,format="PNG")
    requests=[]
    def response(request):
        requests.append(request)
        assert request.url.path=="/v1/images/generations"
        body=json.loads(request.content)
        assert body["prompt"]=="合成水墨图" and body["response_format"]=="b64_json"
        return httpx.Response(200,json={"data":[{"b64_json":base64.b64encode(sample.getvalue()).decode()}]})
    client=httpx.Client(transport=httpx.MockTransport(response))
    adapter=ImageGenerationAdapter(endpoint="https://example.com/v1",model_name="synthetic-image",client=client,
        resolver=lambda *args: [(None, None, None, None, ("93.184.216.34", 443))])
    result=adapter.generate_image("合成水墨图")
    assert Image.open(io.BytesIO(result)).size==(960,720) and len(requests)==1


def test_trace_never_serializes_image_bytes_or_tool_return_bodies():
    from backend.agents.bridge import trace_messages
    result=trace_messages([{"role":"user","content":[{"type":"text","text":"合成问题"},
        {"type":"image_url","image_url":{"url":"data:image/png;base64,synthetic_bytes"}}]},
        {"role":"tool","content":"synthetic_tool_result_body"}])
    encoded=json.dumps(result)
    assert "synthetic_bytes" not in encoded and "synthetic_tool_result_body" not in encoded


def test_root_title_request_is_not_used_as_course_title():
    from backend.agents.tools import planning_title_output_validator
    from backend.planning.outputs import RouteCandidate
    from pydantic_ai.exceptions import ModelRetry
    import pytest
    candidate=RouteCandidate.model_validate({**deepcopy(PAYLOAD),"title":"我想学习后端"})
    validate=planning_title_output_validator("我想学习后端")
    with pytest.raises(ModelRetry): validate(None,candidate)
    fixed=validate(None,candidate)
    assert fixed.title and not fixed.title.startswith("我想")


def test_refresh_resumes_interrupted_worker_after_lease_expiry(env):
    from datetime import timedelta
    from backend.tasks.service import LearningTaskRow
    from backend.planning.models import utcnow
    client,state,user,headers=env
    state.planning.model_adapter_factory=lambda owner,task:FeatureAdapter()
    _,item=draft(env)
    original=state.features.kick
    state.features.kick=lambda task:None
    queued=state.features.enqueue_dialogue(item["draft_id"],owner=user,revision=1,
        content="修改标题",key="interrupted")
    lease=state.tasks.claim(queued["task_id"],lease_owner="interrupted-worker")
    with state.db.session() as sess:
        task=sess.get(LearningTaskRow,lease.task_id)
        task.lease_until=utcnow()-timedelta(seconds=1)
    state.features.kick=original
    turn=wait_turn(client,headers,item["draft_id"])
    assert turn["result"].get("candidate")
    assert state.tasks.get_task(queued["task_id"])["attempt"]==2
