"""Real transaction/lease boundaries, synthetic models and independent voice tracks."""
import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest
from backend.teaching.errors import TeachingError
from backend.teaching.feynman import (
    checkpoint_concept_ids,
    checkpoints,
    review_topics,
    spaced_review_policy,
)
from backend.teaching.feynman_outputs import FeynmanEvaluation
from backend.teaching.learning_models import (
    FeynmanSessionRow,
    FeynmanTurnRow,
    LessonBlueprintRow,
)
from backend.teaching.learning_outputs import (
    LessonBlueprint,
    teaching_schedule,
    validate_blueprint,
)
from backend.teaching.models import TeachingStepRow
from pydantic_ai.models.test import TestModel
from sqlalchemy import select
from test_native_teaching_authorization import approve, env  # noqa: F401


def prepare(env, monkeypatch, voice=False):
    service = env.service
    monkeypatch.setattr(service.feynman, "kick", lambda ident: None)
    start = approve(env, tts=voice)
    conv_id = start["conversation_id"]
    with env.db.session() as sess:
        plan = sess.get(LessonBlueprintRow, conv_id)
        boundary = checkpoints(plan.plan_json)[0]
        for number in range(1, boundary["after_step_no"] + 1):
            sess.add(TeachingStepRow(step_id="step_" + uuid.uuid4().hex,
                conversation_id=conv_id, generation=1, step_no=number, status="ready",
                text="Synthetic taught content: ordered data permits excluding all smaller candidates.",
                phase="example", covered_goal_ids_json=json.dumps(boundary["goal_ids"])))
    service.save_checkpoint(conv_id, actor_id=env.owner, client_seq=1,
        step_no=boundary["after_step_no"], position_ms=1000,
        board_state={"presentation": {"paused": True, "text_complete": True}})
    return conv_id, boundary


def model(env, output):
    original = env.service.model_adapter_factory
    def factory(owner, task_id):
        return TestModel(custom_output_args=output)
    factory.config_for_owner = original.config_for_owner
    env.service.model_adapter_factory = factory


def opening(env, conv_id, boundary):
    model(env, {"question": "Explain why ordered data allows excluding smaller candidates.",
        "criteria": ["Explain the ordering precondition."]})
    env.service.feynman.start(conv_id, env.owner, boundary["checkpoint_id"])
    active = env.service.feynman.view(conv_id, env.owner)["active"]
    assert env.service.feynman.run(active["task_id"])["status"] == "succeeded"
    return env.service.feynman.view(conv_id, env.owner)["active"]


def submit(env, conv_id, output, *, help=False, key=None):
    active = env.service.feynman.view(conv_id, env.owner)["active"]
    model(env, output)
    task = env.service.feynman.answer(conv_id, env.owner, active["question_id"],
        active["revision"], "Synthetic explanation of the ordering condition.",
        key or uuid.uuid4().hex, help=help)
    assert env.service.feynman.run(task["task_id"])["status"] == "succeeded"
    return env.service.feynman.view(conv_id, env.owner)["active"]


def probe():
    return {"decision": "probe", "feedback": "Explain the missing precondition.",
        "evidence": [], "gaps": ["Ordering was not explained."],
        "next_question": "Why would the same exclusion fail for unordered data?"}


def terminal(decision="understood"):
    return {"decision": decision, "feedback": "The ordering condition is now explicit.",
        "evidence": ["The answer explains the ordering condition."] if decision == "understood" else [],
        "gaps": [] if decision == "understood" else ["Needs to connect ordering with exclusion."],
        "next_question": ""}


def test_boundaries_are_goal_ends_not_every_segment(env, monkeypatch):
    _, boundary = prepare(env, monkeypatch)
    assert boundary["after_step_no"] == 6
    assert boundary["goal_ids"] == ["g1"]


