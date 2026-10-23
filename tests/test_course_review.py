"""Owned PostgreSQL review flow; synthetic providers, real task/FSRS persistence."""
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic_ai.models.test import TestModel
from sqlalchemy import func, select

from backend.auth.models import UserRow
from backend.planning.models import ConversationRow, CourseRow, LessonContentVersionRow, LessonRow, utcnow
from backend.review.models import ReviewAnswerRow, ReviewCardRow, ReviewLogRow, ReviewSessionRow
from backend.review.router import AnswerIn, DraftIn
from backend.review.service import ReviewService, digest, domain_binding
from backend.tasks.service import LearningTaskRow
from backend.teaching.learning_models import LessonBlueprintRow, TaughtKnowledgeRow
from test_course_library import confirm, database, draft, env  # noqa: F401


@pytest.fixture
def review(env, monkeypatch):
    client, state, owner, headers = env
    course, proposal = draft(env, {"title": "Synthetic review", "units": [
        {"title": "Lookup", "lessons": [{"title": "Unique keys", "goal": "Read values by key"}]}]}, "course_outline")
    assert confirm(env, proposal).status_code == 200
    with state.db.session() as sess:
        c = sess.get(CourseRow, course)
        lesson = sess.scalar(select(LessonRow).where(LessonRow.outline_version_id == c.current_outline_version_id))
        version = sess.scalar(select(LessonContentVersionRow).where(LessonContentVersionRow.lesson_id == lesson.lesson_id))
        if not version:
            version = LessonContentVersionRow(content_version_id="cv_" + uuid.uuid4().hex, lesson_id=lesson.lesson_id)
            sess.add(version)
            sess.flush()
        conv = "conv_" + uuid.uuid4().hex
        sess.add(ConversationRow(conversation_id=conv, owner_user_id=owner, kind="teaching",
            course_id=course, lesson_id=lesson.lesson_id, outline_version_id=c.current_outline_version_id,
            lesson_content_version_id=version.content_version_id))
        sess.flush()
        sess.add(LessonBlueprintRow(conversation_id=conv, owner_user_id=owner, lesson_id=lesson.lesson_id,
            outline_version_id=c.current_outline_version_id, content_version_id=version.content_version_id,
            plan_json="{}", context_json="{}", source_task_id="synthetic"))
        record = uuid.uuid4().hex * 2
        sess.add(TaughtKnowledgeRow(record_id=record, owner_user_id=owner, course_id=course,
            lesson_id=lesson.lesson_id, content_version_id=version.content_version_id,
            plan_hash=digest("{}"), checkpoint_id="point", conversation_id=conv, after_step_no=1,
            title="Unique lookup", summary="A unique key identifies a value.",
            completed_at=utcnow() - timedelta(days=3)))
        lesson_id = lesson.lesson_id
    service = state.review
    monkeypatch.setattr(service, "kick", lambda task: None)
    return SimpleNamespace(service=service, state=state, client=client, owner=owner,
        headers=headers, course=course, record=record, conv=conv, lesson=lesson_id)


def generate(review, monkeypatch, key=None):
    service = review.service
    view = service.create(review.owner, review.course, key or uuid.uuid4().hex, "Asia/Shanghai")
    question = {"record_id": review.record, "prompt": "How does a unique key identify a value?",
        "answer": "It selects the value mapped to that key.", "criteria": ["Explain unique key mapping."],
        "source_use": "generated", "source_question_id": "", "lesson_ids": [review.lesson],
        "source": {"kind": "generated", "reason": "Synthetic generated question."}}
    monkeypatch.setattr(service, "_compute", lambda task: [question])
    service._run(view["task_id"])
    ready = service.get(view["session_id"], review.owner)
    assert ready["status"] == "ready", ready
    return ready


def submit(review, monkeypatch, view, result="correct", rating=3):
    body = AnswerIn(answer="Read the value by its unique key.", rating=rating,
        idempotency_key=uuid.uuid4().hex, revision=view["revision"])
    sent = review.service.answer(view["session_id"], review.owner, body)
    monkeypatch.setattr(review.service, "_compute", lambda task: {"result": result, "feedback": "Synthetic grading."})
    review.service._run(sent["task_id"])
    return review.service.get(view["session_id"], review.owner), body


