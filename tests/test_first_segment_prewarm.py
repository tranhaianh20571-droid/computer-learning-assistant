"""Owned first-segment cache, learning progress, cost bounds and server lookahead."""
import json
import os
import uuid
import pytest
from datetime import timedelta
from threading import Event
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from sqlalchemy import select,func,create_engine,text
from sqlalchemy.engine import make_url
from common_fixtures import make_user,auth_headers,synthetic_output_schema
from test_teaching_assessments import env,LearningAdapter,finish  # noqa: F401
from backend.db import Database
from backend.logging_audit.db import get_database_url
from test_course_library import draft,confirm
from backend.planning.models import ConversationRow,LessonProgressRow,utcnow
from backend.teaching.learning_models import PreparedFirstSegmentRow,PreparedLessonPlanRow,LessonBlueprintRow
from backend.agents.task_binding import AgentBudgetRow
from backend.tasks.service import LearningTaskRow
from backend.teaching.background import TeachingBackground

OUTLINE={"title":"合成缓存课程","units":[{"title":"基础","lessons":[
    {"title":"键值关系","goal":"理解键值查找"},{"title":"更新映射","goal":"理解按键更新"}]}],"prerequisites":[]}


@pytest.fixture(scope="module")
def database():
    """Background capacity assertions need an owned queue without other suites' leases."""
    url = make_url(get_database_url())
    schema = "prewarm_validation_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = url.update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(hide_password=False)
    previous = {name: os.environ.get(name) for name in ("LEARNING_DATABASE_URL", "DATABASE_URL")}
    os.environ.update(LEARNING_DATABASE_URL=isolated, DATABASE_URL=isolated)
    db = Database(isolated)
    try:
        from backend.slice3_app import Slice3State
        Slice3State(db)
        db.create_all()
        yield db
    finally:
        db.dispose()
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def setup(env,blocked=None):
    client,state,owner,headers=env;calls=[]
    class Adapter(LearningAdapter):
        def generate(self,messages,**options):
            properties=synthetic_output_schema(options).get('properties',{})
            calls.append('refinement' if 'goals' in properties else 'teaching')
            if blocked:blocked[0].set();blocked[1].wait(10)
            return super().generate(messages,**options)
    factory=lambda *args:Adapter()
    factory.config_for_owner=lambda current:{"config_id":"synthetic-model","config_version":1} if current==owner else None
    state.teaching.model_adapter_factory=factory
    course,item=draft(env,OUTLINE,kind='course_outline');assert confirm(env,item).status_code==200
    overview=state.overview.course_overview(course,actor_id=owner)
    # Freeze the real synthetic scope before measuring teaching/cache calls.
    state.teaching.model_adapter_factory=lambda *args:LearningAdapter()
    state.assessments.scopes.kick=lambda task:state.assessments.scopes._run(task)
    try:
        for unit in overview["units"]:
            state.assessments.scopes.generate(course,unit["unit_id"],owner)
            assert state.assessments.scopes.get(course,unit["unit_id"],owner)["status"]=="succeeded"
    finally:
        state.teaching.model_adapter_factory=factory
    return course,overview['units'][0]['lessons'][0]['lesson_id'],calls


def test_preheat_creates_only_first_cache_and_consumption_has_no_second_model_call(env):
    course,lesson,calls=setup(env);client,state,owner,headers=env
    result=state.teaching.preparation.prepare_course(course,owner)
    assert state.teaching.preparation.prepare_course(course,owner)['task_id']==result['task_id']
    assert state.teaching.preparation.run(result['task_id'])['status']=='succeeded'
    assert len(calls)==1
    with state.db.session() as sess:
        assert not sess.scalar(select(func.count()).select_from(ConversationRow).where(ConversationRow.course_id==course))
        assert not sess.scalar(select(func.count()).select_from(LessonProgressRow).where(LessonProgressRow.owner_user_id==owner))
        assert not list(sess.scalars(select(LearningTaskRow).where(LearningTaskRow.kind=='lesson_step_generate',LearningTaskRow.owner_user_id==owner)))
    start=client.post(f'/api/courses/{course}/lessons/{lesson}/start',headers=headers).json()
    assert start['cache_hit'] and len(calls)==1
    workspace=client.get(f"/api/teaching/{start['conversation_id']}",headers=headers).json()
    assert len(workspace['steps'])==1 and workspace['steps'][0]['phase']=='intuition'
    assert not workspace['lesson_finished']
    duplicate=client.post(f'/api/courses/{course}/lessons/{lesson}/start',headers=headers).json()
    assert duplicate['conversation_id']==start['conversation_id'] and len(calls)==1
    stranger,token=make_user(client,state.db)
    assert client.get(f'/api/courses/{course}/first-preparation',headers=auth_headers(token)).status_code==404


def test_start_during_preheat_waits_same_request(env):
    began,release=Event(),Event();course,lesson,calls=setup(env,(began,release));client,state,owner,headers=env
    result=state.teaching.preparation.prepare_course(course,owner)
    with ThreadPoolExecutor(max_workers=1) as pool:
        preparing=pool.submit(state.teaching.preparation.run,result['task_id']);assert began.wait(4)
        start=client.post(f'/api/courses/{course}/lessons/{lesson}/start',headers=headers).json()
        assert client.post(f"/api/teaching/tasks/{start['task_id']}/run",headers=headers).json()['status']=='preparing'
        assert len(calls)==1
        release.set();assert preparing.result(timeout=10)['status']=='succeeded'
    assert client.post(f"/api/teaching/tasks/{start['task_id']}/run",headers=headers).json()['status']=='succeeded'
    assert len(calls)==1


