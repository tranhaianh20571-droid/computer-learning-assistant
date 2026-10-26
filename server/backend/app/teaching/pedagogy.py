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
    PlanningTaskStateRow, RouteVersionRow, OutlineVersionRow, utcnow, load_json_field)
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
        from .examples import EXAMPLE_POLICY_VERSION
        context["example_policy_version"] = EXAMPLE_POLICY_VERSION
        outline = sess.get(OutlineVersionRow, conv.outline_version_id)
        snapshot = load_json_field(outline.source_snapshot_json, {}) if outline else {}
        if snapshot:
            from ..materials.study_sources import verify_sources, chapter_catalog
            docs = verify_sources(sess, conv.owner_user_id, snapshot)
            context["material_ids"] = snapshot["material_ids"]
            context["source_snapshot"] = snapshot
            context["teaching_mode"] = ("source_reading" if lesson.lesson_id in
                snapshot.get("reading_lessons", []) else "standard")
            context["source_supplement_reason"] = snapshot.get("supplements", {}).get(lesson.lesson_id, "")
            ids = snapshot.get("lessons", {}).get(lesson.lesson_id, [])
            context["source_sections"] = [s for s in chapter_catalog(docs) if s["id"] in ids]
        from .unit_scope import UnitScopeRow, scope_capabilities
        from ..tasks.service import LearningTaskRow
        from ..materials.study_sources import digest
        scope_row = sess.scalar(select(UnitScopeRow).join(
            LearningTaskRow, UnitScopeRow.task_id == LearningTaskRow.task_id).where(
                UnitScopeRow.owner_user_id == conv.owner_user_id,
                UnitScopeRow.unit_id == unit.unit_id,
                UnitScopeRow.outline_version_id == conv.outline_version_id,
                LearningTaskRow.status == "succeeded").order_by(UnitScopeRow.created_at.desc()))
        if scope_row:
            scope_inputs = service.assessments.scopes.context(sess, course, unit, conv.owner_user_id)
            if digest(scope_inputs) == scope_row.input_hash:
                saved = load_json_field(scope_row.scope_json, {})
                scope = saved.get("scope", saved)
                capabilities = scope_capabilities(scope, scope_row.scope_hash, lesson.lesson_id)
                context["unit_scope_hash"] = scope_row.scope_hash
                context["scope_capabilities"] = capabilities
    return context


def plan_lesson(service, conv, task_id, adapter):
    with service.db.session() as sess:
        row = sess.get(LessonBlueprintRow, conv.conversation_id)
        context = load_json_field(row.context_json, {}) if row else None
        if context:
            context = {key: value for key, value in context.items() if not key.startswith("_")}
        if row and load_json_field(row.plan_json, {}).get("stages"):
            return LessonBlueprint.model_validate_json(row.plan_json), context
        if row:
            context = None
    context = context or lesson_context(service, conv)
    # A frozen, prepared blueprint is copied into the conversation. Cold start
    # uses a valid local outline and never waits for a second model call.
    return prepared_plan(service, conv, context), context


