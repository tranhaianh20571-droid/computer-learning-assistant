"""Synthetic persona behavior, continuity, native candidate and cache boundaries."""
from types import SimpleNamespace

from backend.characters.delivery import DeliveryProfile, derive_delivery, expression_payload, feedback_event, delivery_context
from backend.characters.expression import PersonaTraits, content_expression, expression_snapshot, learn_expression
from test_classroom_mentor_policy import cards, env, database, lesson  # noqa: F401
from backend.characters.schemas import Profile, UseMentor


def test_author_behavior_overrides_occupation_and_addresses_are_not_shared():
    maid = derive_delivery({"identity": "学习女仆，称呼学习者为主人，自称铃音",
        "personality": "关注学习者卡住的步骤；答错时先请对方指出变化发生在哪一行"})
    teacher = derive_delivery({"identity": "教师，称呼学习者为同学", "personality": "重视条件；重点先限定适用范围"})
    partner = derive_delivery({"identity": "称呼学习者为伙伴", "personality": "关注预测与验证；提问先让对方预测结果"})
    assert "主人" in maid.relationship and "同学" in teacher.relationship and "伙伴" in partner.relationship
    assert "哪一行" in maid.incorrect and "范围" in teacher.key_point and "预测" in partner.question
    assert len({maid.focus, teacher.focus, partner.focus}) == 3
    payload = expression_payload({}, {}, event="feedback")
    assert set(("correct", "partial", "incorrect", "unsure", "frustrated")) <= payload["event_rules"].keys()
    assert payload["event"] == "feedback"


def test_feedback_uses_saved_judgment_not_surface_word_choice():
    assert feedback_event({"decision": "understood", "evidence": ["synthetic"]}) == "correct"
    assert feedback_event({"decision": "probe", "evidence": ["synthetic"], "gaps": ["synthetic"]}) == "partial"
    assert feedback_event({"decision": "probe", "gaps": ["synthetic"]}) == "incorrect"
    assert feedback_event({}, help_requested=True) == "unsure"
    assert feedback_event({"decision": "uncertain"}) == "feedback"


def test_partial_semantic_brief_keeps_author_event_rules(env):
    profile = {"identity": "称呼学习者为同学", "personality": "答错时请检查原始条件"}
    delivery = DeliveryProfile(relationship="师生关系，必要时称同学", rhythm="简短明确", focus="核对条件")
    traits = PersonaTraits(liveliness=.3, warmth=.6, restraint=.8, variety=.4,
        professional_need=.8, confidence=.8, evidence=["personality"], delivery=delivery)
    with env[1].db.session() as sess:
        learn_expression(sess, profile, "synthetic", traits)
    saved = expression_snapshot(env[1].db, profile, "synthetic")
    assert saved.delivery.focus == "核对条件"
    assert "原始条件" in saved.delivery.incorrect


def test_semantic_delivery_is_private_cached_and_segment_retry_freezes_context(env, tmp_path):
    service = cards(env, tmp_path)
    profile = Profile(name="Synthetic", identity="称呼学习者为同学", personality="关注条件与反例")
    card = service.create(env[2], profile)
    service.use(env[2], UseMentor(expected_version=0, card_id=card["card_id"], revision=1))
    _, _, start = lesson(env)
    delivery = derive_delivery(profile.model_dump()).model_copy(update={"focus": "每次先检查适用条件，再核对反例"})
    traits = PersonaTraits(liveliness=.4, warmth=.6, restraint=.8, variety=.4,
        professional_need=.7, confidence=.8, evidence=["personality"], delivery=delivery)
    with env[1].db.session() as sess:
        learn_expression(sess, card["profile"], "synthetic", traits)
    assert expression_snapshot(env[1].db, card["profile"], "synthetic").delivery == delivery
    adapter = SimpleNamespace(protocol="gemini", model_name="synthetic", reasoning_settings={})
    binding = (start["conversation_id"], 1)
    from backend.characters.service import segment_character
    with env[1].db.session() as sess:
        segment_character(sess, *binding, freeze=True)
    first = content_expression(env[1].db, adapter, card["profile"], binding=binding,
        continuity={"recent_teaching": ["synthetic opening"]})
    retry = content_expression(env[1].db, adapter, card["profile"], binding=binding,
        continuity={"recent_teaching": ["changed"]})
    assert first == retry and first["delivery"]["focus"] == delivery.focus
    assert "delivery" not in service.get(env[2], card["card_id"])["profile"]
    history = [{"question": "q"*500, "answer": "a"*500, "feedback": "f"*500}]*6
    context = delivery_context(env[1].db, start["conversation_id"], 0, history=history)
    assert context["recent_teaching"] == []
    assert len(context["recent_dialogue"]) == 3
    assert all(len(row["answer"]) == 240 for row in context["recent_dialogue"])


