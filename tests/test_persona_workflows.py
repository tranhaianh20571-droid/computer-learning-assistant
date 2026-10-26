"""Isolated authored personas, model destinations and genuine SDK build stages."""
import json
import uuid
from types import SimpleNamespace

import pytest
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse,TextPart
from sqlalchemy import select

from backend.agents.model_routing import role_config,set_role,TaskModelRoleRow
from backend.agents.task_binding import bind_task_deps,AgentBudgetRow
from backend.characters.distillation import DistillationService
from backend.characters.distillation_models import BuildRequest,CharacterBuildRow,CharacterBuildStageRow
from backend.characters.persona import PersonaPackage
from backend.characters.schemas import Profile
from backend.characters.delivery import expression_payload
from backend.characters.expression import derive_expression
from backend.slice1.errors import Slice1Error
from backend.slice1.disclosure_service import DisclosureService
from backend.logging_audit.repositories import PgTaskEventStore
from backend.prompts.service import PromptService
from backend.tasks.service import TaskService
from test_character_cards import env  # noqa: F401
from test_native_teaching_authorization import env as teaching_env, approve
from native_model_support import _request

SKILL="# 合成导师\n\n## 心智模型\n先验证条件，再推导因果。每个例子解释适用边界。\n"*12


def test_role_model_prewarm_keeps_existing_authorization(teaching_env,monkeypatch):
    failures=[]
    original=teaching_env.service._controlled_failure
    def capture(exc):
        import traceback
        failures.append("".join(traceback.format_exception(exc)))
        return original(exc)
    monkeypatch.setattr(teaching_env.service,"_controlled_failure",capture)
    warm=approve(teaching_env,purpose="prewarm",prewarm=True,tts=True)
    receipt=teaching_env.service.preparation.run(warm["task_id"])
    assert receipt["status"]=="succeeded",failures


def test_lesson_blueprint_and_teaching_tasks_have_distinct_authorized_models(env,teaching_env):
    from backend.agents.model_routing import task_role_config
    cards,_=env
    service=teaching_env.service
    service.configs=cards.configs
    planner=config(cards,teaching_env.owner,"planning")
    set_role(cards.configs,teaching_env.owner,"planning",planner["config_id"],0)
    set_role(cards.configs,teaching_env.owner,"teaching",teaching_env.content,0)
    started=approve(teaching_env)
    assert task_role_config(cards.configs,teaching_env.owner,started["task_id"])["config_id"]==teaching_env.content
    assert service.run_step_generation(started["task_id"])["status"]=="succeeded"
    refinement=service.preparation.queue_refinement(started["conversation_id"],teaching_env.owner)
    assert refinement.get("task_id"),refinement
    assert task_role_config(cards.configs,teaching_env.owner,refinement["task_id"])["config_id"]==planner["config_id"]
    with cards.db.session() as sess:
        from backend.tasks.service import LearningTaskRow
        assert service.authorization.check_task(sess,sess.get(LearningTaskRow,refinement["task_id"])).content_config_id==planner["config_id"]


def config(cards,owner,label):
    return cards.configs.create(actor_id=owner,is_admin=False,kind="content_model",protocol="openai",
        endpoint=f"https://{label}.example.test/v1",model_name=label,
        credentials={"api_key":"synthetic-key"},owner_scope="user")


def test_three_roles_have_independent_connections_and_revision_guards(env):
    cards,_=env;owner="synthetic-role-"+uuid.uuid4().hex
    choices={role:config(cards,owner,role) for role in ("planning","teaching","research")}
    assert role_config(cards.configs,owner,"research") is None
    for role,view in choices.items():set_role(cards.configs,owner,role,view["config_id"],0)
    assert {role:role_config(cards.configs,owner,role)["model_name"] for role in choices}=={
        "planning":"planning","teaching":"teaching","research":"research"}
    with pytest.raises(Slice1Error) as conflict:
        set_role(cards.configs,owner,"planning",choices["teaching"]["config_id"],0)
    assert conflict.value.status==409
    with pytest.raises(Slice1Error):
        set_role(cards.configs,"other", "research",choices["research"]["config_id"],0)
    set_role(cards.configs,owner,"planning",choices["teaching"]["config_id"],1)
    assert role_config(cards.configs,owner,"research")["config_id"]==choices["research"]["config_id"]


