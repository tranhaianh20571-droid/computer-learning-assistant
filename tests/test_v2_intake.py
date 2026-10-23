"""Dynamic interview plus one required structured time-budget question."""
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
def with_time_answer(values):
    return [({'question_id':'learning_time_budget','selections':[],'skipped':False,
        'time_budget':{'weeks':8,'hours_per_week':6}} if value['question_id']=='learning_time_budget' else value)
        for value in values]

def answer(env,skip=False,free=None):
    pending=read(env)['intake']['questions']
    values=[{'question_id':question['question_id'],'selections':[] if skip else ['a'],'skipped':skip,
        'free_text':free if question['purpose']=='prior_experience' and free else ''} for question in pending]
    return env[0].post('/api/planning/tasks/'+env[4]+'/decision',headers=env[3],json={'kind':'intake','answers':with_time_answer(values)})


def test_first_batch_has_four_ai_questions_and_one_required_time_question(env):
    assert run(env).json()['wait_state']=='awaiting_intake'
    intake=read(env)['intake'];assert intake['shown_count']==5 and intake['min_questions']==5 and intake['max_questions']==7
    assert len(intake['questions'])==5 and any(question['purpose']=='detail_preference' for question in intake['questions'])
    assert intake['questions'][-1]['kind']=='time_budget' and not intake['questions'][-1]['skippable']
    assert not any(question['question_id']=='scope' for question in intake['questions'])
    assert env[5].interview_contexts[0]['original_request']=='想详细学习Java，有C++使用经历但不熟悉所有概念。'
    with env[1].db.session() as session:
        binding=session.scalar(select(PromptBindingRow).where(PromptBindingRow.task_id==env[4],PromptBindingRow.prompt_name=='learning_interview_v3'))
        assert binding and binding.template_sha256


def test_semantic_answers_profile_and_transfer_reach_planning(env):
    run(env);response=answer(env,free='独立写过小程序，遇到复杂问题仍需帮助。');assert response.status_code==200,response.text
    assert response.json()['task_status']=='succeeded'
    intake=read(env)['intake'];assert intake['done'] and len(intake['history'])==5
    assert intake['learner_profile']['detail_status']=='confirmed'
    assert intake['learner_profile']['prior_experience'][0]['level']=='independent'
    assert any('独立写过小程序' in json.dumps(messages,ensure_ascii=False) and 'knowledge_transfers' in json.dumps(messages) for messages in env[5].plan_messages)
    assert intake['answers'][0]['labels']==['希望讲得详细，包含解释和例子']


def test_followups_cannot_exceed_seven_and_refresh_keeps_question_history(env):
    env[5].followups=2;run(env);assert answer(env).json()['wait_state']=='awaiting_intake'
    intake=read(env)['intake'];assert intake['shown_count']==7 and len(intake['history'])==7 and len(intake['questions'])==2
    assert answer(env).json()['task_status']=='succeeded'
    assert read(env)['intake']['shown_count']==7 and read(env)['intake']['done']


def test_six_question_finish_is_allowed(env):
    env[5].followups=1;run(env);answer(env);assert read(env)['intake']['shown_count']==6
    assert answer(env).json()['task_status']=='succeeded'


def test_optional_questions_skipped_is_valid_but_time_answer_is_required(env):
    run(env);response=answer(env,skip=True);assert response.status_code==200,response.text
    data=read(env)['intake'];assert data['done'] and all(item['status']=='skipped' for item in data['answers'] if item['question_id']!='learning_time_budget')
    assert data['answers'][-1]['status']=='answered'
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
    assert read(env)['intake']['shown_count']==5 and not any(question['question_id']=='scope' for question in read(env)['intake']['questions'])
    with env[1].db.session() as session:assert json.loads(session.get(PlanningTaskStateRow,env[4]).intake_json)['legacy_history']==old['questions']


def install_evidence_fault(model, monkeypatch, evidence, *, fix_on_retry=False):
    original=model.generate
    calls=[]
    def generate(messages, **options):
        result=original(messages,**options)
        schema=synthetic_output_schema(options)
        if 'ready_to_plan' not in schema.get('properties',{}):return result
        output=json.loads(result['content'])
        context=model.interview_contexts[-1]
        if context['shown_count']:
            calls.append(messages)
            ids=['q2'] if fix_on_retry and len(calls)>1 else evidence
            output['learner_profile']['prior_experience']=[{'domain':'合成领域','level':'independent',
                'experience':'合成未验证经验','familiar_concepts':['合成未验证概念'],'evidence_question_ids':ids}]
            output['learner_profile']['knowledge_transfers']=[{'source':'合成领域','target':'合成目标',
                'boundary':'合成边界','evidence_question_ids':ids}]
        return synthetic_output_response(output,options)
    monkeypatch.setattr(model,'generate',generate)
    return calls


