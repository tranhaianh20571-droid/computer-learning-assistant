"""Dynamic PydanticAI interview: 4-6 questions, depth, semantic answers and AI scope."""
import json,uuid
from copy import deepcopy
from sqlalchemy import select
from fastapi.testclient import TestClient
import pytest
from threading import Event
from concurrent.futures import ThreadPoolExecutor
from backend.logging_audit.db import Database
from backend.logging_audit.repositories import PgAuditLog
from backend.slice0_app import Slice0State
from backend.slice3_app import Slice3State,create_slice3_app
from backend.planning.models import PlanningTaskStateRow,LearningRouteRow
from backend.prompts.service import PromptBindingRow
from common_fixtures import synthetic_output_schema, synthetic_output_response
from common_fixtures import SyntheticModelAdapter,SyntheticSearchAdapter,ROUTE_OK_PAYLOAD,OUTLINE_OK_PAYLOAD,make_user,auth_headers


def questions(prefix='q'):
    purposes=['detail_preference','prior_experience','constraints','goals']
    labels=['希望讲得详细，包含解释和例子','能独立完成一般任务，较复杂任务需查资料','每周能学习六小时','希望独立完成实际项目']
    return [{'question_id':prefix+str(i+1),'purpose':purpose,'topic_key':prefix+purpose,
        'question':['希望当前主题的讲解展开到什么程度？','已有相关经验的实际使用情况更接近哪一种？','每周能稳定投入多少学习时间？','学完后希望独立完成什么任务？'][i],
        'kind':'single','skippable':True,'information_gap':'补齐影响教学安排的信息','planning_effect':'决定解释、实践和先修安排',
        'options':[{'key':'a','label':labels[i]},{'key':'b','label':'另一种合成学习情况'}]} for i,purpose in enumerate(purposes)]


class InterviewAdapter(SyntheticModelAdapter):
    def __init__(self,scope='route',followups=0,bad_once=False):
        super().__init__();self.scope=scope;self.followups=followups;self.bad_once=bad_once;self.interview_contexts=[];self.plan_messages=[]
    def generate(self,messages,**options):
        functions=[item['function'] for item in options.get('tools',[])]
        native_schema=synthetic_output_schema(options)
        schemas=[native_schema.get('properties',{})] if native_schema else [item.get('parameters',{}).get('properties',{}) for item in functions]
        if any('ready_to_plan' in properties for properties in schemas):
            context=next(json.loads(item['content']) for item in messages if item['role']=='user' and item['content'].startswith('{'))
            self.interview_contexts.append(context)
            first=context['shown_count']==0
            pending=questions() if first else []
            if not first and context['shown_count']==4 and self.followups:
                pending=[{**questions('extra')[0],'question_id':'mastery','purpose':'mastery','topic_key':'actual_application',
                    'question':'独立使用已有语言时通常需要多少帮助？'},
                    {**questions('extra')[1],'question_id':'difficulty','purpose':'difficulties','topic_key':'learning_obstacles',
                    'question':'学习新语言时最常遇到什么困难？'}][:self.followups]
            answered=[answer for answer in context['answers'] if answer.get('status')=='answered']
            profile={'learning_goal':context['original_request'],'detail_preference':'详细解释并提供例子','detail_status':'confirmed' if answered else 'unknown'}
            prior=next((answer for answer in answered if answer.get('purpose')=='prior_experience'),None)
            if prior:
                profile['prior_experience']=[{'domain':'C++','level':'independent','experience':prior['free_text'] or '能独立完成一般任务',
                    'evidence_question_ids':[prior['question_id']],'familiar_concepts':[],'uncertain_concepts':['具体概念是否熟悉尚不确定']}]
                profile['knowledge_transfers']=[{'source':'C++实际编程经验','target':'Java程序结构','boundary':'不是所有概念都熟悉，不自动压缩基础解释',
                    'evidence_question_ids':[prior['question_id']]}]
            output={'questions':pending,'ready_to_plan':not pending,'planning_scope':self.scope,'learner_profile':profile,'assumptions':[]}
            if self.bad_once and first:
                self.bad_once=False;output['questions'][0]['purpose']='goals'
        else:
            self.plan_messages.append(messages)
            output=deepcopy(OUTLINE_OK_PAYLOAD if any('units' in properties for properties in schemas) else ROUTE_OK_PAYLOAD)
        return synthetic_output_response(output,options)


@pytest.fixture(scope='module')
def database():
    db=Database();db.create_all();yield db;db.dispose()