def test_complete_skill_revision_copy_publication_and_frozen_runtime_payload(env):
    cards,_=env;owner="synthetic-persona-"+uuid.uuid4().hex
    card=cards.create(owner,Profile(name="Synthetic",speaking_style="Ask about conditions"))
    package=PersonaPackage(skill=SKILL,references=[{"name":"01-writings.md","body":"Synthetic source evidence."}],
        skills=[{"name":"因果追问","version":"1","instructions":"先让学生说明条件。","enabled":True}])
    card=cards.edit_persona(owner,card["card_id"],1,package)
    assert card["revision"]==2 and card["persona"]["skill"]==SKILL
    revision,profile=cards.prepare_profile(owner,card["card_id"])
    payload=expression_payload(profile,derive_expression(profile).model_dump())
    assert payload["persona"]["skill"]==SKILL
    assert payload["persona"]["references"]==[{"name":"01-writings.md"}]
    cards.publish(owner,card["card_id"],2)
    other="synthetic-recipient"
    copied=cards.copy(other,card["card_id"])
    assert copied["persona"]["skill"]==SKILL
    card=cards.edit(owner,card["card_id"],2,Profile(name="Next"))
    assert card["persona"]["skill"]==SKILL
    assert cards.get(other,card["card_id"])["revision"]==2
    with pytest.raises(Slice1Error):
        cards.edit_persona(other,card["card_id"],2,package)
    from backend.characters.persona import reference_tool
    reader=reference_tool(profile)
    assert reader("01-writings.md")=="Synthetic source evidence."
    assert reader("../private.env")=="reference_unavailable"


