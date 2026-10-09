"""Model-driven planning interview with immutable history and bounded questions."""
import json
from difflib import SequenceMatcher
from pydantic_ai.exceptions import ModelRetry
from sqlalchemy import select
from ..agents.roles import intake_agent
from ..agents.contracts import IntakeInput
from ..agents.runner import deps_for_source
from ..prompts.service import PromptUnavailable
from .models import CourseRow,LearningRouteRow,PlanningTaskStateRow,load_json_field,utcnow
from .outputs import IntakeQuestionSet,KnowledgeTransfer
from .errors import error,WaitStateSignal
from ..tasks.service import LearningTaskRow

MIN_QUESTIONS=4
MAX_QUESTIONS=6


def normalize_intake(raw):
    data=dict(raw or {})
    if data.get('schema_version')==3:return data
    history=[dict(question) for question in data.get('questions',[])]
    answers=[dict(answer) for answer in data.get('answers',[])]
    for question in history:
        question.setdefault('purpose','detail_preference' if question.get('question_id')=='depth' else 'prior_experience')
        question.setdefault('topic_key','legacy:'+question.get('question_id',''))
    by_id={question['question_id']:question for question in history}
    for answer in answers:
        question=by_id.get(answer.get('question_id'))
        if question:
            answer.setdefault('question',question.get('question',''))
            answer.setdefault('purpose',question.get('purpose'))
            answer.setdefault('labels',[option['label'] for option in question.get('options',[]) if option['key'] in answer.get('selections',[])])
        answer.setdefault('status','answered')
    data.update(schema_version=3,history=history,answers=answers,questions=[],shown_count=max(len(history),len({answer.get('question_id') for answer in answers})))
    return data


def profile_text(intake):
    if isinstance(intake,list):intake={'answers':intake}
    profile=intake.get('learner_profile',{})
    return json.dumps({'learner_profile':profile,'answers':intake.get('answers',[]),'assumptions':intake.get('assumptions',[])},ensure_ascii=False)[:14000]


