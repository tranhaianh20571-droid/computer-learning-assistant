"""Synthetic ownership, immutable history and late-result mentor switch fences."""
import json
import uuid
import pytest
from sqlalchemy import select, func, text, inspect
from backend.characters.schemas import Profile, UseMentor, VoiceChoice, ExpressionPreview
from backend.characters.models import (CharacterBindingRow, CharacterSegmentRow,
    CharacterVoiceChoiceRow, ClassroomPlaybackRow, ClassroomHistoryRow, CharacterRevisionRow)
from backend.characters.service import CharacterService, segment_character
from backend.materials.storage import MaterialStorage
from backend.slice1.config_service import ConfigService
from backend.slice1.errors import Slice1Error, AdapterError
from backend.teaching.models import TeachingStepRow, PlaybackCheckpointRow, AudioAssetRow
from backend.tasks.service import LearningTaskRow, LeaseError
from backend.teaching.errors import TeachingError
from test_teaching_assessments import env, database, lesson  # noqa: F401
from test_character_cards import env as voice_env  # noqa: F401
from test_character_voice_policy import VOICES, tts_config


def service(env, tmp_path):
    state = env[1]
    cards = CharacterService(state.db, ConfigService(state.db, encryption_key="synthetic-switch",
        audit=state.audit), state.audit, MaterialStorage(tmp_path))
    cards.teaching = state.teaching
    state.teaching.characters = cards
    return cards


def test_existing_classroom_global_preference_rebinds_same_card_and_rebuilds_current(env, tmp_path):
    cards = service(env, tmp_path)
    _, _, start = lesson(env)
    client, state, owner, headers = env
    assert state.teaching.run_step_generation(start["task_id"])["status"] == "succeeded"
    card = cards.create(owner, Profile(name="Synthetic mentor", personality="活泼温柔"))
    cards.use(owner, UseMentor(expected_version=0, card_id=card["card_id"], revision=1))
    before = client.get(f"/api/teaching/{start['conversation_id']}", headers=headers).json()
    assert before["steps"][0]["character"] is None
    chosen = cards.use(owner, UseMentor(expected_version=1, card_id=card["card_id"], revision=1,
        conversation_id=start["conversation_id"]))
    assert chosen["effective_step_no"] == 1
    with state.db.session() as sess:
        step = sess.scalar(select(TeachingStepRow).where(TeachingStepRow.conversation_id==start["conversation_id"]))
        assert step.text_version_no == 2 and step.text == "" and step.audio_id is None
        history = sess.get(ClassroomHistoryRow, (start["conversation_id"], 1, 1))
        assert json.loads(history.snapshot_json)["text"] == before["steps"][0]["text"]
        assert segment_character(sess, start["conversation_id"], 1)["card_id"] == card["card_id"]
        task = sess.get(LearningTaskRow, step.source_task_id)
        assert not task.cancel_requested and task.idempotency_key.endswith(":mentor:1")
        task_id = task.task_id
    original_tts=state.teaching.tts_adapter_factory
    from types import SimpleNamespace
    state.teaching.tts_adapter_factory=SimpleNamespace(config_for_owner=lambda actor:{"config_id":"synthetic-not-called","config_version":1})
    assert state.teaching.ensure_audio(start["conversation_id"],actor_id=owner,step_no=1)=={"status":"preparing","task_id":None}
    state.teaching.tts_adapter_factory=original_tts
    assert state.teaching.run_step_generation(task_id)["status"] == "succeeded"
    workspace = client.get(f"/api/teaching/{start['conversation_id']}", headers=headers).json()
    assert workspace["playback_version"] == 1
    assert workspace["steps"][0]["status"] == "ready" and workspace["steps"][0]["character"]["card_id"] == card["card_id"]
    assert workspace["last_checkpoint"]["position_ms"] == 0
    assert workspace["last_checkpoint"]["presentation"]["paused"]
    pref = cards.preference(owner, start["conversation_id"])
    assert pref["classroom"]["card_id"] == card["card_id"]
    # Identical selection is idempotent for playback, despite preference CAS advancing.
    cards.use(owner, UseMentor(expected_version=2, card_id=card["card_id"], revision=1,
        conversation_id=start["conversation_id"]))
    with state.db.session() as sess:
        assert sess.get(ClassroomPlaybackRow, start["conversation_id"]).version == 1


def test_switch_preserves_completed_segment_and_rejects_old_checkpoint_and_lease(env, tmp_path):
    cards = service(env, tmp_path)
    owner, state = env[2], env[1]
    first = cards.create(owner, Profile(name="First"))
    second = cards.create(owner, Profile(name="Second"))
    cards.use(owner, UseMentor(expected_version=0, card_id=first["card_id"], revision=1))
    _, _, start = lesson(env)
    assert state.teaching.run_step_generation(start["task_id"])["status"] == "succeeded"
    with state.db.session() as sess:
        old = sess.scalar(select(TeachingStepRow).where(TeachingStepRow.conversation_id==start["conversation_id"]))
        saved = old.text
        sess.add(PlaybackCheckpointRow(checkpoint_id="cp_"+uuid.uuid4().hex, conversation_id=start["conversation_id"],
            client_seq=1, generation=1, step_no=1, position_ms=1000,
            board_state_json=json.dumps({"presentation":{"text_complete":True,"paused":True}})))
        pending = sess.scalar(select(LearningTaskRow).where(LearningTaskRow.subject_id==start["conversation_id"]+":2:1"))
        pending_id = pending.task_id
    lease = state.teaching.tasks.claim(pending_id, lease_owner="synthetic-late-worker")
    result = cards.use(owner, UseMentor(expected_version=1, card_id=second["card_id"], revision=1,
        conversation_id=start["conversation_id"]))
    assert result["effective_step_no"] == 2
    with state.db.session() as sess:
        assert sess.get(TeachingStepRow,old.step_id).text == saved
        assert segment_character(sess,start["conversation_id"],1)["card_id"] == first["card_id"]
        assert segment_character(sess,start["conversation_id"],2)["card_id"] == second["card_id"]
    with pytest.raises(LeaseError):
        state.teaching.tasks.validate_lease(lease, actor_id=owner)
    with pytest.raises(TeachingError):
        state.teaching.save_checkpoint(start["conversation_id"],actor_id=owner,client_seq=999,step_no=2,
            position_ms=10000,board_state={"presentation":{"playback_version":0}})
    assert state.teaching.save_checkpoint(start["conversation_id"],actor_id=owner,client_seq=999,step_no=2,
        position_ms=0,board_state={"presentation":{"playback_version":1,"paused":True}})["saved"]