def test_real_task_fsrs_public_projection_idempotence_and_restart(review, monkeypatch):
    view = generate(review, monkeypatch)
    assert "answer" not in view["question"] and "criteria" not in view["question"]
    finished, body = submit(review, monkeypatch, view)
    assert finished["feedback"]["scheduled"] is True
    duplicate = review.service.answer(view["session_id"], review.owner, body)
    assert duplicate["feedback"] == finished["feedback"]
    with review.state.db.session() as sess:
        logs = list(sess.scalars(select(ReviewLogRow).join(
            ReviewAnswerRow, ReviewLogRow.answer_id == ReviewAnswerRow.answer_id).where(
                ReviewAnswerRow.session_id == view["session_id"])))
        assert len(logs) == 1
        assert json.loads(logs[0].card_after_json)["last_review"]
    restored = ReviewService(review.state.teaching).get(view["session_id"], review.owner)
    assert restored["feedback"] == finished["feedback"]
    moved = review.service.move(view["session_id"], review.owner, finished["revision"])
    assert moved["status"] == "finished"


def test_completed_duplicate_does_not_reauthorize_obsolete_task(review, monkeypatch):
    view = generate(review, monkeypatch)
    done, value = submit(review, monkeypatch, view)
    def no_second_authorization(*args):
        raise AssertionError("completed result must not start another authorization or model call")
    monkeypatch.setattr(review.state.assessments, "_prepare_source_grant", no_second_authorization)
    repeated = review.service.answer(view["session_id"], review.owner, value)
    assert repeated["feedback"] == done["feedback"]


@pytest.mark.parametrize("result,rating,expected", [
    ("correct", 2, 2), ("correct", 3, 3), ("correct", 4, 4), ("incorrect", 4, 1), ("uncertain", 3, None)])
def test_grade_caps_self_rating_and_four_fsrs_ratings(review, monkeypatch, result, rating, expected):
    view = generate(review, monkeypatch)
    done, _ = submit(review, monkeypatch, view, result, rating)
    assert done["feedback"]["scheduled"] == (expected is not None)
    assert done["feedback"].get("rating") == expected


def test_help_and_skip_do_not_change_schedule(review, monkeypatch):
    view = generate(review, monkeypatch)
    helped = review.service.move(view["session_id"], review.owner, view["revision"], help_only=True)
    assert helped["question"]["reference"] and helped["assisted"]
    done, _ = submit(review, monkeypatch, helped)
    assert not done["feedback"]["scheduled"]
    next_view = generate(review, monkeypatch)
    skipped = review.service.move(next_view["session_id"], review.owner, next_view["revision"], skip=True)
    assert skipped["status"] == "finished"
    with review.state.db.session() as sess:
        assert sess.get(ReviewCardRow, review.record).revision == 1


def test_draft_restores_and_cannot_overwrite_submitted_answer(review, monkeypatch):
    view = generate(review, monkeypatch)
    value = DraftIn(revision=view["revision"], answer="Synthetic draft", rating=2)
    assert review.service.draft(view["session_id"], review.owner, value)["saved"]
    assert ReviewService(review.state.teaching).get(
        view["session_id"], review.owner)["draft"] == {"answer": "Synthetic draft", "rating": 2}
    done, _ = submit(review, monkeypatch, view)
    with pytest.raises(Exception) as caught:
        review.service.draft(view["session_id"], review.owner, value)
    assert caught.value.status == 409
    assert done["answer"] != "Synthetic draft"


def test_grade_publish_conflict_keeps_answer_without_extra_log(review, monkeypatch):
    first, second = generate(review, monkeypatch), generate(review, monkeypatch)
    body = AnswerIn(answer="Saved before concurrent grading", rating=4,
        revision=second["revision"], idempotency_key="second")
    queued = review.service.answer(second["session_id"], review.owner, body)
    submit(review, monkeypatch, first)
    review.service._run(queued["task_id"])
    stale = review.service.get(second["session_id"], review.owner)
    assert stale["status"] == "failed"
    assert stale["answer"] == body.answer and stale["feedback"] is None


