"""Synthetic bounded Nuwa creation and budget/attestation contracts."""
import uuid
from datetime import timedelta

from backend.characters.distillation_models import BuildRequest
from backend.characters.persona import publication_quality
from backend.tasks.service import LearningTaskRow
from test_character_cards import env  # noqa: F401
from test_persona_evidence import research_builder
from test_persona_workflows import builder


def test_default_fast_build_keeps_full_artifacts_and_uses_only_three_model_stages(env,monkeypatch):
    from backend.characters import distillation
    observed=[]
    runner=distillation.run_structured_agent
    def run(*args,**kwargs):
        observed.append((kwargs["agent_name"],kwargs["max_requests"],kwargs["timeout_seconds"]))
        return runner(*args,**kwargs)
    monkeypatch.setattr(distillation,"run_structured_agent",run)
    cards,owner,service,calls=builder(env,monkeypatch,pipeline_version=7)
    req=BuildRequest(mode="ai",topic="Synthetic original",subject={"kind":"original"},
        mentor_evaluation=True,confirmed=True,request_key=uuid.uuid4().hex)
    created=service.create(owner,req)
    assert service.create(owner,req)["build_id"]==created["build_id"]
    assert service.run(created["build_id"]).get("accepted")
    saved=service.get(owner,created["build_id"])
    assert [name for name,_ in calls]==["fast_research","fast_replies","fast_verify"]
    persona=saved["result"]["persona"]
    assert len(persona["references"])==6
    assert all(len(item["body"])>50 for item in persona["references"])
    assert persona["quality"]["passed"] and persona["quality"]["default_eligible"]
    assert persona["quality"]["repair_cycles"]==0
    assert persona["quality"]["tests"]==5
    assert saved["timing"]["target_seconds"]==600
    assert 0<=saved["timing"]["elapsed_seconds"]<600
    assert [item[1] for item in observed]==[2]*3
    assert [item[2] for item in observed]==[120,90,90]
    for heading in ("身份卡","回答工作流","核心心智模型","决策启发式","表达DNA","时间线","价值与反模式",
            "智识谱系","诚实边界","每讲末尾费曼对话","来源附录"):
        assert heading in persona["skill"]
    extraction=persona["provenance"]["extraction"]
    assert all(model["mechanism"] in persona["skill"] and model["limits"] in persona["skill"]
        and all(evidence in persona["skill"] for evidence in model["evidence"])
        for model in extraction["models"])
    assert extraction["expression_dna"] in persona["skill"]
    assert all(item in persona["skill"] for item in extraction["values_and_tensions"]+extraction["boundaries"])
    assert "7812110bf7e5a2b01b6aa5a738568044121bbfa2" in persona["skill"]
    with cards.db.session() as sess:
        task=sess.get(LearningTaskRow,saved["task_id"])
        assert 599<=(task.deadline_at-task.created_at).total_seconds()<=601
        from backend.agents.task_binding import AgentBudgetRow
        assert sess.get(AgentBudgetRow,task.task_id).request_limit==24
    card=service.accept(owner,created["build_id"])
    with cards.db.session() as sess:
        assert publication_quality(sess,card["card_id"],card["persona"],require_mentor=True)


def test_fast_public_research_caps_searches_and_reads_without_source_richness_loop(env,monkeypatch):
    cards,owner,service,calls,issued,_=research_builder(env,monkeypatch,pipeline_version=7)
    req=BuildRequest(mode="ai",topic="Synthetic Person",confirmed=True,request_key=uuid.uuid4().hex,
        research_policy={"max_searches":12,"max_reads":60})
    created=service.create(owner,req)
    assert service.create(owner,req)["build_id"]==created["build_id"]
    assert service.run(created["build_id"]).get("accepted")
    saved=service.get(owner,created["build_id"])
    assert len(issued)==1
    assert saved["evidence"]["read_attempts"]<=6
    assert saved["request"]["research_policy"]["max_searches"]==1
    assert saved["request"]["research_policy"]["max_candidates"]==10
    assert len(calls)==3
    assert saved["result"]["persona"]["quality"]["grade"]=="limited"
    card=service.accept(owner,created["build_id"])
    assert card["can_publish"]


def test_fast_failure_is_not_published_or_repaired_automatically(env,monkeypatch):
    _,owner,service,calls=builder(env,monkeypatch,pipeline_version=7)
    import test_persona_workflows
    normal=test_persona_workflows.synthetic_reply
    def reply(stage,payload,props):
        result=normal(stage,payload,props)
        if stage=="fast_verify" and "persona" in result:
            result["persona"]["no_fabricated_quotes"]={"passed":False,"reason":"Synthetic invented quote."}
        return result
    monkeypatch.setattr(test_persona_workflows,"synthetic_reply",reply)
    created=service.create(owner,BuildRequest(mode="ai",topic="Synthetic",subject={"kind":"original"},
        mentor_evaluation=True,confirmed=True,request_key=uuid.uuid4().hex))
    assert service.run(created["build_id"]).get("accepted")
    card=service.accept(owner,created["build_id"])
    assert not card["can_publish"] and not card["persona"]["quality"]["default_eligible"]
    assert len(calls)==3


def test_fast_expired_task_can_explicitly_retry_with_a_new_bounded_window(env,monkeypatch):
    cards,owner,service,_=builder(env,monkeypatch,pipeline_version=7)
    created=service.create(owner,BuildRequest(mode="ai",topic="Synthetic",subject={"kind":"original"},
        confirmed=True,request_key=uuid.uuid4().hex))
    with cards.db.session() as sess:
        row=sess.get(LearningTaskRow,created["task_id"])
        row.deadline_at=row.created_at-timedelta(seconds=1)
        row.status="expired"
    retried=service.action(owner,created["build_id"],"retry")
    assert retried["status"]=="queued"
    with cards.db.session() as sess:
        row=sess.get(LearningTaskRow,created["task_id"])
        assert (row.deadline_at-row.updated_at).total_seconds()<=600
    assert service.run(created["build_id"]).get("accepted")


def test_fast_txt_selection_does_not_leak_internal_runtime_fields_into_request(env,monkeypatch):
    cards,owner,service,_=builder(env,monkeypatch,pipeline_version=7)
    created=service.create(owner,BuildRequest(mode="text",topic="Synthetic collection",
        source_text="Synthetic A and Synthetic B.",confirmed=True,request_key=uuid.uuid4().hex))
    assert service.run(created["build_id"]).get("accepted")
    selected=service.action(owner,created["build_id"],"select","Synthetic A")
    assert BuildRequest.model_validate(selected).selected_character=="Synthetic A"