@pytest.mark.parametrize('evidence',[[],['not_answered'],['q3']])
def test_repeated_evidence_failure_downgrades_only_unsupported_claims(env,monkeypatch,evidence):
    run(env)
    calls=install_evidence_fault(env[5],monkeypatch,evidence)
    pending=read(env)['intake']['questions']
    values=[{'question_id':q['question_id'],'selections':['a'] if q['question_id']!='q3' else [],
        'skipped':q['question_id']=='q3'} for q in pending]
    response=env[0].post('/api/planning/tasks/'+env[4]+'/decision',headers=env[3],json={'kind':'intake','answers':with_time_answer(values)})
    assert response.json()['task_status']=='succeeded',response.text
    assert len(calls)==2
    data=read(env)['intake'];profile=data['learner_profile'];prior=profile['prior_experience'][0]
    assert prior['level']=='unknown' and prior['experience']=='' and prior['familiar_concepts']==[]
    assert prior['evidence_question_ids']==[] and profile['knowledge_transfers']==[]
    assert len(data['history'])==5 and len(data['answers'])==5 and data['answers'][2]['status']=='skipped'
    assert profile['detail_status']=='confirmed'
    with env[1].db.session() as session:
        assert json.loads(session.get(PlanningTaskStateRow,env[4]).intake_json)['assumptions']
    policy=env[5].interview_contexts[-1]['evidence_policy']
    assert 'q3' not in policy['available_question_ids'] and 'original_request' in policy['available_question_ids']
    retry=json.dumps(calls[-1],ensure_ascii=False)
    assert 'evidence_question_ids' in retry and 'unknown' in retry


def test_model_can_correct_evidence_on_retry_without_losing_confirmed_mastery(env,monkeypatch):
    run(env);calls=install_evidence_fault(env[5],monkeypatch,[],fix_on_retry=True)
    assert answer(env).json()['task_status']=='succeeded'
    profile=read(env)['intake']['learner_profile']
    assert len(calls)==2 and profile['prior_experience'][0]['level']=='independent'
    assert profile['prior_experience'][0]['evidence_question_ids']==['q2']
    assert profile['knowledge_transfers'][0]['evidence_question_ids']==['q2']


def test_repair_does_not_finish_followups_or_fabricate_answers(env,monkeypatch):
    env[5].followups=2;run(env);calls=install_evidence_fault(env[5],monkeypatch,[])
    assert answer(env).json()['wait_state']=='awaiting_intake'
    data=read(env)['intake']
    assert len(calls)==2 and not data['done'] and data['shown_count']==7
    assert len(data['questions'])==2 and len(data['answers'])==5
    assert data['learner_profile']['prior_experience'][0]['level']=='unknown'


def install_detail_fault(model,monkeypatch,fault='omitted',*,invalid_first=False):
    original=model.generate;calls=[]
    def generate(messages,**options):
        result=original(messages,**options)
        if 'ready_to_plan' not in synthetic_output_schema(options).get('properties',{}):return result
        if not model.interview_contexts[-1]['shown_count']:return result
        calls.append(messages);output=json.loads(result['content']);profile=output['learner_profile']
        if fault=='omitted':
            profile.pop('detail_status',None);profile.pop('detail_preference',None)
        elif fault=='unknown':profile['detail_status']='unknown'
        elif fault=='assumed':profile['detail_status']='assumed'
        elif fault=='empty':profile['detail_preference']=''
        elif fault=='whitespace':profile['detail_preference']=' \n\t '
        if invalid_first and len(calls)==1:output.pop('ready_to_plan')
        return synthetic_output_response(output,options)
    monkeypatch.setattr(model,'generate',generate)
    return calls


@pytest.mark.parametrize('fault',['omitted','unknown','assumed','empty','whitespace'])
def test_answered_detail_is_recovered_without_model_retry_and_reaches_planning(env,monkeypatch,fault):
    run(env);calls=install_detail_fault(env[5],monkeypatch,fault)
    pending=read(env)['intake']['questions'];free='基础概念简要介绍，只把项目实践展开讲。'
    values=[{'question_id':q['question_id'],'selections':['a'],
        'free_text':free if q['purpose']=='detail_preference' else ''} for q in pending]
    response=env[0].post('/api/planning/tasks/'+env[4]+'/decision',headers=env[3],json={'kind':'intake','answers':with_time_answer(values)})
    assert response.json()['task_status']=='succeeded',response.text
    data=read(env)['intake'];profile=data['learner_profile']
    assert data['done'] and len(calls)==1 and len(data['history'])==len(data['answers'])==5
    assert profile['detail_status']=='confirmed'
    assert profile['detail_preference']=='补充（优先）：'+free+'；选择：'+pending[0]['options'][0]['label']
    assert data['answers'][0]['free_text']==free and data['answers'][0]['selections']==['a']
    assert data['answers'][0]['labels']==[pending[0]['options'][0]['label']]
    assert any(profile['detail_preference'] in json.dumps(messages,ensure_ascii=False) for messages in env[5].plan_messages)
    assert read(env)['draft_id']


