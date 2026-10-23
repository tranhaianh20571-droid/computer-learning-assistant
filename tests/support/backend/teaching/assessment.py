"""Private unit tests based on actual taught content, with fenced grading tasks."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextvars import copy_context
from dataclasses import replace
import json
import re
import threading
import uuid
from hashlib import sha256
from sqlalchemy import select
from pydantic_ai.exceptions import ModelRetry
from ..agents.contracts import AssessmentInput, EvaluationInput
from ..agents.runner import deps_for_source
from ..agents.roles import assessment_agent, evaluation_agent
from ..planning.models import (CourseRow, CourseUnitRow, LessonRow, LessonProgressRow,
    LessonContentVersionRow, ConversationRow, OutlineVersionRow, load_json_field, utcnow)
from ..planning.errors import error
from ..tasks.service import LearningTaskRow
from .models import TeachingStepRow
from .learning_models import (UnitAssessmentRow, AssessmentAttemptRow, LessonBlueprintRow,
                              TaughtKnowledgeRow)
from .learning_outputs import UnitAssessment, UnitAssessmentV2, LessonAssessment, LessonBlueprint, AssessmentFeedbackV2
from .graphics import DrawingSpec, drawing_grade, validate_drawing_answer
from pydantic import BaseModel, TypeAdapter, ValidationError

DRAWING_POLICY = (
    "figure_catalog中content_status=unavailable的资料图只有定位、没有内容依据，"
    "不得凭位置猜图的结构、位数或条件；补齐理解依据或用与题干一致的自含图。"
    "需要画图完成能力证据时使用kind=drawing，不强制凑题型数量。drawing定义kind=plot|circuit及允许工具，"
    "solution提供同kind私有参考解。plot的bounds固定x/y范围，可用表达式、手绘或标点；"
    "题目若要求徒手画形状请设allow_expression=false，allow_sketch=true；只标点可禁用另两种工具。"
    "solution中curves[].expression和points是参考，不得放入drawing.starter或题干泄露待作图的答案。"
    "circuit的drawing给inputs/outputs/allowed_gates，solution给完整gates(id/type/x/y)及wires(source/target/port)。"
    "已有待修电路仅放starter，不能将完整答案当starter。电路最多6个输入，32个门，96根线，参考解无悬空和反馈环。"
    "表达式只用x、+ - * / ^、sin/cos/tan/sqrt/exp/log/abs、pi/e，显式乘法，幂指数为绝对值不超过12的常数。"
    "带图选择/简答题可用figure.kind=generated_figure、graphic提供plot或circuit，figure.task写读图任务。"
    "曲面只在课堂展示，不用于测验。题图不写正确选项、最终结果或解答标注；答案和solution只保存在私有参考字段。"
)

def needs_model_grade(context):
    return any(question.get("kind") not in ("single", "multiple", "drawing")
               for question in context.get("questions", []))


ASSESSMENT_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="unit-assessment")


def question_issues(output, allowed, context=None):
    issues=[];ids=[q.id for q in output.questions]
    if len(ids)!=len(set(ids)):issues.append("题目 ID 重复")
    covered=set()
    for q in output.questions:
        if re.search(r"\\n(?:\\n|```|下列|执行|已知|阅读)", q.prompt):
            issues.append("题干的排版换行被双重转义，请用实际换行；保留代码字符串内部的合法转义")
        if not set(q.lesson_ids)<=allowed:issues.append("题目引用未讲或不属于本单元的讲次")
        covered.update(q.lesson_ids)
        options={o.id for o in q.options}
        if len(options)!=len(q.options):issues.append("选项 ID 重复")
        if q.kind in ("single","multiple"):
            if len(options)<2 or not set(q.answer)<=options:issues.append("客观题答案不属于有效选项")
            if q.kind=="single" and len(q.answer)!=1:issues.append("单选题必须只有一个正确答案")
        elif q.options:issues.append("开放题不应带客观选项")
    if not allowed<=covered:issues.append("未覆盖本单元全部已讲讲次")
    if context is None and not any(q.kind in ("short","code_reading","drawing") for q in output.questions):
        issues.append("至少需要一题开放题")
    if context is not None:
        from .assessment_validation import content_issues
        issues.extend(content_issues(output, context))
    return issues


def objective_grades(questions, answers):
    results=[]
    for q in questions:
        if q["kind"] == "drawing":
            results.append(drawing_grade(q, answers[q["id"]]))
            continue
        if q["kind"] not in ("single","multiple"):continue
        answer=answers.get(q["id"],[])
        right=set(answer)==set(q["answer"])
        results.append({"question_id":q["id"],"earned":q["points"] if right else 0,
            "maximum":q["points"],"reason":"回答正确。" if right else "回答与正确选项不符。",
            "weaknesses":[] if right else [q["goal"]]})
    return results


class AssessmentService:
    def __init__(self, teaching):
        self.teaching=teaching;self.db=teaching.db;self.tasks=teaching.tasks;self.prompts=teaching.prompts
        teaching.assessments = self
        self.active=set();self.lock=threading.Lock()
        from .unit_scope import UnitScopeService
        self.scopes = UnitScopeService(self)

    def _unit(self,sess,course_id,unit_id,owner):
        course=sess.get(CourseRow,course_id)
        unit=sess.get(CourseUnitRow,unit_id)
        if (not course or course.owner_user_id!=owner or course.deleted_at or not unit
            or unit.outline_version_id!=course.current_outline_version_id):raise error("course_not_found",status=404)
        return course,unit

    def _context(self,sess,course,unit,owner):
        from .unit_scope import UnitScopeRow, scope_capabilities
        scope_row = sess.scalar(select(UnitScopeRow).join(
            LearningTaskRow, UnitScopeRow.task_id == LearningTaskRow.task_id).where(
            UnitScopeRow.owner_user_id == owner, UnitScopeRow.unit_id == unit.unit_id,
            UnitScopeRow.outline_version_id == unit.outline_version_id,
            LearningTaskRow.status == "succeeded").order_by(UnitScopeRow.created_at.desc()))
        if scope_row:
            current_inputs = self.scopes.context(sess, course, unit, owner)
            from ..materials.study_sources import digest
            if digest(current_inputs) != scope_row.input_hash or not scope_row.scope_hash:
                raise error("ASSESSMENT_NOT_READY", "单元考纲已失效，请重新准备。", status=409)
            saved = load_json_field(scope_row.scope_json, {})
            saved_scope = saved.get("scope", saved)
            return self._scope_assessment_context(sess, course, unit, owner, scope_row,
                saved_scope, scope_capabilities)
        lessons=list(sess.scalars(select(LessonRow).where(LessonRow.unit_id==unit.unit_id).order_by(LessonRow.order_index)))
        taught=[]
        for lesson in lessons:
            version=sess.execute(select(LessonContentVersionRow).where(LessonContentVersionRow.lesson_id==lesson.lesson_id)
                .order_by(LessonContentVersionRow.content_version_no.desc())).scalars().first()
            progress=sess.execute(select(LessonProgressRow).where(LessonProgressRow.owner_user_id==owner,
                LessonProgressRow.lesson_id==lesson.lesson_id)).scalars().first()
            if (not progress or progress.learning_state!="user_completed" or not version
                or progress.lesson_content_version_id!=version.content_version_id):
                raise error("ASSESSMENT_NOT_READY","请先确认完成本单元全部讲次。",status=409)
            conversations=list(sess.scalars(select(ConversationRow).where(ConversationRow.owner_user_id==owner,
                ConversationRow.lesson_id==lesson.lesson_id,ConversationRow.outline_version_id==unit.outline_version_id,
                ConversationRow.lesson_content_version_id==version.content_version_id,ConversationRow.deleted_at.is_(None))
                .order_by(ConversationRow.created_at.desc())))
            completed=[]
            for conv in conversations:
                steps=list(sess.scalars(select(TeachingStepRow).where(TeachingStepRow.conversation_id==conv.conversation_id,
                    TeachingStepRow.generation==1).order_by(TeachingStepRow.step_no)))
                if any(s.lesson_finished for s in steps):completed=steps;break
            if not completed:raise error("ASSESSMENT_NOT_READY","本单元教学尚未完整生成。",status=409)
            blueprint = sess.get(LessonBlueprintRow, conv.conversation_id)
            plan = LessonBlueprint.model_validate_json(blueprint.plan_json) if blueprint else None
            from ..planning.models import LessonPrerequisiteEdgeRow
            prerequisite = sess.scalar(select(LessonPrerequisiteEdgeRow.edge_id).where(
                LessonPrerequisiteEdgeRow.outline_version_id == unit.outline_version_id,
                LessonPrerequisiteEdgeRow.from_lesson_id == lesson.lesson_id).limit(1))
            taught.append({"lesson_id":lesson.lesson_id,"title":lesson.title_ref or lesson.goal_ref,"goal":lesson.goal_ref,
                "content_version_id":version.content_version_id,
                "knowledge_points": [g.model_dump() for g in plan.goals] if plan else [],
                "is_prerequisite": bool(prerequisite),
                "taught":[{"phase":s.phase,"title":s.title,"text":s.text} for s in completed]})
        if not taught:raise error("ASSESSMENT_NOT_READY",status=409)
        from .assessment_validation import quotas_for
        context = {"assessment_version": 2, "course_title":course.title_ref,"unit_title":unit.title_ref,
                   "lessons":taught, "lesson_quotas": quotas_for(taught)}
        outline = sess.get(OutlineVersionRow, unit.outline_version_id)
        snapshot = load_json_field(outline.source_snapshot_json, {}) if outline else {}
        if snapshot:
            from ..materials.study_sources import source_candidates
            context["source_snapshot"] = snapshot
            from .assessment_sources import figure_catalog
            for lesson in taught:
                lesson["source_candidates"] = source_candidates(sess, owner, snapshot, [lesson], include_examples=True)
                lesson["figure_catalog"] = figure_catalog(sess, owner, snapshot, lesson["lesson_id"])
        self._attach_examples(sess, context, course, owner)
        return context

    def _scope_assessment_context(self, sess, course, unit, owner, scope_row, scope, capability_builder):
        lessons = list(sess.scalars(select(LessonRow).where(
            LessonRow.unit_id == unit.unit_id).order_by(LessonRow.order_index)))
        scoped_lessons = []
        for lesson in lessons:
            version = sess.execute(select(LessonContentVersionRow).where(
                LessonContentVersionRow.lesson_id == lesson.lesson_id).order_by(
                    LessonContentVersionRow.content_version_no.desc())).scalars().first()
            progress = sess.execute(select(LessonProgressRow).where(
                LessonProgressRow.owner_user_id == owner,
                LessonProgressRow.lesson_id == lesson.lesson_id)).scalars().first()
            if (not progress or progress.learning_state != "user_completed" or not version
                    or progress.lesson_content_version_id != version.content_version_id):
                raise error("ASSESSMENT_NOT_READY", "请先完成本单元全部讲次。", status=409)
            capabilities = capability_builder(scope, scope_row.scope_hash, lesson.lesson_id)
            expected = {item["capability_id"] for item in capabilities}
            records = list(sess.scalars(select(TaughtKnowledgeRow).where(
                TaughtKnowledgeRow.owner_user_id == owner,
                TaughtKnowledgeRow.lesson_id == lesson.lesson_id,
                TaughtKnowledgeRow.content_version_id == version.content_version_id,
                TaughtKnowledgeRow.scope_hash == scope_row.scope_hash)))
            covered = {ident for record in records for ident in load_json_field(
                record.scope_capability_ids_json, [])}
            if expected - covered:
                raise error("ASSESSMENT_NOT_READY", "本单元仍有考纲能力尚未完成讲授。", status=409)
            points = [point for point in scope.get("points", [])
                if point.get("lesson_id") == lesson.lesson_id]
            scoped_lessons.append({"lesson_id": lesson.lesson_id,
                "title": lesson.title_ref or lesson.goal_ref, "goal": lesson.goal_ref,
                "content_version_id": version.content_version_id,
                "knowledge_points": [{"id": point["point_id"], "label": point["label"],
                    "capabilities": point["capabilities"], "boundaries": point["boundaries"]}
                    for point in points],
                "scope_capabilities": capabilities, "scope_points": points})
        from .assessment_validation import quotas_for
        context = {"assessment_version": 2, "scope_hash": scope_row.scope_hash,
            "course_title": course.title_ref, "unit_title": unit.title_ref,
            "lessons": scoped_lessons, "lesson_quotas": quotas_for(scoped_lessons)}
        outline = sess.get(OutlineVersionRow, unit.outline_version_id)
        snapshot = load_json_field(outline.source_snapshot_json, {}) if outline else {}
        if snapshot:
            from ..materials.study_sources import source_candidates
            context["source_snapshot"] = snapshot
            from .assessment_sources import figure_catalog
            for lesson in scoped_lessons:
                lesson["source_candidates"] = source_candidates(sess, owner, snapshot, [lesson], include_examples=True)
                lesson["figure_catalog"] = figure_catalog(sess, owner, snapshot, lesson["lesson_id"])
        self._attach_examples(sess, context, course, owner)
        return context

    def _attach_examples(self, sess, context, course, owner):
        from .examples import EXAMPLE_POLICY_VERSION, published_examples, remaining_candidates
        context["example_policy_version"] = EXAMPLE_POLICY_VERSION
        all_examples = []
        for lesson in context["lessons"]:
            examples = published_examples(sess, owner, course.course_id,
                course.current_outline_version_id, lesson["lesson_id"], lesson["content_version_id"])
            lesson["classroom_examples"] = examples
            all_examples.extend(examples)
        context["classroom_examples"] = all_examples
        for lesson in context["lessons"]:
            if context.get("source_snapshot"):
                from ..materials.study_sources import source_candidates
                receipt = {}
                lesson["source_candidates"] = source_candidates(sess, owner, context["source_snapshot"],
                    [lesson], include_examples=True,
                    exclude_ids=[e["source_question_id"] for e in all_examples if e.get("source_question_id")],
                    receipt=receipt)
                lesson["source_retrieval_receipt"] = receipt
            lesson["source_candidates"] = remaining_candidates(lesson.get("source_candidates", []), all_examples)

    def unit_status(self,course_id,unit_id,owner):
        pending=None
        with self.db.session() as sess:
            course,unit=self._unit(sess,course_id,unit_id,owner)
            from .unit_scope import UnitScopeRow
            current_scope = sess.scalar(select(UnitScopeRow).join(
                LearningTaskRow, UnitScopeRow.task_id == LearningTaskRow.task_id).where(
                    UnitScopeRow.owner_user_id == owner, UnitScopeRow.unit_id == unit_id,
                    UnitScopeRow.outline_version_id == unit.outline_version_id,
                    LearningTaskRow.status == "succeeded").order_by(UnitScopeRow.created_at.desc()))
            rows = list(sess.scalars(select(UnitAssessmentRow).where(
                UnitAssessmentRow.owner_user_id==owner, UnitAssessmentRow.unit_id==unit_id)
                .order_by(UnitAssessmentRow.created_at.desc())))
            row = next((candidate for candidate in rows
                if load_json_field(candidate.context_json, {}).get("scope_hash",
                    "") == (current_scope.scope_hash if current_scope else "")), None)
            if row:
                row = self._owned(sess, row.assessment_id, owner)
                result=self._view(sess,row)
                task=sess.get(LearningTaskRow,row.task_id)
                if task.status != "succeeded" and self._recoverable(task):pending=row.task_id
            else:
                try:self._context(sess,course,unit,owner);eligible=True
                except Exception as exc:
                    if getattr(exc,"code","")!="ASSESSMENT_NOT_READY":raise
                    eligible=False
                result={"status":"not_generated","eligible":eligible}
        if pending:self.kick(pending)
        return result

    def generate(self,course_id,unit_id,owner,key):
        with self.db.session() as sess:
            course,unit=self._unit(sess,course_id,unit_id,owner)
            sess.refresh(course,with_for_update=True)
            context=self._context(sess,course,unit,owner)
            # Reopening or double clicking keeps the same valid question version.
            rows = list(sess.scalars(select(UnitAssessmentRow).where(
                UnitAssessmentRow.owner_user_id==owner, UnitAssessmentRow.unit_id==unit_id)
                .order_by(UnitAssessmentRow.created_at.desc())))
            current_scope_hash = context.get("scope_hash", "")
            existing = next((candidate for candidate in rows
                if load_json_field(candidate.context_json, {}).get("scope_hash", "") == current_scope_hash
                and candidate.outline_version_id == unit.outline_version_id), None)
            if existing:
                task_id=existing.task_id;ident=existing.assessment_id
            else:
                task=self.tasks.create_task(owner_user_id=owner,kind="unit_assessment_generate",subject_id=course_id,
                    idempotency_key="assessment:"+sha256(f"{unit_id}:{key}".encode()).hexdigest(),stage="generate",
                    deadline_seconds=max(600, 300 * len(context["lessons"])))
                task_id=task["task_id"];ident="assessment_"+uuid.uuid4().hex[:16]
                sess.add(UnitAssessmentRow(assessment_id=ident,owner_user_id=owner,course_id=course_id,unit_id=unit_id,
                    outline_version_id=unit.outline_version_id,task_id=task_id,context_json=json.dumps(context,ensure_ascii=False)))
                self._budget(sess, task_id, len(context["lessons"]))
            sess.commit()
        self._prepare_source_grant(task_id, owner)
        # Existing work keeps its frozen protocol even when generate is repeated.
        with self.db.session() as sess:
            saved_context = load_json_field(sess.get(UnitAssessmentRow, ident).context_json, {})
        name = ("unit_assessment_v3" if saved_context.get("scope_hash") else
                "unit_assessment_v2" if saved_context.get("assessment_version") == 2 else
                "unit_assessment_v1")
        self._bind(task_id,name);self.kick(task_id)
        return {"assessment_id":ident,"task_id":task_id}

    def _owned(self,sess,ident,owner):
        row=sess.get(UnitAssessmentRow,ident)
        if not row or row.owner_user_id!=owner:raise error("course_not_found",status=404)
        self._unit(sess,row.course_id,row.unit_id,owner)
        context = load_json_field(row.context_json, {})
        from ..materials.study_sources import verify_sources, digest
        verify_sources(sess, owner, context.get("source_snapshot", {}))
        if context.get("scope_hash"):
            from .unit_scope import UnitScopeRow
            current = sess.scalar(select(UnitScopeRow).join(
                LearningTaskRow, UnitScopeRow.task_id == LearningTaskRow.task_id).where(
                    UnitScopeRow.owner_user_id == owner, UnitScopeRow.unit_id == row.unit_id,
                    UnitScopeRow.outline_version_id == row.outline_version_id,
                    UnitScopeRow.scope_hash == context["scope_hash"],
                    LearningTaskRow.status == "succeeded"))
            course, unit = self._unit(sess, row.course_id, row.unit_id, owner)
            if not current or digest(self.scopes.context(sess, course, unit, owner)) != current.input_hash:
                raise error("TASK_CANCELLED", "测验绑定的考纲已失效。", status=409)
        return row

    def _require_scope_coverage(self, sess, assessment, scope_row):
        from .unit_scope import scope_capabilities
        from ..planning.models import LessonRow
        saved = load_json_field(scope_row.scope_json, {})
        scope = saved.get("scope", saved)
        versions = {}
        for lesson in sess.scalars(select(LessonRow).join(
                CourseUnitRow, LessonRow.unit_id == CourseUnitRow.unit_id).where(
                    CourseUnitRow.unit_id == assessment.unit_id)):
            version = sess.execute(select(LessonContentVersionRow).where(
                LessonContentVersionRow.lesson_id == lesson.lesson_id).order_by(
                    LessonContentVersionRow.content_version_no.desc())).scalars().first()
            if version:
                versions[lesson.lesson_id] = version.content_version_id
        records = list(sess.scalars(select(TaughtKnowledgeRow).where(
            TaughtKnowledgeRow.owner_user_id == assessment.owner_user_id,
            TaughtKnowledgeRow.lesson_id.in_(list(versions)),
            TaughtKnowledgeRow.scope_hash == scope_row.scope_hash)))
        covered = {ident for record in records
            if versions.get(record.lesson_id) == record.content_version_id
            for ident in load_json_field(record.scope_capability_ids_json, [])}
        expected = {item["capability_id"] for lesson_id in versions
            for item in scope_capabilities(scope, scope_row.scope_hash, lesson_id)}
        complete_lessons = True
        lessons = list(sess.scalars(select(LessonRow).join(
            CourseUnitRow, LessonRow.unit_id == CourseUnitRow.unit_id).where(
                CourseUnitRow.unit_id == assessment.unit_id)))
        for lesson in lessons:
            version = sess.execute(select(LessonContentVersionRow).where(
                LessonContentVersionRow.lesson_id == lesson.lesson_id).order_by(
                    LessonContentVersionRow.content_version_no.desc())).scalars().first()
            progress = sess.scalar(select(LessonProgressRow).where(
                LessonProgressRow.owner_user_id == assessment.owner_user_id,
                LessonProgressRow.lesson_id == lesson.lesson_id))
            if not (version and progress and progress.learning_state == "user_completed"
                    and progress.lesson_content_version_id == version.content_version_id):
                complete_lessons = False
                break
        return not (expected - covered) and complete_lessons

    def _view(self,sess,row):
        task=sess.get(LearningTaskRow,row.task_id)
        if task.status=="succeeded":
            sess.refresh(row)
        questions=load_json_field(row.questions_json,{})
        context = load_json_field(row.context_json, {})
        eligible = True
        if context.get("scope_hash"):
            from .unit_scope import UnitScopeRow
            scope = sess.scalar(select(UnitScopeRow).join(
                LearningTaskRow, UnitScopeRow.task_id == LearningTaskRow.task_id).where(
                    UnitScopeRow.owner_user_id == row.owner_user_id,
                    UnitScopeRow.unit_id == row.unit_id,
                    UnitScopeRow.outline_version_id == row.outline_version_id,
                    UnitScopeRow.scope_hash == context["scope_hash"],
                    LearningTaskRow.status == "succeeded"))
            eligible = bool(scope and self._require_scope_coverage(sess, row, scope))
        return {"assessment_id":row.assessment_id,"task_id":row.task_id,"status":task.status,
            "title":questions.get("title","单元测验"),"eligible":eligible,"unit_id":row.unit_id,
            "assessment_version": context.get("assessment_version", 1),
            "lesson_quotas": questions.get("lesson_quotas", []),
            "generation_progress": {"completed": (len(questions.get("lesson_quotas", []))
                if task.status == "succeeded" else len(questions.get("_batches", {}))),
                "total": len(load_json_field(row.context_json, {}).get("lessons", []))},
            "questions":[{**{k:q[k] for k in ("id","kind","prompt","options","points")},
                          "source":q.get("source"),"lesson_ids":q.get("lesson_ids",[]),
                          "primary_lesson_id":q.get("primary_lesson_id",""),
                          "figure":q.get("public_figure"),
                          "drawing":q.get("drawing"),
                          "rounding":{k:v for k,v in (q.get("numeric_answer") or {}).items()
                                      if k in ("unit","decimals","notation")}}
                         for q in questions.get("questions",[])]
                         if task.status=="succeeded" and eligible else []}

    def get(self,ident,owner):
        with self.db.session() as sess:
            row=self._owned(sess,ident,owner);view=self._view(sess,row)
            task=sess.get(LearningTaskRow,row.task_id);pending=self._recoverable(task)
            if task.status == "succeeded" and not view["eligible"]:
                view = {**view, "questions": []}
                pending = None
        if pending:self.kick(row.task_id)
        return view

    def submit(self,ident,owner,answers,key):
        answers = {k: v.model_dump() if isinstance(v, BaseModel) else v for k, v in answers.items()}
        with self.db.session() as sess:
            row=self._owned(sess,ident,owner);sess.refresh(row,with_for_update=True)
            if not self._view(sess, row)["eligible"]:
                raise error("ASSESSMENT_NOT_READY", "课程覆盖尚未通过，不能提交测验。", status=409)
            task=sess.get(LearningTaskRow,row.task_id)
            if task.status!="succeeded":raise error("ASSESSMENT_NOT_READY",status=409)
            questions=load_json_field(row.questions_json,{})["questions"]
            requires_model=needs_model_grade({"questions": questions})
            if set(answers)!={q["id"] for q in questions}:raise error("invalid_field","请完成全部题目。")
            for q in questions:
                value=answers[q["id"]]
                if q["kind"] in ("single","multiple"):
                    if (not isinstance(value,list) or not value or len(value)!=len(set(value))
                        or not all(isinstance(v,str) for v in value)
                        or not set(value)<={o["id"] for o in q["options"]}
                        or q["kind"]=="single" and len(value)!=1):raise error("invalid_field")
                elif q["kind"] == "drawing":
                    try:
                        spec = TypeAdapter(DrawingSpec).validate_python(q["drawing"])
                        validate_drawing_answer(value, spec)
                    except (ValueError, ValidationError, KeyError):
                        raise error("invalid_field", "作图答案格式不正确或为空。") from None
                elif not isinstance(value,str) or not value.strip() or len(value)>4000:raise error("invalid_field")
            previous=sess.execute(select(AssessmentAttemptRow).where(AssessmentAttemptRow.owner_user_id==owner,
                AssessmentAttemptRow.assessment_id==ident,AssessmentAttemptRow.idempotency_key==key)).scalars().first()
            if previous:
                if load_json_field(previous.answers_json,{})!=answers:raise error("invalid_field","同次提交的答案已固定。",status=409)
                attempt_id,task_id=previous.attempt_id,previous.task_id
            else:
                created=self.tasks.create_task(owner_user_id=owner,kind="assessment_grade",subject_id=row.course_id,
                    idempotency_key="grade:"+sha256(f"{ident}:{key}".encode()).hexdigest(),stage="generate",
                    deadline_seconds=max(600, 300 * len(load_json_field(row.context_json, {}).get("lessons", []))))
                task_id=created["task_id"];attempt_id="answer_"+uuid.uuid4().hex[:16]
                sess.add(AssessmentAttemptRow(attempt_id=attempt_id,assessment_id=ident,owner_user_id=owner,
                    idempotency_key=key,task_id=task_id,answers_json=json.dumps(answers,ensure_ascii=False)))
                self._budget(sess, task_id, len(load_json_field(row.context_json, {}).get("lessons", [])))
            sess.commit()
        if requires_model:
            self._prepare_source_grant(task_id, owner)
        with self.db.session() as sess:
            saved = load_json_field(sess.get(UnitAssessmentRow, ident).questions_json, {})
        if needs_model_grade(saved):
            self._bind(task_id, "assessment_feedback_v2" if saved.get("assessment_version") == 2
                       else "assessment_feedback_v1")
        self.kick(task_id)
        return {"attempt_id":attempt_id,"task_id":task_id}

    def attempt(self,ident,owner):
        with self.db.session() as sess:
            row=sess.get(AssessmentAttemptRow,ident)
            if not row or row.owner_user_id!=owner:raise error("course_not_found",status=404)
            self._owned(sess,row.assessment_id,owner)
            task=sess.get(LearningTaskRow,row.task_id);pending=self._recoverable(task)
            if task.status=="succeeded":
                sess.refresh(row)
            result={"attempt_id":ident,"assessment_id":row.assessment_id,"task_id":row.task_id,"status":task.status,
                    "grading_progress": {"completed": len(load_json_field(row.feedback_json, {}).get("_batches", {}))},
                    "feedback":load_json_field(row.feedback_json,{}) if task.status=="succeeded" else None}
        if pending:self.kick(row.task_id)
        return result

    def retry(self,task_id,owner):
        with self.db.session() as sess:
            assessment=sess.execute(select(UnitAssessmentRow).where(UnitAssessmentRow.task_id==task_id)).scalars().first()
            attempt=sess.execute(select(AssessmentAttemptRow).where(AssessmentAttemptRow.task_id==task_id)).scalars().first()
            if not assessment and not attempt:raise error("course_not_found",status=404)
            self._owned(sess,assessment.assessment_id if assessment else attempt.assessment_id,owner)
        self.tasks.requeue_for_retry(task_id,actor_id=owner);self.kick(task_id)
        return {"task_id":task_id}

    def _bind(self,task_id,name):
        from ..prompts.service import PromptBindingRow
        with self.db.session() as sess:
            exists=sess.execute(select(PromptBindingRow).where(PromptBindingRow.task_id==task_id,PromptBindingRow.prompt_name==name)).first()
        if not exists:self.prompts.bind_for_task(task_id,name,agent_code_version="4" if name.endswith("_v2") else "3",
            tools_version="3", output_schema_version="assessment-2" if name.endswith("_v2") else "assessment-1")
        else:self.prompts.resolve_for_task(task_id,name)

    def _budget(self, sess, task_id, lessons):
        from ..agents.task_binding import AgentBudgetRow
        if sess.get(AgentBudgetRow, task_id) is None:
            sess.add(AgentBudgetRow(task_id=task_id, request_limit=max(128, 24 * lessons),
                                   tool_limit=max(256, 48 * lessons)))

    def _prepare_source_grant(self, task_id, owner):
        """Only explicit generate/submit actions create a source-scoped grant."""
        if not self.teaching.configs or not self.teaching.disclosures:
            return
        from ..agents.task_binding import _domain_binding
        from ..agents.model_routing import role_config, role_for_task, pin_role, TaskModelRoleRow
        with self.db.session() as sess:
            task = sess.get(LearningTaskRow, task_id)
            scope, _, category = _domain_binding(sess, task)
            role = role_for_task(task)
            pinned = sess.get(TaskModelRoleRow, (task_id, role))
            if pinned:
                self.teaching.disclosures.verify(task_id=task_id, actor_id=owner,
                    config_id=pinned.config_id, content_category=category, scope_snapshot=scope,
                    expected_grant_id=pinned.grant_id, session=sess)
                return
            view = role_config(self.teaching.configs, owner, role)
            if not view:
                raise error("MODEL_CAPABILITY_UNAVAILABLE")
            grant = self.teaching.disclosures.create(actor_id=owner, task_id=task_id,
                config_id=view["config_id"], config_version=view["config_version"],
                content_category=category, scope_snapshot=scope, session=sess)
            pin_role(sess, task_id, role, view, grant["grant_id"])

    def _verify_boundary(self, sess, task_id):
        if not self.teaching.configs or not self.teaching.disclosures:
            return
        from ..agents.task_binding import _domain_binding
        from ..agents.model_routing import role_for_task, TaskModelRoleRow
        task = sess.get(LearningTaskRow, task_id)
        scope, _, category = _domain_binding(sess, task)
        pinned = sess.get(TaskModelRoleRow, (task_id, role_for_task(task)))
        if pinned is None:
            raise error("disclosure_required")
        self.teaching.disclosures.verify_or_audit(task_id=task_id, actor_id=task.owner_user_id,
            config_id=pinned.config_id, content_category=category, scope_snapshot=scope,
            expected_grant_id=pinned.grant_id, session=sess)

    def _recoverable(self,task):
        return task and (task.status in ("queued","expired") or task.status in ("leased","running") and task.lease_until and task.lease_until<=utcnow())

    def kick(self,task_id):
        with self.lock:
            if task_id in self.active:return
            self.active.add(task_id)
        ASSESSMENT_POOL.submit(copy_context().run,self._run,task_id)

    def _run(self,task_id):
        lease=None
        try:
            task=self.tasks.get_task(task_id)
            if task["status"]=="expired":self.tasks.requeue_for_retry(task_id,actor_id=task["owner_user_id"])
            lease=self.tasks.claim(task_id,lease_owner="assessment-worker",lease_seconds=120,allow_reclaim_active=False)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(copy_context().run,self._compute,task_id,lease)
                while True:
                    try:result=future.result(timeout=30);break
                    except FutureTimeout:self.tasks.heartbeat(lease,lease_seconds=120)
            def publish(sess):
                row=sess.execute(select(UnitAssessmentRow).where(UnitAssessmentRow.task_id==task_id)).scalars().first()
                if row:
                    self._verify_boundary(sess, task_id)
                    self._owned(sess,row.assessment_id,row.owner_user_id)
                    context = load_json_field(row.context_json, {})
                    if context.get("scope_hash"):
                        from .unit_scope import UnitScopeRow
                        current_scope = sess.scalar(select(UnitScopeRow).join(
                            LearningTaskRow, UnitScopeRow.task_id == LearningTaskRow.task_id).where(
                                UnitScopeRow.owner_user_id == row.owner_user_id,
                                UnitScopeRow.unit_id == row.unit_id,
                                UnitScopeRow.outline_version_id == row.outline_version_id,
                                UnitScopeRow.scope_hash == context["scope_hash"],
                                LearningTaskRow.status == "succeeded"))
                        if not current_scope or not self._require_scope_coverage(
                                sess, row, current_scope):
                            raise error("TASK_CANCELLED", "assessment coverage changed")
                    row.questions_json=json.dumps(result,ensure_ascii=False)
                else:
                    attempt=sess.execute(select(AssessmentAttemptRow).where(AssessmentAttemptRow.task_id==task_id)).scalar_one()
                    assessment=sess.get(UnitAssessmentRow,attempt.assessment_id)
                    if needs_model_grade(load_json_field(assessment.questions_json,{})):
                        self._verify_boundary(sess, task_id)
                    self._owned(sess,attempt.assessment_id,attempt.owner_user_id);attempt.feedback_json=json.dumps(result,ensure_ascii=False)
                    if getattr(self.teaching, "memory", None):
                        from ..memory.service import enqueue
                        assessment = sess.get(UnitAssessmentRow, attempt.assessment_id)
                        evidence = {"earned": result.get("earned"), "maximum": result.get("maximum"),
                                    "weaknesses": [str(w)[:160] for g in result.get("grades", [])
                                        for w in g.get("weaknesses", [])][:12],
                                    "attempt_id": attempt.attempt_id,
                                    "conditions": "测试表现，不等同永久能力；是否提示若未知则未验证。"}
                        general_source = enqueue(sess, attempt.owner_user_id, "unit_assessment_result",
                                attempt.attempt_id, evidence, course=assessment.course_id)
                        course_source = enqueue(sess, attempt.owner_user_id, "course_assessment_result",
                                attempt.attempt_id, {"assessment_result": result}, course=assessment.course_id)
                        from ..memory.learning import record_assessment
                        record_assessment(sess, attempt.owner_user_id, assessment, attempt, result,
                                          [general_source, course_source])
            self.tasks.submit_result(lease,stage="generate",status="succeeded",duration_ms=0,publish=publish)
        except Exception as exc:
            code=getattr(exc,"code","MODEL_CAPABILITY_UNAVAILABLE")
            from ..logging_audit.contracts import validate_error_code
            try:validate_error_code(code)
            except Exception:code="MODEL_CAPABILITY_UNAVAILABLE"
            if lease:
                try:self.tasks.submit_result(lease,stage="generate",status="failed",error_code=code,duration_ms=0)
                except Exception:pass
        finally:
            with self.lock:self.active.discard(task_id)

    def _compute(self,task_id,lease=None):
        with self.db.session() as sess:
            row=sess.execute(select(UnitAssessmentRow).where(UnitAssessmentRow.task_id==task_id)).scalars().first()
            if row:
                self._owned(sess,row.assessment_id,row.owner_user_id)
                context=load_json_field(row.context_json,{});owner=row.owner_user_id;answers=None
            else:
                attempt=sess.execute(select(AssessmentAttemptRow).where(AssessmentAttemptRow.task_id==task_id)).scalar_one()
                row=self._owned(sess,attempt.assessment_id,attempt.owner_user_id)
                context=load_json_field(row.questions_json,{});owner=attempt.owner_user_id
                answers=load_json_field(attempt.answers_json,{})
        if answers is not None and not needs_model_grade(context):
            grades=objective_grades(context["questions"],answers)
            lookup={grade["question_id"]:grade for grade in grades}
            ordered=[lookup[q["id"]]|{"prompt":q["prompt"],"correct_answer":q["answer"],
                "explanation":q["explanation"],"source":q.get("source"),
                "review_lesson_ids":q["lesson_ids"]} for q in context["questions"]]
            return {"earned":sum(g["earned"] for g in ordered),
                    "maximum":sum(q["points"] for q in context["questions"]),
                    "grades":ordered,"advice":"根据各题反馈回看对应讲次。"}
        # Assessments bind a course/unit scope, not a teaching conversation.
        # The configured factory binds task authorization and checks each wire call.
        factory = self.teaching.model_adapter_factory
        adapter = factory(owner, task_id) if factory else None
        if adapter is None:raise error("MODEL_CAPABILITY_UNAVAILABLE")
        if context.get("assessment_version") == 2:
            try:
                return self._compute_v2(task_id, adapter, context, answers, lease)
            finally:
                if callable(getattr(adapter, "close", None)):
                    adapter.close()
        if answers is None:
            allowed={l["lesson_id"] for l in context["lessons"]}
            def get_unit_learning_context()->dict:
                """Read only the selected unit's actual taught text and goals."""
                return context
            def validate_assessment(assessment: UnitAssessment) -> dict:
                """Validate proposed questions against this unit's actual lesson references."""
                issues = question_issues(assessment, allowed)
                return {"valid": not issues, "issues": issues}
            def validator(ctx,output):
                issues=question_issues(output,allowed)
                if issues:raise ModelRetry("；".join(issues))
                from ..materials.study_sources import validate_source_choices
                validate_source_choices(output.questions, context.get("source_candidates", []),
                                        output.source_rejections)
                return output
            prompt=self.prompts.resolve_for_task(task_id,"unit_assessment_v1")["template_snapshot"]
            from ..materials.study_sources import QUESTION_POLICY, attach_provenance
            prompt += "\n" + QUESTION_POLICY + "\n" + DRAWING_POLICY
            if context.get("source_candidates"):
                adapter.omit_prompt_trace = True
            result=assessment_agent.generate_assessment(adapter,AssessmentInput.from_messages([{"role":"system","content":prompt},
                {"role":"user","content":json.dumps(context,ensure_ascii=False)}]),
                replace(deps_for_source(adapter), omit_content_trace=True),
                tools=[get_unit_learning_context,validate_assessment],output_validator=validator)
            output = result.payload.model_dump()
            known = {q["id"]: q for q in context.get("source_candidates", [])}
            for saved, question in zip(output["questions"], result.payload.questions):
                saved["source"] = attach_provenance(question, context.get("source_candidates", []))
                original = known.get(question.source_question_id, {})
                if question.source_use == "original" and original.get("answer_text"):
                    saved["answer"] = [original["answer_text"]]
                saved["public_figure"] = question.figure.model_dump(exclude={"ref", "source", "task", "variant_of", "variant_kind"})
            return output
        objective=objective_grades(context["questions"],answers)
        open_questions=[q for q in context["questions"] if q["kind"] not in ("single","multiple","drawing")]
        if not open_questions:
            lookup = {g["question_id"]: g for g in objective}
            ordered = [lookup[q["id"]] | {"prompt": q["prompt"], "correct_answer": q["answer"],
                "explanation": q["explanation"], "source": q.get("source"),
                "review_lesson_ids": q["lesson_ids"]} for q in context["questions"]]
            return {"earned": sum(g["earned"] for g in ordered),
                    "maximum": sum(q["points"] for q in context["questions"]),
                    "grades": ordered, "advice": ""}
        expected={q["id"]:q for q in open_questions}
        def grade_objective_answers()->dict:
            """Read immutable objective scores computed by the service."""
            return {"grades":objective}
        def validator(ctx,output):
            if len(output.grades)!=len(expected) or {g.question_id for g in output.grades}!=set(expected):
                raise ModelRetry("所有开放题必须恰好评价一次。")
            if any(g.earned>expected[g.question_id]["points"] for g in output.grades):raise ModelRetry("得分不得超过题目分值。")
            return output
        prompt=self.prompts.resolve_for_task(task_id,"assessment_feedback_v1")["template_snapshot"]
        result=evaluation_agent.evaluate_answers(adapter,EvaluationInput.from_messages([{"role":"system","content":prompt},
            {"role":"user","content":json.dumps({"questions":open_questions,"answers":answers,"objective":objective},ensure_ascii=False)}]),
            replace(deps_for_source(adapter), omit_content_trace=True),
            tools=[grade_objective_answers],output_validator=validator)
        grades=objective+[g.model_dump()|{"maximum":expected[g.question_id]["points"]} for g in result.payload.grades]
        lookup={g["question_id"]:g for g in grades}
        ordered=[lookup[q["id"]]|{"prompt":q["prompt"],"correct_answer":q["answer"],"explanation":q["explanation"],
            "source":q.get("source"),"review_lesson_ids":q["lesson_ids"]} for q in context["questions"]]
        return {"earned":sum(g["earned"] for g in ordered),"maximum":sum(q["points"] for q in context["questions"]),
                "grades":ordered,"advice":result.payload.advice}

    def _checkpoint(self, task_id, lease, *, grading=False, update=None):
        """The task lease fences the private checkpoint in the same transaction."""
        with self.db.session() as sess:
            if lease is not None:
                self.tasks.validate_lease(lease, actor_id=lease.owner_user_id, session=sess)
                self._verify_boundary(sess, task_id)
            model = AssessmentAttemptRow if grading else UnitAssessmentRow
            row = sess.scalar(select(model).where(model.task_id == task_id).with_for_update())
            self._owned(sess, row.assessment_id, row.owner_user_id)
            field = "feedback_json" if grading else "questions_json"
            value = load_json_field(getattr(row, field), {})
            if update is not None:
                update(value)
                setattr(row, field, json.dumps(value, ensure_ascii=False))
            return value

    def _compute_v2(self, task_id, adapter, context, answers, lease):
        from ..materials.study_sources import QUESTION_POLICY, validate_source_choices, attach_provenance
        from .assessment_validation import content_issues
        deps = replace(deps_for_source(adapter), omit_content_trace=True)
        if deps.budget.debit is None:
            # Offline adapters have no durable task binding; retain a bounded
            # aggregate budget rather than treating eight requests as eight lessons.
            limit = max(128, 24 * len(context.get("lesson_quotas", [])))
            deps = replace(deps, budget=replace(deps.budget, request_limit=limit, wire_limit=limit))
        grading = answers is not None
        checkpoint = self._checkpoint(task_id, lease, grading=grading)
        batches = checkpoint.get("_batches", {})
        calls = checkpoint.get("_calls", {})
        review_cache = checkpoint.get("_example_reviews", {})
        question_searches = checkpoint.get("_question_searches", {})

        def begin(key):
            deps.check()
            if calls.get(key, 0) >= 3:
                raise error("AGENT_LOOP_LIMIT")
            calls[key] = calls.get(key, 0) + 1
            self._checkpoint(task_id, lease, grading=grading,
                update=lambda value: value.update(_calls=calls))

        def save(key, value):
            deps.check()
            batches[key] = value
            self._checkpoint(task_id, lease, grading=grading,
                update=lambda data: data.update(_batches=batches))

        if not grading:
            prompt_name = "unit_assessment_v3" if context.get("scope_hash") else "unit_assessment_v2"
            prompt = self.prompts.resolve_for_task(task_id, prompt_name)["template_snapshot"] + "\n" + QUESTION_POLICY + "\n" + DRAWING_POLICY
            from ..materials.question_retrieval import POLICY as QUESTION_SEARCH_POLICY, question_search
            prompt += "\n" + QUESTION_SEARCH_POLICY
            from .examples import (ASSESSMENT_EXAMPLE_POLICY, assessment_example_issues,
                                   review_variations, variation_items)
            example_policy = context.get("example_policy_version") == 1
            if example_policy:
                prompt += "\n" + ASSESSMENT_EXAMPLE_POLICY
            all_questions, rejections = [], []
            for number, (lesson, quota) in enumerate(zip(context["lessons"], context["lesson_quotas"])):
                key = lesson["lesson_id"]
                local = {"lessons": [lesson], "lesson_quotas": [quota],
                         "source_candidates": [dict(q) for q in lesson.get("source_candidates", [])],
                         "prior_questions": [{"prompt": q["prompt"], "source_question_id": q["source_question_id"]}
                                             for q in all_questions]}
                if example_policy:
                    local["classroom_examples"] = lesson.get("classroom_examples", [])
                    local["used_classroom_examples"] = context.get("classroom_examples", local["classroom_examples"])
                reviews = review_cache.setdefault(key, {})
                prior_originals = {q["source_question_id"] for q in all_questions if q["source_use"] == "original"}
                for candidate in local["source_candidates"]:
                    if candidate["id"] in prior_originals:
                        candidate["original_allowed"] = False
                local["remaining_original_question_ids"] = [
                    candidate["id"] for candidate in local["source_candidates"]
                    if candidate["original_allowed"]]
                search_tools = []
                if context.get("source_snapshot"):
                    excluded = prior_originals | {
                        e["source_question_id"] for e in context.get("classroom_examples", [])
                        if e.get("source_question_id")}
                    search_state = question_searches.setdefault(key, {})
                    tool = question_search(self.db, deps.actor_id, context["source_snapshot"], [lesson],
                        local["source_candidates"], state=search_state, guard=deps.check,
                        save=lambda: self._checkpoint(task_id, lease,
                            update=lambda value: value.update(_question_searches=question_searches)),
                        excluded=excluded)
                    def more_questions(query: str) -> dict:
                        result = tool(query)
                        local["remaining_original_question_ids"] = [
                            q["id"] for q in local["source_candidates"] if q["original_allowed"]]
                        return result
                    more_questions.__name__ = "search_more_source_questions"
                    more_questions.__doc__ = tool.__doc__
                    search_tools = [more_questions]
                def validate(ctx, output):
                    issues = question_issues(output, {key}, local)
                    if issues:
                        raise ModelRetry("；".join(issues))
                    validate_source_choices(output.questions, local["source_candidates"],
                        output.source_rejections, assessment_version=2)
                    if example_policy:
                        issues = assessment_example_issues(output, local)
                        if issues:
                            raise ModelRetry("；".join(issues))
                        references = {q["id"]: q for q in [
                            *local["source_candidates"], *local["classroom_examples"]]}
                        items = variation_items(output.questions, references, output.source_rejections)
                        fingerprint = sha256(json.dumps(items, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                        if items and fingerprint not in reviews:
                            reviews[fingerprint] = review_variations(adapter, deps, items, {
                                "goal": lesson.get("goal", ""), "points": lesson.get("scope_points",
                                    lesson.get("knowledge_points", [])),
                                "figure_catalog": lesson.get("figure_catalog", {}),
                                "source_candidates": local["source_candidates"]})
                            self._checkpoint(task_id, lease,
                                update=lambda value: value.update(_example_reviews=review_cache))
                    return output
                output = None
                if key in batches:
                    try:
                        output = validate(None, LessonAssessment.model_validate(batches[key]))
                    except ModelRetry:
                        # A completed later lesson may conflict with a repaired
                        # earlier one. Discard only this dependent batch.
                        batches.pop(key)
                        self._checkpoint(task_id, lease, update=lambda data: data.update(_batches=batches))
                if output is None:
                    begin(key)
                    result = assessment_agent.generate_assessment(adapter, AssessmentInput.from_messages([
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": json.dumps(local, ensure_ascii=False)}]),
                        deps, assessment_version=2, output_validator=validate, tools=search_tools)
                    output = validate(None, result.payload)
                    save(key, output.model_dump())
                candidates = {q["id"]: q for q in local["source_candidates"]}
                figures = {f["ref"]: f for f in lesson.get("figure_catalog", {}).get("figures", [])}
                for index, question in enumerate(output.questions):
                    saved = question.model_dump()
                    saved["id"] = f"l{number + 1}_q{index + 1}"
                    saved["source"] = attach_provenance(question, local["source_candidates"])
                    source = candidates.get(question.source_question_id, {})
                    if question.source_use == "original" and source.get("answer_text"):
                        saved["answer"] = [source["answer_text"]]
                    figure = question.figure
                    public = figure.model_dump(include={"kind", "caption", "chart_spec", "table", "graphic"})
                    if figure.kind == "material_figure":
                        reference = figures[figure.ref]
                        from .assessment_sources import public_figure_source
                        public["source"] = public_figure_source(reference)
                    saved["public_figure"] = public
                    all_questions.append(saved)
                rejections.extend(r.model_dump() for r in output.source_rejections)
            result = {"assessment_version": 2, "title": context.get("unit_title", "单元测验"),
                "lesson_quotas": context["lesson_quotas"], "questions": all_questions,
                "source_rejections": rejections}
            if example_policy:
                result["example_reviews"] = {key: list(value.values()) for key, value in review_cache.items()}
            checked = UnitAssessmentV2.model_validate(result)
            issues = content_issues(checked, context)
            if issues:
                raise error("OUTPUT_INVALID")
            return result

        questions = context["questions"]
        grades = objective_grades(questions, answers)
        advice = []
        prompt = self.prompts.resolve_for_task(task_id, "assessment_feedback_v2")["template_snapshot"]
        for quota in context["lesson_quotas"]:
            key = quota["lesson_id"]
            opened = [q for q in questions if q["primary_lesson_id"] == key
                      and q["kind"] not in ("single", "multiple", "drawing")]
            if not opened:
                continue
            expected = {q["id"]: q for q in opened}
            def validate(ctx, output):
                if (len(output.grades) != len(expected)
                        or {g.question_id for g in output.grades} != set(expected)
                        or any(g.earned > expected[g.question_id]["points"] for g in output.grades)):
                    raise ModelRetry("本批开放题须恰好评价一次，分数不能超过各题分值。")
                return output
            if key in batches:
                output = validate(None, AssessmentFeedbackV2.model_validate(batches[key]))
            else:
                begin(key)
                output = evaluation_agent.evaluate_answers(adapter, EvaluationInput.from_messages([
                    {"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(
                        {"questions": opened, "answers": {q["id"]: answers[q["id"]] for q in opened}},
                        ensure_ascii=False)}]), deps, assessment_version=2, output_validator=validate).payload
                validate(None, output)
                save(key, output.model_dump())
            grades.extend(g.model_dump() | {"maximum": expected[g.question_id]["points"]} for g in output.grades)
            advice.append(output.advice)
        lookup = {g["question_id"]: g for g in grades}
        ordered = [lookup[q["id"]] | {"prompt": q["prompt"], "correct_answer": q["answer"],
            "explanation": q["explanation"], "source": q.get("source"),
            "knowledge_points": q.get("knowledge_point_ids", []), "goal": q["goal"],
            "cognitive_action": q.get("cognitive_action", ""), "review_lesson_ids": q["lesson_ids"]}
            for q in questions]
        return {"earned": sum(g["earned"] for g in ordered), "maximum": sum(q["points"] for q in questions),
                "grades": ordered, "advice": "\n".join(advice)}
