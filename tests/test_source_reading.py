"""Source reading contracts use synthetic text and native PDF fixtures only."""
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.teaching.source_reading import (
    ReadingUnit, SourceReadingPlan, SourceReadingStepCandidate, _chunks,
    emphasis_regions, pdf_paragraphs, source_reading_blueprint,
)
from backend.teaching.learning_outputs import LessonBlueprint, teaching_schedule
from backend.teaching.outputs import StepCandidate, validate_step_duration
from test_document_attachments import pdf


def unit(number=1):
    text = "Recorded discovery supports replay."
    return ReadingUnit(unit_id=f"u{number}", material_id="synthetic", target_id="p1_para1_s0",
        title="核心方法", section_role="core_method", reading_mode="close_reading",
        source_text=text, context_text=text,
        target={"page_no":1,"text":text,"char_boxes":[[i/100,.2,(i+1)/100,.22] for i in range(len(text))]},
        knowledge_point_id="replay", knowledge_point_label="历史回放", knowledge_point_end=number==30)


def test_reading_plan_long_schedule_roundtrip_does_not_relax_standard():
    plan = SourceReadingPlan(title="Synthetic paper", source_fingerprint="a"*64,
        units=[unit(i) for i in range(1,31)])
    blueprint=source_reading_blueprint(plan,{"lesson_goal":"理解回放", "scope_capabilities":[]})
    assert len(teaching_schedule(LessonBlueprint.model_validate_json(blueprint.model_dump_json())))==30
    invalid=blueprint.model_dump(); invalid["kind"]="standard"
    with pytest.raises(ValidationError):
        LessonBlueprint.model_validate(invalid)


def test_exact_bilingual_order_and_ordinary_text_ceiling():
    original="Recorded discovery supports replay."
    translated="已记录的发现过程支持回放。"
    data={"title":"回放","board":{"title":"回放"},"source_target_id":"p1_para1_s0",
        "original_text":original,"translation":translated,"text":original+translated+"关键在于复用已经观察到的结果。"}
    assert SourceReadingStepCandidate.model_validate(data).translation==translated
    for text in (translated+original,original+"This is not a translation."):
        with pytest.raises(ValidationError):
            SourceReadingStepCandidate.model_validate({**data,"text":text})
    with pytest.raises(ValidationError):
        StepCandidate.model_validate({"title":"普通课","text":"字"*481,"board":{"title":"普通课"}})
    with pytest.raises(Exception):
        validate_step_duration("字"*600,source_reading=True)


def test_chunks_preserve_all_source_and_emphasis_uses_character_boxes():
    text="A full sentence. "*45
    pieces=_chunks(text)
    assert " ".join(pieces)==text.strip()
    assert all(len(p)<=240 for p in pieces)
    reading=unit()
    regions=emphasis_regions("discovery",reading)
    assert regions and regions[0]["bbox"][0]==reading.source_text.index("discovery")/100
    assert emphasis_regions("fabricated",reading)==[]


def test_native_pdf_paragraphs_have_true_geometry(tmp_path):
    path=tmp_path/"synthetic.pdf";path.write_bytes(pdf(2))
    targets=pdf_paragraphs(path,"synthetic","a"*64,1)
    assert targets
    for target in targets:
        assert len(target["text"])==len(target["char_boxes"])
        assert target["regions"]
        assert all(0<=v<=1 for region in target["regions"] for v in region["bbox"])