def test_persona_question_feedback_share_frozen_delivery_and_persist_audio_event(env, monkeypatch):
    from backend.characters.models import CharacterCardRow, CharacterSegmentRow
    from backend.characters.schemas import Profile
    from backend.agents.roles import feynman_agent
    conv, point = prepare(env, monkeypatch)
    card_id = "synthetic_" + uuid.uuid4().hex
    with env.db.session() as sess:
        sess.add(CharacterCardRow(card_id=card_id, owner_user_id=env.owner, current_revision=1))
        sess.flush()
        sess.add(CharacterSegmentRow(conversation_id=conv, step_no=point["after_step_no"],
            card_id=card_id, revision=1, profile_json=Profile(name="Synthetic",
                identity="称呼学习者为同学", personality="关注条件；答错先检查前提").model_dump_json()))
    captured = []
    for name in ("question", "evaluate"):
        original = getattr(feynman_agent, name)
        def wrapped(adapter, input, *args, _original=original, **kwargs):
            captured.append(json.loads(input.messages[-1]["content"])["character_expression"])
            return _original(adapter, input, *args, **kwargs)
        monkeypatch.setattr(feynman_agent, name, wrapped)
    opening(env, conv, point)
    active = submit(env, conv, probe())
    assert [p["event"] for p in captured] == ["question", "feedback"]
    assert captured[0]["expression_plan"] == captured[1]["expression_plan"]
    assert "同学" in captured[1]["expression_plan"]["delivery"]["relationship"]
    assert captured[1]["continuity"]["recent_dialogue"][-1]["answer"]
    with env.db.session() as sess:
        output = sess.scalar(select(FeynmanTurnRow).where(
            FeynmanTurnRow.session_id == active["session_id"]).order_by(FeynmanTurnRow.turn_no.desc()))
        assert json.loads(output.evaluation_json)["gaps"] == probe()["gaps"]


def test_opening_is_private_until_published_and_is_not_a_probe(env, monkeypatch):
    conv, point = prepare(env, monkeypatch)
    active = opening(env, conv, point)
    assert active["state"] == "waiting" and active["probe_count"] == 0
    assert active["history"][0]["text"].startswith("Explain")
    assert "criteria" not in str(active)
    repeated = env.service.feynman.start(conv, env.owner, point["checkpoint_id"])
    assert repeated["session_id"] == active["session_id"]


@pytest.mark.parametrize("result", ["understood", "review"])
def test_five_probes_wait_for_fifth_answer_and_never_publish_sixth(env, monkeypatch, result):
    conv, point = prepare(env, monkeypatch)
    opening(env, conv, point)
    for count in range(1, 6):
        active = submit(env, conv, probe())
        assert active["state"] == "waiting" and active["probe_count"] == count
    active = submit(env, conv, terminal(result))
    assert active["state"] == "feedback" and active["probe_count"] == 5 and active["result"] == result
    with env.db.session() as sess:
        questions = list(sess.scalars(select(FeynmanTurnRow).where(
            FeynmanTurnRow.session_id == active["session_id"], FeynmanTurnRow.kind == "question")))
        assert len(questions) == 6


def test_early_understanding_closes_after_feedback_and_unblocks_course(env, monkeypatch):
    conv, point = prepare(env, monkeypatch)
    opening(env, conv, point)
    active = submit(env, conv, terminal())
    assert active["state"] == "feedback" and active["probe_count"] == 0
    with pytest.raises(TeachingError):
        env.service.save_checkpoint(conv, actor_id=env.owner, client_seq=2, step_no=7, position_ms=0)
    env.service.feynman.action(conv, env.owner, active["revision"], "continue")
    env.service.save_checkpoint(conv, actor_id=env.owner, client_seq=2, step_no=7, position_ms=0)


def test_answer_retry_is_idempotent_and_stale_question_cannot_overwrite(env, monkeypatch):
    conv, point = prepare(env, monkeypatch)
    active = opening(env, conv, point)
    model(env, probe())
    args = (conv, env.owner, active["question_id"], active["revision"], "Synthetic answer", "same-key")
    first = env.service.feynman.answer(*args)
    assert env.service.feynman.answer(*args) == first
    env.service.feynman.run(first["task_id"])
    with pytest.raises(TeachingError):
        env.service.feynman.answer(conv, env.owner, active["question_id"], active["revision"],
            "A late answer", "different-key")


def test_help_keeps_question_and_does_not_consume_probe(env, monkeypatch):
    conv, point = prepare(env, monkeypatch)
    active = opening(env, conv, point)
    reply = submit(env, conv, terminal("uncertain"), help=True)
    assert reply["state"] == "waiting" and reply["probe_count"] == 0
    assert reply["question_id"] == active["question_id"]


