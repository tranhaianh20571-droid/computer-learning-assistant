"""Synthetic evidence, durable search budgets and server-owned publication gates."""
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select,inspect,text

from backend.characters.evidence import (
    canonical_source,page_status,body_hash,verified_assessment,evidence_summary)
from backend.characters.distillation_models import (
    BuildRequest,ResearchPolicy,SourceAssessment,CharacterBuildRow,CharacterBuildStageRow)
from backend.characters.research_search import parse_search_response
from backend.characters.persona import PersonaPackage
from backend.characters.schemas import Profile
from backend.slice1.errors import Slice1Error
from backend.agents.model_routing import role_config,set_role
from test_persona_workflows import builder,synthetic_reply,config,SKILL
from test_character_cards import env  # noqa: F401
from test_native_migrations import engine,migration  # noqa: F401


BODY="Synthetic original statement: learning needs feedback and explicit conditions. "*12


@pytest.mark.parametrize("url,expected",[
    ("https://www.example.test/talk/?utm_source=a&lang=en#t","https://example.test/talk?lang=en"),
    ("http://example.test/a",""),("https://u:p@example.test/a",""),
    ("https://example.test:444/a",""),("javascript:alert(1)","")])
def test_canonical_evidence_url(url,expected):
    assert canonical_source(url)==expected


@pytest.mark.parametrize("body,status",[
    ("short","too_short"),("Just a moment... please enable JavaScript. "+"x"*600,"challenge"),
    (BODY,"readable")])
def test_challenges_and_short_pages_never_count_as_full_text(body,status):
    assert page_status(body)==status


def test_verified_quote_uses_actual_text_offsets_and_body_version():
    source={"id":"s","text":BODY.replace("needs feedback","needs\n  feedback"),"read_status":"full_text"}
    item=SourceAssessment(source_id="s",kind="primary",evidence="learning needs feedback and explicit conditions.",
        reason="Synthetic original.",dimensions=["expression"])
    value=verified_assessment(source,item)
    assert value["kind"]=="primary" and value["usable"]
    assert value["evidence"]==value["text"][value["evidence_start"]:value["evidence_end"]]
    assert value["body_sha256"]==body_hash(value["text"])
    item.evidence="Someone said: "+item.evidence
    assert verified_assessment(source,item)["kind"]=="unknown"
    item.evidence="learning needs feedback and explicit conditions."
    source["read_status"]="duplicate_body"
    assert not verified_assessment(source,item)["usable"]


@pytest.mark.parametrize("output",[
    [{"type":"message","content":[{"annotations":[{"url":"https://example.test/a"}]}]}],
    [{"type":"web_search_call","status":"completed","action":{"type":"search"}}],
    [{"type":"web_search_call","status":"in_progress","action":{"sources":[{"url":"https://example.test/a"}]}}]])
def test_text_links_or_incomplete_tools_are_not_proof_of_search(output):
    with pytest.raises(Exception) as failure:
        parse_search_response({"status":"completed","output":output})
    assert failure.value.code=="SEARCH_OUTPUT_INVALID"


def test_search_counts_observed_actions_and_deduplicates_citations():
    result=parse_search_response({"status":"completed","output":[
        *[{"type":"web_search_call","status":"completed","action":{"type":"search",
            "sources":[{"url":"https://example.test/a?utm_source=x","title":"Synthetic"}]}} for _ in range(17)],
        {"type":"message","content":[{"annotations":[{"url":"https://example.test/a","title":"Synthetic"}]}]}]})
    assert len(result["results"])==1
    assert result["metrics"]["completed_actions"]==17
    assert result["metrics"]["actions"]=={"search":17}


def test_search_prioritizes_final_citations_before_raw_candidate_pool():
    result=parse_search_response({"status":"completed","output":[
        {"type":"web_search_call","status":"completed","action":{"type":"search",
            "sources":[{"url":f"https://raw.example.test/{i}"} for i in range(10)]}},
        {"type":"message","content":[{"annotations":[
            {"url":"https://primary.example.test/transcript","title":"Synthetic original transcript"}]}]}]},1)
    assert result["results"][0]["url"]=="https://primary.example.test/transcript"