@pytest.fixture
def env(database):
    model=InterviewAdapter();state=Slice3State(database,PgAuditLog(database),model_adapter_factory=lambda *args:model,
        search_adapter_factory=lambda *args:SyntheticSearchAdapter())
    app=create_slice3_app(state);app.state.slice0=Slice0State(database,PgAuditLog(database));client=TestClient(app)
    owner,token=make_user(client,database)
    route=client.post('/api/planning/routes',headers=auth_headers(token),json={'title':'Java学习','input':'想详细学习Java，有C++使用经历但不熟悉所有概念。'}).json()['route_id']
    task=client.post('/api/planning/tasks',headers=auth_headers(token),json={'task_kind':'route_generate','planning_scope':'auto',
        'target_route_id':route,'idempotency_key':uuid.uuid4().hex}).json()['task_id']
    return client,state,owner,auth_headers(token),task,model,route


def read(env):return env[0].get('/api/planning/tasks/'+env[4],headers=env[3]).json()
def run(env):return env[0].post('/api/planning/tasks/'+env[4]+'/run',headers=env[3])
def answer(env,skip=False,free=None):
    pending=read(env)['intake']['questions']
    values=[{'question_id':question['question_id'],'selections':[] if skip else ['a'],'skipped':skip,
        'free_text':free if question['purpose']=='prior_experience' and free else ''} for question in pending]
    return env[0].post('/api/planning/tasks/'+env[4]+'/decision',headers=env[3],json={'kind':'intake','answers':values})


def test_first_batch_is_ai_generated_four_questions_with_depth_and_original_goal(env):
    assert run(env).json()['wait_state']=='awaiting_intake'
    intake=read(env)['intake'];assert intake['shown_count']==4 and intake['min_questions']==4 and intake['max_questions']==6
    assert len(intake['questions'])==4 and any(question['purpose']=='detail_preference' for question in intake['questions'])
    assert not any(question['question_id']=='scope' for question in intake['questions'])
    assert env[5].interview_contexts[0]['original_request']=='想详细学习Java，有C++使用经历但不熟悉所有概念。'
    with env[1].db.session() as session:
        binding=session.scalar(select(PromptBindingRow).where(PromptBindingRow.task_id==env[4],PromptBindingRow.prompt_name=='learning_interview_v3'))
        assert binding and binding.template_sha256


def test_semantic_answers_profile_and_transfer_reach_planning(env):
    run(env);response=answer(env,free='独立写过小程序，遇到复杂问题仍需帮助。');assert response.status_code==200,response.text
    assert response.json()['task_status']=='succeeded'
    intake=read(env)['intake'];assert intake['done'] and len(intake['history'])==4
    assert intake['learner_profile']['detail_status']=='confirmed'
    assert intake['learner_profile']['prior_experience'][0]['level']=='independent'
    assert any('独立写过小程序' in json.dumps(messages,ensure_ascii=False) and 'knowledge_transfers' in json.dumps(messages) for messages in env[5].plan_messages)
    assert intake['answers'][0]['labels']==['希望讲得详细，包含解释和例子']


def test_followups_cannot_exceed_six_and_refresh_keeps_question_history(env):
    env[5].followups=2;run(env);assert answer(env).json()['wait_state']=='awaiting_intake'
    intake=read(env)['intake'];assert intake['shown_count']==6 and len(intake['history'])==6 and len(intake['questions'])==2
    assert answer(env).json()['task_status']=='succeeded'
    assert read(env)['intake']['shown_count']==6 and read(env)['intake']['done']


def test_five_question_finish_is_allowed(env):
    env[5].followups=1;run(env);answer(env);assert read(env)['intake']['shown_count']==5
    assert answer(env).json()['task_status']=='succeeded'


def test_all_skipped_is_valid_and_depth_is_assumption(env):
    run(env);response=answer(env,skip=True);assert response.status_code==200,response.text
    data=read(env)['intake'];assert data['done'] and all(item['status']=='skipped' for item in data['answers'])
    assert data['learner_profile']['detail_status']=='assumed'


def test_validator_uses_pydanticai_retry_for_missing_depth(env):
    env[5].bad_once=True;assert run(env).json()['wait_state']=='awaiting_intake'
    assert len(env[5].interview_contexts)==2


def test_ai_course_scope_produces_course_draft_without_scope_question(env):
    env[5].scope='course';run(env);response=answer(env)
    assert response.json()['task_status']=='succeeded',response.text
    state=read(env);assert state['resolved_scope']=='course' and state['target_kind']=='course'
    draft=env[0].get('/api/planning/drafts/'+state['draft_id'],headers=env[3]).json()
    assert draft['draft_kind']=='course_outline' and draft['content']['units']
    with env[1].db.session() as session:assert session.get(LearningRouteRow,env[6]).deleted_at


@pytest.mark.parametrize('values',[
    [{'question_id':'fake','selections':['a']}],
    [{'question_id':'q1','selections':['invalid']}],
    [{'question_id':'q1','selections':['a','b']}],
    [{'question_id':'q1','selections':['a']},{'question_id':'q1','selections':['a']}],
])
def test_forged_or_invalid_answer_does_not_mutate_state(env,values):
    run(env);before=read(env)['intake']
    response=env[0].post('/api/planning/tasks/'+env[4]+'/decision',headers=env[3],json={'kind':'intake','answers':values})
    assert response.status_code==400 and read(env)['intake']==before


