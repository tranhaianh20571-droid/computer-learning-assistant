"""Build real lesson context and run an independent, version-bound planning Agent."""
import json
import uuid
import hashlib
from pydantic_ai.exceptions import ModelRetry
from sqlalchemy import select
from ..agents.roles import lesson_planning_agent
from ..agents.contracts import LessonPlanningInput
from ..agents.runner import deps_for_source
from ..planning.models import (CourseRow, CourseUnitRow, LessonRow, LessonContentVersionRow,
    PlanningTaskStateRow, RouteVersionRow, utcnow, load_json_field)
from ..planning.errors import error
from .learning_models import LessonBlueprintRow, PreparedLessonPlanRow
from .learning_outputs import LessonBlueprint, validate_new_blueprint
from .models import TeachingStepRow


def lesson_context(service, conv):
    with service.db.session() as sess:
        course = sess.get(CourseRow, conv.course_id)
        lesson = sess.get(LessonRow, conv.lesson_id)
        unit = sess.get(CourseUnitRow, lesson.unit_id) if lesson else None
        if (not course or course.deleted_at or course.owner_user_id != conv.owner_user_id
            or not lesson or lesson.outline_version_id != conv.outline_version_id or not unit):
            raise error("course_not_found", status=404)
        targets = [course.course_id]
        source_route = sess.get(RouteVersionRow, course.source_route_version_id) if course.source_route_version_id else None
        if source_route: targets.append(source_route.route_id)
        state = sess.execute(select(PlanningTaskStateRow).where(
            PlanningTaskStateRow.owner_user_id == conv.owner_user_id,
            PlanningTaskStateRow.target_id.in_(targets)).order_by(PlanningTaskStateRow.updated_at.desc())).scalars().first()
        interview=load_json_field(state.intake_json,{}) if state else {}
        context = {"course_title": course.title_ref, "unit_title": unit.title_ref,
            "lesson_id": lesson.lesson_id, "lesson_title": lesson.title_ref or lesson.goal_ref,
            "lesson_goal": lesson.goal_ref, "outline_version_id": conv.outline_version_id,
            "learner_profile": {"profile":interview.get("learner_profile",{}),"answers":interview.get("answers",[]),
                "assumptions":interview.get("assumptions",[])},
            "material_ids": load_json_field(state.material_ids_json, []) if state else []}
    return context


def plan_lesson(service, conv, task_id, adapter):
    with service.db.session() as sess:
        row = sess.get(LessonBlueprintRow, conv.conversation_id)
        context = load_json_field(row.context_json, {}) if row else None
        if context:
            context = {key: value for key, value in context.items() if not key.startswith("_")}
        if row and load_json_field(row.plan_json, {}).get("stages"):
            return LessonBlueprint.model_validate_json(row.plan_json), context
    context = context or lesson_context(service, conv)
    # A frozen, prepared blueprint is copied into the conversation. Cold start
    # uses a valid local outline and never waits for a second model call.
    return prepared_plan(service, conv, context), context


def lightweight_plan(context):
    return LessonBlueprint.model_validate({"goals": [{"id": "g1", "label": context["lesson_goal"][:200]}],
        "stages": [{"phase": phase, "goal_ids": ["g1"], "purpose": purpose,
            "segments":[{"purpose":part} for part in {
                "intuition":["从具体熟悉的情境建立直觉，不立即堆砌所有概念", "说明类比对应关系与边界，连接即将解释的概念"],
                "concept":["给出准确概念和关键机制，承接刚才的直觉", "解释条件与边界，澄清容易混淆的关系"],
                "example":["逐步完成一个最小应用，解释每个操作的原因", "换一个细节验证同一知识目标，避免重讲前面的完整例子"],
                "summary":["用简洁关系回顾已覆盖的目标与应用条件"]}[phase]]} for phase, purpose in (
            ("intuition", "用学习者熟悉的具体事物建立目标知识的直觉，说明对应关系和类比边界"),
            ("concept", "连接直觉解释本讲目标的定义、机制和适用条件"),
            ("example", "用具体应用验证概念，按步骤解释原因"),
            ("summary", "回顾本讲目标、关键关系与类比边界"))]})


def plan_key(service, conv, context):
    template = service.prompts.load_local_template("lesson_blueprint_v1")
    with service.db.session() as sess:
        version = sess.scalars(select(LessonContentVersionRow).where(
            LessonContentVersionRow.lesson_id == conv.lesson_id).order_by(
            LessonContentVersionRow.content_version_no.desc())).first()
        content_version = version.content_version_no if version else 1
    model = service.preparation._model(conv.owner_user_id)
    data = {"owner": conv.owner_user_id, "course": conv.course_id, "context": context,
            "template": template["sha256"], "content_version": content_version,
            "model": [model["config_id"], model["config_version"]] if model else None,
            "code_version": "presentation-3"}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def prepared_plan(service, conv, context):
    key = plan_key(service, conv, context)
    with service.db.session() as sess:
        row = sess.get(PreparedLessonPlanRow, key)
        if (row and row.owner_user_id == conv.owner_user_id
            and row.course_id == conv.course_id and row.lesson_id == conv.lesson_id):
            try:
                return validate_new_blueprint(None, LessonBlueprint.model_validate_json(row.plan_json))
            except (ValueError, ModelRetry):
                pass
    return lightweight_plan(context)


def refine_plan(service, conv, task_id, adapter, context):
    prompt = service.prompts.resolve_for_task(task_id, "lesson_blueprint_v1")["template_snapshot"]
    def get_lesson_context() -> dict:
        """Read only this version-bound lesson's learning goal and learner profile."""
        return context
    tools = []
    if context["material_ids"]:
        from ..agents.tools import make_search_materials_tool
        tools.append(make_search_materials_tool(service.retrieval, owner_user_id=conv.owner_user_id,
                                                material_ids=context["material_ids"]))
    run = lesson_planning_agent.plan_lesson(adapter, LessonPlanningInput.from_messages(
        [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]),
        deps_for_source(adapter,max_requests=3), tools=[get_lesson_context,*tools], output_validator=validate_new_blueprint)
    return run.payload, context


def previous_teaching(service, conv_id, step_no):
    with service.db.session() as sess:
        steps = list(sess.scalars(select(TeachingStepRow).where(TeachingStepRow.conversation_id == conv_id,
            TeachingStepRow.generation == 1, TeachingStepRow.step_no < step_no).order_by(TeachingStepRow.step_no)))
        return [{"step_no": s.step_no, "phase": s.phase, "title": s.title,
                 "summary": s.text[:300]} for s in steps]