def test_provider_billing_block_is_a_controlled_circuit_breaker():
    with pytest.raises(Exception) as caught:
        parse_search_response({"error":{"type":"billing_error"}})
    assert caught.value.code=="SEARCH_QUOTA_EXCEEDED"


@pytest.mark.parametrize("failure",[False,True])
def test_grok_wire_uses_authorized_pinned_exit_without_read_failure_resubmission(failure):
    import asyncio
    import socket
    import httpx2
    from backend.agents.contracts import AgentDeps,RunBudget
    from backend.agents.native import SafeModelTransport
    from backend.characters.research_search import search_responses
    debits=[];authorized=[];wires=[]
    deps=AgentDeps(budget=RunBudget(debit=debits.append),
        authorize_payload=lambda body,url,scope:authorized.append((url,json.loads(body)["input"])))
    def reply(request):
        wires.append(request)
        assert request.headers["host"]=="search.example.test"
        assert request.url.host=="93.184.216.34"
        if failure:
            raise httpx2.ReadTimeout("synthetic",request=request)
        return httpx2.Response(200,json={"status":"completed","output":[
            {"type":"web_search_call","status":"completed","action":{"type":"search",
             "sources":[{"url":"https://source.example.test/a","title":"Synthetic"}]}}]})
    transport=SafeModelTransport("https://search.example.test/v1",deps,inner=httpx2.MockTransport(reply),
        resolver=lambda *args:[(socket.AF_INET,socket.SOCK_STREAM,6,"",("93.184.216.34",443))])
    run=search_responses("https://search.example.test/v1","synthetic-grok","synthetic-key","Synthetic query",
        deps,transport=transport)
    if failure:
        with pytest.raises(Exception) as caught:asyncio.run(run)
        assert caught.value.code=="MODEL_TIMEOUT"
    else:
        assert len(asyncio.run(run)["results"])==1 and debits==["logical","wire","tool"]
    assert len(wires)==1 and authorized


def test_evidence_migration_matches_stage_model_and_refuses_loss(engine):
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE character_build_stages (task_id varchar(64) PRIMARY KEY)"))
        script=migration("b2g3h4i5j6k7_persona_evidence.py",conn)
        script.upgrade()
        column=next(item for item in inspect(conn).get_columns("character_build_stages") if item["name"]=="state_json")
        assert not column["nullable"] and "TEXT" in str(column["type"])
        conn.execute(text("INSERT INTO character_build_stages VALUES ('synthetic',:state)"),
            {"state":json.dumps({"search_started":True})})
        with pytest.raises(RuntimeError,match="nonempty"):
            script.downgrade()
        conn.execute(text("DELETE FROM character_build_stages"))
        script.downgrade()


