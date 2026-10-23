"""Fixed role-owned candidate interfaces over native PydanticAI execution."""
from __future__ import annotations

from .contracts import (
    AgentDeps, IntakeInput, RoutePlanningInput, CoveragePlanningInput, ReviewInput,
    OutlinePlanningInput, LessonPlanningInput, TeachingStepInput, FollowupInput,
    AssessmentInput, EvaluationInput, DialogueInput, RevisionInput, CoverInput,
    ChatInput, FeynmanInput, UnitScopeInput, UnitScopeReviewInput, UnitScopeCoverageInput,
)
from .runner import _run_sync, execute_agent


INPUT_TYPES = {
    "submit_learning_interview": IntakeInput,
    "submit_route_candidate": RoutePlanningInput,
    "submit_coverage_plan": CoveragePlanningInput,
    "submit_unit_scope": UnitScopeInput,
    "submit_unit_scope_review": UnitScopeReviewInput,
    "submit_unit_scope_coverage": UnitScopeCoverageInput,
    "submit_quality_review": ReviewInput,
    "submit_outline_candidate": OutlinePlanningInput,
    "submit_lesson_blueprint": LessonPlanningInput,
    "submit_lesson_steps": TeachingStepInput,
    "submit_followup": FollowupInput,
    "submit_unit_assessment": AssessmentInput,
    "submit_assessment_feedback": EvaluationInput,
    "submit_feynman_question": FeynmanInput,
    "submit_feynman_evaluation": FeynmanInput,
    "submit_planning_dialogue": DialogueInput,
    "submit_plan_revision": RevisionInput,
    "submit_cover": CoverInput,
    "submit_chat": ChatInput,
}


class _Role:
    name: str

    def _execute(self, source, input, deps, output, tool_name, **options):
        from .runner import deps_for_source
        if type(input) is not INPUT_TYPES[tool_name]:
            raise TypeError("role-specific typed input required")
        if not isinstance(deps, AgentDeps):
            raise TypeError("trusted AgentDeps required")
        deps = deps_for_source(source, deps, options.pop("max_requests", None))
        return _run_sync(execute_agent(source, output, input, deps,
            tool_name=tool_name, agent_name=self.name, **options))


class IntakeAgent(_Role):
    name = "learning_intake_agent"

    def interview(self, source, input: IntakeInput, deps: AgentDeps, **options):
        from ..planning.outputs import IntakeQuestionSet
        return self._execute(source, input, deps, IntakeQuestionSet, "submit_learning_interview", **options)


class PlanningAgent(_Role):
    name = "course_planning_agent"

    def generate_route(self, source, input: RoutePlanningInput, deps: AgentDeps, **options):
        from ..planning.outputs import RouteCandidate
        return self._execute(source, input, deps, RouteCandidate, "submit_route_candidate", **options)

    def plan_coverage(self, source, input: CoveragePlanningInput, deps: AgentDeps, **options):
        from ..planning.outputs import CoveragePlan
        return self._execute(source, input, deps, CoveragePlan, "submit_coverage_plan", **options)

    def review_route(self, source, input: ReviewInput, deps: AgentDeps, **options):
        from ..planning.outputs import QualityReview
        return self._execute(source, input, deps, QualityReview, "submit_quality_review", **options)

    def repair_route(self, source, input: RoutePlanningInput, deps: AgentDeps, **options):
        from ..planning.outputs import RouteCandidate
        return self._execute(source, input, deps, RouteCandidate, "submit_route_candidate", **options)

    def generate_outline(self, source, input: OutlinePlanningInput, deps: AgentDeps, **options):
        from ..planning.outputs import OutlineCandidate
        return self._execute(source, input, deps, OutlineCandidate, "submit_outline_candidate", **options)

    def refine_outline(self, source, input: OutlinePlanningInput, deps: AgentDeps, **options):
        return self.generate_outline(source, input, deps, **options)


class UnitScopeAgent(_Role):
    name = "unit_scope_agent"

    def plan_scope(self, source, input: UnitScopeInput, deps: AgentDeps, **options):
        from ..teaching.unit_scope import UnitScope
        return self._execute(source, input, deps, UnitScope, "submit_unit_scope", **options)


class UnitScopeReviewAgent(_Role):
    name = "unit_scope_review_agent"

    def review_scope(self, source, input: UnitScopeReviewInput, deps: AgentDeps, **options):
        from ..teaching.unit_scope import UnitScopeReview
        return self._execute(source, input, deps, UnitScopeReview, "submit_unit_scope_review", **options)


