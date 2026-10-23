"""Version/coverage completion is deterministic; models have no write authority."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select
from backend.planning.models import ConversationRow, LessonRow
from backend.teaching.errors import TeachingError
from backend.teaching.knowledge import CoverageIn, KnowledgeCompleteIn, digest, merge_intervals, LessonTitleIn
from backend.teaching.learning_models import LessonBlueprintRow, TaughtKnowledgeRow
from backend.teaching.models import TeachingStepRow, BoardSceneRow
from test_feynman_dialogues import prepare
from test_native_teaching_authorization import env  # noqa: F401


def ready(env, monkeypatch, voice=False):
    conv, point = prepare(env, monkeypatch, voice=voice)
    with env.db.session() as sess:
        steps = list(sess.scalars(select(TeachingStepRow).where(TeachingStepRow.conversation_id == conv)
            .order_by(TeachingStepRow.step_no)))
        for step in steps:
            ident = "scene_" + uuid.uuid4().hex
            step.board_scene_id = ident
            sess.add(BoardSceneRow(scene_id=ident, conversation_id=conv, generation=1, step_no=step.step_no,
                declaration_json=json.dumps({"title":"Synthetic board","points":["Taught content"]}),
                scene_json=json.dumps({"knowledge_recap": {"summary": "Synthetic recap tied to taught content."}})))
        plan = sess.get(LessonBlueprintRow, conv)
        request = KnowledgeCompleteIn(checkpoint_id=point["checkpoint_id"], plan_hash=digest(plan.plan_json))
        values = [CoverageIn(step_id=s.step_id, text_version=1, client_seq=1,
            intervals=[(0, len(s.text))], visible_chars=len(s.text), board_complete=True) for s in steps]
    return conv, point, request, values


def cover(env, conv, values):
    for value in values:
        env.service.knowledge.coverage(conv, env.owner, value)


def test_complete_requires_every_segment_and_persists_before_question(env, monkeypatch):
    conv, point, request, values = ready(env, monkeypatch)
    cover(env, conv, values[1:])
    with pytest.raises(TeachingError):
        env.service.knowledge.complete(conv, env.owner, request)
    cover(env, conv, values[:1])
    receipt = env.service.knowledge.complete(conv, env.owner, request)
    view = env.service.knowledge.view(conv, env.owner)
    assert view["lesson_id"] == env.lesson and view["lesson_title"] == "Synthetic lesson"
    assert view["total"] == 1 and view["data"][0]["record_id"] == receipt["record_id"]
    assert view["data"][0]["assessment"] == "pending"
    assert len(view["data"][0]["sources"]) == len(values)


@pytest.mark.parametrize("failure", ["gap", "text", "board", "audio_identity", "version"])
def test_incomplete_or_mismatched_evidence_never_records(env, monkeypatch, failure):
    conv, _, request, values = ready(env, monkeypatch)
    value = values[0]
    updates = {"gap": {"intervals": [(0, 10), (12, value.visible_chars)]},
        "text": {"visible_chars": 3}, "board": {"board_complete": False},
        "audio_identity": {"audio_id": "aud_wrong"}, "version": {"text_version": 2}}[failure]
    bad = value.model_copy(update=updates)
    if failure in ("audio_identity", "version"):
        with pytest.raises(TeachingError):
            env.service.knowledge.coverage(conv, env.owner, bad)
        return
    cover(env, conv, [bad, *values[1:]])
    with pytest.raises(TeachingError):
        env.service.knowledge.complete(conv, env.owner, request)
    assert env.service.knowledge.view(conv, env.owner)["total"] == 0


def test_parallel_retry_and_generation_change_keep_one_record(env, monkeypatch):
    conv, _, request, values = ready(env, monkeypatch)
    cover(env, conv, values)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: env.service.knowledge.complete(conv, env.owner, request), range(2)))
    assert receipts[0]["record_id"] == receipts[1]["record_id"]
    before = env.service.knowledge.view(conv, env.owner)["data"][0]["completed_at"]
    with env.db.session() as sess:
        sess.get(ConversationRow, conv).generation += 1
    env.service.knowledge.complete(conv, env.owner, request)
    after = env.service.knowledge.view(conv, env.owner)
    assert after["total"] == 1 and after["data"][0]["completed_at"] == before


def test_owner_deleted_and_changed_plan_cannot_publish(env, monkeypatch):
    conv, _, request, values = ready(env, monkeypatch)
    with pytest.raises(TeachingError) as caught:
        env.service.knowledge.view(conv, "someone-else")
    assert caught.value.status == 404
    cover(env, conv, values)
    with env.db.session() as sess:
        row = sess.get(LessonBlueprintRow, conv)
        row.plan_json = row.plan_json + " "
    with pytest.raises(TeachingError):
        env.service.knowledge.complete(conv, env.owner, request)
    from backend.planning.models import utcnow
    with env.db.session() as sess:
        sess.get(ConversationRow, conv).deleted_at = utcnow()
    with pytest.raises(TeachingError):
        env.service.knowledge.coverage(conv, env.owner, values[0])


def test_question_outcome_is_separate_and_rename_conflicts(env, monkeypatch):
    from backend.teaching.learning_models import FeynmanSessionRow
    conv, point, request, values = ready(env, monkeypatch)
    cover(env, conv, values)
    env.service.knowledge.complete(conv, env.owner, request)
    with env.db.session() as sess:
        sess.add(FeynmanSessionRow(session_id="fey_"+uuid.uuid4().hex, conversation_id=conv,
            owner_user_id=env.owner, checkpoint_id=point["checkpoint_id"],
            after_step_no=point["after_step_no"], source_hash="x"*64, context_json="{}",
            state="closed", result="skipped"))
    assert env.service.knowledge.view(conv, env.owner)["data"][0]["assessment"] == "skipped"
    env.service.knowledge.rename(conv, env.owner, LessonTitleIn(title="Renamed lesson", expected_title="Synthetic lesson"))
    assert env.service.get_workspace(conv, actor_id=env.owner)["lesson_title"] == "Renamed lesson"
    with pytest.raises(TeachingError):
        env.service.knowledge.rename(conv, env.owner, LessonTitleIn(title="Stale", expected_title="Synthetic lesson"))
    assert env.service.knowledge.view(conv, env.owner)["total"] == 1


def test_merge_retains_real_gaps_and_bounds():
    assert merge_intervals([(0, 20), (10, 30), (40, 50)]) == [[0, 30], [40, 50]]
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        CoverageIn(step_id="x", text_version=1, client_seq=1, visible_chars=0,
            intervals=[(9, 2)], board_complete=False)


def test_current_lesson_does_not_count_other_content_version(env, monkeypatch):
    conv, _, request, values = ready(env, monkeypatch)
    cover(env, conv, values)
    env.service.knowledge.complete(conv, env.owner, request)
    with env.db.session() as sess:
        record = sess.scalar(select(TaughtKnowledgeRow).where(TaughtKnowledgeRow.conversation_id == conv))
        record.content_version_id = "other-content"
    assert env.service.knowledge.view(conv, env.owner)["total"] == 0


def test_tts_requires_same_audio_complete_played_coverage(env, monkeypatch):
    from backend.teaching.models import AudioAssetRow
    conv, _, request, values = ready(env, monkeypatch, voice=True)
    with env.db.session() as sess:
        for value in values:
            step = sess.get(TeachingStepRow, value.step_id)
            ident = "aud_" + uuid.uuid4().hex
            sess.add(AudioAssetRow(audio_id=ident, conversation_id=conv, generation=1,
                step_no=step.step_no, text_version_no=step.text_version_no,
                text_sha256=digest(step.text), status="ready", duration_ms=1000))
            step.audio_id = ident
            value.audio_id = ident
            value.intervals = [(0, 1000)]
    invalid = values[0].model_copy(update={"intervals": [(900, 1000)]})
    cover(env, conv, [invalid, *values[1:]])
    with pytest.raises(TeachingError):
        env.service.knowledge.complete(conv, env.owner, request)
    values[0].client_seq += 1
    cover(env, conv, values)
    env.service.knowledge.complete(conv, env.owner, request)
    assert env.service.knowledge.view(conv, env.owner)["total"] == 1
    # Revoking the approved mode cannot be bypassed by posting text intervals.
    from backend.slice1.models import DisclosureGrantRow
    from backend.teaching.authorization import LessonAuthorizationRow
    from backend.planning.models import utcnow
    with env.db.session() as sess:
        consent = sess.scalar(select(LessonAuthorizationRow).where(LessonAuthorizationRow.conversation_id == conv))
        sess.get(DisclosureGrantRow, consent.tts_root_grant_id).revoked_at = utcnow()
    from backend.slice1.errors import Slice1Error
    with pytest.raises(Slice1Error):
        env.service.knowledge.coverage(conv, env.owner, values[0].model_copy(update={"audio_id": ""}))