def research_builder(env,monkeypatch,*,provider="anysearch",pipeline_version=5):
    cards,owner,service,calls=builder(env,monkeypatch,pipeline_version=pipeline_version)
    from backend.auth.models import UserRow
    with cards.db.session() as sess:
        sess.add(UserRow(user_id=owner,email="synthetic-evidence-"+uuid.uuid4().hex+"@example.test",
            password_hash="synthetic-hash",status="approved"))
    if provider=="anysearch":
        search_view=cards.configs.create(actor_id=owner,is_admin=False,kind="search",protocol="anysearch",
            endpoint="https://api.anysearch.com",model_name="",credentials={"api_key":"synthetic-search-key"})
    else:
        search_view=config(cards,owner,"search")
        set_role(cards.configs,owner,"research_search",search_view["config_id"],0)
    issued=[]
    def factory(current,task_id):
        def attempt(query,max_results):
            issued.append(query)
            offset=0 if len(issued)==1 else 2
            return SimpleNamespace(state="succeeded",results=[
                SimpleNamespace(url=f"https://evidence.example.test/{i}",title=f"Synthetic {i}",excerpt="Synthetic excerpt.")
                for i in range(offset,offset+3)])
        return SimpleNamespace(attempt=attempt)
    service.search_factory=factory
    monkeypatch.setattr(service,"_read_source",lambda root,url:BODY+" "+url.rsplit("/",1)[-1])
    def reply(stage,payload,props):
        if "writings" in props and "extraction" in props:
            return synthetic_reply(stage,payload,props)
        if "source_id" in props:
            source=payload["source"]
            return {"source_id":source["id"],"kind":"primary","evidence":
                "learning needs feedback and explicit conditions.","reason":"Synthetic original.",
                "relevant":True,"dimensions":["writings","conversations","expression","external","decisions","timeline"]}
        if "assessments" in props:
            return {"assessments":[{"source_id":item["id"],"kind":"primary","evidence":
                "learning needs feedback and explicit conditions.","reason":"Synthetic original.",
                "relevant":True,"dimensions":["writings","conversations","expression","external","decisions","timeline"]}
                for item in payload["sources"]]}
        value=synthetic_reply(stage,payload,props)
        if stage.startswith("research_external"):
            value["gaps"]=["Synthetic independent criticism needs another document."]
        return value
    monkeypatch.setattr(__import__("test_persona_workflows"),"synthetic_reply",reply)
    return cards,owner,service,calls,issued,search_view


def test_six_dimensions_request_gap_search_reuse_pages_and_preserve_full_artifacts(env,monkeypatch):
    cards,owner,service,calls,issued,_=research_builder(env,monkeypatch)
    reads=[]
    monkeypatch.setattr(service,"_read_source",lambda root,url:reads.append(url) or BODY+" "+url)
    request=BuildRequest(mode="ai",topic="Synthetic Person",confirmed=True,request_key=uuid.uuid4().hex,
        research_policy={"min_documents":3,"min_primary":2,"max_searches":3,"max_reads":10})
    created=service.create(owner,request)
    receipt=service.run(created["build_id"])
    saved=service.get(owner,created["build_id"])
    assert receipt.get("accepted"),saved
    assert len(issued)==2 and len(reads)==5 and len(reads)==len(set(reads))
    package=saved["result"]["persona"]
    assert package["quality"]["passed"] and len(package["references"])==6
    assert package["provenance"]["evidence"]["verified_primary"]==5
    assert saved["evidence"]["search_calls"]==2
    assert saved["evidence"]["read_attempts"]==5
    assert all(stage.get("metrics",{}).get("duration_ms",0)>=0 for stage in saved["stages"])
    accepted=service.accept(owner,created["build_id"])
    assert accepted["can_publish"]
    cards.publish(owner,accepted["card_id"],accepted["revision"])
    copied=cards.copy("synthetic-recipient",accepted["card_id"])
    assert copied["can_publish"]
    cards.publish("synthetic-recipient",copied["card_id"],copied["revision"])
    # Editing authored behavior invalidates the attestation; changing images/profile does not.
    altered=cards.edit_persona(owner,accepted["card_id"],accepted["revision"],
        PersonaPackage.model_validate({**accepted["persona"],"skill":SKILL+"altered"}))
    assert not altered["can_publish"]
    with pytest.raises(Slice1Error):
        cards.publish(owner,altered["card_id"],altered["revision"])
    service.run(created["build_id"])
    assert len(issued)==2