def test_cancel_generation_does_not_publish_or_schedule(review):
    view = review.service.create(review.owner, review.course, "cancel", "Asia/Shanghai")
    review.service.tasks.request_cancel(view["task_id"], actor_id=review.owner)
    review.service._run(view["task_id"])
    stopped = review.service.get(view["session_id"], review.owner)
    assert stopped["status"] == "cancelled" and stopped["question"] is None
    with review.state.db.session() as sess:
        assert sess.get(ReviewCardRow, review.record).revision == 1


def test_failed_grading_retry_preserves_answer_and_updates_once(review, monkeypatch):
    from backend.planning.errors import error
    view = generate(review, monkeypatch)
    body = AnswerIn(answer="Preserved across failure", rating=3,
        revision=view["revision"], idempotency_key="retry-answer")
    queued = review.service.answer(view["session_id"], review.owner, body)
    def fail_compute(task):
        raise error("MODEL_TIMEOUT")
    monkeypatch.setattr(review.service, "_compute", fail_compute)
    review.service._run(queued["task_id"])
    failed = review.service.get(view["session_id"], review.owner)
    assert failed["status"] == "failed" and failed["answer"] == body.answer
    with review.state.db.session() as sess:
        assert sess.get(ReviewCardRow, review.record).revision == 1
    review.service.retry(view["session_id"], review.owner)
    monkeypatch.setattr(review.service, "_compute", lambda task: {"result": "correct", "feedback": "Recovered."})
    review.service._run(queued["task_id"])
    done = review.service.get(view["session_id"], review.owner)
    assert done["status"] == "feedback" and done["answer"] == body.answer
    assert done["feedback"]["scheduled"]
    with review.state.db.session() as sess:
        assert sess.get(ReviewCardRow, review.record).revision == 2


def test_concurrent_session_rejects_stale_card_and_keeps_answer(review, monkeypatch):
    one, two = generate(review, monkeypatch), generate(review, monkeypatch)
    submit(review, monkeypatch, one)
    with pytest.raises(Exception) as caught:
        review.service.answer(two["session_id"], review.owner, AnswerIn(
            answer="stale", rating=4, revision=two["revision"], idempotency_key="stale"))
    assert caught.value.status == 409


def test_changed_plan_deleted_course_and_disabled_owner_fence(review, monkeypatch):
    view = generate(review, monkeypatch)
    with review.state.db.session() as sess:
        task = sess.get(LearningTaskRow, view["task_id"])
        assert domain_binding(sess, task)[0]["course_id"] == review.course
        sess.get(LessonBlueprintRow, review.conv).plan_json = '{"changed":true}'
    assert review.client.get("/api/review/overview", headers=review.headers).json()["total"] == 0
    assert review.client.get(f"/api/review/sessions/{view['session_id']}", headers=review.headers).status_code == 409
    with review.state.db.session() as sess:
        sess.get(UserRow, review.owner).status = "disabled"
    with pytest.raises(Exception) as caught:
        review.service.overview(review.owner, "Asia/Shanghai")
    assert caught.value.status == 403


def test_all_points_not_context_limit_and_timezone(review):
    with review.state.db.session() as sess:
        origin = sess.get(TaughtKnowledgeRow, review.record)
        for i in range(130):
            sess.add(TaughtKnowledgeRow(record_id=uuid.uuid4().hex * 2, owner_user_id=review.owner,
                course_id=review.course, lesson_id=review.lesson, content_version_id=origin.content_version_id,
                plan_hash=origin.plan_hash, checkpoint_id=f"point-{i}", conversation_id=review.conv,
                after_step_no=i+2, title="Synthetic point", summary="Synthetic",
                completed_at=utcnow()-timedelta(days=3)))
    assert review.service.overview(review.owner, "Asia/Shanghai")["total"] == 131
    assert review.service.overview(review.owner, "America/Los_Angeles")["counts"]["overdue"] == 131
    assert review.client.get("/api/review/overview?timezone=NotAZone", headers=review.headers).status_code == 400