def test_skip_is_unverified_and_late_model_result_cannot_reopen(env, monkeypatch):
    conv, point = prepare(env, monkeypatch)
    active = opening(env, conv, point)
    task = env.service.feynman.answer(conv, env.owner, active["question_id"], active["revision"],
        "Synthetic answer", "skip-race")
    changed = env.service.feynman.view(conv, env.owner)["active"]
    env.service.feynman.action(conv, env.owner, changed["revision"], "skip")
    assert env.service.feynman.run(task["task_id"])["status"] == "failed"
    assert env.service.feynman.view(conv, env.owner)["active"] is None
    with env.db.session() as sess:
        assert sess.get(FeynmanSessionRow, active["session_id"]).result == "skipped"


def test_owner_and_playback_projection_are_scoped(env, monkeypatch):
    conv, point = prepare(env, monkeypatch)
    active = opening(env, conv, point)
    with pytest.raises(TeachingError) as caught:
        env.service.feynman.view(conv, "another-owner")
    assert caught.value.status == 404
    env.service.feynman.playback(conv, env.owner, active["question_id"], 2, 0, True)
    assert not env.service.feynman.playback(conv, env.owner, active["question_id"], 1, 0, False)["saved"]


def test_audio_gates_publication_and_answer_then_survives_refresh(env, monkeypatch):
    conv, point = prepare(env, monkeypatch, voice=True)
    active = opening(env, conv, point)
    assert active["state"] == "audio_pending" and active["history"] == []
    with env.db.session() as sess:
        turn = sess.scalar(select(FeynmanTurnRow).where(FeynmanTurnRow.session_id == active["session_id"]))
        audio_task = turn.audio_task_id
    receipt = env.service.feynman.run(audio_task)
    assert receipt["status"] == "succeeded", receipt
    active = env.service.feynman.view(conv, env.owner)["active"]
    assert active["state"] == "waiting" and active["history"][0]["audio_id"]
    with pytest.raises(TeachingError):
        env.service.feynman.answer(conv, env.owner, active["question_id"], active["revision"], "Answer", "too-soon")
    env.service.feynman.playback(conv, env.owner, active["question_id"], 1, 1000, True)
    assert env.service.feynman.view(conv, env.owner)["active"]["history"][0]["playback"]["ended"]
    changed = submit(env, conv, terminal())
    assert changed["state"] == "audio_pending" and changed["result"] == "understood"
    with env.db.session() as sess:
        final = sess.scalar(select(FeynmanTurnRow).where(FeynmanTurnRow.session_id == active["session_id"])
            .order_by(FeynmanTurnRow.turn_no.desc()))
        final_task = final.audio_task_id
    assert final_task != audio_task
    assert env.service.feynman.run(final_task)["status"] == "succeeded"


def test_false_positive_understanding_is_rejected():
    with pytest.raises(ValueError):
        FeynmanEvaluation(decision="understood", feedback="Good", evidence=[], gaps=[])


def test_two_small_points_under_one_goal_get_distinct_checkpoints():
    stages=[]
    for point in ["point_a","point_b"]:
        for phase in ["intuition","concept","example"]:
            stages.append({"phase":phase,"goal_ids":["g1"],"purpose":point,
                "segments":[{"purpose":point+" "+phase,"knowledge_point_id":point,
                    "knowledge_point_label":point,"knowledge_point_end":phase=="example"}]})
    stages.append({"phase":"summary","goal_ids":["g1"],"purpose":"summary","segments":[{"purpose":"summary"}]})
    plan=LessonBlueprint.model_validate({"goals":[{"id":"g1","label":"Broad goal"}],"stages":stages})
    validate_blueprint(None,plan)
    points=checkpoints(plan.model_dump_json())
    assert [point["after_step_no"] for point in points]==[3,6]
    assert [point["knowledge_point_id"] for point in points]==["point_a","point_b"]
    assert checkpoint_concept_ids(points[0]) == ["point_a"]
    assert review_topics(teaching_schedule(plan), [{"step_no": 3}], plan.goals) == {
        "point_a": "point_a"}
    assert spaced_review_policy(points[0], SimpleNamespace(enabled=True), "alice")
    assert not spaced_review_policy(points[0], SimpleNamespace(enabled=False), "alice")
    assert not spaced_review_policy(points[0], SimpleNamespace(enabled=True), "guest_alice")
    assert checkpoint_concept_ids({"knowledge_point_ids": ["a", "b"],
        "scope_capability_ids": ["capability"]}) == []