def synthetic_reply(stage,payload,props):
    if "persona" in props and "mentor" in props:
        return {"persona":synthetic_reply(stage,payload,
            {name:{} for name in ("grounding","identity_and_version","no_fabricated_quotes",
                "teaching_accuracy","uncertainty","role_consistency","feynman_protocol","weaknesses")}),
            "mentor":{name:{"passed":True,"reason":"Synthetic actual classroom answer passed."}
                for name in ("explanation","misconception","encouragement","honesty","feynman")}}
    if "writings" in props and "extraction" in props:
        return {"assessments":[{"source_id":item["id"],"kind":"secondary",
                "reason":"Synthetic public description.","relevant":True,
                "dimensions":["conversations","decisions"]} for item in payload["sources"]],
            **{key:{"body":"Synthetic full dimension report with a specific example and evidence boundary.",
                "source_ids":[item["id"] for item in payload["sources"]],"gaps":[],"contradictions":[]}
                for key in ("writings","conversations","expression","external","decisions","timeline")},
            "extraction":synthetic_reply(stage,payload,{"models":{}}),
            "profile":{"name":"Synthetic tutor","identity":"Synthetic simulation"}}
    if "explanation" in props and "misconception" in props and "honesty" in props and stage=="fast_replies":
        return {name:"Synthetic actual classroom reply with an actionable question."
            for name in ("explanation","misconception","encouragement","honesty","feynman")}
    if "grounding" in props:
        return {**{name:{"passed":True,"reason":"Synthetic grounded acceptance."}
            for name in props if name!="weaknesses"},"weaknesses":[]}
    if "misconception" in props and "honesty" in props:
        return {name:{"passed":True,"reason":"Synthetic classroom acceptance."} for name in props}
    if "source_id" in props:
        source=payload["source"]
        return {"source_id":source["id"],"kind":"primary",
            "evidence":"learning needs feedback and explicit conditions.",
            "reason":"Synthetic original source evidence.","relevant":True,
            "dimensions":["writings","conversations","expression","external","decisions","timeline"]}
    if "name" in props and "appearance" in props:
        return {"name":"Synthetic tutor","identity":"Synthetic mentor"}
    if "queries" in props:
        return {"queries":[{"query":payload["topic"]+" "+("original transcripts" if stage=="research_plan" else "independent criticism"),
            "dimensions":["writings","conversations","expression","external","decisions","timeline"]}]}
    if "assessments" in props:
        return {"assessments":[{"source_id":item["id"],"kind":"unknown","evidence":"",
            "reason":"Synthetic source provenance is deliberately unknown."} for item in payload["sources"]]}
    if "body" in props:
        return {"body":"Synthetic independent dimension report. Conditions explain the reasoning and limitations.",
            "source_ids":[item["id"] for item in payload["sources"]],"gaps":[],"contradictions":[]}
    if "models" in props:
        return {"models":[{"name":f"Model {i}","mechanism":"Check the condition before inferring the result.",
            "evidence":["Synthetic input evidence."],"applications":["Lookup","Comparison"],
            "limits":"Not a guarantee beyond the source.","tentative":True} for i in range(2)],
            "heuristics":["If evidence is absent, state uncertainty."],"expression_dna":"Synthetic clear questions with concrete causal detail.",
            "values_and_tensions":["Precision versus speed.","Simplicity versus completeness."],
            "boundaries":["No invented quotes.","No unknown biography.","No unsupported certainty."],"influences":[]}
    if "heuristics" in props:
        return {"heuristics":["If evidence is absent, state uncertainty."],
            "expression_dna":"Synthetic clear questions with concrete causal detail.",
            "values_and_tensions":["Precision versus speed.","Simplicity versus completeness."],
            "boundaries":["No invented quotes.","No unknown biography.","No unsupported certainty."],"influences":[]}
    if "profile" in props:
        return {"profile":{"name":"Synthetic tutor","identity":"Synthetic mentor"},"skill":SKILL}
    if "cases" in props:
        return {"cases":[{"kind":kind,"question":f"Synthetic verification question {index}?",
            "expected_evidence":"Synthetic source evidence."} for index,kind in enumerate(["known"]*3+["edge","voice"])]}
    if "answer" in props:return {"answer":"Synthetic response: check conditions and state the boundary."}
    if "checks" in props:return {"checks":[{"name":name,"passed":True,"reason":"Synthetic evidence."}
        for name in ("known","edge","voice")],"weaknesses":[]}
    if "revised_skill" in props:return {"revised_skill":"","changes":["Synthetic review."],"concerns":[]}
    if "candidates" in props:return {"candidates":["Synthetic A","Synthetic B"]}
    raise AssertionError(stage)


def builder(env,monkeypatch,*,streaming=False,pipeline_version=5):
    cards,_=env;owner="synthetic-build-"+uuid.uuid4().hex
    chosen=config(cards,owner,"research");set_role(cards.configs,owner,"research",chosen["config_id"],0)
    events=PgTaskEventStore(cards.db);tasks=TaskService(cards.db,events)
    disclosures=DisclosureService(cards.db,cards.audit)
    called=[]
    def factory(current,task_id):
        with cards.db.session() as sess:
            stage=sess.get(CharacterBuildStageRow,task_id)
            pin=sess.get(TaskModelRoleRow,(task_id,"research"))
            assert pin.config_id==chosen["config_id"]
            name=stage.stage_key
        def generate(messages,info):
            translated,_=_request(messages,info)
            payload=json.loads(next(message["content"] for message in reversed(translated) if message["role"]=="user"))
            if name=="extract" or name.startswith("verify_") or name=="final_verify":
                assert isinstance(payload["sources"],list) and all("text" in item for item in payload["sources"])
            called.append((name,messages))
            if name.startswith("merge_skill_part_") or name.startswith("repair_") and "_part_" in name:
                return ModelResponse(parts=[TextPart(payload["original"])])
            if name.startswith("build_skill_part_"):
                return ModelResponse(parts=[TextPart(SKILL+f"\n\n## segment: {name}\n")])
            if name=="merge_skill":
                return ModelResponse(parts=[TextPart(payload["original"]["skill"])])
            if name in ("build_skill","fast_skill"):
                return ModelResponse(parts=[TextPart(SKILL)])
            return ModelResponse(parts=[TextPart(json.dumps(
                synthetic_reply(name,payload,info.model_request_parameters.output_object.json_schema["properties"])))])
        async def streamed(messages,info):
            output=generate(messages,info).parts[0].content
            for index in range(0,len(output),41):
                yield output[index:index+41]
        model=FunctionModel(generate,stream_function=streamed)
        model.supports_text_stream=streaming
        model.agent_deps=bind_task_deps(cards.db,SimpleNamespace(config_id=None),
            actor_id=current,task_id=task_id,disclosures=disclosures)
        return model
    service=DistillationService(cards,tasks,PromptService(cards.db),disclosures,factory)
    service.pipeline_version=pipeline_version
    monkeypatch.setattr(service,"kick",lambda ident:None)
    return cards,owner,service,called


