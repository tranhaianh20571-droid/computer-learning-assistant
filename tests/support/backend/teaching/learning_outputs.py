from typing import Literal
from pydantic import BaseModel, Field
from pydantic_ai.exceptions import ModelRetry

Phase = Literal["intuition", "concept", "example", "misconception", "summary"]


class KnowledgeGoal(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=200)


class PlannedSegment(BaseModel):
    title: str = Field(default="",max_length=80)
    purpose: str = Field(min_length=1,max_length=240)


class TeachingStage(BaseModel):
    phase: Phase
    goal_ids: list[str] = Field(min_length=1, max_length=4)
    purpose: str = Field(min_length=1, max_length=400)
    segments: list[PlannedSegment] = Field(default_factory=list,max_length=4)


class ScheduledSegment(BaseModel):
    phase: Phase
    goal_ids: list[str]
    purpose: str
    title: str = ""
    stage_no: int
    stage_segment_no: int
    stage_segment_count: int


def teaching_schedule(blueprint):
    result=[]
    for stage_no,stage in enumerate(blueprint.stages,1):
        segments=stage.segments or [PlannedSegment(purpose=stage.purpose[:240])]
        for number,segment in enumerate(segments,1):
            result.append(ScheduledSegment(phase=stage.phase,goal_ids=stage.goal_ids,
                purpose=f"阶段目标：{stage.purpose}\n当前短段：{segment.purpose}",title=segment.title,
                stage_no=stage_no,stage_segment_no=number,stage_segment_count=len(segments)))
    return result


class LessonBlueprint(BaseModel):
    goals: list[KnowledgeGoal] = Field(min_length=1, max_length=4)
    stages: list[TeachingStage] = Field(min_length=4, max_length=12)


def validate_blueprint(ctx, output):
    if len(teaching_schedule(output))>24:
        raise ModelRetry("轻量计划展平后最多24个短段，请缩小单段数量但保留教学目标。")
    ids = {g.id for g in output.goals}
    if len(ids) != len(output.goals): raise ModelRetry("知识目标 ID 重复。")
    seen_intuition, seen_concept, seen_example = set(), set(), set()
    for stage in output.stages:
        refs = set(stage.goal_ids)
        if not refs <= ids: raise ModelRetry("阶段引用了不存在的知识目标。")
        if stage.phase == "intuition": seen_intuition |= refs
        elif stage.phase == "concept":
            if not refs <= seen_intuition: raise ModelRetry("每个新知识点必须先建立直觉，再解释概念。")
            seen_concept |= refs
        elif stage.phase == "example":
            if not refs <= seen_concept: raise ModelRetry("应用例子必须在对应概念之后。")
            seen_example |= refs
    if output.stages[-1].phase != "summary" or not ids <= seen_example or not ids <= set(output.stages[-1].goal_ids):
        raise ModelRetry("必须完整覆盖直觉、概念、应用，并以小结结束。")
    return output


def validate_new_blueprint(ctx, output):
    """New generation is strict; saved legacy plans remain readable."""
    validate_blueprint(ctx, output)
    for stage in output.stages:
        minimum = 2 if stage.phase in ("intuition", "concept", "example") else 1
        purposes = [segment.purpose.strip() for segment in stage.segments]
        if len(purposes) < minimum or len(set(purposes)) != len(purposes):
            raise ModelRetry("直觉、概念、应用阶段各至少两个不同目的的短段，其它阶段至少一个；必须提供segments。")
    return output


class AssessmentOption(BaseModel):
    id: str = Field(min_length=1, max_length=10)
    text: str = Field(min_length=1, max_length=600)


class AssessmentQuestion(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    kind: Literal["single", "multiple", "short", "code_reading"]
    prompt: str = Field(min_length=1, max_length=1600)
    options: list[AssessmentOption] = Field(default_factory=list, max_length=6)
    lesson_ids: list[str] = Field(min_length=1, max_length=5)
    goal: str = Field(min_length=1, max_length=300)
    answer: list[str] = Field(min_length=1, max_length=8)
    rubric: list[str] = Field(min_length=1, max_length=6)
    explanation: str = Field(min_length=1, max_length=1200)
    points: int = Field(default=20, ge=1, le=100)


class UnitAssessment(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    questions: list[AssessmentQuestion] = Field(min_length=3, max_length=8)


class OpenGrade(BaseModel):
    question_id: str = Field(min_length=1, max_length=40)
    earned: int = Field(ge=0, le=100)
    reason: str = Field(min_length=1, max_length=1000)
    weaknesses: list[str] = Field(default_factory=list, max_length=5)


class AssessmentFeedback(BaseModel):
    grades: list[OpenGrade] = Field(default_factory=list, max_length=8)
    advice: str = Field(min_length=1, max_length=2000)
