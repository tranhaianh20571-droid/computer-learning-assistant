"""Prepared audio survives transfer, voice-only refresh and cleanup limits."""
from datetime import timedelta
import json
from types import SimpleNamespace
from sqlalchemy import select
from backend.slice1.config_service import ConfigService
from backend.slice1.disclosure_service import DisclosureService
from backend.teaching.tts_config import configured_tts_factory
from backend.teaching.learning_models import PreparedFirstSegmentRow
from backend.tasks.service import LearningTaskRow
from backend.planning.models import CourseRow,utcnow
from backend.teaching.background import TeachingBackground
from test_first_segment_prewarm import setup,OUTLINE,database,env  # noqa: F401
from test_course_library import draft,confirm


def configure(env,monkeypatch):
    client,state,owner,headers=env;calls=[]
    configs=ConfigService(state.db,encryption_key='synthetic-first-key')
    view=configs.create(actor_id=owner,is_admin=False,kind='tts',protocol='minimax',endpoint='https://voice.example.test',
        model_name='speech-2.8-hd',credentials={'api_key':'synthetic-key','voice_id':'voice-one'})
    disclosures=DisclosureService(state.db,state.audit)
    state.teaching.tts_adapter_factory=configured_tts_factory(configs,disclosures)
    content=configs.create(actor_id=owner,is_admin=False,kind='content_model',protocol='openai',
        endpoint='https://model.example.test/v1',model_name='synthetic-content',credentials={'api_key':'synthetic-content-key'})
    state.teaching.model_adapter_factory.config_for_owner=lambda current:configs.get(content['config_id'],actor_id=owner,is_admin=False) if current==owner else None
    state.teaching.configs=configs
    state.teaching.disclosures=disclosures
    state.teaching.tts_adapter_factory.authorize_task=state.teaching._check_authorization
    def build(protocol,**options):
        def synth(text):
            calls.append(options['voice_id'])
            return {'audio_hex':b'synthetic-prepared-audio'.hex(),'subtitle':[{'text':text,'begin_ms':0,'end_ms':1000}],'audio_format':'mp3'}
        return SimpleNamespace(synthesize=synth,close=options['client'].close)
    monkeypatch.setattr('backend.teaching.tts_config.build_adapter',build)
    return configs,view,calls


def approve(env, course, lesson, *, purpose='prewarm'):
    client,_,_,headers=env
    path=f'/api/courses/{course}/lessons/{lesson}'
    preflight=client.get(path+'/authorization-preflight',headers=headers,params={'purpose':purpose})
    assert preflight.status_code==200,preflight.text
    confirmed=client.post(path+'/authorization-confirm',headers=headers,json={
        'fingerprint':preflight.json()['fingerprint'],'purpose':purpose,
        'content_confirmed':True,'tts_confirmed':True,'prewarm_opt_in':True})
    assert confirmed.status_code in (200,201),confirmed.text
    return confirmed.json()


def test_prepared_audio_transfers_without_resynthesis_or_file_copy(env,monkeypatch):
    course,lesson,calls=setup(env);client,state,owner,headers=env
    configs,view,speech=configure(env,monkeypatch)
    approve(env,course,lesson)
    task=state.teaching.preparation.prepare_course(course,owner)['task_id']
    assert state.teaching.preparation.run(task)['status']=='succeeded'
    with state.db.session() as sess:
        row=sess.scalars(select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.course_id==course)).first()
        original_key=row.audio_object_key;assert original_key
    start=approve(env,course,lesson,purpose='start')
    step=client.get(f"/api/teaching/{start['conversation_id']}",headers=headers).json()['steps'][0]
    assert step['audio_ready'] and len(calls)==len(speech)==1
    with state.db.session() as sess:
        row=sess.scalars(select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.course_id==course)).first()
        assert row.audio_object_key=='' and row.audio_bytes==0;row.expires_at=utcnow()-timedelta(seconds=1)
    state.teaching.preparation.cleanup()
    assert state.storage.get(original_key)==b'synthetic-prepared-audio'