def test_completion_targets_second_lesson_and_daily_cap_does_not_block_start(env):
    course,lesson,calls=setup(env);client,state,owner,headers=env
    result=state.teaching.preparation.prepare_course(course,owner);state.teaching.preparation.run(result['task_id'])
    start=client.post(f'/api/courses/{course}/lessons/{lesson}/start',headers=headers).json();finish(env,start)
    status=state.teaching.preparation.status(course,owner)
    assert status['lesson_id']!=lesson and status['status']=='queued'
    state.teaching.preparation.daily_limit=0
    other,item=draft(env,{**OUTLINE,'title':'合成另一门缓存课程'},kind='course_outline');assert confirm(env,item).status_code==200
    assert state.teaching.preparation.prepare_course(other,owner)['status']=='deferred'
    next_lesson=state.overview.course_overview(other,actor_id=owner)['units'][0]['lessons'][0]['lesson_id']
    assert client.post(f'/api/courses/{other}/lessons/{next_lesson}/start',headers=headers).status_code==201


def test_changed_model_or_expiration_is_not_consumed_and_cleanup_preserves_audio(env):
    course,lesson,calls=setup(env);client,state,owner,headers=env
    result=state.teaching.preparation.prepare_course(course,owner);state.teaching.preparation.run(result['task_id'])
    state.teaching.model_adapter_factory.config_for_owner=lambda owner:{'config_id':'synthetic-model','config_version':2}
    start=client.post(f'/api/courses/{course}/lessons/{lesson}/start',headers=headers).json()
    assert not start['cache_hit']
    with state.db.session() as sess:
        row=sess.scalars(select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.course_id==course)).first()
        row.expires_at=utcnow()-timedelta(seconds=1)
    state.teaching.preparation.cleanup()
    with state.db.session() as sess:assert not sess.scalar(select(PreparedFirstSegmentRow.cache_id).where(PreparedFirstSegmentRow.course_id==course))


def test_server_runs_next_segment_while_first_is_not_displayed_and_pause_stops_more(env):
    course,lesson,calls=setup(env);client,state,owner,headers=env
    start=client.post(f'/api/courses/{course}/lessons/{lesson}/start',headers=headers).json()
    service=state.teaching;service.run_step_generation(start['task_id'])
    background=TeachingBackground(service)
    try:
        background.tick()
        for group,future,subject in list(background.pending.values()):future.result(timeout=10)
        workspace=client.get(f"/api/teaching/{start['conversation_id']}",headers=headers).json()
        assert [step['step_no'] for step in workspace['steps']]==[1,2]
        assert any(task['step_no']==3 for task in workspace['queued_tasks'])
        client.post(f"/api/teaching/{start['conversation_id']}/checkpoint",headers=headers,json={
            'client_seq':1,'step_no':2,'position_ms':0,'board_state':{'presentation':{'paused':True}}})
        with service.db.session() as sess:
            task=service.tasks.create_task(owner_user_id=owner,kind='lesson_step_generate',subject_id=f"{start['conversation_id']}:3:1",
                idempotency_key='synthetic-paused-next',stage='generate',lane='teaching_prefetch')
            item=sess.get(LearningTaskRow,task['task_id']);assert not background.eligible(sess,item)
    finally:background.close()


def test_expired_queue_is_visible_and_managed_retry_is_only_an_acknowledgement(env):
    course,lesson,calls=setup(env);client,state,owner,headers=env
    start=client.post(f'/api/courses/{course}/lessons/{lesson}/start',headers=headers).json()
    service=state.teaching;service.run_step_generation(start['task_id']);service.server_managed=True
    with service.db.session() as sess:
        next_task=sess.scalars(select(LearningTaskRow).where(LearningTaskRow.subject_id==f"{start['conversation_id']}:2:1")).first()
        task_id=next_task.task_id;next_task.deadline_at=utcnow()-timedelta(seconds=1)
        active_plan=sess.get(LessonBlueprintRow,start['conversation_id']).plan_json
    background=TeachingBackground(service)
    try:
        background.tick()
        with service.db.session() as sess:assert sess.get(LearningTaskRow,task_id).status=='expired'
        before=len(calls);teaching_before=calls.count('teaching')
        response=client.post(f'/api/teaching/tasks/{task_id}/run',headers=headers)
        assert response.json()['status']=='queued' and len(calls)==before
        background.tick()
        pending=list(background.pending.values())
        assert sum(group=='refine' for group,future,subject in pending)<=1
        for group,future,subject in pending:future.result(timeout=10)
        assert calls.count('teaching')==teaching_before+1
        assert 1<=calls.count('refinement')<=3
        with service.db.session() as sess:
            refinements=list(sess.scalars(select(LearningTaskRow).where(
                LearningTaskRow.owner_user_id==owner,LearningTaskRow.kind=='lesson_plan_refine')))
            assert len(refinements)==1 and refinements[0].status=='succeeded'
            budget=sess.get(AgentBudgetRow,refinements[0].task_id)
            assert budget.request_limit==3 and 1<=budget.logical_requests<=3
            assert 0<=budget.wire_requests<=3
            cache_key=refinements[0].subject_id.partition(':')[2]
            assert sess.get(PreparedLessonPlanRow,cache_key).source=='agent'
            assert sess.get(LessonBlueprintRow,start['conversation_id']).plan_json==active_plan
    finally:background.close()
