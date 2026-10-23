"""Independent answer audio, generation fences and ownership using synthetic TTS."""
import hashlib
import uuid
from sqlalchemy import select
from backend.planning.models import ConversationRow, MessageRow
from backend.teaching.models import AudioAssetRow
from common_fixtures import make_user, auth_headers
from test_teaching_assessments import env, database  # noqa: F401
from test_lecture_audio import prepared


def answer(env, monkeypatch, on_synthesize=None):
    start, configs, view, disclosures, calls = prepared(env, monkeypatch, on_synthesize=on_synthesize)
    client, state, user, headers = env
    with state.db.session() as sess:
        conv = sess.get(ConversationRow, start["conversation_id"])
        conv.generation = 2
        conv.session_state = "awaiting_resume"
        sess.add(MessageRow(message_id="msg_"+uuid.uuid4().hex, conversation_id=conv.conversation_id,
            generation=2, role="assistant", content_ref="Synthetic answer."))
    return start, calls


def test_answer_audio_does_not_reuse_lesson_track_or_resume(env, monkeypatch):
    start, calls = answer(env, monkeypatch)
    client, state, user, headers = env
    path=f"/api/teaching/{start['conversation_id']}/answer-audio"
    request=client.post(path, headers=headers)
    assert request.status_code==200, request.text
    task_id=request.json()["task_id"]
    result=client.post(f"/api/teaching/tasks/{task_id}/run", headers=headers)
    assert result.status_code==200 and result.json()["status"]=="succeeded", result.text
    workspace=client.get(f"/api/teaching/{start['conversation_id']}", headers=headers).json()
    assert workspace["session_state"]=="awaiting_resume" and workspace["answer_audio"]["audio_id"]
    assert client.post(path,headers=headers).json()["task_id"] is None
    assert calls==["Synthetic answer."]
    with state.db.session() as sess:
        audio=sess.get(AudioAssetRow,workspace["answer_audio"]["audio_id"])
        assert audio.step_no==0 and audio.generation==2
        assert audio.text_sha256==hashlib.sha256(b"Synthetic answer.").hexdigest()
    _, other_token=make_user(client, state.db)
    other=auth_headers(other_token)
    assert client.post(path,headers=other).status_code==404
    assert client.get(f"/api/teaching/audio/{audio.audio_id}/content",headers=other).status_code==404


def test_new_generation_blocks_late_answer_audio(env, monkeypatch):
    conv_id=[None]
    def interrupt(*args):
        with env[1].db.session() as sess:
            sess.get(ConversationRow,conv_id[0]).generation=3
    start,calls=answer(env,monkeypatch,on_synthesize=interrupt)
    conv_id[0]=start["conversation_id"]
    client,state,user,headers=env
    task=client.post(f"/api/teaching/{conv_id[0]}/answer-audio",headers=headers).json()["task_id"]
    result=client.post(f"/api/teaching/tasks/{task}/run",headers=headers)
    assert result.json()["status"]=="failed",result.text
    with state.db.session() as sess:
        assert not sess.scalar(select(AudioAssetRow).where(AudioAssetRow.conversation_id==conv_id[0],AudioAssetRow.step_no==0))