def test_start_reusing_legacy_lesson_applies_current_global_mentor(env, tmp_path):
    cards = service(env, tmp_path)
    _, _, start = lesson(env)
    state, owner = env[1], env[2]
    assert state.teaching.run_step_generation(start["task_id"])["status"] == "succeeded"
    card = cards.create(owner, Profile(name="Current"))
    cards.use(owner, UseMentor(expected_version=0,card_id=card["card_id"],revision=1))
    from backend.planning.models import ConversationRow, LessonRow
    with state.db.session() as sess:
        conv = sess.get(ConversationRow,start["conversation_id"])
        state.teaching._reuse_lesson(sess,conv,sess.get(LessonRow,conv.lesson_id))
    with state.db.session() as sess:
        assert sess.get(CharacterBindingRow,start["conversation_id"]).card_id == card["card_id"]
        assert sess.get(ClassroomPlaybackRow,start["conversation_id"]).version == 1


def test_voice_choice_for_collected_card_is_private_manual_and_does_not_edit_author(voice_env):
    cards, media = voice_env
    author, owner = "author_"+uuid.uuid4().hex, "reader_"+uuid.uuid4().hex
    card = cards.create(author, Profile(name="Published",identity="男性导师"))
    cards.publish(author,card["card_id"],1)
    cards.collect(owner,card["card_id"])
    config = tts_config(cards,owner)
    media.adapter_factory=lambda *a,**k: type("Adapter",(),{"list_voices":lambda self:VOICES,"close":lambda self:None})()
    chosen=cards.choose_voice(owner,card["card_id"],VoiceChoice(revision=1,voice_id="male-bright",config_id=config["config_id"]))
    assert not chosen["editable"] and chosen["profile"]["voice_selection"] == "manual"
    assert chosen["profile"]["voice_id"] == "male-bright" and chosen["voice_choice_version"] == 1
    assert cards.get(author,card["card_id"])["profile"]["voice_id"] == ""
    assert cards.get("other-reader",card["card_id"])["profile"]["voice_id"] == ""
    with pytest.raises(Slice1Error):
        cards.choose_voice(owner,card["card_id"],VoiceChoice(revision=1,voice_id="male-calm",config_id=config["config_id"]))
    revision, profile=cards.prepare_profile(owner,card["card_id"],1)
    assert revision==1 and profile["voice_id"]=="male-bright"
    # An explicit personal choice is allowed; automatic recommendation stays male.
    from backend.characters.voice_policy import resolve_voice
    assert resolve_voice({"identity":"男性导师","voice_selection":"manual"},VOICES,"female-warm")=="female-warm"


@pytest.mark.parametrize("fail",[False,True])
def test_preview_uses_current_selection_without_saving_card_and_closes_adapter(voice_env, fail):
    cards,media=voice_env
    owner="preview_"+uuid.uuid4().hex
    config=tts_config(cards,owner);sent=[];closed=[]
    class Adapter:
        def list_voices(self):return VOICES
        def synthesize(self,content,**options):
            sent.append(options)
            if fail:raise AdapterError("timeout",detail="private provider failure")
            return {"audio_hex":b"synthetic-audio".hex()}
        def close(self):closed.append(True)
    media.adapter_factory=lambda *a,**k:Adapter()
    request=ExpressionPreview(profile=Profile(name="Draft",identity="男性导师",personality="活泼温柔",
        voice_selection="manual",voice_id="male-bright"),config_id=config["config_id"])
    if fail:
        with pytest.raises(Slice1Error) as error:media.preview_voice(owner,request)
        assert "private provider failure" not in str(error.value)
    else:
        assert media.preview_voice(owner,request)==b"synthetic-audio"
    assert sent[0]["voice_id"]=="male-bright" and cards.list(owner)==[]
    assert len(closed)==2


def test_mentor_migration_upgrade_and_nonempty_downgrade(engine):
    from test_native_migrations import migration
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE conversations (conversation_id varchar(64) PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE character_cards (card_id varchar(64) PRIMARY KEY)"))
        module=migration("w7b8c9d0e1f2_mentor_playback_revisions.py",conn)
        module.upgrade()
        assert {"character_voice_choices","classroom_segment_history","classroom_playback_revisions"} <= set(inspect(conn).get_table_names())
        conn.execute(text("INSERT INTO conversations VALUES ('synthetic')"))
        conn.execute(text("INSERT INTO classroom_playback_revisions VALUES ('synthetic',1,1,'{}')"))
        with pytest.raises(RuntimeError):module.downgrade()
        conn.execute(text("DELETE FROM classroom_playback_revisions"))
        module.downgrade()


from test_native_migrations import engine  # noqa: E402,F401