def test_actual_teaching_role_receives_delivery_and_real_continuity(env, tmp_path, monkeypatch):
    import json
    from backend.agents.roles import teaching_agent
    service = cards(env, tmp_path)
    card = service.create(env[2], Profile(name="Synthetic", identity="称呼学习者为伙伴",
        personality="关注预测与验证；提问先比较实际结果"))
    service.use(env[2], UseMentor(expected_version=0, card_id=card["card_id"], revision=1))
    _, _, start = lesson(env)
    captured = []
    original = teaching_agent.generate_step
    def generate(adapter, input, *args, **kwargs):
        captured.append(json.loads(input.messages[-1]["content"])["character_expression"])
        return original(adapter, input, *args, **kwargs)
    monkeypatch.setattr(teaching_agent, "generate_step", generate)
    client, state, owner, headers = env
    result = client.post(f"/api/teaching/tasks/{start['task_id']}/run", headers=headers).json()
    assert result["status"] == "succeeded", result
    brief = captured[-1]
    assert brief["event"] == "explaining"
    assert "伙伴" in brief["expression_plan"]["delivery"]["relationship"]
    assert brief["continuity"]["recent_teaching"] == []
    assert "incorrect" in brief["event_rules"]


def test_full_persona_and_frozen_reference_tool_reach_teaching_role(env,tmp_path,monkeypatch):
    import json
    from backend.agents.roles import teaching_agent
    from backend.characters.persona import PersonaPackage
    service=cards(env,tmp_path)
    card=service.create(env[2],Profile(name="Synthetic complete persona"))
    full_skill="# Synthetic causal tutor\nExplain the necessary conditions before each inference.\n"*40
    card=service.edit_persona(env[2],card["card_id"],1,PersonaPackage(skill=full_skill,
        references=[{"name":"01-writings.md","body":"Synthetic frozen source paragraph."}],
        skills=[{"name":"Synthetic comparison","version":"1","instructions":"Compare two concrete inputs."}]))
    service.use(env[2],UseMentor(expected_version=0,card_id=card["card_id"],revision=card["revision"]))
    _,_,start=lesson(env)
    captured=[]
    original=teaching_agent.generate_step
    def generate(adapter,input,*args,**kwargs):
        brief=json.loads(input.messages[-1]["content"])["character_expression"]
        captured.append(brief)
        readers=[tool for tool in kwargs.get("tools",[]) if getattr(tool,"__name__","")=="read_persona_reference"]
        assert len(readers)==1
        assert readers[0]("01-writings.md")=="Synthetic frozen source paragraph."
        assert readers[0]("../private")=="reference_unavailable"
        return original(adapter,input,*args,**kwargs)
    monkeypatch.setattr(teaching_agent,"generate_step",generate)
    result=env[0].post(f"/api/teaching/tasks/{start['task_id']}/run",headers=env[3]).json()
    assert result["status"]=="succeeded",result
    assert captured[-1]["persona"]["skill"]==full_skill
    assert captured[-1]["persona"]["skills"][0]["instructions"]=="Compare two concrete inputs."