@pytest.mark.parametrize('invalid_first',[False,True])
def test_six_answers_with_evidence_and_detail_faults_finish_within_shared_budget(env,monkeypatch,invalid_first):
    env[5].followups=2;run(env);assert answer(env).json()['wait_state']=='awaiting_intake'
    evidence_calls=install_evidence_fault(env[5],monkeypatch,[])
    detail_calls=install_detail_fault(env[5],monkeypatch,invalid_first=invalid_first)
    response=answer(env);assert response.json()['task_status']=='succeeded',response.text
    data=read(env)['intake'];profile=data['learner_profile']
    assert len(detail_calls)==len(evidence_calls)==(3 if invalid_first else 2)
    assert data['done'] and data['shown_count']==7 and not data['questions']
    assert len(data['history'])==len(data['answers'])==7 and all(a['status']=='answered' for a in data['answers'])
    assert profile['detail_status']=='confirmed' and profile['detail_preference']=='选择：'+data['answers'][0]['labels'][0]
    assert profile['prior_experience'][0]['level']=='unknown' and not profile['knowledge_transfers']
    assert read(env)['draft_id'] and env[5].plan_messages


def test_free_text_only_detail_keeps_full_answer_and_bounds_profile_summary(env,monkeypatch):
    run(env);calls=install_detail_fault(env[5],monkeypatch)
    free='先给简短总览，再详解示例。'*70
    values=[{'question_id':q['question_id'],'selections':[] if q['purpose']=='detail_preference' else ['a'],
        'free_text':free if q['purpose']=='detail_preference' else ''} for q in read(env)['intake']['questions']]
    response=env[0].post('/api/planning/tasks/'+env[4]+'/decision',headers=env[3],json={'kind':'intake','answers':with_time_answer(values)})
    assert response.json()['task_status']=='succeeded',response.text
    data=read(env)['intake'];profile=data['learner_profile']
    assert len(calls)==1 and profile['detail_status']=='confirmed' and len(profile['detail_preference'])==400
    assert profile['detail_preference']==('补充（优先）：'+free)[:400]
    assert data['answers'][0]['free_text']==free and not data['answers'][0]['labels']
    assert any(free in json.dumps(messages,ensure_ascii=False) for messages in env[5].plan_messages)


def test_skipped_detail_stays_assumed_even_when_other_questions_are_answered(env,monkeypatch):
    run(env);install_detail_fault(env[5],monkeypatch,'empty')
    values=[{'question_id':q['question_id'],'selections':[] if q['purpose']=='detail_preference' else ['a'],
        'skipped':q['purpose']=='detail_preference'} for q in read(env)['intake']['questions']]
    response=env[0].post('/api/planning/tasks/'+env[4]+'/decision',headers=env[3],json={'kind':'intake','answers':with_time_answer(values)})
    assert response.json()['task_status']=='succeeded',response.text
    data=read(env)['intake'];assert data['learner_profile']['detail_status']=='assumed'
    assert data['answers'][0]['status']=='skipped' and not data['answers'][0]['labels']


def test_failed_six_answer_task_recovers_saved_detail_without_reasking(env,monkeypatch):
    env[5].followups=2;run(env);answer(env)
    with monkeypatch.context() as patch:
        original=env[5].generate
        def invalid(messages,**options):
            result=original(messages,**options)
            if 'ready_to_plan' in synthetic_output_schema(options).get('properties',{}):
                output=json.loads(result['content']);output.pop('ready_to_plan')
                return synthetic_output_response(output,options)
            return result
        patch.setattr(env[5],'generate',invalid)
        assert answer(env).json()['task_status']=='failed'
    before=read(env)['intake']
    assert len(before['answers'])==7 and not before['done'] and not before['questions']
    with env[1].db.session() as session:
        row=session.get(PlanningTaskStateRow,env[4]);data=json.loads(row.intake_json)
        data['learner_profile'].update(detail_status='unknown',detail_preference='')
        row.intake_json=json.dumps(data,ensure_ascii=False)
    install_evidence_fault(env[5],monkeypatch,[]);calls=install_detail_fault(env[5],monkeypatch)
    assert run(env).json()['task_status']=='succeeded'
    after=read(env)['intake']
    assert after['done'] and after['shown_count']==7 and len(calls)==2
    assert after['answers']==before['answers'] and after['history']==before['history']
    assert after['learner_profile']['detail_status']=='confirmed' and after['learner_profile']['detail_preference']=='选择：'+before['answers'][0]['labels'][0]