class IntakeService:
    def __init__(self,planning):self.planning=planning

    def publish(self,task_id,state,lease,data,scope=None):
        with self.planning.db.session() as sess:
            parent=sess.get(CourseRow,state.target_id,with_for_update=True) if state.target_kind=='course' else sess.get(LearningRouteRow,state.target_id,with_for_update=True)
            if not parent or parent.deleted_at or parent.owner_user_id!=state.owner_user_id:raise error('STALE_RESULT_DISCARDED')
            task=sess.get(LearningTaskRow,task_id,with_for_update=True)
            row=sess.get(PlanningTaskStateRow,task_id,with_for_update=True)
            if not row or row.owner_user_id!=state.owner_user_id:raise error('task_not_found',status=404)
            if not task or task.cancel_requested or task.attempt!=lease.attempt or task.lease_token!=lease.lease_token or not task.lease_until:
                raise error('STALE_RESULT_DISCARDED')
            from datetime import timezone
            until=task.lease_until if task.lease_until.tzinfo else task.lease_until.replace(tzinfo=timezone.utc)
            if until<=utcnow():raise error('STALE_RESULT_DISCARDED')
            row.intake_json=json.dumps(data,ensure_ascii=False);row.updated_at=utcnow()
            if scope:row.resolved_scope=scope

    def context(self,state,data):
        with self.planning.db.session() as sess:
            parent=sess.get(CourseRow,state.target_id) if state.target_kind=='course' else sess.get(LearningRouteRow,state.target_id)
            if not parent or parent.owner_user_id!=state.owner_user_id or parent.deleted_at:
                raise error('course_not_found' if state.target_kind=='course' else 'route_not_found',status=404)
            original=getattr(parent,'input_ref','') or getattr(parent,'title_ref','')
            from ..materials.models import MaterialRow
            selected=load_json_field(state.material_ids_json,[])
            materials=list(sess.scalars(select(MaterialRow).where(MaterialRow.material_id.in_(selected),MaterialRow.owner_user_id==state.owner_user_id,MaterialRow.deleted_at.is_(None)))) if selected else []
        return {'original_request':data.get('original_request') or original,'topic':parent.title_ref,
            'scenario':parent.scenario,'explicit_scope':state.planning_scope if state.planning_scope!='auto' else None,
            'selected_materials':{'count':len(materials)},'history':data.get('history',[]),'answers':data.get('answers',[]),
            'learner_profile':data.get('learner_profile',{}),'assumptions':data.get('assumptions',[]),
            'shown_count':data.get('shown_count',0),'remaining_questions':max(0,MAX_QUESTIONS-data.get('shown_count',0))}

    def generate(self,task_id,state,data):
        context=self.context(state,data)
        try:prompt=self.planning.prompts.resolve_for_task(task_id,'learning_interview_v3')['template_snapshot']
        except PromptUnavailable:
            self.planning.prompts.bind_for_task(task_id,'learning_interview_v3',allow_fallback=True)
            prompt=self.planning.prompts.resolve_for_task(task_id,'learning_interview_v3')['template_snapshot']
        adapter=self.planning._require_model_adapter(state.owner_user_id,task_id)
        shown=context['shown_count'];history=context['history'];answers=context['answers']
        available={answer['question_id'] for answer in answers if answer.get('status')!='skipped'}|{'original_request'}
        ids={question['question_id'] for question in history}
        topics={question.get('topic_key') for question in history}
        def validator(ctx,output):
            count=len(output.questions)
            if shown==0 and count!=4:raise ModelRetry('第一批必须恰好4题，包含详略程度题。')
            if shown and (count>min(2,MAX_QUESTIONS-shown) or count<max(0,MIN_QUESTIONS-shown)):
                raise ModelRetry('只补充剩余额度内的问题，累计至少4题、最多6题。')
            if output.ready_to_plan and (count or shown<4):raise ModelRetry('展示至少4题且不再返回问题，才可结束访谈。')
            if not count and not output.ready_to_plan:raise ModelRetry('无问题时必须结束访谈，达到6题不得继续循环。')
            has_depth=any(question.get('purpose')=='detail_preference' for question in history)
            if not has_depth and not any(question.purpose=='detail_preference' for question in output.questions):
                raise ModelRetry('必须包含希望学习详略程度的问题，不得默认用户意愿。')
            new_ids=[question.question_id for question in output.questions];new_topics=[question.topic_key for question in output.questions]
            if len(set(new_ids))!=count or ids.intersection(new_ids) or len(set(new_topics))!=count or topics.intersection(new_topics):
                raise ModelRetry('题目ID和主题不能重复，已跳过的主题不得再问。')
            for question in output.questions:
                if len(question.options)>6:raise ModelRetry('每题提供2～6个简短选项，其余情况通过自由补充。')
                if question.question_id=='scope' or question.topic_key in ('scope','planning_scope') or {option.key for option in question.options}=={'course','route'}:
                    raise ModelRetry('规划范围必须内部判断，不能让用户选择单门或多门。')
                question.skippable=True
                option_keys=[option.key for option in question.options];labels=[option.label for option in question.options]
                if len(set(option_keys))!=len(option_keys) or len(set(labels))!=len(labels):raise ModelRetry('选项key与标签必须唯一。')
                if any(SequenceMatcher(None,question.question,old.get('question','')).ratio()>.9 for old in history):
                    raise ModelRetry('问题与已展示的问题重复，请补齐其它有效信息。')
            if context['explicit_scope'] and output.planning_scope!=context['explicit_scope']:
                raise ModelRetry('用户明确的学习范围必须遵守。')
            for entry in [*output.learner_profile.prior_experience,*output.learner_profile.knowledge_transfers]:
                if not set(entry.evidence_question_ids)<=available:raise ModelRetry('画像依据只能引用原始要求或非跳过的已答问题。')
                if hasattr(entry,'level') and entry.level!='unknown' and not entry.evidence_question_ids:
                    raise ModelRetry('已有知识的掌握程度必须有用户回答或原始要求作依据。')
                if isinstance(entry,KnowledgeTransfer) and not entry.evidence_question_ids:
                    raise ModelRetry('类比迁移必须有用户确认的已有知识依据；未知基础不能当成已掌握。')
            if output.ready_to_plan:
                depths=[answer for answer in answers if answer.get('purpose')=='detail_preference']
                confirmed=any(answer.get('status')!='skipped' for answer in depths)
                if confirmed and (output.learner_profile.detail_status!='confirmed' or not output.learner_profile.detail_preference):
                    raise ModelRetry('必须把用户的详略程度回答整理进画像。')
                if not confirmed:
                    output.learner_profile.detail_status='assumed'
                    output.learner_profile.detail_preference='系统讲解，保留基础说明、例子和必要边界'
                    if '详略程度未确认，采用系统讲解。' not in output.assumptions:
                        output.assumptions=output.assumptions[:7]+['详略程度未确认，采用系统讲解。']
            return output
        try:
            return intake_agent.interview(adapter,IntakeInput.from_messages([{'role':'system','content':prompt},
                {'role':'user','content':json.dumps(context,ensure_ascii=False)}]),deps_for_source(adapter,max_requests=3),
                output_validator=validator).payload,context
        finally:
            if hasattr(adapter,'close'):adapter.close()

    def gate(self,lease,task_id,state):
        raw=load_json_field(state.intake_json,None)
        if raw and raw.get('done'):return {'answers':raw.get('answers',[])}
        data=normalize_intake(raw)
        legacy=[question for question in data.get('questions',[]) if question.get('question_id')!='scope']
        if data.get('questions') and len(legacy)!=len(data['questions']):
            data['questions']=legacy;self.planning._intake_save(task_id,data)
        if data.get('questions'):
            raise WaitStateSignal('awaiting_intake',question='请选择、补充或跳过当前问题。')
        output,context=self.generate(task_id,state,data)
        if output.ready_to_plan:
            data.update(done=True,learner_profile=output.learner_profile.model_dump(),assumptions=output.assumptions,
                original_request=context['original_request'],planning_scope=output.planning_scope)
            self.publish(task_id,state,lease,data,output.planning_scope)
            state.intake_json=json.dumps(data,ensure_ascii=False);state.resolved_scope=output.planning_scope
            return data
        questions=[question.model_dump() for question in output.questions]
        data.update(questions=questions,history=data.get('history',[])+questions,shown_count=data.get('shown_count',0)+len(questions),
            original_request=context['original_request'],learner_profile=output.learner_profile.model_dump(),
            assumptions=output.assumptions,done=False)
        self.publish(task_id,state,lease,data)
        state.intake_json=json.dumps(data,ensure_ascii=False)
        raise WaitStateSignal('awaiting_intake',question='根据你的学习要求，准备了几个问题，可选择、补充或跳过。')

    def decide(self,task_id,actor_id,answers):
        if not isinstance(answers,list) or len(answers)>6:raise error('invalid_field','答题格式无效。')
        with self.planning.db.session() as sess:
            row=sess.get(PlanningTaskStateRow,task_id,with_for_update=True)
            if not row or row.owner_user_id!=actor_id:raise error('task_not_found',status=404)
            if row.abandoned_at:raise error('TASK_CANCELLED',status=409)
            if row.wait_state not in ('awaiting_intake','awaiting_scope_clarification'):raise error('state_invalid','当前没有待答问题。',status=409)
            raw=load_json_field(row.intake_json,{})
            if raw.get('schema_version')!=3 and not raw.get('answers') and not answers and row.wait_state=='awaiting_intake':
                data={'schema_version':3,'history':[],'questions':[],'answers':[],'shown_count':0,'done':False,
                    'legacy_history':raw.get('questions',[])}
                row.intake_json=json.dumps(data,ensure_ascii=False);row.wait_state=None;row.clarification_question=None
                return {'task_id':task_id,'shown_count':0,'wait_state':None}
            data=normalize_intake(raw)
            if row.wait_state=='awaiting_scope_clarification':
                data['done']=False;data['questions']=[]
                row.intake_json=json.dumps(data,ensure_ascii=False);row.wait_state=None;row.clarification_question=None
                return {'task_id':task_id,'wait_state':None}
            questions=data.get('questions') or raw.get('questions',[])
            lookup={question['question_id']:question for question in questions}
            submitted={}
            for answer in answers:
                if not isinstance(answer,dict):raise error('invalid_field','答题格式无效。')
                ident=answer.get('question_id')
                if ident not in lookup or ident in submitted:raise error('invalid_field','题目不存在或重复提交。')
                selections=answer.get('selections',[]);free=answer.get('free_text') or ''
                if not isinstance(selections,list) or not all(isinstance(value,str) for value in selections) or not isinstance(free,str):
                    raise error('invalid_field','选项或补充格式无效。')
                question=lookup[ident];options={option['key']:option['label'] for option in question['options']}
                if len(set(selections))!=len(selections) or not set(selections)<=set(options) or (question['kind']=='single' and len(selections)>1):
                    raise error('invalid_field','请选择本题有效选项。')
                if len(free)>1000:raise error('invalid_field','补充内容过长。')
                free=''.join(character for character in free if character>=' ' or character in '\n\t').strip()
                skipped=bool(answer.get('skipped')) or not selections and not free
                if skipped:selections=[];free=''
                submitted[ident]={'question_id':ident,'question':question['question'],'purpose':question.get('purpose'),
                    'selections':selections,'labels':[options[value] for value in selections],'free_text':free,'status':'skipped' if skipped else 'answered'}
            for ident,question in lookup.items():
                submitted.setdefault(ident,{'question_id':ident,'question':question['question'],'purpose':question.get('purpose'),
                    'selections':[],'labels':[],'free_text':'','status':'skipped'})
            data['answers']=data.get('answers',[])+list(submitted.values());data['questions']=[]
            row.answer_draft_json='[]';row.answer_draft_revision+=1
            row.intake_json=json.dumps(data,ensure_ascii=False);row.wait_state=None;row.clarification_question=None;row.updated_at=utcnow()
        return {'task_id':task_id,'shown_count':data['shown_count'],'wait_state':None}
