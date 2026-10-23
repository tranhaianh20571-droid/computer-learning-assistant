"""Synthetic v6 delivery, canon boundaries and immutable publication attestations."""
import uuid

import pytest
from pydantic import ValidationError

from backend.characters.distillation_models import BuildRequest,CharacterSubject,PersonaAcceptance
from backend.characters.persona import PersonaPackage,publication_quality
from backend.slice1.errors import Slice1Error
from test_character_cards import env  # noqa: F401
from test_persona_workflows import builder
from test_persona_evidence import research_builder


def request(**values):
    return BuildRequest(mode="ai",topic="Synthetic Person",confirmed=True,
        request_key=uuid.uuid4().hex,**values)


def test_subject_and_acceptance_schema_require_explicit_fiction_and_all_checks():
    with pytest.raises(ValidationError):
        CharacterSubject(kind="fictional")
    with pytest.raises(ValidationError):
        PersonaAcceptance.model_validate({"checks":[{"passed":True}]})
    assert CharacterSubject(kind="fictional",work="Synthetic novel",version="Volume 1").version=="Volume 1"


@pytest.mark.parametrize("kind",["real","fictional"])
def test_one_grounded_work_can_publish_without_meeting_research_count_targets(env,monkeypatch,kind):
    cards,owner,service,_,_,_=research_builder(env,monkeypatch,pipeline_version=6)
    from test_persona_evidence import BODY
    monkeypatch.setattr(service,"_read_source",lambda *args:BODY)
    subject={"kind":kind}
    if kind=="fictional":subject.update(work="Synthetic novel",version="Original book",story_point="Chapter 3")
    created=service.create(owner,request(subject=subject,research_policy={"max_searches":1}))
    assert service.run(created["build_id"]).get("accepted")
    saved=service.get(owner,created["build_id"])
    package=saved["result"]["persona"]
    assert saved["request"]["pipeline_version"]==6
    assert saved["evidence"]["effective_documents"]==1
    assert package["quality"]["passed"]
    assert package["quality"]["grade"]==("limited" if kind=="real" else "grounded")
    assert len(package["references"])==6
    if kind=="fictional":
        assert package["references"][0]["name"]=="01-dialogue.md"
        assert package["provenance"]["subject"]==saved["request"]["subject"]
    card=service.accept(owner,created["build_id"])
    assert card["can_publish"]
    cards.publish(owner,card["card_id"],card["revision"])
    with cards.db.session() as sess:
        assert not publication_quality(sess,card["card_id"],card["persona"],require_mentor=True)
    # Public quality disclosures cannot be rewritten independently of the attested content.
    forged=PersonaPackage.model_validate({**card["persona"],
        "quality":{**card["persona"]["quality"],"grade":"forged","default_eligible":True}})
    edited=cards.edit_persona(owner,card["card_id"],card["revision"],forged)
    assert not edited["can_publish"]


def test_original_ai_uses_authored_settings_without_search_configuration(env,monkeypatch):
    cards,owner,service,calls=builder(env,monkeypatch,pipeline_version=6)
    service.search_factory=lambda *args:pytest.fail("original creation must not search")
    created=service.create(owner,request(subject={"kind":"original"}))
    assert service.run(created["build_id"]).get("accepted")
    saved=service.get(owner,created["build_id"])
    assert saved["evidence"]["search_calls"]==0
    assert not any(key=="research_plan" for key,_ in calls)
    package=saved["result"]["persona"]
    assert package["quality"]["passed"] and package["quality"]["grade"]=="authored"
    assert len(package["references"])==6
    card=service.accept(owner,created["build_id"])
    assert card["can_publish"]


def test_zero_evidence_remains_draft_and_two_empty_rounds_stop_search(env,monkeypatch):
    cards,owner,service,calls,issued,_=research_builder(env,monkeypatch,pipeline_version=6)
    monkeypatch.setattr(service,"_read_source",lambda *args:"short")
    created=service.create(owner,request(research_policy={"max_searches":5}))
    assert service.run(created["build_id"]).get("accepted")
    saved=service.get(owner,created["build_id"])
    assert len(issued)==2
    assert "gap_plan" not in [key for key,_ in calls]
    assert saved["result"]["persona"]["quality"]["grade"]=="draft"
    card=service.accept(owner,created["build_id"])
    assert not card["can_publish"]
    with pytest.raises(Slice1Error):
        cards.publish(owner,card["card_id"],card["revision"])


