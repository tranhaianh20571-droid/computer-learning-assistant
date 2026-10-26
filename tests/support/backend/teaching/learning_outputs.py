from typing import Literal
from pydantic import BaseModel, Field, model_validator
from pydantic_ai.exceptions import ModelRetry
from .assessment_contract import AssessmentFigure, LessonQuota, NumericAnswer
from .graphics import DrawingSpec, QuestionGraphic, circuit_table

Phase = Literal["intuition", "concept", "example", "misconception", "summary"]


class KnowledgeGoal(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=200)
    scope_point_ids: list[str] = Field(default_factory=list, max_length=120)
    scope_capability_ids: list[str] = Field(default_factory=list, max_length=240)


class PlannedSegment(BaseModel):
    title: str = Field(default="",max_length=80)
    purpose: str = Field(min_length=1,max_length=240)
    knowledge_point_id: str = Field(default="", max_length=40, pattern=r"^[A-Za-z0-9_-]*$")
    knowledge_point_label: str = Field(default="", max_length=120)
    knowledge_point_end: bool = False
    scope_point_ids: list[str] = Field(default_factory=list, max_length=120)
    scope_capability_ids: list[str] = Field(default_factory=list, max_length=240)


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
    knowledge_point_id: str = ""
    knowledge_point_label: str = ""
    knowledge_point_end: bool = False
    scope_point_ids: list[str] = Field(default_factory=list, max_length=120)
    scope_capability_ids: list[str] = Field(default_factory=list, max_length=240)


def teaching_schedule(blueprint):
    result=[]
    for stage_no,stage in enumerate(blueprint.stages,1):
        segments=stage.segments or [PlannedSegment(purpose=stage.purpose[:240])]
        for number,segment in enumerate(segments,1):
            result.append(ScheduledSegment(phase=stage.phase,goal_ids=stage.goal_ids,
                purpose=f"阶段目标：{stage.purpose}\n当前短段：{segment.purpose}",title=segment.title,
                stage_no=stage_no,stage_segment_no=number,stage_segment_count=len(segments),
                knowledge_point_id=segment.knowledge_point_id,knowledge_point_label=segment.knowledge_point_label,
                knowledge_point_end=segment.knowledge_point_end,
                scope_point_ids=segment.scope_point_ids,
                scope_capability_ids=segment.scope_capability_ids))
    return result


class ReadingStage(TeachingStage):
    segments: list[PlannedSegment] = Field(default_factory=list, max_length=240)


class LessonBlueprint(BaseModel):
    kind: Literal["standard", "source_reading"] = "standard"
    goals: list[KnowledgeGoal] = Field(min_length=1, max_length=4)
    stages: list[ReadingStage | TeachingStage] = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def mode_bounds(self):
        if self.kind == "standard" and (not 4 <= len(self.stages) <= 12
                or any(len(stage.segments) > 4 for stage in self.stages)):
            raise ValueError("standard lesson requires 4-12 stages and at most four segments per stage")
        if self.kind == "source_reading" and len(teaching_schedule(self)) > 240:
            raise ValueError("source reading plan exceeds 240 playback segments")
        return self


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
    points={}
    for number, segment in enumerate(teaching_schedule(output),1):
        if segment.knowledge_point_end and (not segment.knowledge_point_id or not segment.knowledge_point_label):
            raise ModelRetry("知识点结束标记必须有稳定知识点ID和清楚名称。")
        if segment.knowledge_point_id:
            points.setdefault(segment.knowledge_point_id,[]).append((number,segment))
    for point in points.values():
        endings=[part for _,part in point if part.knowledge_point_end]
        if (len(endings)!=1 or not point[-1][1].knowledge_point_end
            or endings[0].phase not in ("example","misconception")
            or not {"intuition","concept","example"}<={part.phase for _,part in point}):
            raise ModelRetry("每个标注知识点须覆盖直觉、概念与应用，并且只在其最后应用或误区短段结束一次。")
    return output


