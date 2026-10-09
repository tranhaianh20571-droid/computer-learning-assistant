"""Configured owner voice, consent, task-version fence and old-text audio backfill."""
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from backend.slice1.config_service import ConfigService
from backend.slice1.disclosure_service import DisclosureService
from backend.teaching.tts_config import configured_tts_factory
from backend.planning.models import ConversationRow
from backend.teaching.authorization import LessonAuthorizationRow
from backend.teaching.models import AudioAssetRow
from backend.tasks.service import LearningTaskRow
from common_fixtures import make_user,auth_headers
from test_teaching_assessments import env,database,lesson  # noqa: F401


def prepared(env,monkeypatch,subtitle=True,on_synthesize=None):
    _,_,start=lesson(env);client,state,user,headers=env
    client.post(f"/api/teaching/tasks/{start['task_id']}/run",headers=headers)
    configs=ConfigService(state.db,encryption_key="synthetic-audio-key")
    view=configs.create(actor_id=user,is_admin=False,kind="tts",protocol="minimax",
        endpoint="https://voice.example.test",model_name="speech-2.8-hd",
        credentials={"api_key":"synthetic-key","voice_id":"synthetic-voice"})
    disclosures=DisclosureService(state.db,state.audit)
    state.teaching.tts_adapter_factory=configured_tts_factory(configs,disclosures)
    content=configs.create(actor_id=user,is_admin=False,kind='content_model',protocol='openai',
        endpoint='https://model.example.test/v1',model_name='synthetic-model',credentials={'api_key':'synthetic-content-key'})
    state.teaching.configs=configs
    state.teaching.disclosures=disclosures
    state.teaching.tts_adapter_factory.authorize_task=state.teaching._check_authorization
    state.teaching.model_adapter_factory.config_for_owner=lambda current:configs.get(content['config_id'],actor_id=user,is_admin=False) if current==user else None
    with state.db.session() as sess:
        conv=sess.get(ConversationRow,start['conversation_id'])
    preflight=state.teaching.preflight_lesson(conv.course_id,conv.lesson_id,actor_id=user,
        purpose='recover',conversation_id=conv.conversation_id)
    state.teaching.confirm_lesson(conv.course_id,conv.lesson_id,actor_id=user,
        purpose='recover',conversation_id=conv.conversation_id,fingerprint=preflight['fingerprint'],
        content_confirmed=True,tts_confirmed=True)
    calls=[]
    def build(protocol,**options):
        assert options["credentials"]=={"api_key":"synthetic-key","voice_id":"synthetic-voice"}
        assert options["voice_id"]=="synthetic-voice"
        class Adapter:
            def synthesize(self,text):
                calls.append(text)
                if on_synthesize:on_synthesize(configs,view,user)
                return {"audio_hex":b"synthetic-audio".hex(),"subtitle":[{"text":text,"begin_ms":0,"end_ms":1000}] if subtitle else None}
            def close(self):options["client"].close()
        return Adapter()
    monkeypatch.setattr("backend.teaching.tts_config.build_adapter",build)
    return start,configs,view,disclosures,calls


def test_saved_tts_backfills_old_segment_and_returns_synchronized_metadata(env,monkeypatch):
    start,configs,view,disclosures,calls=prepared(env,monkeypatch)
    client,state,user,headers=env;conv=start["conversation_id"]
    workspace=client.get(f"/api/teaching/{conv}",headers=headers).json()
    assert workspace["tts_config"]=={"config_id":view["config_id"],"config_version":1}
    made=client.post(f"/api/teaching/{conv}/audio",headers=headers,json={"step_no":1})
    assert made.status_code==200,made.text
    task=made.json()["task_id"]
    assert client.post(f"/api/teaching/{conv}/audio",headers=headers,json={"step_no":1}).json()["task_id"]==task
    assert client.post(f"/api/teaching/tasks/{task}/run",headers=headers).json()["audio_status"]=="ready"
    after=client.get(f"/api/teaching/{conv}",headers=headers).json()["steps"][0]
    assert after["audio_ready"] and after["audio_duration_ms"]==1000
    assert after["audio_subtitles"][0]["text"]==after["text"] and len(calls)==1
    assert client.get(f"/api/teaching/audio/{after['audio_id']}/content",headers=headers).content==b"synthetic-audio"
    ranged=client.get(f"/api/teaching/audio/{after['audio_id']}/content",headers={**headers,"Range":"bytes=1-3"})
    assert ranged.status_code==206 and ranged.content==b"ynt"
    assert ranged.headers["content-range"]=="bytes 1-3/15"
    assert client.get(f"/api/teaching/audio/{after['audio_id']}/content",headers={**headers,"Range":"bytes=100-"}).status_code==416
    assert client.get(f"/api/teaching/audio/{after['audio_id']}/content",headers={**headers,"Range":"bytes="+"1"*150+"-"}).status_code==416
    assert client.post(f"/api/teaching/{conv}/audio",headers=headers,json={"step_no":1}).json()["status"]=="ready"
    other,token=make_user(client,state.db)
    assert client.post(f"/api/teaching/{conv}/audio",headers=auth_headers(token),json={"step_no":1}).status_code==404
    assert client.post(f"/api/teaching/{conv}/audio",headers=headers,json={"step_no":3}).status_code==404