def test_primary_shortfall_triggers_coverage_search_before_dimension_research(env,monkeypatch):
    _,owner,service,calls,issued,_=research_builder(env,monkeypatch)
    from test_persona_workflows import synthetic_reply as original
    def reply(stage,payload,props):
        value=original(stage,payload,props)
        if "source_id" in props and payload["source"]["url"].rsplit("/",1)[-1] in ("0","1","2"):
            value.update(kind="secondary",evidence="")
        if "body" in props:
            value["gaps"]=[]
        return value
    monkeypatch.setattr(__import__("test_persona_workflows"),"synthetic_reply",reply)
    created=service.create(owner,BuildRequest(mode="ai",topic="Synthetic Person",
        confirmed=True,request_key=uuid.uuid4().hex,
        research_policy={"min_documents":3,"min_primary":2,"max_searches":2,"max_reads":10}))
    assert service.run(created["build_id"]).get("accepted")
    saved=service.get(owner,created["build_id"])
    keys=[key for key,_ in calls]
    first_dimension=min(index for index,key in enumerate(keys)
        if key.startswith("research_") and key!="research_plan")
    assert keys.index("coverage_gap_plan")<first_dimension
    assert len(issued)==2
    assert saved["evidence"]["verified_primary"]==2
    assert saved["result"]["persona"]["quality"]["passed"]


def test_resume_after_gap_search_keeps_plan_snapshot_and_never_respends(env,monkeypatch):
    _,owner,service,_,issued,_=research_builder(env,monkeypatch)
    original=service.model_factory
    from backend.planning.errors import error
    def fail_extract(current,task_id):
        with service.db.session() as sess:
            if sess.get(CharacterBuildStageRow,task_id).stage_key.startswith("extract"):
                raise error("MODEL_TIMEOUT")
        return original(current,task_id)
    service.model_factory=fail_extract
    created=service.create(owner,BuildRequest(mode="ai",topic="Synthetic",confirmed=True,
        research_policy={"min_documents":3,"min_primary":2,"max_searches":3},
        request_key=uuid.uuid4().hex))
    assert service.run(created["build_id"])["status"]=="failed" and len(issued)==2
    service.model_factory=original
    service.action(owner,created["build_id"],"retry")
    assert service.run(created["build_id"]).get("accepted")
    assert len(issued)==2


def test_reuse_is_owner_topic_bound_and_does_not_inherit_quality_or_search_spend(env,monkeypatch):
    cards,owner,service,calls,issued,_=research_builder(env,monkeypatch)
    request=BuildRequest(mode="ai",topic="Synthetic",confirmed=True,
        research_policy={"min_documents":3,"min_primary":2,"max_searches":1},request_key=uuid.uuid4().hex)
    old=service.create(owner,request)
    assert service.run(old["build_id"]).get("accepted")
    with service.db.session() as sess:
        row=sess.get(CharacterBuildRow,old["build_id"])
        legacy=json.loads(row.sources_json)
        for source in legacy:
            source.pop("canonical_url",None)
            source.pop("body_sha256",None)
        legacy.extend([{**legacy[0],"id":"synthetic_duplicate_url",
            "url":legacy[0]["url"]+"?utm_source=synthetic"},
            {**legacy[0],"id":"synthetic_duplicate_body","url":"https://evidence.example.test/mirror"}])
        row.sources_json=json.dumps(legacy)
    reused=service.create(owner,request.model_copy(update={"request_key":uuid.uuid4().hex,"reuse_build_id":old["build_id"]}))
    with service.db.session() as sess:
        row=sess.get(CharacterBuildRow,reused["build_id"])
        sources=json.loads(row.sources_json)
        assert len(sources)==3 and all(item["kind"]=="unknown" for item in sources)
        assert all(item.get("usable") is False and not item.get("evidence") for item in sources)
        assert all(item.get("canonical_url") and item.get("body_sha256") for item in sources)
    assert service.run(reused["build_id"]).get("accepted")
    saved=service.get(owner,reused["build_id"])
    assert saved["evidence"]["reused_documents"]==3
    # The independent-criticism gap causes only one new request.
    assert len(issued)==2 and saved["evidence"]["search_calls"]==1
    assert cards.get(owner,reused["card_id"])["published_revision"] is None
    with pytest.raises(Slice1Error):
        service.create(owner,request.model_copy(update={"request_key":uuid.uuid4().hex,
            "reuse_build_id":old["build_id"],"topic":"Different person"}))
    with pytest.raises(Slice1Error):
        service.create(owner,request.model_copy(update={"request_key":uuid.uuid4().hex,
            "reuse_build_id":"another-private-build"}))
    from backend.auth.models import UserRow
    other="synthetic-other-"+uuid.uuid4().hex
    with cards.db.session() as sess:
        sess.add(UserRow(user_id=other,email=other+"@example.test",password_hash="synthetic",status="approved"))
    set_role(cards.configs,other,"research",config(cards,other,"other-research")["config_id"],0)
    cards.configs.create(actor_id=other,is_admin=False,kind="search",protocol="anysearch",
        endpoint="https://api.anysearch.com",model_name="",credentials={"api_key":"synthetic-key"})
    with pytest.raises(Slice1Error) as cross:
        service.create(other,request.model_copy(update={"request_key":uuid.uuid4().hex,
            "reuse_build_id":old["build_id"]}))
    assert cross.value.status==404