@pytest.mark.parametrize("streaming",[False,True])
def test_text_distillation_runs_six_independent_agents_verifies_and_refines(env,monkeypatch,streaming):
    cards,owner,service,calls=builder(env,monkeypatch,streaming=streaming)
    request=BuildRequest(mode="text",topic="Synthetic A",source_text="Synthetic source text.",
        selected_character="Synthetic A",request_key=uuid.uuid4().hex,confirmed=True)
    created=service.create(owner,request)
    with cards.db.session() as sess:
        from datetime import datetime,timezone
        sess.get(CharacterBuildRow,created["build_id"]).created_at=datetime(2024,1,1,tzinfo=timezone.utc)
    assert service.run(created["build_id"])["accepted"]
    saved=service.get(owner,created["build_id"])
    assert saved["status"]=="succeeded",saved
    research=[key for key,_ in calls if key.startswith("research_")]
    assert len(research)==6 and len(set(research))==6
    for name,messages in calls:
        if name.startswith("research_"):
            from backend.agents.bridge import messages_to_adapter
            translated=messages_to_adapter(messages)
            payload=json.loads(next(message["content"] for message in reversed(translated) if message["role"]=="user"))
            assert payload["as_of"]=="2024-01-01"
    assert sum(key.startswith("test_0_") for key,_ in calls)==5
    assert "verify_0" in [key for key,_ in calls]
    assert {"refine_structure","refine_role"}<=set(key for key,_ in calls)
    assert len(saved["result"]["persona"]["references"])==6
    assert saved["result"]["persona"]["skill"].startswith(SKILL)
    assert "7812110bf" in saved["result"]["persona"]["skill"]
    assert not saved["result"]["persona"]["quality"]["passed"]
    count=len(calls);service.run(created["build_id"]);assert len(calls)==count
    accepted=service.accept(owner,created["build_id"])
    assert accepted["persona"]["skill"].startswith(SKILL) and accepted["revision"]==2
    assert service.accept(owner,created["build_id"])["revision"]==2
    with pytest.raises(Slice1Error):service.get("other",created["build_id"])
    with cards.db.session() as sess:
        budget=sess.get(AgentBudgetRow,created["task_id"])
        assert budget.logical_requests>0


def test_multiple_text_characters_produce_a_checkpoint_before_distilling(env,monkeypatch):
    _,owner,service,calls=builder(env,monkeypatch)
    created=service.create(owner,BuildRequest(mode="text",topic="Synthetic corpus",
        source_text="Synthetic A and Synthetic B.",request_key=uuid.uuid4().hex,confirmed=True))
    service.run(created["build_id"])
    saved=service.get(owner,created["build_id"])
    assert saved["result"]["candidates"]==["Synthetic A","Synthetic B"]
    assert [name for name,_ in calls]==["candidates"]
    data=service.action(owner,created["build_id"],"select","Synthetic A")
    assert data["selected_character"]=="Synthetic A"


