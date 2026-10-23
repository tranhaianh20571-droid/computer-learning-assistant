"""Typed teaching objects and their references reject ambiguous or unsafe layouts."""
import pytest
from backend.teaching.board_blocks import BLOCK_ADAPTER
from backend.teaching.outputs import BoardDeclaration, StepCandidate, declaration_to_scene


@pytest.mark.parametrize("block", [
    {"id":"array","type":"array","cells":[{"id":"a","value":"23","index":"5","state":"found","pointer":"mid"}]},
    {"id":"stack","type":"stack","frames":[{"id":"f","label":"fact(1)","variables":["n = 1"],"active":True}]},
    {"id":"layers","type":"layers","layers":[{"id":"html","label":"HTML","detail":"内容与结构"}]},
    {"id":"flow","type":"graph","nodes":[{"id":"n","label":"是否匹配？","shape":"decision"}],"edges":[]},
    {"id":"code","type":"code","code":"return 1","active_lines":[1]},
])
def test_new_objects_are_serializable_in_version_three(block):
    value = BLOCK_ADAPTER.validate_python(block)
    scene = declaration_to_scene(BoardDeclaration(title="Synthetic board",format_version=3,blocks=[value]))
    assert scene["presentation"]["version"] == 3
    assert scene["presentation"]["blocks"][0]["type"] == block["type"]


@pytest.mark.parametrize("block", [
    {"id":"array","type":"array","cells":[{"id":"a","value":"1"},{"id":"a","value":"2"}]},
    {"id":"stack","type":"stack","frames":[{"id":"a","label":"a","active":True},{"id":"b","label":"b","active":True}]},
    {"id":"layers","type":"layers","layers":[{"id":"l","label":"L","detail":"x"},{"id":"l","label":"L","detail":"y"}]},
    {"id":"code","type":"code","code":"return 1","active_lines":[2]},
    {"id":"array","type":"array","cells":[{"id":"a","value":"1","html":"<script>"}]},
])
def test_invalid_structure_is_rejected(block):
    with pytest.raises(ValueError):
        BLOCK_ADAPTER.validate_python(block)


def test_stack_cannot_reference_a_missing_code_snapshot():
    with pytest.raises(ValueError):
        StepCandidate(title="Synthetic",text="Synthetic text. "*8,board={"title":"Synthetic",
            "blocks":[{"id":"s","type":"stack","code_ref":"missing","frames":[{"id":"f","label":"call"}]}]})


def test_semantic_focus_can_reference_array_cells():
    candidate=StepCandidate(title="Synthetic",text="Synthetic text. "*8,
        board={"title":"Synthetic","format_version":3},
        beats=[{"id":"beat","text":"Synthetic text. "*8,"blocks":[
            {"id":"a","type":"array","cells":[{"id":"cell","value":"1"}]}],"highlight_ids":["cell"]}])
    assert candidate.board.blocks[0].cells[0].value == "1"


def test_graph_generation_checks_are_specific_and_preserve_old_snapshots():
    from backend.teaching.board_blocks import graph_issues
    block = BLOCK_ADAPTER.validate_python({"id":"g","type":"graph","diagram":"flowchart",
        "nodes":[{"id":"a","label":"Synthetic condition","shape":"decision"},
            {"id":"b","label":"Synthetic branch"},{"id":"c","label":"Synthetic alternative"}],
        "edges":[{"from_id":"a","to_id":"b"},{"from_id":"a","to_id":"c"}]})
    assert graph_issues([block])
    assert graph_issues([block], {"g"}) == []
    block.edges[0].label, block.edges[1].label = "yes", "no"
    assert graph_issues([block]) == []
    block.edges.append(block.edges[0])
    assert graph_issues([block])


def test_state_loop_requires_trigger_only_for_new_generation():
    from backend.teaching.board_blocks import graph_issues
    block = BLOCK_ADAPTER.validate_python({"id":"g","type":"graph","diagram":"state",
        "nodes":[{"id":"a","label":"Waiting"}],"edges":[{"from_id":"a","to_id":"a"}]})
    assert graph_issues([block])
    block.edges[0].label = "No input"
    assert graph_issues([block]) == []