def test_cancelled_parent_cannot_publish_a_late_page_into_the_evidence_pool(env,monkeypatch):
    _,owner,service,_,issued,_=research_builder(env,monkeypatch)
    created=service.create(owner,BuildRequest(mode="ai",topic="Synthetic",confirmed=True,
        request_key=uuid.uuid4().hex,research_policy={"max_searches":1}))
    def read(*args):
        service.action(owner,created["build_id"],"cancel")
        return BODY
    monkeypatch.setattr(service,"_read_source",read)
    service.run(created["build_id"])
    saved=service.get(owner,created["build_id"])
    assert saved["status"]=="cancelled" and saved["source_count"]==0
    assert len(issued)==1


def test_classification_timeout_keeps_unknown_evidence_and_does_not_respend_on_resume(env,monkeypatch):
    _,owner,service,_,issued,_=research_builder(env,monkeypatch)
    from backend.planning.errors import error
    factory=service.model_factory
    def fail(current,task):
        with service.db.session() as sess:
            key=sess.get(CharacterBuildStageRow,task).stage_key
        if key.startswith("source_review_compact_"):raise error("MODEL_TIMEOUT")
        return factory(current,task)
    service.model_factory=fail
    created=service.create(owner,BuildRequest(mode="ai",topic="Synthetic",confirmed=True,
        request_key=uuid.uuid4().hex,research_policy={"max_searches":1}))
    assert service.run(created["build_id"]).get("accepted")
    saved=service.get(owner,created["build_id"])
    assert saved["evidence"]["effective_documents"]==0 and not saved["result"]["persona"]["quality"]["passed"]
    assert any(stage["stage"].startswith("source_review_compact_") and stage["stage"].endswith("_fallback")
        for stage in saved["stages"])
    assert len(issued)==1


def test_stage_finishing_during_heartbeat_does_not_mask_its_model_error(env,monkeypatch):
    _,owner,service,_,_,_=research_builder(env,monkeypatch)
    created=service.create(owner,BuildRequest(mode="text",topic="Synthetic",selected_character="Synthetic",
        source_text=BODY,confirmed=True,request_key=uuid.uuid4().hex))
    task=service.tasks.create_task(owner_user_id=owner,kind="character_build_stage",
        subject_id=created["build_id"]+":race",idempotency_key=uuid.uuid4().hex,stage="generate")
    with service.db.session() as sess:
        sess.add(CharacterBuildStageRow(task_id=task["task_id"],build_id=created["build_id"],
            stage_key="race",input_hash="synthetic"))
    lease=service.tasks.claim(task["task_id"],lease_owner="synthetic-race")
    heartbeat=service.tasks.heartbeat
    def raced(current,**kwargs):
        service.tasks.submit_result(lease,stage="generate",status="failed",error_code="MODEL_TIMEOUT")
        return heartbeat(current,**kwargs)
    monkeypatch.setattr(service.tasks,"heartbeat",raced)
    service._renew_stages(created["build_id"],owner)
    assert service.tasks.get_task(lease.task_id)["error_code"]=="MODEL_TIMEOUT"