def validate_new_blueprint(ctx, output):
    """New generation is strict; saved legacy plans remain readable."""
    if output.kind == "source_reading":
        if not isinstance(ctx, dict) or ctx.get("teaching_mode") != "source_reading":
            raise ModelRetry("论文计划只用于绑定原文的精读讲次。")
        requirements = {r["capability_id"] for r in ctx.get("scope_capabilities", [])}
        assigned = {ident for part in teaching_schedule(output) for ident in part.scope_capability_ids}
        if requirements != assigned or output.stages[-1].phase != "summary":
            raise ModelRetry("精读计划必须承接全部考纲能力并以论文回顾结束。")
        return output
    validate_blueprint(ctx, output)
    for stage in output.stages:
        minimum = 2 if stage.phase in ("intuition", "concept", "example") else 1
        purposes = [segment.purpose.strip() for segment in stage.segments]
        if len(purposes) < minimum or len(set(purposes)) != len(purposes):
            raise ModelRetry("直觉、概念、应用阶段各至少两个不同目的的短段，其它阶段至少一个；必须提供segments。")
    validation_context = ctx if isinstance(ctx, dict) else {}
    scope_capabilities = validation_context.get("scope_capabilities", [])
    requirements = {item["capability_id"] for item in scope_capabilities}
    scope_points = {item["point_id"] for item in scope_capabilities}
    if requirements:
        for segment in teaching_schedule(output):
            if segment.phase != "summary" and (
                    not segment.knowledge_point_id or not segment.knowledge_point_label):
                raise ModelRetry("接入考纲的每个教学短段都必须归属一个显式课堂知识点分组。")
        goal_ids = [ident for goal in output.goals for ident in goal.scope_capability_ids]
        if set(goal_ids) != requirements or len(goal_ids) != len(set(goal_ids)):
            raise ModelRetry("蓝图目标必须将全部考纲能力各自归属一次，不能遗漏或重复。")
        goal_point_ids = [ident for goal in output.goals for ident in goal.scope_point_ids]
        if set(goal_point_ids) != scope_points or len(goal_point_ids) != len(set(goal_point_ids)):
            raise ModelRetry("蓝图目标必须将全部考纲 point_id 各自归属一次，不能遗漏或重复。")
        capability_points = {item["capability_id"]: item["point_id"] for item in scope_capabilities}
        phase_coverage = {ident: set() for ident in requirements}
        for stage in output.stages:
            associated_goals = [goal for goal in output.goals
                if goal.id in stage.goal_ids]
            goal_scope = set().union(*(set(goal.scope_capability_ids)
                for goal in associated_goals))
            goal_points = set().union(*(set(goal.scope_point_ids)
                for goal in associated_goals))
            for segment in stage.segments:
                assigned = set(segment.scope_capability_ids)
                assigned_points = set(segment.scope_point_ids)
                actual_points = {capability_points[ident] for ident in assigned
                    if ident in capability_points}
                if (not assigned <= requirements or not assigned <= goal_scope
                        or not assigned_points <= scope_points or not assigned_points <= goal_points
                        or actual_points != assigned_points):
                    raise ModelRetry("短段必须引用阶段目标所承接的真实考纲点，并完整关联其能力 ID。")
                for ident in assigned:
                    phase_coverage[ident].add(stage.phase)
        if any(not {"intuition", "concept", "example"} <= phases
               for phases in phase_coverage.values()):
            raise ModelRetry("每项考纲能力均须安排直觉、概念与应用讲解。")
    elif any(goal.scope_point_ids or goal.scope_capability_ids for goal in output.goals) or any(
            segment.scope_point_ids or segment.scope_capability_ids
            for stage in output.stages for segment in stage.segments):
        raise ModelRetry("无冻结考纲时不能伪造考纲能力引用。")
    return output


class AssessmentOption(BaseModel):
    id: str = Field(min_length=1, max_length=10)
    text: str = Field(min_length=1, max_length=600)