def test_explicit_mentor_evaluation_is_required_and_five_cases_are_attested(env,monkeypatch):
    cards,owner,service,calls=builder(env,monkeypatch,pipeline_version=6)
    created=service.create(owner,request(subject={"kind":"original"},mentor_evaluation=True))
    assert service.run(created["build_id"]).get("accepted")
    saved=service.get(owner,created["build_id"])
    quality=saved["result"]["persona"]["quality"]
    assert quality["default_eligible"] and quality["mentor_tests"]==5
    assert sum(key.startswith("mentor_test_") for key,_ in calls)==5
    card=service.accept(owner,created["build_id"])
    with cards.db.session() as sess:
        assert publication_quality(sess,card["card_id"],card["persona"],require_mentor=True)


def test_fictional_version_is_frozen_and_cannot_reuse_other_version(env,monkeypatch):
    _,owner,service,_,_,_=research_builder(env,monkeypatch,pipeline_version=6)
    original=service.create(owner,request(subject={"kind":"fictional","work":"Synthetic novel","version":"Book"}))
    assert service.run(original["build_id"]).get("accepted")
    with pytest.raises(Slice1Error):
        service.create(owner,request(subject={"kind":"fictional","work":"Synthetic novel","version":"Anime"},
            reuse_build_id=original["build_id"]))


def test_failed_honesty_cannot_be_offset_by_complete_sources(env,monkeypatch):
    cards,owner,service,_,_,_=research_builder(env,monkeypatch,pipeline_version=6)
    import test_persona_workflows
    original=test_persona_workflows.synthetic_reply
    def reply(stage,payload,props):
        result=original(stage,payload,props)
        if "no_fabricated_quotes" in props:
            result["no_fabricated_quotes"]={"passed":False,"reason":"Synthetic fabricated quote was found."}
        return result
    monkeypatch.setattr(test_persona_workflows,"synthetic_reply",reply)
    created=service.create(owner,request(research_policy={"max_searches":1}))
    assert service.run(created["build_id"]).get("accepted")
    saved=service.get(owner,created["build_id"])
    assert not saved["result"]["persona"]["quality"]["passed"]
    assert saved["result"]["persona"]["quality"]["repair_cycles"]==2
    card=service.accept(owner,created["build_id"])
    assert not card["can_publish"]


def test_v6_txt_hard_failure_also_prevents_publication(env,monkeypatch):
    cards,owner,service,_=builder(env,monkeypatch,pipeline_version=6)
    import test_persona_workflows
    original=test_persona_workflows.synthetic_reply
    def reply(stage,payload,props):
        result=original(stage,payload,props)
        if "teaching_accuracy" in props:
            result["teaching_accuracy"]={"passed":False,"reason":"Synthetic incorrect teaching."}
        return result
    monkeypatch.setattr(test_persona_workflows,"synthetic_reply",reply)
    created=service.create(owner,BuildRequest(mode="text",topic="Synthetic",
        selected_character="Synthetic",source_text="Synthetic author supplied text.",
        request_key=uuid.uuid4().hex,confirmed=True))
    assert service.run(created["build_id"]).get("accepted")
    card=service.accept(owner,created["build_id"])
    assert not card["can_publish"]
    with pytest.raises(Slice1Error):
        cards.publish(owner,card["card_id"],card["revision"])


def test_balanced_verification_context_keeps_each_source_and_its_quote():
    from backend.characters.distillation import DistillationService
    sources=[{"id":str(index),"text":"x"*6000+"VERIFIED"+"y"*6000,
        "evidence_start":6000,"evidence":"VERIFIED","usable":True,"kind":"primary"} for index in range(10)]
    context=DistillationService._source_context(sources,limit=4000,balanced=True)
    assert len(context)==10 and sum(len(item["text"]) for item in context)<=4000
    assert all(item["verified_quote"]=="VERIFIED" and "VERIFIED" in item["text"] for item in context)


