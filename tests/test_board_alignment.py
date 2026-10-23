"""Synthetic narration references, version fences and safe partial publication."""
import json

import pytest
from backend.teaching.board_alignment import aligned_beats, board_targets, scene_beats, require_alignment, exact_subtitles
from backend.teaching.board_blocks import TeachingBeat, BLOCK_ADAPTER
from backend.teaching.outputs import StepCandidate, FollowupCandidate
from backend.teaching.content_stream import partial_step
from backend.prompts.service import PromptService, PromptUnavailable
from backend.db import Database
from test_teaching_assessments import env, database, lesson  # noqa: F401


TEXT = "先校验输入，再调用工具，最后汇总结果。" + "讲解会逐一解释每个动作的含义，并保持板书里的词语与这里的说明一致。" * 3


def payload():
    return {"alignment_version": 1, "title": "Synthetic", "text": TEXT,
            "board": {"title": "Synthetic", "format_version": 3},
            "beats": [{"id": "one", "text": TEXT, "blocks": [
                {"id": "check", "type": "text", "text": "校验输入"},
                {"id": "tool", "type": "text", "text": "调用工具"}],
                "cues": [{"block_id": "check", "target": "text", "quote": "校验输入"},
                         {"block_id": "tool", "target": "text", "quote": "调用工具"}]}]}


def test_exact_quotes_resolve_offsets_and_keep_legacy_outputs_compatible():
    candidate = StepCandidate.model_validate(payload())
    result = scene_beats(candidate)
    assert result[0]["cues"][0]["start"] == 1
    assert TEXT[result[0]["cues"][1]["start"]:result[0]["cues"][1]["end"]] == "调用工具"
    assert candidate.board.format_version == 3
    legacy = payload()
    legacy.pop("alignment_version")
    legacy["beats"][0].pop("cues")
    assert StepCandidate.model_validate(legacy).alignment_version == 0
    with pytest.raises(Exception):
        require_alignment(StepCandidate.model_validate(legacy))
    assert FollowupCandidate(alignment_version=1, answer=TEXT).alignment_version == 1


@pytest.mark.parametrize("failure", ["quote", "wording", "target", "coverage", "order", "legacy", "negation"])
def test_mismatches_cannot_publish_as_aligned_content(failure):
    value = payload()
    beat = value["beats"][0]
    if failure == "quote":
        beat["cues"][0]["quote"] = "not present"
    elif failure == "wording":
        beat["blocks"][0]["text"] = "绕过输入检查"
    elif failure == "target":
        beat["cues"][0]["target"] = "cell:0:0"
    elif failure == "coverage":
        beat["cues"].pop()
    elif failure == "order":
        beat["cues"].reverse()
    elif failure == "negation":
        value["text"] = value["text"].replace("再调用工具", "再不调用工具")
        beat["text"] = value["text"]
    else:
        value["board"]["points"] = ["Unbound text"]
    with pytest.raises(ValueError):
        StepCandidate.model_validate(value)


@pytest.mark.parametrize("block", [
    {"id": "b", "type": "text", "title": "校验", "text": "校验输入"},
    {"id": "b", "type": "table", "columns": ["类型", "值"], "rows": [["输入", "23"]]},
    {"id": "b", "type": "graph", "nodes": [{"id": "a", "label": "输入"}, {"id": "z", "label": "结果"}],
     "edges": [{"from_id": "a", "to_id": "z", "label": "处理"}]},
    {"id": "b", "type": "code", "code": "return value + 23;"},
    {"id": "b", "type": "formula", "formula": "x+23", "plain_text": "x+23"},
    {"id": "b", "type": "array", "cells": [{"id": "c", "value": "23", "index": "5", "pointer": "mid"}], "note": "范围"},
    {"id": "b", "type": "stack", "frames": [{"id": "f", "label": "call", "variables": ["n = 23"]}]},
    {"id": "b", "type": "layers", "layers": [{"id": "l", "label": "结构", "detail": "组织内容"}]},
    {"id": "b", "type": "chart", "chart": "bar", "labels": ["输入"], "values": [[23]], "is_example": True},
])
def test_every_visible_expression_has_a_typed_narration_reference(block):
    parsed = BLOCK_ADAPTER.validate_python(block)
    targets = board_targets(parsed)
    quotes = [" ".join(filter(None, values)) for values, _ in targets.values()]
    beat = TeachingBeat.model_validate({"id": "one", "blocks": [block], "text": "；".join(quotes),
        "cues": [{"block_id": "b", "target": target, "quote": quote} for target, quote in zip(targets, quotes)]})
    assert len(aligned_beats([beat], [parsed])[0]["cues"]) == len(targets)