class UnitScopeCoverageAgent(_Role):
    name = "unit_scope_coverage_agent"

    def plan_coverage(self, source, input: UnitScopeCoverageInput, deps: AgentDeps, **options):
        from ..teaching.unit_scope import ScopeCoveragePlan
        return self._execute(source, input, deps, ScopeCoveragePlan, "submit_unit_scope_coverage", **options)


class LessonPlanningAgent(_Role):
    name = "lesson_planning_agent"

    def plan_lesson(self, source, input: LessonPlanningInput, deps: AgentDeps, **options):
        from ..teaching.learning_outputs import LessonBlueprint
        return self._execute(source, input, deps, LessonBlueprint, "submit_lesson_blueprint", **options)


class TeachingAgent(_Role):
    name = "teaching_agent"

    def generate_step(self, source, input: TeachingStepInput, deps: AgentDeps, **options):
        from ..teaching.outputs import LessonPlanCandidate
        return self._execute(source, input, deps, LessonPlanCandidate, "submit_lesson_steps", **options)

    def answer_followup(self, source, input: FollowupInput, deps: AgentDeps, **options):
        from ..teaching.outputs import FollowupCandidate
        return self._execute(source, input, deps, FollowupCandidate, "submit_followup", **options)


class AssessmentAgent(_Role):
    name = "unit_assessment_agent"

    def generate_assessment(self, source, input: AssessmentInput, deps: AgentDeps, *, assessment_version=1, **options):
        from ..teaching.learning_outputs import UnitAssessment, LessonAssessment
        return self._execute(source, input, deps,
            LessonAssessment if assessment_version == 2 else UnitAssessment, "submit_unit_assessment", **options)


class EvaluationAgent(_Role):
    name = "answer_evaluation_agent"

    def evaluate_answers(self, source, input: EvaluationInput, deps: AgentDeps, *, assessment_version=1, **options):
        from ..teaching.learning_outputs import AssessmentFeedback, AssessmentFeedbackV2
        return self._execute(source, input, deps,
            AssessmentFeedbackV2 if assessment_version == 2 else AssessmentFeedback,
            "submit_assessment_feedback", **options)


class FeynmanAgent(_Role):
    name = "feynman_dialogue_agent"

    def question(self, source, input: FeynmanInput, deps: AgentDeps, **options):
        from ..teaching.feynman_outputs import FeynmanQuestion
        return self._execute(source, input, deps, FeynmanQuestion, "submit_feynman_question", **options)

    def evaluate(self, source, input: FeynmanInput, deps: AgentDeps, *, protocol_version=1, **options):
        from ..teaching.feynman_outputs import FeynmanEvaluation
        from ..teaching.feynman_protocol import LessonFeynmanEvaluation
        output = LessonFeynmanEvaluation if protocol_version == 2 else FeynmanEvaluation
        return self._execute(source, input, deps, output, "submit_feynman_evaluation", **options)


class DialogueAgent(_Role):
    name = "planning_dialogue_agent"

    def answer(self, source, input: DialogueInput, deps: AgentDeps, **options):
        from ..planning.features import DialogueAnswer
        return self._execute(source, input, deps, DialogueAnswer, "submit_planning_dialogue", **options)


class RevisionAgent(_Role):
    name = "planning_revision_agent"

    def revise(self, source, input: RevisionInput, deps: AgentDeps, **options):
        from ..planning.features import RevisionAnswer
        return self._execute(source, input, deps, RevisionAnswer, "submit_plan_revision", **options)


class CoverAgent(_Role):
    name = "cover_agent"

    def propose(self, source, input: CoverInput, deps: AgentDeps, **options):
        from ..planning.features import CoverAnswer
        return self._execute(source, input, deps, CoverAnswer, "submit_cover", **options)


class ChatAgent(_Role):
    name = "chat_agent"

    def answer(self, source, input: ChatInput, deps: AgentDeps, **options):
        return self._execute(source, input, deps, str, "submit_chat", **options)


planning_agent = PlanningAgent()
unit_scope_agent = UnitScopeAgent()
unit_scope_review_agent = UnitScopeReviewAgent()
unit_scope_coverage_agent = UnitScopeCoverageAgent()
intake_agent = IntakeAgent()
lesson_planning_agent = LessonPlanningAgent()
teaching_agent = TeachingAgent()
assessment_agent = AssessmentAgent()
evaluation_agent = EvaluationAgent()
feynman_agent = FeynmanAgent()
dialogue_agent = DialogueAgent()
revision_agent = RevisionAgent()
cover_agent = CoverAgent()
chat_agent = ChatAgent()