def test_deleted_card_stops_all_build_publication(env,monkeypatch):
    cards,owner,service,calls=builder(env,monkeypatch)
    created=service.create(owner,BuildRequest(mode="text",topic="Synthetic",
        source_text="Synthetic evidence.",selected_character="Synthetic",request_key=uuid.uuid4().hex,confirmed=True))
    cards.delete(owner,created["card_id"])
    service.run(created["build_id"])
    assert calls==[]
    with pytest.raises(Slice1Error):service.get(owner,created["build_id"])


def test_distillation_resumes_only_unfinished_dimensions_after_model_failure(env,monkeypatch):
    from backend.planning.errors import error
    _,owner,service,calls=builder(env,monkeypatch)
    factory=service.model_factory
    def failing(current,task_id):
        with service.db.session() as sess:
            stage=sess.get(CharacterBuildStageRow,task_id)
            if stage.stage_key=="research_expression":raise error("MODEL_OUTPUT_INVALID")
        return factory(current,task_id)
    service.model_factory=failing
    created=service.create(owner,BuildRequest(mode="text",topic="Synthetic",
        source_text="Synthetic evidence.",selected_character="Synthetic",
        request_key=uuid.uuid4().hex,confirmed=True))
    service.run(created["build_id"])
    assert service.get(owner,created["build_id"])["status"]=="failed"
    completed=[name for name,_ in calls if name.startswith("research_")]
    assert len(completed)==5
    service.model_factory=factory
    service.action(owner,created["build_id"],"retry")
    with service.db.session() as sess:
        from datetime import datetime,timezone,timedelta
        from backend.tasks.service import LearningTaskRow
        deadline=sess.get(LearningTaskRow,created["task_id"]).deadline_at
        assert timedelta(hours=1.9)<deadline-datetime.now(timezone.utc)<=timedelta(hours=2,seconds=1)
    service.run(created["build_id"])
    assert service.get(owner,created["build_id"])["status"]=="succeeded"
    assert all([name for name,_ in calls].count(name)==1 for name in completed)


@pytest.mark.parametrize("change",["cancel","config","budget"])
def test_build_parent_fences_cover_cancel_config_and_cumulative_budget(env,monkeypatch,change):
    from backend.slice1.models import ServiceConfigRow
    cards,owner,service,calls=builder(env,monkeypatch)
    created=service.create(owner,BuildRequest(mode="text",topic="Synthetic",
        source_text="Synthetic evidence.",selected_character="Synthetic",
        request_key=uuid.uuid4().hex,confirmed=True))
    if change=="cancel":service.action(owner,created["build_id"],"cancel")
    else:
        with cards.db.session() as sess:
            row=sess.get(CharacterBuildRow,created["build_id"])
            if change=="config":sess.get(ServiceConfigRow,row.config_id).config_version+=1
            else:sess.get(AgentBudgetRow,created["task_id"]).request_limit=1
    service.run(created["build_id"])
    saved=service.get(owner,created["build_id"])
    assert saved["status"] in ("failed","cancelled")
    assert saved["result"]=={}
    if change!="budget":assert calls==[]
    with cards.db.session() as sess:
        assert sess.get(AgentBudgetRow,created["task_id"]).logical_requests<=1


def test_source_blacklist_cannot_enter_evidence_pool():
    for url in ("https://www.zhihu.com/a","https://mp.weixin.qq.com/a","https://baike.baidu.com/a",
                "https://www.youtube.com/watch?v=synthetic","https://www.linkedin.com/posts/synthetic",
                "https://www.scribd.com/document/synthetic"):
        assert not DistillationService._allowed_source(url)
    assert DistillationService._allowed_source("https://writings.example.test/a")