def test_question_cannot_be_started_before_presented_text_is_complete(env,monkeypatch):
    conv,point=prepare(env,monkeypatch)
    env.service.save_checkpoint(conv,actor_id=env.owner,client_seq=2,step_no=6,position_ms=0,
        board_state={"presentation":{"text_complete":False}})
    with pytest.raises(TeachingError):
        env.service.feynman.start(conv,env.owner,point["checkpoint_id"])


def test_changed_source_cannot_publish_evaluation(env,monkeypatch):
    conv,point=prepare(env,monkeypatch)
    active=opening(env,conv,point)
    model(env,terminal())
    task=env.service.feynman.answer(conv,env.owner,active["question_id"],active["revision"],"Answer","source-change")
    with env.db.session() as sess:
        step=sess.scalar(select(TeachingStepRow).where(TeachingStepRow.conversation_id==conv))
        step.text="Changed version"
    assert env.service.feynman.run(task["task_id"])["status"]=="failed"
    assert env.service.feynman.view(conv,env.owner)["active"]["state"]=="evaluating"


def test_sixth_probe_candidate_is_rejected_and_does_not_consume_count(env,monkeypatch):
    conv,point=prepare(env,monkeypatch)
    active=opening(env,conv,point)
    with env.db.session() as sess:
        sess.get(FeynmanSessionRow,active["session_id"]).probe_count=5
    active=env.service.feynman.view(conv,env.owner)["active"]
    model(env,probe())
    task=env.service.feynman.answer(conv,env.owner,active["question_id"],active["revision"],"Answer","sixth")
    assert env.service.feynman.run(task["task_id"])["status"]=="failed"
    active=env.service.feynman.view(conv,env.owner)["active"]
    assert active["probe_count"]==5 and active["retry_available"]
    assert len([item for item in active["history"] if item["role"]=="assistant"])==1


@pytest.mark.parametrize("valid_subtitles",[True,False])
def test_saved_voice_factory_authorizes_private_turn_and_failure_does_not_publish(env,monkeypatch,valid_subtitles):
    from backend.teaching.tts_config import configured_tts_factory
    from backend.slice1.models import ServiceConfigRow
    conv,point=prepare(env,monkeypatch,voice=True)
    describe=env.service.tts_adapter_factory.config_for_owner
    def view(owner):
        base=describe(owner)
        return {**base,"endpoint":"https://voice.example.test","model_name":"synthetic",
            "is_active":True} if base else None
    configs=SimpleNamespace(db=env.db,latest_active_for_owner=lambda owner,kind:view(owner),
        get=lambda ident,**options:view(env.owner),
        resolve_credentials=lambda ident:{"api_key":"synthetic-key","voice_id":"synthetic-voice"})
    calls=[]
    def build(protocol,**options):
        def synthesize(value,**kwargs):
            calls.append(value)
            return {"audio_hex":b"synthetic-audio".hex(),
                "subtitle":[{"text":value,"begin_ms":0,"end_ms":1000}] if valid_subtitles else None}
        return SimpleNamespace(synthesize=synthesize,close=options["client"].close)
    monkeypatch.setattr("backend.teaching.tts_config.build_adapter",build)
    env.service.tts_adapter_factory=configured_tts_factory(configs,env.service.disclosures)
    env.service.tts_adapter_factory.authorize_task=env.service._check_authorization
    active=opening(env,conv,point)
    with env.db.session() as sess:
        turn=sess.scalar(select(FeynmanTurnRow).where(FeynmanTurnRow.session_id==active["session_id"]))
        ident=turn.audio_task_id
    result=env.service.feynman.run(ident)
    assert result["status"]==("succeeded" if valid_subtitles else "failed"),result
    assert len(calls)==1
    after=env.service.feynman.view(conv,env.owner)["active"]
    assert (after["state"],len(after["history"]))==(("waiting",1) if valid_subtitles else ("audio_pending",0))
    if not valid_subtitles:
        assert after["retry_available"] and after["probe_count"]==0


