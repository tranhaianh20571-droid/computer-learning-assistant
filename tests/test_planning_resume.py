"""Synthetic PostgreSQL planning recovery, cancellation and late-publication checks."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep
import importlib.util
from pathlib import Path
import uuid
import pytest
from sqlalchemy import select, text, inspect
from alembic.migration import MigrationContext
from alembic.operations import Operations
from backend.planning.models import PlanningTaskStateRow, PlanningDraftRow
from backend.logging_audit.models import TaskStateRow, TaskEventRow
from backend.tasks.service import LearningTaskRow, LeaseError
from backend.planning.errors import PlanningError
from common_fixtures import make_user, auth_headers
from test_v2_intake import database, env as interview_env, run, read, answer


@pytest.fixture
def env(interview_env):
    yield interview_env
    interview_env[1].planning.background.close()
    with interview_env[1].db.session() as sess:
        for row in sess.scalars(select(PlanningTaskStateRow).where(
                PlanningTaskStateRow.owner_user_id == interview_env[2])):
            row.background_enabled = False


def endpoint(env, suffix):
    return '/api/planning/tasks/'+env[4]+suffix


def wait_for(env, predicate):
    end=monotonic()+10
    while monotonic()<end:
        state=read(env)
        if predicate(state):return state
        sleep(.03)
    pytest.fail('planning state did not settle')


def test_current_is_read_only_and_owner_scoped(env):
    run(env)
    for _ in range(3):
        result=env[0].get('/api/planning/current',headers=env[3]).json()['planning']
        assert result['task_id']==env[4] and len(result['intake']['questions'])==5
    assert len(env[5].interview_contexts)==1
    _,token=make_user(env[0],env[1].db)
    assert env[0].get('/api/planning/current',headers=auth_headers(token)).json()=={'planning':None}
    for suffix,body in [('/resume',{'automatic':True}),('/abandon',{})]:
        assert env[0].post(endpoint(env,suffix),headers=auth_headers(token),json=body).status_code==404


def test_answer_draft_persists_conflicts_and_does_not_submit(env):
    run(env)
    values=[{'question_id':'q1','selections':['a'],'free_text':'合成补充','editing_free':True}]
    response=env[0].put(endpoint(env,'/answer-draft'),headers=env[3],json={'revision':0,'answers':values})
    assert response.status_code==200 and response.json()['revision']==1
    state=read(env)
    assert state['answer_draft'][0]['free_text']=='合成补充' and not state['intake']['answers']
    assert env[0].put(endpoint(env,'/answer-draft'),headers=env[3],json={'revision':0,'answers':[]}).status_code==409
    assert env[0].put(endpoint(env,'/answer-draft'),headers=env[3],json={'revision':1,'answers':[{'question_id':'fake'}]}).status_code==400
    _,token=make_user(env[0],env[1].db)
    assert env[0].put(endpoint(env,'/answer-draft'),headers=auth_headers(token),json={'revision':1,'answers':[]}).status_code==404
    assert answer(env).status_code==200
    assert read(env)['answer_draft']==[]


def test_async_execution_and_reentry_do_not_duplicate_calls(env):
    entered,release=Event(),Event();generate=env[5].generate
    def delayed(*args,**kw):
        entered.set();assert release.wait(8);return generate(*args,**kw)
    env[5].generate=delayed
    try:
        response=env[0].post(endpoint(env,'/resume'),headers=env[3],json={'automatic':True})
        assert response.status_code==200 and entered.wait(3)
        for _ in range(3):
            assert env[0].post(endpoint(env,'/resume'),headers=env[3],json={'automatic':True}).status_code==200
        assert read(env)['attempt']==1
    finally:release.set()
    wait_for(env,lambda s:s['wait_state']=='awaiting_intake')
    assert len(env[5].interview_contexts)==1


def fail_task(env,code='MODEL_PROVIDER_ERROR'):
    with env[1].db.session() as sess:
        row=sess.get(LearningTaskRow,env[4]);row.status='failed';row.lease_token=None;row.lease_until=None
        state=sess.get(TaskStateRow,env[4]);state.error_code=code;state.status='failed'


def test_successful_interview_starts_a_new_retry_episode(env, monkeypatch):
    monkeypatch.setattr(env[1].planning.background, "start", lambda: None)
    fail_task(env)
    planning = env[1].planning
    first = planning.workspace.resume(env[4], env[2], automatic=True, dispatch=False)
    assert first["auto_retry_count"] == 1
    planning.run_task(env[4])
    assert read(env)["wait_state"] == "awaiting_intake"
    assert read(env)["auto_retry_count"] == 0
    with env[1].db.session() as session:
        session.get(PlanningTaskStateRow, env[4]).auto_retry_count = 5
    assert answer(env).status_code == 200
    assert read(env)["auto_retry_count"] == 0
    fail_task(env)
    second = planning.workspace.resume(env[4], env[2], automatic=True, dispatch=False)
    assert second["auto_retry_count"] == 1


def test_automatic_retry_budget_is_durable_and_concurrent(env,monkeypatch):
    fail_task(env);monkeypatch.setattr(env[1].planning.background,'start',lambda:None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:env[1].planning.workspace.resume(env[4],env[2],automatic=True),range(2)))
    assert all(r['auto_retry_count']==1 for r in responses)
    assert read(env)['generation']==2
    for count in range(2, 6):
        fail_task(env)
        snapshot = env[1].planning.workspace.resume(env[4],env[2],automatic=True)
        assert snapshot['auto_retry_count'] == count and snapshot['task_status'] == 'queued'
    fail_task(env)
    for _ in range(3):
        env[1].planning.workspace.resume(env[4],env[2],automatic=True)
    assert read(env)['task_status']=='failed' and read(env)['generation']==6
    assert read(env)['auto_retry_count']==5 and read(env)['auto_retry_exhausted']
    env[1].planning.workspace.resume(env[4],env[2],automatic=False)
    assert read(env)['task_status']=='queued' and read(env)['auto_retry_count']==5


def test_credentials_failure_never_auto_retries(env,monkeypatch):
    fail_task(env,'MODEL_CREDENTIAL_INVALID');monkeypatch.setattr(env[1].planning.background,'start',lambda:None)
    snapshot=env[1].planning.workspace.resume(env[4],env[2],automatic=True)
    assert snapshot['task_status']=='failed' and snapshot['auto_retry_count']==0


def test_abandon_is_terminal_idempotent_and_does_not_restore_older_task(env):
    run(env)
    result=env[0].post(endpoint(env,'/abandon'),headers=env[3]);assert result.status_code==200
    assert env[0].post(endpoint(env,'/abandon'),headers=env[3]).status_code==200
    assert env[0].get('/api/planning/current',headers=env[3]).json()=={'planning':None}
    assert read(env)['task_status']=='cancelled'
    assert env[0].post(endpoint(env,'/resume'),headers=env[3],json={}).status_code==409
    assert env[0].post(endpoint(env,'/run'),headers=env[3]).status_code==409
    assert env[0].post(endpoint(env,'/decision'),headers=env[3],json={'kind':'intake','answers':[]}).status_code==409
    with env[1].db.session() as sess:
        events=list(sess.scalars(select(TaskEventRow).where(TaskEventRow.task_id==env[4],TaskEventRow.status=='cancelled')))
        assert len(events)==1


def test_abandon_during_model_call_rejects_late_interview(env):
    entered,release=Event(),Event();generate=env[5].generate
    def delayed(*args,**kw):
        entered.set();assert release.wait(8);return generate(*args,**kw)
    env[5].generate=delayed
    try:
        env[0].post(endpoint(env,'/resume'),headers=env[3],json={});assert entered.wait(3)
        assert env[0].post(endpoint(env,'/abandon'),headers=env[3]).status_code==200
    finally:release.set()
    env[1].planning.background.close()
    state=read(env);assert state['abandoned'] and not state['intake']['history'] and not state['draft_id']
    with env[1].db.session() as sess:
        assert sess.get(TaskStateRow,env[4]).status=='cancelled'


def test_abandoned_draft_cannot_be_saved_or_confirmed(env):
    run(env);answer(env);state=read(env);draft=env[1].drafts.get_draft(state['draft_id'],actor_id=env[2])
    env[1].planning.workspace.abandon(env[4],env[2])
    with pytest.raises(PlanningError) as exc:env[1].drafts.save_draft(draft['draft_id'],actor_id=env[2],revision=draft['revision'],content=draft['content'])
    assert exc.value.code=='TASK_CANCELLED'
    response=env[0].post('/api/planning/drafts/'+draft['draft_id']+'/confirm',headers=env[3],json={'revision':draft['revision'],'idempotency_key':uuid.uuid4().hex})
    assert response.status_code==409


def test_confirmed_course_is_protected(env):
    run(env);answer(env);state=read(env);draft=env[1].drafts.get_draft(state['draft_id'],actor_id=env[2])
    response=env[0].post('/api/planning/drafts/'+draft['draft_id']+'/confirm',headers=env[3],json={'revision':draft['revision'],'idempotency_key':uuid.uuid4().hex})
    assert response.status_code==200,response.text
    assert env[0].post(endpoint(env,'/abandon'),headers=env[3]).status_code==409
    assert read(env)['confirmed']


def test_migration_preserves_existing_planning_and_downgrades(database):
    schema='planning_resume_migration_'+uuid.uuid4().hex[:10]
    path=Path(__file__).parent/'support/backend/alembic/versions/h2b3c4d5e6f7_planning_resume.py'
    spec=importlib.util.spec_from_file_location('planning_resume_migration',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    with database.engine.begin() as conn:
        conn.execute(text('CREATE SCHEMA '+schema));conn.execute(text('SET LOCAL search_path TO '+schema))
        conn.execute(text('CREATE TABLE planning_task_states (task_id text PRIMARY KEY, intake_json text)'))
        conn.execute(text("INSERT INTO planning_task_states VALUES ('synthetic-task','{\"answers\":[]}')"))
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            row=conn.execute(text('SELECT * FROM planning_task_states')).mappings().one()
            assert row['intake_json']=='{"answers":[]}' and row['auto_retry_count']==0 and row['answer_draft_json']=='[]'
            module.downgrade()
            assert {c['name'] for c in inspect(conn).get_columns('planning_task_states',schema=schema)}=={'task_id','intake_json'}
        conn.execute(text('DROP SCHEMA '+schema+' CASCADE'))