class AssessmentQuestion(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    kind: Literal["single", "multiple", "short", "code_reading", "drawing"]
    prompt: str = Field(min_length=1, max_length=1600)
    options: list[AssessmentOption] = Field(default_factory=list, max_length=6)
    lesson_ids: list[str] = Field(min_length=1, max_length=5)
    goal: str = Field(min_length=1, max_length=300)
    answer: list[str] = Field(min_length=1, max_length=8)
    rubric: list[str] = Field(min_length=1, max_length=6)
    explanation: str = Field(min_length=1, max_length=1200)
    points: int = Field(default=20, ge=1, le=100)
    source_question_id: str = Field(default="", max_length=160)
    source_use: Literal["original", "adapted", "generated"] = "generated"
    example_reference_id: str = Field(default="", max_length=160)
    variation_reason: str = Field(default="", max_length=600)
    primary_lesson_id: str = Field(default="", max_length=64)
    cognitive_action: str = Field(default="", max_length=80)
    construction_method: str = Field(default="", max_length=80)
    knowledge_point_ids: list[str] = Field(default_factory=list, max_length=240)
    figure: AssessmentFigure = Field(default_factory=AssessmentFigure)
    numeric_answer: NumericAnswer | None = None
    drawing: DrawingSpec | None = None
    solution: QuestionGraphic | None = None

    @model_validator(mode="after")
    def drawing_reference(self):
        if self.kind != "drawing":
            if self.drawing is not None or self.solution is not None:
                raise ValueError("drawing fields only belong to drawing questions")
            return self
        if not self.drawing or not self.solution or self.drawing.kind != self.solution.kind or self.options:
            raise ValueError("drawing requires a matching private solution and no options")
        if self.drawing.kind == "circuit":
            if (self.solution.inputs != self.drawing.inputs or self.solution.outputs != self.drawing.outputs
                    or any(g.type not in self.drawing.allowed_gates for g in self.solution.gates)):
                raise ValueError("solution must follow the drawing constraints")
            circuit_table(self.solution)
            if self.drawing.starter:
                try:
                    starter_table = circuit_table(self.drawing.starter)
                except ValueError:
                    starter_table = None
                if starter_table == circuit_table(self.solution):
                    raise ValueError("starter must not provide the completed reference circuit")
        elif (self.solution.bounds != self.drawing.bounds
              or not (self.solution.curves or self.solution.points)
              or self.solution.points and not self.drawing.allow_points
              or self.solution.curves and not (self.drawing.allow_expression or self.drawing.allow_sketch)):
            raise ValueError("plot solution must match drawable bounds and tools")
        if self.solution.kind == "plot":
            self.solution.validate_visible()
            if self.drawing.starter and self.drawing.starter == self.solution:
                raise ValueError("starter must not provide the completed reference plot")
        return self


class SourceRejection(BaseModel):
    question_id: str = Field(min_length=1, max_length=160)
    reason: Literal["not_taught", "missing_context", "unsuitable_feynman", "duplicate", "too_long",
                    "too_shallow", "not_fit_goal"]
    explanation: str = Field(min_length=8, max_length=240)


class UnitAssessment(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    questions: list[AssessmentQuestion] = Field(min_length=3, max_length=8)
    source_rejections: list[SourceRejection] = Field(default_factory=list, max_length=40)


class UnitAssessmentV2(UnitAssessment):
    assessment_version: Literal[2] = 2
    lesson_quotas: list[LessonQuota] = Field(min_length=1)
    questions: list[AssessmentQuestion] = Field(min_length=5)
    source_rejections: list[SourceRejection] = Field(default_factory=list)


class LessonAssessmentQuestion(AssessmentQuestion):
    primary_lesson_id: str = Field(min_length=1, max_length=64)


class LessonAssessment(UnitAssessmentV2):
    lesson_quotas: list[LessonQuota] = Field(min_length=1, max_length=1)
    questions: list[LessonAssessmentQuestion] = Field(min_length=5, max_length=12)
    source_rejections: list[SourceRejection] = Field(default_factory=list, max_length=40)


class OpenGrade(BaseModel):
    question_id: str = Field(min_length=1, max_length=40)
    earned: int = Field(ge=0, le=100)
    reason: str = Field(min_length=1, max_length=1000)
    weaknesses: list[str] = Field(default_factory=list, max_length=5)


class AssessmentFeedback(BaseModel):
    grades: list[OpenGrade] = Field(default_factory=list, max_length=8)
    advice: str = Field(min_length=1, max_length=2000)


class AssessmentFeedbackV2(AssessmentFeedback):
    grades: list[OpenGrade] = Field(min_length=1, max_length=12)