def test_parent_retry_fences_orphan_child_before_resuming_checkpoint(env,monkeypatch):
    from backend.characters import distillation_prompts as prompts
    from backend.characters.distillation import digest
    cards,owner,service,_=builder(env,monkeypatch)
    created=service.create(owner,BuildRequest(mode="text",topic="Synthetic",
        selected_character="Synthetic",source_text=BODY,confirmed=True,request_key=uuid.uuid4().hex))
    root=service.tasks.claim(created["task_id"],lease_owner="synthetic-parent")
    child=service.tasks.create_task(owner_user_id=owner,kind="character_build_stage",
        subject_id=created["build_id"]+":orphan",idempotency_key=uuid.uuid4().hex,stage="generate")
    with cards.db.session() as sess:
        sess.add(CharacterBuildStageRow(task_id=child["task_id"],build_id=created["build_id"],
            stage_key="orphan",input_hash=digest({})))
    child_lease=service.tasks.claim(child["task_id"],lease_owner="synthetic-child")
    service.tasks.submit_result(root,stage="generate",status="failed",error_code="MODEL_TIMEOUT")
    service.action(owner,created["build_id"],"retry")
    assert service.tasks.get_task(child_lease.task_id)["error_code"]=="TASK_LEASE_EXPIRED"
    retried=service.tasks.claim(created["task_id"],lease_owner="synthetic-parent-retry")
    assert service._stage(retried,"orphan",{},None,prompts.COMMON,
        work=lambda task:{"resumed":True})=={"resumed":True}
    service.tasks.submit_result(retried,stage="generate",status="succeeded")


def test_duplicate_body_and_unrelated_sources_do_not_satisfy_evidence_gate(env,monkeypatch):
    cards,owner,service,_,issued,_=research_builder(env,monkeypatch)
    monkeypatch.setattr(service,"_read_source",lambda *args:BODY)
    created=service.create(owner,BuildRequest(mode="ai",topic="Synthetic",confirmed=True,
        request_key=uuid.uuid4().hex,research_policy={"max_searches":1,"min_documents":3,"min_primary":2}))
    assert service.run(created["build_id"]).get("accepted")
    saved=service.get(owner,created["build_id"])
    assert saved["evidence"]["effective_documents"]==1 and not saved["result"]["persona"]["quality"]["passed"]
    card=service.accept(owner,created["build_id"])
    forged=PersonaPackage.model_validate({**card["persona"],"quality":{"passed":True}})
    card=cards.edit_persona(owner,card["card_id"],card["revision"],forged)
    assert not card["can_publish"]
    with pytest.raises(Slice1Error):cards.publish(owner,card["card_id"],card["revision"])
    assert len(issued)==1


def test_copied_historical_ai_cannot_erase_lineage_to_bypass_publication(env,monkeypatch):
    cards,owner,service,_,_,_=research_builder(env,monkeypatch)
    created=service.create(owner,BuildRequest(mode="ai",topic="Synthetic",confirmed=True,
        request_key=uuid.uuid4().hex,research_policy={"max_searches":1}))
    assert service.run(created["build_id"]).get("accepted")
    original=service.accept(owner,created["build_id"])
    assert not original["can_publish"]
    from backend.characters.models import CharacterCardRow
    # Simulate a pre-gate publication that remains readable for existing users.
    with cards.db.session() as sess:
        sess.get(CharacterCardRow,original["card_id"]).published_revision=original["revision"]
    recipient="synthetic-recipient"
    copied=cards.copy(recipient,original["card_id"])
    stripped=PersonaPackage.model_validate({**copied["persona"],
        "source_kind":"custom","provenance":{},"quality":{"passed":True}})
    edited=cards.edit_persona(recipient,copied["card_id"],copied["revision"],stripped)
    assert not edited["can_publish"]
    with pytest.raises(Slice1Error):
        cards.publish(recipient,edited["card_id"],edited["revision"])
    profile_edit=cards.edit(recipient,edited["card_id"],edited["revision"],Profile(name="Renamed"))
    assert not profile_edit["can_publish"]


