"""Synthetic presentation contracts, private previews and legacy compatibility."""
import json
import pytest
from pydantic import ValidationError
from backend.teaching.board_blocks import BLOCK_ADAPTER
from backend.teaching.outputs import StepCandidate, FollowupCandidate, BoardDeclaration, declaration_to_scene
from backend.teaching.content_stream import partial_step
from test_teaching_latency import TEXT
from test_teaching_assessments import env, database, lesson  # noqa: F401

BLOCK={"id":"mapping","type":"table","title":"标签与内容","columns":["标签","内容"],"rows":[["键","值"]]}
BEATS=[{"id":"b1","blocks":[BLOCK],"text":TEXT[:70],"highlight_ids":["mapping"]},
       {"id":"b2","blocks":[{"id":"boundary","type":"text","text":"标签唯一，内容可以更新"}],
        "text":TEXT[70:],"highlight_ids":["boundary"]}]


def candidate(**changes):
    return {"beats":BEATS,"text":TEXT,"title":"键值查找","board":{"title":"字典"},**changes}


def test_typed_beats_derive_and_validate_one_final_text_and_board():
    step=StepCandidate.model_validate(candidate(text=""))
    assert step.text==TEXT and step.board.format_version==2
    assert [b.id for b in step.board.blocks]==["mapping","boundary"]
    with pytest.raises(ValidationError):StepCandidate.model_validate(candidate(text=TEXT+"不一致"))
    bad=json.loads(json.dumps(BEATS));bad[0]["highlight_ids"]=["future"]
    with pytest.raises(ValidationError):StepCandidate.model_validate(candidate(beats=bad))


@pytest.mark.parametrize("block",[
    {"id":"bad","type":"table","columns":["a","b"],"rows":[["missing"]]},
    {"id":"bad","type":"graph","nodes":[{"id":"n","label":"节点"}],"edges":[{"from_id":"n","to_id":"missing"}]},
    {"id":"bad","type":"chart","chart":"line","labels":["a"],"values":[[1]]},
    {"id":"bad","type":"chart","chart":"line","labels":["a"],"values":[[float('nan')]],"is_example":True},
    {"id":"bad","type":"html","text":"<script>synthetic</script>"},
])
def test_reject_invalid_tables_graphs_unsourced_data_and_unknown_renderers(block):
    with pytest.raises(ValidationError):BLOCK_ADAPTER.validate_python(block)


@pytest.mark.parametrize("chart",["bar","line","heatmap"])
def test_numerical_examples_are_explicitly_labeled(chart):
    parsed=BLOCK_ADAPTER.validate_python({"id":"example","type":"chart","chart":chart,"labels":["x","y"],
        "values":[[1,-2]],"is_example":True})
    assert parsed.is_example


def test_preview_never_exposes_an_unclosed_graph_before_its_complete_edges():
    raw='{"steps":[{"beats":[{"id":"b1","blocks":[{"id":"g","type":"graph","nodes":[{"id":"n","label":"合成节点"}],"edges":['
    assert partial_step(raw)["board"] is None
    raw+=']}],"text":"解释这个节点'
    preview=partial_step(raw)
    assert preview["text"]=="解释这个节点"
    assert preview["board"]["presentation"]["blocks"][0]["id"]=="g"
    assert preview["beats"][0]["block_ids"]==["g"]


def test_partial_beat_text_and_complete_board_are_incremental():
    raw=json.dumps({"steps":[{"beats":BEATS}]},ensure_ascii=False)
    view=partial_step(raw)
    assert view["text"]==TEXT and len(view["beats"])==2
    assert [b["id"] for b in view["board"]["presentation"]["blocks"]]==["mapping","boundary"]


def test_legacy_board_still_converts_without_losing_code_or_relations():
    legacy=BoardDeclaration(title="旧版",points=["定义"],code="\n".join(f"x = {i}" for i in range(15)),
        nodes=[{"id":"a","label":"输入"},{"id":"b","label":"输出"}],edges=[{"from_id":"a","to_id":"b"}])
    blocks=declaration_to_scene(legacy)["presentation"]["blocks"]
    assert [b["type"] for b in blocks]==["text","code","graph"]
    assert blocks[1]["code"].splitlines()[-1]=="x = 14"


def test_followup_beats_derive_answer_and_optional_board():
    output=FollowupCandidate.model_validate({"beats":BEATS,"answer":""})
    assert output.answer==TEXT and len(output.board.blocks)==2


def test_real_agent_pipeline_saves_beats_in_authorized_workspace(env):
    _,_,start=lesson(env);client,state,user,headers=env
    class Adapter:
        supports_text_stream=True
        model_name="synthetic-beats"
        def stream(self,messages):
            payload=candidate(phase="intuition",covered_goal_ids=["g1"],lesson_finished=False)
            value=json.dumps({"steps":[payload]},ensure_ascii=False)
            for pos in range(0,len(value),17):yield value[pos:pos+17]
    state.teaching.model_adapter_factory=lambda *args:Adapter()
    response=client.post(f"/api/teaching/tasks/{start['task_id']}/run",headers=headers)
    assert response.json()["status"]=="succeeded",response.text
    workspace=client.get(f"/api/teaching/{start['conversation_id']}",headers=headers).json()
    saved=workspace["steps"][0]
    assert saved["text"]==TEXT and len(saved["board"]["beats"])==2
    assert saved["board"]["presentation"]["version"]==2