def test_cross_owner_denied_and_duplicate_submission_does_not_add_questions(env):
    run(env);stranger,token=make_user(env[0],env[1].db)
    assert env[0].get('/api/planning/tasks/'+env[4],headers=auth_headers(token)).status_code==404
    answer(env);before=read(env)['intake']['shown_count']
    response=env[0].post('/api/planning/tasks/'+env[4]+'/decision',headers=env[3],json={'kind':'intake','answers':[{'question_id':'q1','selections':['a']}]})
    assert response.status_code==409 and read(env)['intake']['shown_count']==before


def test_provider_failure_does_not_fallback_to_fixed_questionnaire(env):
    env[1].planning.model_adapter_factory=lambda *args:None
    assert run(env).json()['task_status']=='failed'
    assert not read(env)['intake']['questions']


def test_legacy_unfinished_answers_are_preserved_and_depth_not_reasked(env):
    from backend.planning.intake import normalize_intake
    old={'round':1,'questions':[{'question_id':'depth','question':'旧详略题','kind':'single','options':[{'key':'deep','label':'详细学习'}]}],
        'answers':[{'question_id':'depth','selections':['deep'],'free_text':''}],'done':False}
    migrated=normalize_intake(old)
    assert migrated['schema_version']==3 and migrated['shown_count']==1
    assert migrated['answers'][0]['labels']==['详细学习'] and migrated['history'][0]['purpose']=='detail_preference'


def test_profile_reaches_lesson_context_and_changes_prewarm_key(env):
    from test_teaching_assessments import LearningAdapter
    from backend.teaching.pedagogy import lesson_context
    client,state,owner,headers,task,model,route=env
    model.scope='course';run(env);answer(env,free='相关语言只跟过教程，独立使用不熟悉。')
    info=read(env);draft=client.get('/api/planning/drafts/'+info['draft_id'],headers=headers).json()
    assert client.post('/api/planning/drafts/'+draft['draft_id']+'/confirm',headers=headers,json={'revision':draft['revision'],'idempotency_key':uuid.uuid4().hex}).status_code==200
    overview=state.overview.course_overview(info['target_id'],actor_id=owner);lesson=overview['units'][0]['lessons'][0]['lesson_id']
    state.teaching.model_adapter_factory=lambda *args:LearningAdapter()
    start=state.teaching.start_lesson(info['target_id'],lesson,actor_id=owner)
    conv=state.teaching._conversation(start['conversation_id'],owner)
    context=lesson_context(state.teaching,conv)
    assert context['learner_profile']['profile']['detail_status']=='confirmed'
    assert context['learner_profile']['answers'][1]['free_text']=='相关语言只跟过教程，独立使用不熟悉。'
    before=state.teaching.preparation.keys(conv)[0]
    with state.db.session() as session:
        row=session.get(PlanningTaskStateRow,task);data=json.loads(row.intake_json)
        data['learner_profile']['detail_preference']='希望简洁介绍';row.intake_json=json.dumps(data,ensure_ascii=False)
    assert state.teaching.preparation.keys(conv)[0]!=before


@pytest.mark.parametrize('action',['cancel','delete'])
def test_late_interview_result_does_not_publish_after_cancel_or_delete(env,action):
    began,release=Event(),Event();model=env[5];original=model.generate
    def paused(messages,**options):
        began.set();assert release.wait(10);return original(messages,**options)
    model.generate=paused
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(run,env);assert began.wait(4)
        if action=='cancel':env[1].tasks.request_cancel(env[4],actor_id=env[2])
        else:assert env[0].delete('/api/planning/routes/'+env[6],headers=env[3]).status_code==200
        release.set();result=future.result(timeout=10)
    assert result.json()['task_status']=='failed'
    assert not read(env)['intake']['questions']


def test_unanswered_old_questionnaire_is_replaced_and_keeps_legacy_snapshot(env):
    old={'round':1,'questions':[{'question_id':'scope','question':'旧范围题','kind':'single','options':[{'key':'route','label':'路线'}]}],
        'answers':[],'done':False}
    with env[1].db.session() as session:
        row=session.get(PlanningTaskStateRow,env[4]);row.intake_json=json.dumps(old,ensure_ascii=False);row.wait_state='awaiting_intake'
    response=env[0].post('/api/planning/tasks/'+env[4]+'/decision',headers=env[3],json={'kind':'intake','answers':[]})
    assert response.json()['wait_state']=='awaiting_intake'
    assert read(env)['intake']['shown_count']==4 and not any(question['question_id']=='scope' for question in read(env)['intake']['questions'])
    with env[1].db.session() as session:assert json.loads(session.get(PlanningTaskStateRow,env[4]).intake_json)['legacy_history']==old['questions']