def test_unknown_search_submission_is_not_reissued_on_explicit_resume(env,monkeypatch):
    _,owner,service,_,issued,_=research_builder(env,monkeypatch)
    from backend.planning.errors import error
    def factory(*args):
        def attempt(*args,**kwargs):
            issued.append("sent")
            raise error("SEARCH_TIMEOUT")
        return SimpleNamespace(attempt=attempt)
    service.search_factory=factory
    created=service.create(owner,BuildRequest(mode="ai",topic="Synthetic",confirmed=True,
        request_key=uuid.uuid4().hex,research_policy={"max_searches":1}))
    assert service.run(created["build_id"])["status"]=="failed"
    service.action(owner,created["build_id"],"retry")
    assert service.run(created["build_id"]).get("accepted")
    saved=service.get(owner,created["build_id"])
    assert issued==["sent"] and saved["evidence"]["search_calls"]==1
    assert not saved["result"]["persona"]["quality"]["passed"]


def test_grok_has_an_independent_frozen_configuration_and_authorization(env,monkeypatch):
    cards,owner,service,_,issued,search_view=research_builder(env,monkeypatch,provider="grok")
    assert role_config(cards.configs,owner,"research")["config_id"]!=search_view["config_id"]
    created=service.create(owner,BuildRequest(mode="ai",topic="Synthetic",search_provider="grok",
        confirmed=True,request_key=uuid.uuid4().hex))
    from backend.agents.model_routing import TaskModelRoleRow
    from backend.slice1.models import DisclosureGrantRow,ServiceConfigRow
    with cards.db.session() as sess:
        research=sess.get(TaskModelRoleRow,(created["task_id"],"research"))
        search=sess.get(TaskModelRoleRow,(created["task_id"],"research_search"))
        assert research.grant_id!=search.grant_id
        assert sess.get(DisclosureGrantRow,search.grant_id).config_id==search_view["config_id"]
        sess.get(ServiceConfigRow,search_view["config_id"]).config_version+=1
    assert service.run(created["build_id"])["status"]=="failed" and not issued


def test_effect_case_bank_is_paired_and_has_thirty_unique_inputs():
    from persona_effect_live import case_groups
    groups=case_groups()
    assert len(groups)==10
    assert len({case["id"] for group in groups for case in group["cases"]})==30


def test_initial_collection_reserves_read_budget_for_gap_search():
    from backend.characters.distillation import DistillationService
    policy=ResearchPolicy(max_reads=40)
    per_query=DistillationService._initial_read_budget(policy,6)
    assert per_query==5
    assert per_query*6<policy.max_reads
    assert DistillationService._initial_read_budget(ResearchPolicy(max_reads=10),6)==1


def test_dimension_context_is_scoped_and_bounded():
    from backend.characters.distillation import DistillationService
    sources=[
        {"id":"writing","text":"w"*9000,"usable":True,"kind":"primary",
         "dimensions":["writings"],"read_status":"full_text"},
        {"id":"conversation","text":"c"*5000,"usable":True,"kind":"primary",
         "dimensions":["conversations"],"read_status":"full_text"},
        {"id":"unknown","text":"u"*1000,"usable":False,"dimensions":[]},
    ]
    writings=DistillationService._source_context(sources,dimension="writings")
    assert [item["id"] for item in writings]==["writing"]
    assert len(writings[0]["text"])==4000 and writings[0]["context_truncated"]
    assert DistillationService._source_context(sources,dimension="expression")==[]
    reports=DistillationService._report_context({"writings":{"body":"x"*7000,"source_ids":[]}})
    assert len(reports["writings"]["body"])==6000 and reports["writings"]["context_truncated"]


def test_internal_pipeline_version_does_not_change_idempotent_user_request(env,monkeypatch):
    _,owner,service,_,_,_=research_builder(env,monkeypatch)
    request=BuildRequest(mode="ai",topic="Synthetic",confirmed=True,request_key=uuid.uuid4().hex)
    created=service.create(owner,request)
    with service.db.session() as sess:
        build=sess.get(CharacterBuildRow,created["build_id"])
        stored=json.loads(build.request_json)
        stored["pipeline_version"]=2
        build.request_json=json.dumps(stored)
    assert service.create(owner,request)["build_id"]==created["build_id"]


