"""Private unit tests based on actual taught content, with fenced grading tasks."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextvars import copy_context
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
    LessonContentVersionRow, ConversationRow, load_json_field, utcnow)
from ..planning.errors import error
from ..tasks.service import LearningTaskRow
from .models import TeachingStepRow
from .learning_models import UnitAssessmentRow, AssessmentAttemptRow
from .learning_outputs import UnitAssessment, AssessmentFeedback

ASSESSMENT_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="unit-assessment")


def question_issues(output, allowed):
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
    if not any(q.kind in ("short","code_reading") for q in output.questions):issues.append("至少需要一题开放题")
    return issues


def objective_grades(questions, answers):
    results=[]
    for q in questions:
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
        self.active=set();self.lock=threading.Lock()

    def _unit(self,sess,course_id,unit_id,owner):
        course=sess.get(CourseRow,course_id)
        unit=sess.get(CourseUnitRow,unit_id)
        if (not course or course.owner_user_id!=owner or course.deleted_at or not unit
            or unit.outline_version_id!=course.current_outline_version_id):raise error("course_not_found",status=404)
        return course,unit

    def _context(self,sess,course,unit,owner):
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
            taught.append({"lesson_id":lesson.lesson_id,"title":lesson.title_ref or lesson.goal_ref,"goal":lesson.goal_ref,
                "content_version_id":version.content_version_id,
                "taught":[{"phase":s.phase,"title":s.title,"text":s.text} for s in completed]})
        if not taught:raise error("ASSESSMENT_NOT_READY",status=409)
        return {"course_title":course.title_ref,"unit_title":unit.title_ref,"lessons":taught}

    def unit_status(self,course_id,unit_id,owner):
        pending=None
        with self.db.session() as sess:
            course,unit=self._unit(sess,course_id,unit_id,owner)
            row=sess.execute(select(UnitAssessmentRow).where(UnitAssessmentRow.owner_user_id==owner,
                UnitAssessmentRow.unit_id==unit_id).order_by(UnitAssessmentRow.created_at.desc())).scalars().first()
            if row:
                result=self._view(sess,row)
                task=sess.get(LearningTaskRow,row.task_id)
                if self._recoverable(task):pending=row.task_id
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
            existing=sess.execute(select(UnitAssessmentRow).where(UnitAssessmentRow.owner_user_id==owner,
                UnitAssessmentRow.unit_id==unit_id).order_by(UnitAssessmentRow.created_at.desc())).scalars().first()
            if existing:
                task_id=existing.task_id;ident=existing.assessment_id
            else:
                task=self.tasks.create_task(owner_user_id=owner,kind="unit_assessment_generate",subject_id=course_id,
                    idempotency_key="assessment:"+sha256(f"{unit_id}:{key}".encode()).hexdigest(),stage="generate")
                task_id=task["task_id"];ident="assessment_"+uuid.uuid4().hex[:16]
                sess.add(UnitAssessmentRow(assessment_id=ident,owner_user_id=owner,course_id=course_id,unit_id=unit_id,
                    outline_version_id=unit.outline_version_id,task_id=task_id,context_json=json.dumps(context,ensure_ascii=False)))
            sess.commit()
        self._bind(task_id,"unit_assessment_v1");self.kick(task_id)
        return {"assessment_id":ident,"task_id":task_id}

    def _owned(self,sess,ident,owner):
        row=sess.get(UnitAssessmentRow,ident)
        if not row or row.owner_user_id!=owner:raise error("course_not_found",status=404)
        self._unit(sess,row.course_id,row.unit_id,owner)
        return row

    def _view(self,sess,row):
        task=sess.get(LearningTaskRow,row.task_id)
        if task.status=="succeeded":
            sess.refresh(row)
        questions=load_json_field(row.questions_json,{})
        return {"assessment_id":row.assessment_id,"task_id":row.task_id,"status":task.status,
            "title":questions.get("title","单元测验"),"eligible":True,"unit_id":row.unit_id,
            "questions":[{k:q[k] for k in ("id","kind","prompt","options","goal","points")}
                         for q in questions.get("questions",[])] if task.status=="succeeded" else []}

    def get(self,ident,owner):
        with self.db.session() as sess:
            row=self._owned(sess,ident,owner);view=self._view(sess,row)
            task=sess.get(LearningTaskRow,row.task_id);pending=self._recoverable(task)
        if pending:self.kick(row.task_id)
        return view

    def submit(self,ident,owner,answers,key):
        with self.db.session() as sess:
            row=self._owned(sess,ident,owner);sess.refresh(row,with_for_update=True)
            task=sess.get(LearningTaskRow,row.task_id)
            if task.status!="succeeded":raise error("ASSESSMENT_NOT_READY",status=409)
            questions=load_json_field(row.questions_json,{})["questions"]
            if set(answers)!={q["id"] for q in questions}:raise error("invalid_field","请完成全部题目。")
            for q in questions:
                value=answers[q["id"]]
                if q["kind"] in ("single","multiple"):
                    if (not isinstance(value,list) or not value or len(value)!=len(set(value))
                        or not all(isinstance(v,str) for v in value)
                        or not set(value)<={o["id"] for o in q["options"]}
                        or q["kind"]=="single" and len(value)!=1):raise error("invalid_field")
                elif not isinstance(value,str) or not value.strip() or len(value)>4000:raise error("invalid_field")
            previous=sess.execute(select(AssessmentAttemptRow).where(AssessmentAttemptRow.owner_user_id==owner,
                AssessmentAttemptRow.assessment_id==ident,AssessmentAttemptRow.idempotency_key==key)).scalars().first()
            if previous:
                if load_json_field(previous.answers_json,{})!=answers:raise error("invalid_field","同次提交的答案已固定。",status=409)
                attempt_id,task_id=previous.attempt_id,previous.task_id
            else:
                created=self.tasks.create_task(owner_user_id=owner,kind="assessment_grade",subject_id=row.course_id,
                    idempotency_key="grade:"+sha256(f"{ident}:{key}".encode()).hexdigest(),stage="generate")
                task_id=created["task_id"];attempt_id="answer_"+uuid.uuid4().hex[:16]
                sess.add(AssessmentAttemptRow(attempt_id=attempt_id,assessment_id=ident,owner_user_id=owner,
                    idempotency_key=key,task_id=task_id,answers_json=json.dumps(answers,ensure_ascii=False)))
            sess.commit()
        self._bind(task_id,"assessment_feedback_v1");self.kick(task_id)
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
        if not exists:self.prompts.bind_for_task(task_id,name,agent_code_version="3",tools_version="3")
        else:self.prompts.resolve_for_task(task_id,name)

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
                future=pool.submit(copy_context().run,self._compute,task_id)
                while True:
                    try:result=future.result(timeout=30);break
                    except FutureTimeout:self.tasks.heartbeat(lease,lease_seconds=120)
            def publish(sess):
                row=sess.execute(select(UnitAssessmentRow).where(UnitAssessmentRow.task_id==task_id)).scalars().first()
                if row:
                    self._owned(sess,row.assessment_id,row.owner_user_id);row.questions_json=json.dumps(result,ensure_ascii=False)
                else:
                    attempt=sess.execute(select(AssessmentAttemptRow).where(AssessmentAttemptRow.task_id==task_id)).scalar_one()
                    self._owned(sess,attempt.assessment_id,attempt.owner_user_id);attempt.feedback_json=json.dumps(result,ensure_ascii=False)
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

    def _compute(self,task_id):
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
        adapter=self.teaching._model_adapter(owner,task_id)
        if adapter is None:raise error("MODEL_CAPABILITY_UNAVAILABLE")
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
                return output
            prompt=self.prompts.resolve_for_task(task_id,"unit_assessment_v1")["template_snapshot"]
            result=assessment_agent.generate_assessment(adapter,AssessmentInput.from_messages([{"role":"system","content":prompt},
                {"role":"user","content":json.dumps(context,ensure_ascii=False)}]),deps_for_source(adapter),
                tools=[get_unit_learning_context,validate_assessment],output_validator=validator)
            return result.payload.model_dump()
        objective=objective_grades(context["questions"],answers)
        open_questions=[q for q in context["questions"] if q["kind"] not in ("single","multiple")]
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
            deps_for_source(adapter),tools=[grade_objective_answers],output_validator=validator)
        grades=objective+[g.model_dump()|{"maximum":expected[g.question_id]["points"]} for g in result.payload.grades]
        lookup={g["question_id"]:g for g in grades}
        ordered=[lookup[q["id"]]|{"prompt":q["prompt"],"correct_answer":q["answer"],"explanation":q["explanation"],
            "review_lesson_ids":q["lesson_ids"]} for q in context["questions"]]
        return {"earned":sum(g["earned"] for g in ordered),"maximum":sum(q["points"] for q in context["questions"]),
                "grades":ordered,"advice":result.payload.advice}