def test_role_memory_is_structured_bounded_and_excludes_research(env):
    from backend.memory.service import MemoryService
    memory=MemoryService(env[0].db,env[0].audit)
    seen=[]
    async def read(owner,scope,tag):
        seen.append((scope,tag))
        return {"status":"ready","data":{"evidence":["synthetic-"*2000]*9}}
    memory.read=read
    assert memory.context_for_role("owner","research")["status"]=="excluded" and not seen
    assert memory.context_for_role("owner","research_search")["status"]=="excluded" and not seen
    planning=memory.context_for_role("owner","planning",course="synthetic",card="synthetic-card")
    assert planning["status"]=="ready" and ("general","character:synthetic-card") not in seen
    seen.clear()
    teaching=memory.context_for_role("owner","teaching",course="synthetic",card="synthetic-card")
    assert ("general","character:synthetic-card") in seen
    assert len(json.dumps(teaching))<4200
    assert json.loads(memory.context("owner",course="synthetic"))["status"]=="ready"


@pytest.mark.parametrize("provenance",["unknown","unverified_primary"])
def test_ai_distillation_uses_one_real_search_record_and_does_not_respend_on_resume(env,monkeypatch,provenance):
    from backend.search.service import SearchService
    from test_anysearch_service import Adapter
    cards,owner,service,calls=builder(env,monkeypatch)
    from backend.auth.models import UserRow
    with cards.db.session() as sess:
        sess.add(UserRow(user_id=owner,email="synthetic-search-"+uuid.uuid4().hex+"@example.test",
                         password_hash="synthetic-hash",status="approved"))
    cards.configs.create(actor_id=owner,is_admin=False,kind="search",protocol="anysearch",
        endpoint="https://api.anysearch.com",model_name="",credentials={"api_key":"synthetic-search-key"})
    search=Adapter()
    if provenance=="unverified_primary":
        original=synthetic_reply
        def reply(stage,payload,props):
            value=original(stage,payload,props)
            if "assessments" in value:
                for item in value["assessments"]:
                    item.update(kind="primary",evidence="Synthetic invented quotation.")
            return value
        monkeypatch.setattr(__import__(__name__),"synthetic_reply",reply)
    service.search_factory=lambda user,task:SearchService(cards.db,cards.configs,
        owner_user_id=user,task_id=task,adapter_factory=lambda *_:search)
    monkeypatch.setattr(service,"_read_source",lambda root,url:"Synthetic full primary-page text.")
    created=service.create(owner,BuildRequest(mode="ai",topic="Synthetic A",
        research_policy={"max_searches":1},request_key=uuid.uuid4().hex,confirmed=True))
    receipt=service.run(created["build_id"])
    assert receipt.get("accepted"),receipt
    result=service.get(owner,created["build_id"])
    assert result["source_count"]==1
    package=result["result"]["persona"]
    skill_parts=[key for key,_ in calls if key.startswith("build_skill_part_")]
    assert skill_parts==[f"build_skill_part_{index}" for index in range(5)]
    assert "build_skill_part_0" in package["skill"]
    assert "build_skill_part_4" in package["skill"]
    assert [key for key,_ in calls if key.startswith("merge_skill_part_")]==[
        f"merge_skill_part_{index}" for index in range(5)]
    assert package["provenance"]["sources"][0]["kind"]=="unknown"
    assert package["provenance"]["sources"][0]["evidence"]==""
    assert not next(check["passed"] for check in package["quality"]["report"]["checks"]
                    if check["name"]=="primary_source_ratio")
    assert search.calls==1
    service.run(created["build_id"])
    assert search.calls==1
    from backend.search.repository import SearchCallRow
    with cards.db.session() as sess:
        sources=sess.scalar(select(CharacterBuildStageRow).where(
            CharacterBuildStageRow.build_id==created["build_id"],CharacterBuildStageRow.stage_key=="sources_0"))
        records=list(sess.scalars(select(SearchCallRow).where(SearchCallRow.task_id==sources.task_id)))
        assert len(records)==1 and records[0].result_count==1