def test_read_idle_timeout_is_separate_from_phase_wall_timeout(env,monkeypatch):
    _,owner,service,_,_,_=research_builder(env,monkeypatch)
    factory=service.model_factory
    seen=[]
    def adapted(current,task):
        model=factory(current,task)
        model.timeout=999
        seen.append(model)
        return model
    service.model_factory=adapted
    created=service.create(owner,BuildRequest(mode="text",topic="Synthetic",
        selected_character="Synthetic",source_text=BODY,confirmed=True,request_key=uuid.uuid4().hex))
    assert service.run(created["build_id"]).get("accepted")
    assert seen and all(model.timeout==180 for model in seen)


@pytest.mark.parametrize("private",[False,True])
def test_public_page_reader_never_sends_credentials_or_contacts_private_dns(monkeypatch,private):
    import asyncio
    import socket
    import httpx2
    from backend.agents.native import SafeModelTransport
    from backend.agents.contracts import AgentDeps
    from backend.characters.evidence import read_public_source
    original=SafeModelTransport.__init__
    seen=[]
    monkeypatch.setattr(socket,"getaddrinfo",lambda *args:[
        (socket.AF_INET,socket.SOCK_STREAM,6,"",("127.0.0.1" if private else "93.184.216.34",443))])
    def initialize(self,endpoint,deps,**kwargs):
        def respond(request):
            seen.append(request)
            assert "authorization" not in request.headers
            return httpx2.Response(200,headers={"content-type":"text/html"},
                text="<html><script>untrusted instruction</script><article>"+BODY+"</article></html>")
        kwargs["inner"]=httpx2.MockTransport(respond)
        original(self,endpoint,deps,**kwargs)
    monkeypatch.setattr(SafeModelTransport,"__init__",initialize)
    operation=read_public_source("https://source.example.test/a",AgentDeps())
    if private:
        with pytest.raises(Exception):asyncio.run(operation)
        assert seen==[]
    else:
        value=asyncio.run(operation)
        assert "untrusted instruction" not in value and page_status(value)=="readable"


@pytest.mark.parametrize("destination",["public","private","http","loop"])
def test_public_page_redirects_revalidate_destination_and_never_forward_cookies(monkeypatch,destination):
    import asyncio
    import socket
    import httpx2
    from backend.agents.native import SafeModelTransport
    from backend.agents.contracts import AgentDeps
    from backend.characters.evidence import read_public_source
    original=SafeModelTransport.__init__
    contacted=[]
    monkeypatch.setattr(socket,"getaddrinfo",lambda host,*args:[
        (socket.AF_INET,socket.SOCK_STREAM,6,"",("127.0.0.1" if host=="private.example.test" else "93.184.216.34",443))])
    targets={"public":"https://target.example.test/article","private":"https://private.example.test/article",
        "http":"http://target.example.test/article","loop":"https://source.example.test/a"}
    def initialize(self,endpoint,deps,**kwargs):
        def respond(request):
            assert "authorization" not in request.headers and "cookie" not in request.headers
            contacted.append(request.headers["host"])
            if request.headers["host"]=="source.example.test":
                return httpx2.Response(302,headers={"location":targets[destination],"set-cookie":"synthetic=secret"})
            return httpx2.Response(200,headers={"content-type":"text/html"},text="<article>"+BODY+"</article>")
        kwargs["inner"]=httpx2.MockTransport(respond)
        original(self,endpoint,deps,**kwargs)
    monkeypatch.setattr(SafeModelTransport,"__init__",initialize)
    result=None
    try:
        result=asyncio.run(read_public_source("https://source.example.test/a",AgentDeps()))
    except Exception:
        assert destination=="private"
    assert contacted==(["source.example.test","target.example.test"] if destination=="public" else ["source.example.test"])
    assert page_status(result)=="readable" if destination=="public" else not result