def test_unicode_repeated_quotes_and_partial_beats_are_not_guessed():
    block = {"id": "b", "type": "text", "text": "校验"}
    beat = {"id": "one", "blocks": [block], "text": "😀校验，然后再次校验。",
            "cues": [{"block_id": "b", "target": "text", "quote": "校验", "occurrence": 1}]}
    parsed = TeachingBeat.model_validate(beat)
    cues = aligned_beats([parsed], [BLOCK_ADAPTER.validate_python(block)])[0]["cues"]
    assert cues[0]["start"] == parsed.text.rfind("校验")
    raw = '{"steps":[{"beats":[' + json.dumps(beat, ensure_ascii=False)
    assert partial_step(raw[:-1], require_alignment=True)["text"] == ""
    view = partial_step(raw, require_alignment=True)
    assert view["text"] == beat["text"] and view["board"]["alignment_version"] == 1
    changed = {**block, "text": "不能改写旧内容"}
    assert partial_step(raw, require_alignment=True, existing_blocks={"b": changed})["text"] == ""


def test_new_alignment_snapshot_is_immutable_when_continued():
    db = Database()
    prompts = PromptService(db)
    prompts.bind_for_task("synthetic-aligned-source", "board_alignment_v1", allow_fallback=True)
    prompts.clone_for_task("synthetic-aligned-next", "synthetic-aligned-source", "board_alignment_v1")
    assert prompts.resolve_for_task("synthetic-aligned-source", "board_alignment_v1")["template_sha256"] == \
        prompts.resolve_for_task("synthetic-aligned-next", "board_alignment_v1")["template_sha256"]
    with pytest.raises(PromptUnavailable):
        prompts.resolve_for_task("synthetic-legacy-source", "board_alignment_v1")
    db.dispose()


@pytest.mark.parametrize("quote", ["x 加 24", "x 减 23", "y 加 23"])
def test_formula_cannot_change_number_operator_or_identifier(quote):
    block = BLOCK_ADAPTER.validate_python({"id":"f","type":"formula","formula":"x+23","plain_text":"x+23"})
    beat = TeachingBeat.model_validate({"id":"one","text":quote,"blocks":[block.model_dump()],
        "cues":[{"block_id":"f","target":"formula","quote":quote}]})
    with pytest.raises(ValueError):
        aligned_beats([beat],[block])


def test_new_table_cells_cannot_be_written_backwards_or_after_a_later_block():
    table = {"id":"t","type":"table","columns":["输入","输出"],"rows":[["23","24"]]}
    other = {"id":"o","type":"text","text":"处理"}
    for quotes, targets in [
        (["输出","输入"], [("t","cell:0:1"),("t","cell:0:0")]),
        (["输入","处理","输出"], [("t","cell:0:0"),("o","text"),("t","cell:0:1")]),
    ]:
        beat = TeachingBeat.model_validate({"id":"one","text":"。".join(quotes),"blocks":[table,other],
            "cues":[{"block_id":block,"target":target,"quote":quote}
                    for quote,(block,target) in zip(quotes,targets)]})
        with pytest.raises(ValueError, match="board cue"):
            aligned_beats([beat], [BLOCK_ADAPTER.validate_python(table),BLOCK_ADAPTER.validate_python(other)])


def test_raw_new_block_cannot_bypass_beat_coverage_but_frozen_block_can_be_referenced():
    value = payload()
    frozen = BLOCK_ADAPTER.validate_python({"id":"old","type":"text","text":"旧知识"}).model_dump()
    value["board"]["blocks"] = [frozen]
    candidate = StepCandidate.model_validate(value)
    with pytest.raises(Exception):
        require_alignment(candidate)
    require_alignment(candidate, {"old":frozen})


def test_subtitle_validation_preserves_unicode_and_rejects_missing_words_or_reordered_times():
    text = "😀输入，输出。"
    cues = [{"text":"😀输入","display_start":0,"display_end":3,"begin_ms":0,"end_ms":100},
            {"text":"输出","display_start":4,"display_end":6,"begin_ms":100,"end_ms":200}]
    assert exact_subtitles(text,cues)
    assert not exact_subtitles(text,cues[1:])
    assert not exact_subtitles(text,[cues[0],{**cues[1],"begin_ms":90}])
    assert not exact_subtitles(text,[{**cues[0],"display_end":2},cues[1]])
    assert exact_subtitles(text,[{"word":text,"begin_ms":0,"end_ms":100}])


def test_real_service_publishes_references_and_freezes_them_for_next_segment(env):
    _, _, start = lesson(env)
    client, state, owner, headers = env
    result = state.teaching.run_step_generation(start["task_id"])
    assert result["status"] == "succeeded"
    workspace = client.get(f"/api/teaching/{start['conversation_id']}", headers=headers).json()
    board = workspace["steps"][0]["board"]
    assert board["alignment_version"] == 1
    text = workspace["steps"][0]["text"]
    for beat in board["beats"]:
        for cue in beat["cues"]:
            assert text[cue["start"]:cue["end"]]
    following = next(task for task in workspace["queued_tasks"]
                     if task["kind"] == "lesson_step_generate" and task["step_no"] == 2)
    assert state.teaching.prompts.resolve_for_task(start["task_id"], "board_alignment_v1")["template_sha256"] == \
        state.teaching.prompts.resolve_for_task(following["task_id"], "board_alignment_v1")["template_sha256"]