@pytest.mark.parametrize('time_value',[
    None,{}, {'weeks':0,'hours_per_week':6}, {'weeks':8.5,'hours_per_week':6},
    {'weeks':105,'hours_per_week':6}, {'weeks':8,'hours_per_week':0},
    {'weeks':8,'hours_per_week':81}, {'weeks':True,'hours_per_week':6},
    {'weeks':'8','hours_per_week':6}, {'weeks':8,'hours_per_week':True},
])
def test_time_budget_invalid_submission_is_atomic(env,time_value):
    run(env);before=read(env)
    response=env[0].post('/api/planning/tasks/'+env[4]+'/decision',headers=env[3],
        json={'kind':'intake','answers':[{'question_id':'q1','selections':['a']},
            {'question_id':'learning_time_budget','time_budget':time_value}]})
    assert response.status_code==400
    after=read(env)
    assert after['intake']==before['intake'] and after['constraints']==before['constraints']


@pytest.mark.parametrize('values',[
    [], [{'question_id':'q1','selections':['a']}],
    [{'question_id':'learning_time_budget','skipped':True,'time_budget':{'weeks':8,'hours_per_week':6}}],
])
def test_time_budget_cannot_be_omitted_or_skipped(env,values):
    run(env);before=read(env)['intake']
    response=env[0].post('/api/planning/tasks/'+env[4]+'/decision',headers=env[3],
        json={'kind':'intake','answers':values})
    assert response.status_code==400 and read(env)['intake']==before


def test_time_budget_partial_draft_refresh_and_downstream_budget(env):
    run(env);path='/api/planning/tasks/'+env[4]
    draft=[{'question_id':'learning_time_budget','time_budget':{'weeks':12,'hours_per_week':''}}]
    saved=env[0].put(path+'/answer-draft',headers=env[3],json={'revision':0,'answers':draft})
    assert saved.status_code==200 and saved.json()['answers'][0]['time_budget']=={'weeks':12}
    assert read(env)['answer_draft'][0]['time_budget']=={'weeks':12}
    pending=read(env)['intake']['questions']
    values=with_time_answer([{'question_id':q['question_id'],'selections':['a']} for q in pending])
    values[-1]['time_budget']={'weeks':12,'hours_per_week':3.5}
    response=env[0].post(path+'/decision',headers=env[3],json={'kind':'intake','answers':values})
    assert response.status_code==200 and response.json()['task_status']=='succeeded',response.text
    state=read(env)
    assert state['time_budget']=={'weeks':12,'hours_per_week':3.5,'total_hours':42}
    assert state['intake']['answers'][-1]['labels']==['12周，每周3.5小时']
    assert state['scope_contract']['time_budget']['total_hours']==42
    assert env[5].interview_contexts[-1]['time_budget']['total_hours']==42
    assert not state['answer_draft']


def test_time_budget_draft_can_be_corrected_while_typing(env):
    run(env);path='/api/planning/tasks/'+env[4]+'/answer-draft'
    for revision,week in enumerate([0,'',12]):
        response=env[0].put(path,headers=env[3],json={'revision':revision,'answers':[
            {'question_id':'learning_time_budget','time_budget':{'weeks':week}}]})
        assert response.status_code==200
    assert read(env)['answer_draft'][0]['time_budget']=={'weeks':12}


def test_existing_four_question_interview_keeps_legacy_budget(env):
    pending=questions()
    with env[1].db.session() as session:
        row=session.get(PlanningTaskStateRow,env[4])
        row.intake_json=json.dumps({'schema_version':3,'questions':pending,'history':pending,
            'answers':[],'shown_count':4,'done':False},ensure_ascii=False)
        row.wait_state='awaiting_intake'
    assert answer(env).json()['task_status']=='succeeded'
    assert read(env)['intake']['shown_count']==4 and read(env)['intake']['max_questions']==6


@pytest.mark.parametrize('value',[float('nan'),float('inf'),-float('inf')])
def test_time_budget_nonfinite_is_rejected(value):
    from backend.planning.intake import validate_time_answer
    from backend.planning.errors import PlanningError
    with pytest.raises(PlanningError):
        validate_time_answer({'weeks':8,'hours_per_week':value})
