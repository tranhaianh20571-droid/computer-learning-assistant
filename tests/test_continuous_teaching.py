"""Frozen multi-segment stages, accumulated board references and final completion."""
import json
import pytest
from pydantic_ai.exceptions import ModelRetry
from backend.teaching.learning_outputs import LessonBlueprint, teaching_schedule, validate_blueprint
from backend.teaching.pedagogy import lightweight_plan
from backend.teaching.learning_models import LessonBlueprintRow
from test_teaching_assessments import env,database,lesson,recap_for  # noqa: F401
from test_teaching_latency import TEXT


def test_new_lightweight_plan_has_multiple_segments_per_semantic_stage():
    blueprint=lightweight_plan({"lesson_goal":"理解字典查找"})
    schedule=teaching_schedule(blueprint)
    assert len(schedule)==7
    assert [s.phase for s in schedule]==["intuition","intuition","concept","concept","example","example","summary"]
    assert schedule[0].stage_no==schedule[1].stage_no==1
    assert schedule[0].stage_segment_no==1 and schedule[1].stage_segment_no==2
    assert schedule[2].stage_no==2
    assert validate_blueprint(None,blueprint) is blueprint


def test_legacy_plan_preserves_one_segment_per_saved_stage():
    blueprint=lightweight_plan({"lesson_goal":"合成目标"})
    for stage in blueprint.stages:stage.segments=[]
    assert len(teaching_schedule(blueprint))==4
    assert all(s.stage_segment_count==1 for s in teaching_schedule(blueprint))


def test_reject_expanded_plan_beyond_safe_budget():
    blueprint=lightweight_plan({"lesson_goal":"合成目标"})
    blueprint.stages=blueprint.stages*4
    with pytest.raises(ModelRetry):validate_blueprint(None,blueprint)


def test_preview_cannot_change_an_already_saved_block():
    from backend.teaching.content_stream import partial_step
    from backend.teaching.board_blocks import BLOCK_ADAPTER
    saved=BLOCK_ADAPTER.validate_python({"id":"same","type":"text","text":"原内容"}).model_dump()
    raw=json.dumps({"steps":[{"beats":[{"id":"b","blocks":[{"id":"same","type":"text","text":"错误改写"}],"text":"不应提前展示"}]}]},ensure_ascii=False)
    preview=partial_step(raw,existing_blocks={"same":saved})
    assert preview["text"]=="" and preview["board"] is None


def test_same_stage_keeps_board_references_until_stage_finishes(env):
    _,_,start=lesson(env);client,state,user,headers=env
    state.teaching.feynman.kick=lambda task_id:None
    with state.db.session() as sess:
        row=sess.get(LessonBlueprintRow,start["conversation_id"])
        row.plan_json=lightweight_plan(json.loads(row.context_json)).model_dump_json()
    seen=[]
    class Adapter:
        supports_text_stream=True
        model_name="synthetic-continuous"
        def stream(self,messages):
            context=json.loads(next(m["content"] for m in reversed(messages) if m["role"]=="user"))
            seen.append(context)
            phase=context["current_stage"];identifier=f"s{phase['stage_no']}_{phase['stage_segment_no']}"
            block={"id":identifier,"type":"text","text":f"本段新增合成知识 {identifier}"}
            previous=list(context["existing_board"].values())
            first_text=block["text"]+"。"+TEXT[:70]
            beats=[{"id":identifier,"blocks":[block],"text":first_text,
                    "cues":[{"block_id":identifier,"target":"text","quote":block["text"]}],
                    "highlight_ids":[previous[0]["id"] if previous else identifier]},
                {"id":identifier+"b","blocks":[],"text":TEXT[70:],"highlight_ids":[identifier]}]
            output={"steps":[{"alignment_version":1,"beats":beats,"text":first_text+TEXT[70:],"title":"自然讲解","board":{"title":"字典","blocks":previous},
                "phase":phase["phase"],"covered_goal_ids":phase["goal_ids"],"lesson_finished":context["is_final"],
                "covered_scope_capability_ids":phase.get("scope_capability_ids", []),
                "knowledge_recap":recap_for(context)}]}
            if phase["phase"]=="example":
                earlier=context.get("previous_examples", [])
                example={"prompt":first_text,"source_use":"generated",
                    "selection_reason":"Synthetic example checks this segment."}
                if earlier:
                    example={**earlier[0],"continues_example_id":earlier[0]["id"]}
                    beats[0]["text"]=example["prompt"]+"。"+first_text
                    output["steps"][0]["text"]="".join(beat["text"] for beat in beats)
                output["steps"][0]["examples"]=[example]
            raw=json.dumps(output,ensure_ascii=False)
            for n in range(0,len(raw),23):yield raw[n:n+23]
    state.teaching.model_adapter_factory=lambda *args:Adapter()
    task=start["task_id"]
    for number in range(1,8):
        result=client.post(f"/api/teaching/tasks/{task}/run",headers=headers)
        assert result.json()["status"]=="succeeded",result.text
        workspace=client.get(f"/api/teaching/{start['conversation_id']}",headers=headers).json()
        saved=workspace["steps"][-1]
        if number==1:
            assert not saved["stage_finished"] and not saved["lesson_finished"]
        if number==2:
            assert saved["stage_finished"] and saved["phase"]=="intuition"
            assert saved["board_scope"]==workspace["steps"][0]["board_scope"]
            assert seen[-1]["existing_board"]["s1_1"]["text"]=="本段新增合成知识 s1_1"
        if number==3:
            assert saved["phase"]=="concept" and seen[-1]["existing_board"]=={}
        assert workspace["planned_steps"]==7
        assert saved["lesson_finished"]==(number==7)
        client.post(f"/api/teaching/{start['conversation_id']}/checkpoint",headers=headers,
            json={"client_seq":number*2,"step_no":number,"position_ms":0,
                "board_state":{"presentation":{"text_complete":True}}})
        workspace=client.get(f"/api/teaching/{start['conversation_id']}",headers=headers).json()
        for point in workspace["feynman"]["checkpoints"]:
            if point["after_step_no"]==number and not point["resolved"]:
                if point.get("scope")=="lesson":
                    assert number==7
                    assert client.post(f"/api/teaching/{start['conversation_id']}/complete",headers=headers).status_code==409
                    continue
                opened=client.post(f"/api/teaching/{start['conversation_id']}/feynman/start",headers=headers,
                    json={"checkpoint_id":point["checkpoint_id"]})
                assert opened.status_code==200,opened.text
                assert client.post(f"/api/teaching/{start['conversation_id']}/feynman/skip",headers=headers,
                    json={"revision":opened.json()["revision"]}).status_code==200
                client.post(f"/api/teaching/{start['conversation_id']}/checkpoint",headers=headers,
                    json={"client_seq":number*2+1,"step_no":number,"position_ms":0})
                workspace=client.get(f"/api/teaching/{start['conversation_id']}",headers=headers).json()
        if number<7:task=next(t["task_id"] for t in workspace["queued_tasks"] if t["step_no"]==number+1)
    assert not any(t["step_no"]==8 for t in workspace["queued_tasks"])
    assert client.post(f"/api/teaching/{start['conversation_id']}/complete",headers=headers).status_code==409