def test_role_routing_reaches_three_distinct_native_model_destinations(env,monkeypatch):
    import socket
    import httpx2
    from backend.agents.native import SafeModelTransport
    from backend.agents.runner import run_structured_agent
    from backend.characters.distillation_models import PersonaReply
    from backend.slice3_app import _config_model_factory
    from backend.tasks.service import LearningTaskRow
    cards,_=env;owner="synthetic-wire-"+uuid.uuid4().hex
    monkeypatch.setattr("backend.slice1.capability_refresh.CapabilityRefresher.register",lambda *args:None)
    tasks=TaskService(cards.db,PgTaskEventStore(cards.db))
    view={role:config(cards,owner,role) for role in ("planning","teaching","research")}
    for role,value in view.items():set_role(cards.configs,owner,role,value["config_id"],0)
    seen=[]
    original=SafeModelTransport.__init__
    def init(self,endpoint,deps,**kwargs):
        def reply(request):
            seen.append((endpoint,request.headers["host"]))
            return httpx2.Response(200,json={"id":"synthetic","object":"chat.completion",
                "created":1,"model":"synthetic","choices":[{"index":0,"message":{"role":"assistant",
                "content":json.dumps({"answer":"Synthetic native output."})},"finish_reason":"stop"}]})
        kwargs.update(inner=httpx2.MockTransport(reply),
            resolver=lambda *args:[(socket.AF_INET,socket.SOCK_STREAM,6,"",("93.184.216.34",443))])
        original(self,endpoint,deps,**kwargs)
    monkeypatch.setattr(SafeModelTransport,"__init__",init)
    factory=_config_model_factory(cards.configs)
    for role in view:
        task=tasks.create_task(owner_user_id=owner,kind="generic",subject_id="synthetic",
            idempotency_key=uuid.uuid4().hex,stage="generate")
        # Generic isolated task exercises the bound connection without pretending to
        # own a classroom domain. Business authorizations are covered separately.
        with cards.db.session() as sess:
            from backend.agents.model_routing import pin_role
            pin_role(sess,task["task_id"],"planning",view[role])
        DisclosureService(cards.db).create(actor_id=owner,task_id=task["task_id"],
            config_id=view[role]["config_id"],content_category="query",scope_snapshot={"material_ids":[]})
        tasks.claim(task["task_id"],lease_owner="synthetic-native")
        adapter=factory(owner,task["task_id"])
        assert adapter.model_name==role
        run_structured_agent(adapter,PersonaReply,[{"role":"user","content":"Synthetic request."}])
        adapter.close()
    assert {endpoint for endpoint,_ in seen}=={value["endpoint"] for value in view.values()}
    assert {host for _,host in seen}=={f"{role}.example.test" for role in view}


def test_role_model_api_uses_revision_and_hides_cross_owner_configs(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from fastapi.responses import JSONResponse
    from backend.slice1.router import create_slice1_router
    cards,_=env;owner="synthetic-api-"+uuid.uuid4().hex
    chosen=config(cards,owner,"research")
    app=FastAPI()
    app.include_router(create_slice1_router(configs=cards.configs,capabilities=None,
        disclosures=None,connectors=None,resolve_user=lambda request:SimpleNamespace(user_id=owner,is_admin=False)))
    @app.exception_handler(Slice1Error)
    async def handled(request,exc):
        return JSONResponse({"error_code":exc.code},status_code=exc.status)
    client=TestClient(app)
    response=client.put("/api/capabilities/model-roles/research",json={
        "config_id":chosen["config_id"],"expected_revision":0})
    assert response.status_code==200
    assert "synthetic-key" not in response.text
    assert client.put("/api/capabilities/model-roles/research",json={
        "config_id":chosen["config_id"],"expected_revision":0}).status_code==409
    assert {row["role"] for row in client.get("/api/capabilities/model-roles").json()["data"]}=={
        "planning","teaching","dialogue","research","research_search","video_understanding"}
