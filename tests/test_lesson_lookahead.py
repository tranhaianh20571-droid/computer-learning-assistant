"""Default cross-lesson lookahead uses isolated PostgreSQL and synthetic providers."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from backend.planning.models import ConversationRow, CourseRow, CourseUnitRow, LessonProgressRow, LessonRow, utcnow
from backend.slice1.models import ServiceConfigRow
from backend.tasks.service import LearningTaskRow
from backend.teaching.background import TeachingBackground
from backend.teaching.learning_models import PreparedFirstSegmentRow
from backend.teaching.models import TeachingStepRow
from test_native_teaching_authorization import env  # noqa: F401


@pytest.fixture
def automatic(env):
    env.service.authorization.automatic_enabled=True
    return env


def next_lesson(env, *, new_unit=False):
    with env.db.session() as sess:
        first=sess.get(LessonRow,env.lesson)
        unit_id=first.unit_id
        if new_unit:
            unit_id=first.unit_id+'_second'
            sess.add(CourseUnitRow(unit_id=unit_id,outline_version_id=first.outline_version_id,
                order_index=1,title_ref='Synthetic second unit'))
            sess.flush()
        ident=env.lesson+'_next'
        sess.add(LessonRow(lesson_id=ident,outline_version_id=first.outline_version_id,unit_id=unit_id,
            order_index=0 if new_unit else first.order_index+1,title_ref='Synthetic next lecture',goal_ref='Practice lookup'))
    return ident


def start(env, lesson=None):
    lesson=lesson or env.lesson
    request=env.service.authorization.automatic_request(env.course,lesson,actor_id=env.owner)
    return env.service.confirm_lesson(env.course,lesson,actor_id=env.owner,**request)


def caches(env):
    with env.db.session() as sess:
        return list(sess.scalars(select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.owner_user_id==env.owner,
            PreparedFirstSegmentRow.course_id==env.course).order_by(PreparedFirstSegmentRow.created_at)))


def test_unstarted_course_default_first_cache_does_not_start_learning(automatic):
    next_lesson(automatic)
    result=automatic.service.preparation.prepare_course(automatic.course,automatic.owner)
    assert result['lesson_id']==automatic.lesson and result['status']=='queued'
    with automatic.db.session() as sess:
        assert not sess.scalar(select(ConversationRow.conversation_id).where(ConversationRow.owner_user_id==automatic.owner))
        assert not sess.scalar(select(LessonProgressRow.progress_id).where(LessonProgressRow.owner_user_id==automatic.owner))
    assert automatic.calls==[]


@pytest.mark.parametrize('new_unit',[False,True])
def test_first_publication_prepares_next_lecture_without_overwriting_current(automatic,new_unit):
    upcoming=next_lesson(automatic,new_unit=new_unit)
    warm=automatic.service.preparation.prepare_course(automatic.course,automatic.owner)
    assert automatic.service.preparation.run(warm['task_id'])['status']=='succeeded'
    original=caches(automatic)[0]
    started=start(automatic)
    rows=caches(automatic)
    assert len(rows)==2 and {row.lesson_id for row in rows}=={automatic.lesson,upcoming}
    current=next(row for row in rows if row.lesson_id==automatic.lesson)
    following=next(row for row in rows if row.lesson_id==upcoming)
    assert current.cache_id==original.cache_id and current.candidate_json==original.candidate_json
    assert current.consumed_conversation_id==started['conversation_id'] and not following.consumed_conversation_id
    assert following.status=='queued'
    with automatic.db.session() as sess:
        assert not sess.scalar(select(LessonProgressRow.progress_id).where(LessonProgressRow.lesson_id==upcoming))
        assert not sess.scalar(select(ConversationRow.conversation_id).where(ConversationRow.lesson_id==upcoming))
    assert automatic.service.preparation.prepare_next(started['conversation_id'],automatic.owner)['task_id']==following.task_id
    assert len([call for call in automatic.calls if call[0]=='content'])==1


def test_last_lecture_stops_cross_lecture_lookahead(automatic):
    started=start(automatic)
    assert automatic.service.run_step_generation(started['task_id'])['status']=='succeeded'
    assert automatic.service.preparation.prepare_next(started['conversation_id'],automatic.owner)['status']=='completed'
    assert not caches(automatic)


def test_repeated_start_and_reconcile_are_idempotent(automatic):
    upcoming=next_lesson(automatic)
    started=start(automatic)
    assert automatic.service.run_step_generation(started['task_id'])['status']=='succeeded'
    repeated=start(automatic)
    assert repeated['conversation_id']==started['conversation_id']
    existing=next(row for row in caches(automatic) if row.lesson_id==upcoming)
    for _ in range(3):
        assert automatic.service.preparation.prepare_course(automatic.course,automatic.owner)['task_id']==existing.task_id
    assert len([call for call in automatic.calls if call[0]=='content'])==1
    with automatic.db.session() as sess:
        assert sess.scalar(select(func.count()).select_from(ConversationRow).where(ConversationRow.owner_user_id==automatic.owner))==1


def test_pending_cold_first_is_not_generated_twice(automatic):
    started=start(automatic)
    assert automatic.service.preparation.prepare_course(automatic.course,automatic.owner)['status']=='deferred'
    assert not caches(automatic)
    assert automatic.calls==[]
    assert automatic.service.run_step_generation(started['task_id'])['status']=='succeeded'


def test_voice_version_creates_new_cache_and_keeps_previous_text(automatic):
    first=automatic.service.preparation.prepare_course(automatic.course,automatic.owner)
    assert automatic.service.preparation.run(first['task_id'])['status']=='succeeded'
    original=caches(automatic)[0]
    with automatic.db.session() as sess:sess.get(ServiceConfigRow,automatic.voice).config_version+=1
    second=automatic.service.preparation.prepare_course(automatic.course,automatic.owner)
    assert second['task_id']!=first['task_id']
    rows=caches(automatic)
    assert len(rows)==2 and rows[0].cache_id==original.cache_id and rows[0].candidate_json==original.candidate_json
    assert rows[0].audio_key!=rows[1].audio_key


def test_changed_content_keys_do_not_reuse_old_cache(automatic):
    warm=automatic.service.preparation.prepare_course(automatic.course,automatic.owner)
    assert automatic.service.preparation.run(warm['task_id'])['status']=='succeeded'
    previous=caches(automatic)[0]
    with automatic.db.session() as sess:sess.get(ServiceConfigRow,automatic.content).config_version+=1
    following=automatic.service.preparation.prepare_course(automatic.course,automatic.owner)
    assert following['task_id']!=warm['task_id']
    rows=caches(automatic)
    assert len(rows)==2 and rows[0].text_key!=rows[1].text_key and rows[0].cache_id==previous.cache_id


def test_expired_version_is_not_claimed_for_start(automatic):
    warm=automatic.service.preparation.prepare_course(automatic.course,automatic.owner)
    assert automatic.service.preparation.run(warm['task_id'])['status']=='succeeded'
    with automatic.db.session() as sess:
        row=sess.scalar(select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.task_id==warm['task_id']))
        row.expires_at=utcnow()-timedelta(seconds=1)
        course=sess.get(CourseRow,automatic.course);lesson=sess.get(LessonRow,automatic.lesson)
        assert automatic.service.preparation.claim_for_start(sess,course,lesson) is None


def test_text_can_be_consumed_during_speech_and_no_duplicate_tts(automatic):
    began,release=Event(),Event()
    old=automatic.service.tts_adapter_factory
    calls=[]
    def factory(owner,task_id):
        def synth(text):
            calls.append(task_id);began.set();assert release.wait(10)
            return {'audio_hex':b'synthetic delayed audio'.hex(),'subtitle':[{'begin_ms':0,'end_ms':1000,'text':text}]}
        return SimpleNamespace(synthesize=synth,close=lambda:None)
    factory.config_for_owner=old.config_for_owner
    factory.disclosures=old.disclosures
    factory.uses_saved_config=True
    automatic.service.tts_adapter_factory=factory
    warm=automatic.service.preparation.prepare_course(automatic.course,automatic.owner)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(automatic.service.preparation.run,warm['task_id'])
        try:
            assert began.wait(5)
            assert caches(automatic)[0].status=='text_ready'
            started=start(automatic)
            workspace=automatic.service.get_workspace(started['conversation_id'],actor_id=automatic.owner)
            assert started['cache_hit'] and len(workspace['steps'])==1 and not workspace['steps'][0]['audio_ready']
            assert automatic.service.ensure_audio(started['conversation_id'],actor_id=automatic.owner,step_no=1)['status']=='preparing'
            assert len(calls)==1
        finally:release.set()
        assert future.result(timeout=10)['status']=='succeeded'
    workspace=automatic.service.get_workspace(started['conversation_id'],actor_id=automatic.owner)
    assert workspace['steps'][0]['audio_ready'] and len(calls)==1
    assert len([call for call in automatic.calls if call[0]=='content'])==1


@pytest.mark.parametrize('change',['version','revoke'])
def test_voice_change_during_synthesis_preserves_text_and_rejects_late_audio(automatic,change):
    from backend.slice1.models import DisclosureGrantRow
    old=automatic.service.tts_adapter_factory
    def factory(owner,task_id):
        def synth(text):
            with automatic.db.session() as sess:
                if change=='version':sess.get(ServiceConfigRow,automatic.voice).config_version+=1
                else:
                    grant=sess.scalar(select(DisclosureGrantRow).where(DisclosureGrantRow.task_id==task_id,
                        DisclosureGrantRow.content_category=='audio_text'))
                    grant.revoked_at=utcnow()
            return {'audio_hex':b'synthetic rejected audio'.hex(),'subtitle':[{'text':text,'begin_ms':0,'end_ms':1000}]}
        return SimpleNamespace(synthesize=synth,close=lambda:None)
    factory.config_for_owner=old.config_for_owner;factory.disclosures=old.disclosures;factory.uses_saved_config=True
    automatic.service.tts_adapter_factory=factory
    warm=automatic.service.preparation.prepare_course(automatic.course,automatic.owner)
    assert automatic.service.preparation.run(warm['task_id'])['status']=='succeeded'
    row=caches(automatic)[0]
    assert row.candidate_json!='{}' and row.status=='text_ready' and not row.audio_object_key
    assert automatic.blobs=={}
    started=start(automatic)
    workspace=automatic.service.get_workspace(started['conversation_id'],actor_id=automatic.owner)
    assert started['cache_hit'] and len(workspace['steps'])==1 and not workspace['steps'][0]['audio_ready']
    assert len([call for call in automatic.calls if call[0]=='content'])==1


def test_server_reconcile_is_bounded_and_ignores_unconfigured_owners(automatic):
    service=automatic.service
    with automatic.db.session() as sess:
        extra=CourseRow(course_id='zz_unconfigured_'+automatic.course,owner_user_id='other-owner',
            current_outline_version_id=None,title_ref='Synthetic ignored course')
        sess.add(extra)
    outcomes=service.preparation.reconcile(limit=1)
    assert len(outcomes)<=1
    # Direct same-key discovery cannot multiply queued work.
    for _ in range(3):service.preparation.prepare_course(automatic.course,automatic.owner)
    assert len(caches(automatic))==1
    assert automatic.calls==[]


def test_interactive_generation_has_priority_over_background_prewarm(automatic,monkeypatch):
    service=automatic.service
    warm=service.preparation.prepare_course(automatic.course,automatic.owner)
    started=start(automatic)
    background=TeachingBackground(service)
    submitted=[]
    monkeypatch.setattr(background,'_execute',lambda task_id,group:submitted.append((task_id,group)))
    try:
        background.tick()
        for _,future,_ in list(background.pending.values()):future.result(timeout=5)
        assert submitted and submitted[0][0]==warm['task_id']
        # Current first waits for the same promoted warm task, rather than issuing a cold duplicate.
        with automatic.db.session() as sess:
            assert sess.get(LearningTaskRow,warm['task_id']).lane=='interactive'
        assert service.tasks.get_task(started['task_id'])['status']=='queued'
    finally:background.close()