def test_deploying_new_templates_preserves_frozen_lesson_authorization(env,monkeypatch):
    start=approve(env,tts=True)
    original=env.service.prompts.load_local_template
    def changed(name):
        data=dict(original(name))
        if name in ("teaching_step_v1","lesson_blueprint_v1"):
            data["template"]+=" Synthetic deployment change."
            data["sha256"]=hashlib.sha256(data["template"].encode()).hexdigest()
        return data
    monkeypatch.setattr(env.service.prompts,"load_local_template",changed)
    with env.db.session() as sess:
        from backend.planning.models import ConversationRow
        conv=sess.get(ConversationRow,start["conversation_id"])
        assert env.service.authorization.require(sess,conv)
        assert env.service.authorization.require(sess,conv,audio=True)


def test_parallel_tabs_share_one_session_and_one_answer_task(env,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy import func
    from backend.tasks.service import LearningTaskRow
    conv,point=prepare(env,monkeypatch)
    ready=Barrier(2)
    def begin():
        ready.wait()
        return env.service.feynman.start(conv,env.owner,point["checkpoint_id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(begin) for _ in range(2)]
        sessions=[future.result() for future in futures]
    assert sessions[0]["session_id"]==sessions[1]["session_id"]
    model(env,{"question":"Explain why sorted data permits excluding a whole range.",
        "criteria":["Ordering must be explicit."]})
    env.service.feynman.run(sessions[0]["task_id"])
    active=env.service.feynman.view(conv,env.owner)["active"]
    ready=Barrier(2)
    def answer_once():
        ready.wait()
        return env.service.feynman.answer(conv,env.owner,active["question_id"],active["revision"],
            "Synthetic concurrent answer","parallel-idempotency")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(answer_once) for _ in range(2)]
        answers=[future.result() for future in futures]
    assert answers[0]==answers[1]
    with env.db.session() as sess:
        assert sess.scalar(select(func.count()).select_from(FeynmanSessionRow).where(
            FeynmanSessionRow.conversation_id==conv))==1
        assert sess.scalar(select(func.count()).select_from(LearningTaskRow).where(
            LearningTaskRow.subject_id.like(conv+":feynman:%:answer")))==1


def test_feynman_source_question_persists_exactly_and_delete_blocks_publish(env, monkeypatch):
    from backend.materials.study_sources import documents, chapter_catalog, freeze_sources
    from backend.materials.models import MaterialRow
    from backend.planning.models import LessonRow, OutlineVersionRow
    from test_study_sources import attachment
    ident = attachment(env.db, env.owner)
    with env.db.session() as sess:
        lesson = sess.get(LessonRow, env.lesson)
        chapter = chapter_catalog(documents(sess, env.owner, [ident]))[0]
        frozen = freeze_sources(sess, env.owner, [ident],
            [(env.lesson, {"source_section_ids": [chapter["id"]]})])
        sess.get(OutlineVersionRow, lesson.outline_version_id).source_snapshot_json = json.dumps(frozen)
    conv, point = prepare(env, monkeypatch)
    with env.db.session() as sess:
        blueprint = sess.get(LessonBlueprintRow, conv)
        assert json.loads(blueprint.context_json)["source_snapshot"] == frozen
    opened = env.service.feynman.start(conv, env.owner, point["checkpoint_id"])
    with env.db.session() as sess:
        session = sess.get(FeynmanSessionRow, opened["session_id"])
        candidates = json.loads(session.context_json)["source_candidates"]
    candidate = next(c for c in candidates if c["feynman_suitable"])
    rejected = [{"question_id": c["id"], "reason": "unsuitable_feynman",
                 "explanation": "This candidate requires a calculation or additional case context."}
                for c in candidates if c["id"] != candidate["id"]]
    model(env, {"question": candidate["prompt"], "criteria": ["Explain the sampling mechanism."],
        "source_question_id": candidate["id"], "source_use": "original", "source_rejections": rejected})
    assert env.service.feynman.run(opened["task_id"])["status"] == "succeeded"
    active = env.service.feynman.view(conv, env.owner)["active"]
    assert active["history"][0]["text"] == candidate["prompt"]
    assert active["history"][0]["source"]["kind"] == "original"
    model(env, probe())
    answer = env.service.feynman.answer(conv, env.owner, active["question_id"],
        active["revision"], "A synthetic source-based explanation.", uuid.uuid4().hex)
    with env.db.session() as sess:
        sess.get(MaterialRow, ident).deleted_at = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc)
    assert env.service.feynman.run(answer["task_id"])["status"] == "failed"
