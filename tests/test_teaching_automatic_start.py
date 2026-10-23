"""Configured teaching keeps precise grants without a per-lesson dialog."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.planning.models import LessonProgressRow, LessonRow, utcnow
from backend.slice1.errors import Slice1Error
from backend.slice1.models import DisclosureGrantRow, ServiceConfigRow
from backend.tasks.service import LearningTaskRow
from backend.teaching.authorization import LessonAuthorizationRow
from backend.teaching.learning_models import PreparedFirstSegmentRow
from backend.teaching.router import create_teaching_router
from test_native_teaching_authorization import env, consent  # noqa: F401


@pytest.fixture
def automatic(env):
    env.service.authorization.automatic_enabled = True
    return env


def start(env, *, purpose="start", conversation_id=None):
    request = env.service.authorization.automatic_request(env.course, env.lesson,
        actor_id=env.owner, purpose=purpose, conversation_id=conversation_id)
    return env.service.confirm_lesson(env.course, env.lesson, actor_id=env.owner, **request)


def test_start_route_uses_saved_configuration_without_confirmation_body(automatic):
    app = FastAPI()
    app.include_router(create_teaching_router(teaching=automatic.service,
        resolve_user=lambda request: SimpleNamespace(user_id=automatic.owner)))
    response = TestClient(app).post(f"/api/courses/{automatic.course}/lessons/{automatic.lesson}/start")
    assert response.status_code == 201
    row = consent(automatic)
    assert row.content_root_grant_id and row.tts_root_grant_id and row.prewarm_opt_in
    assert automatic.calls == []
    assert automatic.service.run_step_generation(response.json()["task_id"])["status"] == "succeeded"
    assert automatic.service.get_workspace(response.json()["conversation_id"], actor_id=automatic.owner)["tts_authorized"]


def test_automatic_prewarm_transfers_first_segment_and_reuses_root(automatic):
    result = automatic.service.preparation.prepare_course(automatic.course, automatic.owner)
    assert result["status"] == "queued"
    root = consent(automatic)
    assert automatic.service.preparation.run(result["task_id"])["status"] == "succeeded"
    session = start(automatic)
    workspace = automatic.service.get_workspace(session["conversation_id"], actor_id=automatic.owner)
    assert session["cache_hit"] and len(workspace["steps"]) == 1
    assert consent(automatic).content_root_grant_id == root.content_root_grant_id
    assert len([call for call in automatic.calls if call[0] == "content"]) == 1


def test_repeated_prewarm_does_not_rotate_valid_root_or_create_tasks(automatic):
    first = automatic.service.preparation.prepare_course(automatic.course, automatic.owner)
    root = consent(automatic)
    for _ in range(3):
        assert automatic.service.preparation.prepare_course(automatic.course, automatic.owner)["task_id"] == first["task_id"]
    with automatic.db.session() as sess:
        assert sess.scalar(select(func.count()).select_from(LessonAuthorizationRow).where(
            LessonAuthorizationRow.owner_user_id == automatic.owner)) == 1
    assert consent(automatic).content_root_grant_id == root.content_root_grant_id


def test_revocation_blocks_automatic_recovery_and_prewarm(automatic):
    session = start(automatic)
    root = consent(automatic)
    automatic.service.disclosures.revoke(root.content_root_grant_id, actor_id=automatic.owner)
    with pytest.raises(Slice1Error) as caught:
        start(automatic, purpose="recover", conversation_id=session["conversation_id"])
    assert caught.value.code == "disclosure_revoked"
    assert automatic.service.preparation.prepare_course(automatic.course, automatic.owner)["status"] == "authorization_required"
    assert automatic.calls == []


def test_revoked_audio_is_text_only_and_never_regranted(automatic):
    session = start(automatic)
    root = consent(automatic)
    automatic.service.disclosures.revoke(root.tts_root_grant_id, actor_id=automatic.owner)
    start(automatic, purpose="recover", conversation_id=session["conversation_id"])
    workspace = automatic.service.get_workspace(session["conversation_id"], actor_id=automatic.owner)
    assert workspace["tts_authorized"] is False
    with automatic.db.session() as sess:
        grants = list(sess.scalars(select(DisclosureGrantRow).where(
            DisclosureGrantRow.owner_user_id == automatic.owner, DisclosureGrantRow.content_category == "audio_text")))
        assert len(grants) == 1 and grants[0].revoked_at


def test_revoked_audio_child_disables_audio_in_workspace(automatic):
    session = start(automatic)
    assert automatic.service.run_step_generation(session["task_id"])["status"] == "succeeded"
    audio = automatic.service.ensure_audio(session["conversation_id"], actor_id=automatic.owner, step_no=1)
    with automatic.db.session() as sess:
        grant = sess.scalar(select(DisclosureGrantRow).where(DisclosureGrantRow.task_id == audio["task_id"],
            DisclosureGrantRow.content_category == "audio_text"))
        grant_id = grant.grant_id
    automatic.service.disclosures.revoke(grant_id, actor_id=automatic.owner)
    assert not automatic.service.get_workspace(session["conversation_id"], actor_id=automatic.owner)["tts_authorized"]
    start(automatic, purpose="recover", conversation_id=session["conversation_id"])
    assert not automatic.service.get_workspace(session["conversation_id"], actor_id=automatic.owner)["tts_authorized"]
    assert not any(call[0] == "audio" for call in automatic.calls)


def test_automatic_prewarm_failures_have_cooldown_and_two_attempt_limit(automatic):
    service = automatic.service
    first = service.preparation.prepare_course(automatic.course, automatic.owner)
    info = service.tasks.get_task(first["task_id"], actor_id=automatic.owner)
    service.tasks.fail_queued(first["task_id"], error_code="MODEL_CONNECTION_ERROR",
        expected_generation=info["generation"], expected_revision=info["revision"])
    with automatic.db.session() as sess:
        row = sess.scalar(select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.task_id == first["task_id"]))
        row.status = "failed"; row.error_code = "MODEL_CONNECTION_ERROR"
    assert service.preparation.prepare_course(automatic.course, automatic.owner)["status"] == "failed"
    with automatic.db.session() as sess:
        sess.get(LearningTaskRow, first["task_id"]).updated_at = utcnow() - timedelta(seconds=61)
    second = service.preparation.prepare_course(automatic.course, automatic.owner)
    assert second["status"] == "queued" and second["task_id"] != first["task_id"]
    info = service.tasks.get_task(second["task_id"], actor_id=automatic.owner)
    service.tasks.fail_queued(second["task_id"], error_code="MODEL_CONNECTION_ERROR",
        expected_generation=info["generation"], expected_revision=info["revision"])
    with automatic.db.session() as sess:
        row = sess.scalar(select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.task_id == second["task_id"]))
        row.status = "failed"; row.error_code = "MODEL_CONNECTION_ERROR"
        sess.get(LearningTaskRow, second["task_id"]).updated_at = utcnow() - timedelta(seconds=61)
    for _ in range(3):
        assert service.preparation.prepare_course(automatic.course, automatic.owner)["status"] == "failed"


def test_next_lesson_prewarm_uses_configuration_without_new_dialog(automatic):
    service = automatic.service
    with automatic.db.session() as sess:
        lesson = sess.get(LessonRow, automatic.lesson)
        next_id = automatic.lesson + "_next"
        sess.add(LessonRow(lesson_id=next_id, outline_version_id=lesson.outline_version_id,
            unit_id=lesson.unit_id, order_index=lesson.order_index + 1, title_ref="Synthetic next lesson", goal_ref="Practice lookup"))
        sess.add(LessonProgressRow(progress_id="progress_" + automatic.lesson, owner_user_id=automatic.owner,
            lesson_id=automatic.lesson, learning_state="completed"))
    result = service.preparation.prepare_course(automatic.course, automatic.owner)
    assert result["status"] == "queued" and result["lesson_id"] == next_id
    assert consent(automatic).lesson_id == next_id


def test_direct_recover_route_repairs_success_without_output(automatic):
    from test_teaching_artifact_recovery import missing_first
    broken = missing_first(automatic, second=True)
    app = FastAPI()
    app.include_router(create_teaching_router(teaching=automatic.service,
        resolve_user=lambda request: SimpleNamespace(user_id=automatic.owner)))
    response = TestClient(app).post(f"/api/teaching/{broken['conversation_id']}/recover")
    assert response.status_code == 200
    repaired = response.json()
    assert repaired["task_id"] != broken["task_id"] and repaired["artifact_recovery"]["status"] == "repairing"
    result = automatic.service.run_step_generation(repaired["task_id"])
    assert result["status"] == "succeeded", result
    assert automatic.service.get_workspace(broken["conversation_id"], actor_id=automatic.owner)["steps"]


def test_expired_roots_can_be_renewed_without_changing_success_task(automatic):
    from test_teaching_artifact_recovery import missing_first
    broken = missing_first(automatic)
    old = consent(automatic)
    with automatic.db.session() as sess:
        sess.get(DisclosureGrantRow, old.content_root_grant_id).expires_at = utcnow() - timedelta(seconds=1)
    repaired = start(automatic, purpose="recover", conversation_id=broken["conversation_id"])
    assert consent(automatic).content_root_grant_id != old.content_root_grant_id
    assert automatic.service.run_step_generation(repaired["task_id"])["status"] == "succeeded"
    with automatic.db.session() as sess:
        assert sess.get(LearningTaskRow, broken["task_id"]).status == "succeeded"


def test_readonly_status_distinguishes_disabled_and_configuration_missing(automatic):
    service = automatic.service
    service.preparation.enabled = False
    assert service.preparation.status(automatic.course, automatic.owner)["status"] == "disabled"
    service.preparation.enabled = True
    with automatic.db.session() as sess:
        sess.get(ServiceConfigRow, automatic.content).is_active = False
    assert service.preparation.status(automatic.course, automatic.owner)["status"] == "unconfigured"
    assert service.preparation.prepare_course(automatic.course, automatic.owner)["status"] == "unconfigured"
    assert automatic.calls == []