def test_voice_change_reuses_prepared_text_and_regenerates_only_audio(env,monkeypatch):
    course,lesson,calls=setup(env);client,state,owner,headers=env
    configs,view,speech=configure(env,monkeypatch)
    approve(env,course,lesson)
    task=state.teaching.preparation.prepare_course(course,owner)['task_id'];state.teaching.preparation.run(task)
    configs.update(view['config_id'],actor_id=owner,is_admin=False,credentials={'api_key':'synthetic-key','voice_id':'voice-two'})
    next_task=approve(env,course,lesson)['task_id'];assert next_task!=task
    assert state.teaching.preparation.run(next_task)['status']=='succeeded'
    assert len(calls)==1 and speech==['voice-one','voice-two']


def test_capacity_eviction_and_expired_lease_recovery(env):
    course,lesson,calls=setup(env);client,state,owner,headers=env
    service=state.teaching;service.preparation.max_courses=1
    first=service.preparation.prepare_course(course,owner);service.preparation.run(first['task_id'])
    second,item=draft(env,{**OUTLINE,'title':'合成容量课程'},kind='course_outline');assert confirm(env,item).status_code==200
    result=service.preparation.prepare_course(second,owner)
    with state.db.session() as sess:
        assert not sess.scalar(select(PreparedFirstSegmentRow.cache_id).where(PreparedFirstSegmentRow.course_id==course))
    lease=service.tasks.claim(result['task_id'],lease_owner='synthetic-old-process',lease_seconds=120)
    with state.db.session() as sess:
        task=sess.get(LearningTaskRow,result['task_id']);task.lease_until=utcnow()-timedelta(seconds=1)
        row=sess.scalars(select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.task_id==result['task_id'])).first()
        data=json.loads(row.context_json);data['_request_count']=0;row.context_json=json.dumps(data)
    background=TeachingBackground(service)
    try:
        background.tick()
        for group,future,subject in background.pending.values():future.result(timeout=10)
        assert service.preparation.status(second,owner)['status']=='ready'
    finally:background.close()
    service.preparation.max_bytes=1;service.preparation.enforce_limits(owner)
    with state.db.session() as sess:assert not sess.scalar(select(PreparedFirstSegmentRow.cache_id).where(PreparedFirstSegmentRow.course_id==second))


def test_deleted_course_cache_cannot_be_read_or_run(env):
    course,lesson,calls=setup(env);client,state,owner,headers=env
    task=state.teaching.preparation.prepare_course(course,owner)['task_id']
    with state.db.session() as sess:sess.get(CourseRow,course).deleted_at=utcnow()
    assert state.teaching.preparation.run(task)['status']=='failed'
    assert not calls
    assert client.get(f'/api/courses/{course}/first-preparation',headers=headers).status_code==404
    state.teaching.preparation.cleanup()


def test_failed_prewarm_is_not_restarted_by_refresh_and_model_requests_are_bounded(env):
    course,lesson,calls=setup(env);client,state,owner,headers=env
    class Invalid:
        def generate(self,*args,**kwargs):calls.append(1);return {'content':'synthetic-invalid-output'}
    factory=lambda *args:Invalid()
    factory.config_for_owner=lambda current:{'config_id':'synthetic-invalid-model','config_version':1} if current==owner else None
    state.teaching.model_adapter_factory=factory
    task=state.teaching.preparation.prepare_course(course,owner)['task_id']
    assert state.teaching.preparation.run(task)['status']=='failed'
    assert len(calls)<=2
    for _ in range(3):assert state.teaching.preparation.prepare_course(course,owner)['status']=='failed'
    with state.db.session() as sess:
        tasks=list(sess.scalars(select(LearningTaskRow).where(LearningTaskRow.owner_user_id==owner,LearningTaskRow.kind=='lesson_first_prepare')))
        assert len(tasks)==1