def lightweight_plan(context):
    requirements = context.get("scope_capabilities", [])
    if requirements:
        from collections import OrderedDict
        groups = OrderedDict()
        for item in requirements:
            group = groups.setdefault(item["point_id"], {"label": item["point_label"], "ids": []})
            group["ids"].append(item["capability_id"])
        points = list(groups.items())
        goals = [{"id": f"scope_goal_{index + 1}",
            "label": context["lesson_goal"][:200],
            "scope_point_ids": [],
            "scope_capability_ids": []} for index in range(4)]
        for index, (point_id, point) in enumerate(points):
            goal_index = min(3, index * 4 // len(points))
            if len(goals[goal_index]["scope_capability_ids"]) + len(point["ids"]) > 240:
                goal_index = next((candidate for candidate, goal in enumerate(goals)
                    if len(goal["scope_capability_ids"]) + len(point["ids"]) <= 240), -1)
            if goal_index < 0:
                raise error("ASSESSMENT_NOT_READY",
                    "单讲考纲能力超过蓝图结构上限，请在课程规划中拆分讲次；能力不会被省略。",
                    status=409)
            point["goal_id"] = goals[goal_index]["id"]
            point["goal_index"] = goal_index
            goals[goal_index]["scope_point_ids"].append(point_id)
            goals[goal_index]["scope_capability_ids"].extend(point["ids"])
        goals = [goal for goal in goals if goal["scope_capability_ids"]]
        goal_ids = [goal["id"] for goal in goals]
        chunk_size = max(1, (len(points) + 3) // 4)
        chunks = [points[index:index + chunk_size] for index in range(0, len(points), chunk_size)]
        if len(chunks) == 1:
            chunks.append(chunks[0])
        phases = (
            ("intuition", "先建立每个考点的直觉，再连接已有知识"),
            ("concept", "解释各考点定义、方法与适用条件"),
            ("example", "通过不同例子应用并检查各考点边界"),
        )
        stages = []
        for phase, stage_purpose in phases:
            segments = []
            for index, chunk in enumerate(chunks):
                point_ids = [point_id for point_id, _ in chunk]
                labels = [point["label"] for _, point in chunk]
                capability_ids = [ident for _, point in chunk for ident in point["ids"]]
                segment_id = "sc_" + hashlib.sha256(
                    "|".join(point_ids).encode()).hexdigest()[:24]
                segments.append({"purpose": "、".join(labels)[:80] + "：" +
                    stage_purpose + ("；另作核验" if len(chunks) == 2 and index == 1 else ""),
                    "knowledge_point_id": segment_id,
                    "knowledge_point_label": "、".join(labels)[:120],
                    "scope_point_ids": point_ids,
                    "knowledge_point_end": phase == "example" and
                        not any(point_ids == [ident for ident, _ in later]
                                for later in chunks[index + 1:]),
                    "scope_capability_ids": capability_ids})
            stages.append({"phase": phase, "goal_ids": goal_ids,
                "purpose": stage_purpose, "segments": segments})
        stages.append({"phase": "summary", "goal_ids": goal_ids,
            "purpose": "回顾本讲全部考纲能力及其适用条件",
            "segments": [{"purpose": "逐项总结已讲能力、方法与边界"}]})
        return LessonBlueprint.model_validate({"goals": goals, "stages": stages})
    scope_ids = []
    return LessonBlueprint.model_validate({"goals": [{"id": "g1", "label": context["lesson_goal"][:200],
        "scope_capability_ids": scope_ids}],
        "stages": [{"phase": phase, "goal_ids": ["g1"], "purpose": purpose,
            "segments":[{"purpose":part,**({"knowledge_point_id":"g1",
                "knowledge_point_label":context["lesson_goal"][:120],
                "knowledge_point_end":phase=="example" and index==1,
                **({"scope_capability_ids": scope_ids} if scope_ids else {})} if phase!="summary" else {})} for index,part in enumerate({
                "intuition":["从具体熟悉的情境建立直觉，不立即堆砌所有概念", "说明类比对应关系与边界，连接即将解释的概念"],
                "concept":["给出准确概念和关键机制，承接刚才的直觉", "解释条件与边界，澄清容易混淆的关系"],
                "example":["逐步完成一个最小应用，解释每个操作的原因", "换一个细节验证同一知识目标，避免重讲前面的完整例子"],
                "summary":["用简洁关系回顾已覆盖的目标与应用条件"]}[phase])]} for phase, purpose in (
            ("intuition", "用学习者熟悉的具体事物建立目标知识的直觉，说明对应关系和类比边界"),
            ("concept", "连接直觉解释本讲目标的定义、机制和适用条件"),
            ("example", "用具体应用验证概念，按步骤解释原因"),
            ("summary", "回顾本讲目标、关键关系与类比边界"))]})


def plan_key(service, conv, context, *, include_memory=True):
    context = text_context(context)
    template = service.prompts.load_local_template("lesson_blueprint_v1")
    with service.db.session() as sess:
        version = sess.scalars(select(LessonContentVersionRow).where(
            LessonContentVersionRow.lesson_id == conv.lesson_id).order_by(
            LessonContentVersionRow.content_version_no.desc())).first()
        content_version = version.content_version_no if version else 1
    model = service.preparation._model(conv.owner_user_id)
    if getattr(service, "configs", None):
        from ..agents.model_routing import role_config
        model = role_config(service.configs, conv.owner_user_id, "planning")
    data = {"owner": conv.owner_user_id, "course": conv.course_id, "context": context,
            "template": template["sha256"], "content_version": content_version,
            "model": [model["config_id"], model["config_version"]] if model else None,
            "code_version": "presentation-6-source-examples"}
    if include_memory and getattr(service, "memory", None):
        data["memory_revision"] = service.memory.version(conv.owner_user_id)
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def text_context(context):
    """Voice-only changes must not invalidate accepted teaching text."""
    context = {key:value for key,value in context.items() if not key.startswith("_") and key != "source_reading_plan"}
    if context.get("prepared_character"):
        character=context["prepared_character"]
        profile={key:value for key,value in character["profile"].items()
                 if key not in ("voice_id","voice_config_id","voice_selection")}
        context={**context,"prepared_character":{**character,"profile":profile}}
    return context


def prepared_plan(service, conv, context):
    key = plan_key(service, conv, context)
    with service.db.session() as sess:
        row = sess.get(PreparedLessonPlanRow, key)
        if (row and row.owner_user_id == conv.owner_user_id
            and row.course_id == conv.course_id and row.lesson_id == conv.lesson_id):
            try:
                return validate_new_blueprint(context, LessonBlueprint.model_validate_json(row.plan_json))
            except (ValueError, ModelRetry):
                pass
    return lightweight_plan(context)


def refine_plan(service, conv, task_id, adapter, context):
    prompt = service.prompts.resolve_for_task(task_id, "lesson_blueprint_v1")["template_snapshot"]
    from ..materials.study_sources import LEARNING_FOCUS_POLICY
    prompt += "\n" + LEARNING_FOCUS_POLICY
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
        deps_for_source(adapter,max_requests=3), tools=[get_lesson_context,*tools],
        output_validator=lambda _ctx, output: validate_new_blueprint(context, output),
        timeout_seconds=180)
    return run.payload, context


def previous_teaching(service, conv_id, step_no):
    with service.db.session() as sess:
        steps = list(sess.scalars(select(TeachingStepRow).where(TeachingStepRow.conversation_id == conv_id,
            TeachingStepRow.generation == 1, TeachingStepRow.step_no < step_no).order_by(TeachingStepRow.step_no)))
        return [{"step_no": s.step_no, "phase": s.phase, "title": s.title,
                 "summary": s.text[:300]} for s in steps]