def test_attested_candidate_can_upgrade_existing_card_and_keep_original_identity(env,monkeypatch):
    from backend.characters.schemas import Profile
    cards,owner,service,_,_,_=research_builder(env,monkeypatch,pipeline_version=6)
    baseline=cards.create(owner,Profile(name="Synthetic original mentor"))
    created=service.create(owner,request(research_policy={"max_searches":1}))
    assert service.run(created["build_id"]).get("accepted")
    candidate=service.accept(owner,created["build_id"])
    upgraded=cards.edit_persona(owner,baseline["card_id"],baseline["revision"],
        PersonaPackage.model_validate(candidate["persona"]))
    assert upgraded["card_id"]==baseline["card_id"] and upgraded["can_publish"]
    assert upgraded["profile"]==baseline["profile"]
    cards.publish(owner,upgraded["card_id"],upgraded["revision"])
    with cards.db.session() as sess:
        assert not publication_quality(sess,upgraded["card_id"],upgraded["persona"],require_mentor=True)


def test_effect_subset_keeps_paired_inputs_and_rejects_empty_or_unknown_categories():
    from persona_effect_live import case_groups
    selected=case_groups(["explain","feynman"])
    assert [item["category"] for item in selected]==["explain","feynman"]
    assert sum(len(item["cases"]) for item in selected)==6
    for categories in ([],["unknown-category"],["explain","explain"]):
        with pytest.raises(ValueError):
            case_groups(categories)


def test_generation_retry_uses_frozen_research_without_research_or_search_respend(env,monkeypatch):
    from backend.characters.distillation_models import CharacterBuildStageRow
    from backend.planning.errors import error
    _,owner,service,calls,issued,_=research_builder(env,monkeypatch,pipeline_version=6)
    factory=service.model_factory
    fail=True
    def model(current,task):
        with service.db.session() as sess:
            key=sess.get(CharacterBuildStageRow,task).stage_key
        if fail and key.startswith("extract_models_v6"):raise error("MODEL_CONNECTION_ERROR")
        return factory(current,task)
    service.model_factory=model
    created=service.create(owner,request(research_policy={"max_searches":1}))
    assert service.run(created["build_id"])["status"]=="failed"
    prior=len(calls);searches=len(issued);fail=False
    service.action(owner,created["build_id"],"retry")
    assert service.run(created["build_id"]).get("accepted")
    assert len(issued)==searches
    assert not any(key.startswith("research_") or key.startswith("source_review") for key,_ in calls[prior:])


def test_v6_budget_upgrade_preserves_consumption_and_legacy_ceiling(env,monkeypatch):
    from backend.agents.task_binding import AgentBudgetRow
    cards,owner,service,_=builder(env,monkeypatch,pipeline_version=6)
    created=service.create(owner,request(subject={"kind":"original"}))
    with cards.db.session() as sess:
        budget=sess.get(AgentBudgetRow,created["task_id"])
        assert budget.request_limit==256
        budget.request_limit=128;budget.logical_requests=23;budget.wire_requests=41
    service._ensure_execution_budget(owner,created["build_id"])
    service._ensure_execution_budget(owner,created["build_id"])
    with cards.db.session() as sess:
        budget=sess.get(AgentBudgetRow,created["task_id"])
        assert (budget.request_limit,budget.logical_requests,budget.wire_requests)==(256,23,41)


def test_optional_gap_plan_outage_keeps_grounded_card_and_failure_disclosure(env,monkeypatch):
    from backend.characters.distillation_models import CharacterBuildStageRow
    from backend.planning.errors import error
    _,owner,service,_,issued,_=research_builder(env,monkeypatch,pipeline_version=6)
    factory=service.model_factory
    def model(current,task):
        with service.db.session() as sess:
            key=sess.get(CharacterBuildStageRow,task).stage_key
        if key=="gap_plan":raise error("MODEL_PROVIDER_ERROR")
        return factory(current,task)
    service.model_factory=model
    created=service.create(owner,request(research_policy={"max_searches":2}))
    assert service.run(created["build_id"]).get("accepted")
    saved=service.get(owner,created["build_id"])
    assert saved["result"]["persona"]["quality"]["passed"]
    assert saved["result"]["persona"]["quality"]["grade"]=="limited"
    assert saved["result"]["persona"]["provenance"]["research_failures"]["gap_plan"]=="MODEL_PROVIDER_ERROR"
    assert len(issued)==1
    assert any(item["stage"]=="gap_plan_fallback" and item["status"]=="succeeded" for item in saved["stages"])