def test_http_owner_boundary_and_course_delete_cleans_all_rows(review, monkeypatch):
    view = generate(review, monkeypatch)
    submit(review, monkeypatch, view)
    assert review.service.overview(review.owner, "Asia/Shanghai")["resume_session_id"] == view["session_id"]
    with pytest.raises(Exception) as caught:
        review.service.get(view["session_id"], "other-owner")
    assert caught.value.status == 404
    review.state.deletion.delete_course(review.course, actor_id=review.owner)
    with review.state.db.session() as sess:
        assert sess.get(ReviewSessionRow, view["session_id"]) is None
        assert sess.get(ReviewCardRow, review.record) is None
        assert sess.scalar(select(func.count()).select_from(ReviewAnswerRow).where(
            ReviewAnswerRow.session_id == view["session_id"])) == 0


def test_native_structured_generation_and_grading(review, monkeypatch):
    view = review.service.create(review.owner, review.course, "native", "Asia/Shanghai")
    generated = {"questions": [{"record_id": review.record, "prompt": "How does a unique key identify a value?",
        "answer": "A key maps to a value.", "criteria": ["Explain unique mapping."],
        "lesson_ids": [review.lesson], "source_use": "generated", "source_question_id": ""}],
        "source_rejections": []}
    review.state.teaching.model_adapter_factory = lambda owner, task: TestModel(custom_output_args=generated)
    output = review.service._compute(view["task_id"])
    assert output[0]["source"]["kind"] == "generated"
    monkeypatch.setattr(review.service, "_compute", lambda task: output)
    review.service._run(view["task_id"])
    ready = review.service.get(view["session_id"], review.owner)
    sent = review.service.answer(ready["session_id"], review.owner, AnswerIn(
        answer="A key maps to a value.", rating=3, revision=ready["revision"], idempotency_key="native-answer"))
    review.state.teaching.model_adapter_factory = lambda owner, task: TestModel(
        custom_output_args={"result": "correct", "feedback": "The mapping is explained."})
    assert ReviewService._compute(review.service, sent["task_id"])["result"] == "correct"


def test_configured_grant_is_required_and_revocation_blocks_review_wire(review):
    from backend.agents.task_binding import bind_task_deps
    from backend.slice1.disclosure_service import DisclosureService
    from backend.slice1.errors import Slice1Error
    from backend.slice1.models import DisclosureGrantRow, ServiceConfigRow
    view = review.service.create(review.owner, review.course, "wire-auth", "Asia/Shanghai")
    review.service.tasks.claim(view["task_id"], lease_owner="synthetic-review")
    config = "cfg_" + uuid.uuid4().hex
    with review.state.db.session() as sess:
        sess.add(ServiceConfigRow(config_id=config, owner_user_id=review.owner,
            owner_scope="user", kind="content_model", protocol="openai",
            endpoint="https://example.invalid/v1", model_name="synthetic"))
    source = SimpleNamespace(config_id=config, config_version=1, endpoint="https://example.invalid/v1")
    disclosures = DisclosureService(review.state.db)
    deps = bind_task_deps(review.state.db, source, actor_id=review.owner,
        task_id=view["task_id"], disclosures=disclosures)
    with pytest.raises(Slice1Error):
        deps.authorize_payload(b"synthetic", source.endpoint, deps.disclosure_scope)
    receipt = disclosures.create(actor_id=review.owner, task_id=view["task_id"],
        config_id=config, content_category="generated_text", scope_snapshot=deps.disclosure_scope)
    deps.authorize_payload(b"synthetic", source.endpoint, deps.disclosure_scope)
    with review.state.db.session() as sess:
        sess.get(DisclosureGrantRow, receipt["grant_id"]).revoked_at = utcnow()
    with pytest.raises(Slice1Error):
        deps.authorize_payload(b"synthetic", source.endpoint, deps.disclosure_scope)
    review.service.tasks.request_cancel(view["task_id"], actor_id=review.owner)


def test_generation_late_after_deletion_cannot_restore_review_rows(review, monkeypatch):
    view = review.service.create(review.owner, review.course, "late-delete", "Asia/Shanghai")
    def late_compute(task):
        review.state.deletion.delete_course(review.course, actor_id=review.owner)
        return []
    monkeypatch.setattr(review.service, "_compute", late_compute)
    review.service._run(view["task_id"])
    with review.state.db.session() as sess:
        assert sess.get(ReviewSessionRow, view["session_id"]) is None
        assert sess.get(ReviewCardRow, review.record) is None
    assert review.service.tasks.get_task(view["task_id"])["status"] != "succeeded"