def test_revoked_consent_is_not_recreated_or_sent(env,monkeypatch):
    start,configs,view,disclosures,calls=prepared(env,monkeypatch)
    client,state,user,headers=env;conv=start["conversation_id"]
    task=client.post(f"/api/teaching/{conv}/audio",headers=headers,json={"step_no":1}).json()["task_id"]
    grant=disclosures.list_for_task(task,actor_id=user)[0]
    disclosures.revoke(grant["grant_id"],actor_id=user)
    response=client.post(f"/api/teaching/{conv}/audio",headers=headers,json={"step_no":1})
    assert response.status_code==409,response.text
    assert response.json()["error_code"]=="disclosure_revoked"
    after_grants=disclosures.list_for_task(task,actor_id=user)
    assert len(after_grants)==1 and after_grants[0]["grant_id"]==grant["grant_id"]
    assert after_grants[0]["revoked_at"] is not None
    response=client.post(f"/api/teaching/tasks/{task}/run",headers=headers)
    assert response.status_code==409 and response.json()["error_code"]=="disclosure_revoked",response.text
    saved=client.get(f"/api/teaching/{conv}",headers=headers).json()["steps"][0]
    assert not saved["audio_ready"] and not calls
    with state.db.session() as sess:
        assert not sess.scalar(select(AudioAssetRow.audio_id).where(AudioAssetRow.conversation_id==conv))


def test_config_change_during_synthesis_cannot_publish(env,monkeypatch):
    def change(configs,view,user):configs.update(view["config_id"],actor_id=user,is_admin=False,model_name="changed-synthetic")
    start,configs,view,disclosures,calls=prepared(env,monkeypatch,on_synthesize=change)
    client,state,user,headers=env;conv=start["conversation_id"]
    task=client.post(f"/api/teaching/{conv}/audio",headers=headers,json={"step_no":1}).json()["task_id"]
    before=client.get(f"/api/teaching/{conv}",headers=headers).json()["steps"][0]
    response=client.post(f"/api/teaching/tasks/{task}/run",headers=headers)
    assert response.json()["error_code"]=="DISCLOSURE_REQUIRED",response.text
    after=client.get(f"/api/teaching/{conv}",headers=headers).json()["steps"][0]
    assert not after["audio_ready"] and after["text"]==before["text"]
    assert configs.get(view["config_id"],actor_id=user,is_admin=False)["config_version"]==2
    with state.db.session() as sess:
        assert not sess.scalar(select(AudioAssetRow.audio_id).where(AudioAssetRow.conversation_id==conv))


def test_missing_subtitle_is_visible_and_not_playable(env,monkeypatch):
    start,configs,view,disclosures,calls=prepared(env,monkeypatch,subtitle=False)
    client,state,user,headers=env;conv=start["conversation_id"]
    task=client.post(f"/api/teaching/{conv}/audio",headers=headers,json={"step_no":1}).json()["task_id"]
    client.post(f"/api/teaching/tasks/{task}/run",headers=headers)
    saved=client.get(f"/api/teaching/{conv}",headers=headers).json()["steps"][0]
    assert saved["audio_status"]=="invalid" and saved["audio_error"]=="subtitle_missing" and not saved["audio_ready"]
    monkeypatch.setattr("backend.teaching.tts_config.build_adapter",lambda protocol,**options:
        SimpleNamespace(synthesize=lambda text:{"audio_hex":b"synthetic-retry".hex(),"subtitle":[{"text":text,"begin_ms":0,"end_ms":1000}]},close=options["client"].close))
    retry=client.post(f"/api/teaching/tasks/{task}/run",headers=headers)
    assert retry.json()["audio_status"]=="ready",retry.text
    assert client.get(f"/api/teaching/{conv}",headers=headers).json()["steps"][0]["audio_ready"]


def test_consent_revoked_during_synthesis_discards_audio(env,monkeypatch):
    start,configs,view,disclosures,calls=prepared(env,monkeypatch)
    client,state,user,headers=env;conv=start["conversation_id"]
    task=client.post(f"/api/teaching/{conv}/audio",headers=headers,json={"step_no":1}).json()["task_id"]
    grant=disclosures.list_for_task(task,actor_id=user)[0]
    def synthesize(text):
        disclosures.revoke(grant["grant_id"],actor_id=user)
        return {"audio_hex":b"synthetic-late".hex(),"subtitle":[{"text":text,"begin_ms":0,"end_ms":1000}]}
    monkeypatch.setattr("backend.teaching.tts_config.build_adapter",lambda protocol,**options:
        SimpleNamespace(synthesize=synthesize,close=options["client"].close))
    before=client.get(f"/api/teaching/{conv}",headers=headers).json()["steps"][0]
    response=client.post(f"/api/teaching/tasks/{task}/run",headers=headers)
    assert response.json()["error_code"]=="DISCLOSURE_REVOKED",response.text
    after=client.get(f"/api/teaching/{conv}",headers=headers).json()["steps"][0]
    assert not after["audio_ready"] and after["text"]==before["text"]
    assert disclosures.list_for_task(task,actor_id=user)[0]["revoked_at"] is not None
    with state.db.session() as sess:
        assert not sess.scalar(select(AudioAssetRow.audio_id).where(AudioAssetRow.conversation_id==conv))


@pytest.mark.parametrize("end",[float("inf"),float("nan"),120001])
def test_invalid_subtitle_times_are_not_published(env,monkeypatch,end):
    start,configs,view,disclosures,calls=prepared(env,monkeypatch)
    client,state,user,headers=env;conv=start["conversation_id"]
    task=client.post(f"/api/teaching/{conv}/audio",headers=headers,json={"step_no":1}).json()["task_id"]
    monkeypatch.setattr("backend.teaching.tts_config.build_adapter",lambda protocol,**options:
        SimpleNamespace(synthesize=lambda text:{"audio_hex":b"synthetic-invalid".hex(),"subtitle":[{"text":text,"begin_ms":0,"end_ms":end}]},close=options["client"].close))
    client.post(f"/api/teaching/tasks/{task}/run",headers=headers)
    saved=client.get(f"/api/teaching/{conv}",headers=headers).json()["steps"][0]
    assert not saved["audio_ready"] and saved["audio_error"]=="subtitle_missing"
