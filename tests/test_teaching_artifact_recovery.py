"""Artifact repair on a disposable PostgreSQL schema with synthetic models only."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from backend.agents.task_binding import AgentBudgetRow
from backend.planning.models import ConversationRow, LessonRow
from backend.slice1.errors import Slice1Error
from backend.slice1.models import DisclosureGrantRow
from backend.tasks.service import LearningTaskRow
from backend.teaching.models import BoardSceneRow, TeachingStepRow
from test_native_teaching_authorization import env, approve, consent  # noqa: F401


def missing_first(env, *, second=False):
    started = approve(env)
    service = env.service
    lease = service.tasks.claim(started["task_id"], lease_owner="synthetic-corruption")
    with env.db.session() as sess:
        budget = sess.get(AgentBudgetRow, started["task_id"])
        budget.logical_requests = 1
    service.tasks.submit_result(lease, stage="generate", status="succeeded")
    if second:
        with env.db.session() as sess:
            conv = sess.get(ConversationRow, started["conversation_id"])
            task = service.tasks.create_task(owner_user_id=env.owner, kind="lesson_step_generate",
                subject_id=f"{conv.conversation_id}:2:1", idempotency_key=f"step:{conv.conversation_id}:2:1",
                stage="generate", lane="teaching_prefetch", session=sess)
            service.authorization.bind_task(sess, conv, task["task_id"])
        service.prompts.clone_for_task(task["task_id"], started["task_id"], "teaching_step_v1")
        lease = service.tasks.claim(task["task_id"], lease_owner="synthetic-corruption")
        service.tasks.submit_result(lease, stage="generate", status="succeeded")
    return started


def repair_tasks(env):
    with env.db.session() as sess:
        return list(sess.scalars(select(LearningTaskRow).where(LearningTaskRow.owner_user_id == env.owner,
            LearningTaskRow.idempotency_key.like("artifact-repair:%")).order_by(LearningTaskRow.created_at)))


def test_workspace_reports_success_without_artifact_and_does_not_create_tasks(env):
    started = missing_first(env)
    view = env.service.get_workspace(started["conversation_id"], actor_id=env.owner)
    assert view["steps"] == [] and view["queued_tasks"] == []
    assert view["artifact_recovery"]["status"] == "missing"
    assert view["artifact_recovery"]["source_task_id"] == started["task_id"]
    assert view["required_action"] == "retry"
    assert env.service.run_step_generation(started["task_id"])["artifact_missing"]
    assert repair_tasks(env) == [] and env.calls == []
    with env.db.session() as sess:
        assert sess.get(LearningTaskRow, started["task_id"]).status == "succeeded"


def test_explicit_recovery_creates_one_new_task_and_preserves_source_budget(env):
    started = missing_first(env)
    recovered = env.service.recover_lesson(started["conversation_id"], actor_id=env.owner)
    repeated = env.service.recover_lesson(started["conversation_id"], actor_id=env.owner)
    assert recovered["task_id"] == repeated["task_id"] != started["task_id"]
    assert recovered["status"] == "queued" and len(repair_tasks(env)) == 1
    assert env.service.get_workspace(started["conversation_id"], actor_id=env.owner)["artifact_recovery"]["status"] == "repairing"
    with env.db.session() as sess:
        original = sess.get(LearningTaskRow, started["task_id"])
        repair = sess.get(LearningTaskRow, recovered["task_id"])
        assert original.status == "succeeded" and original.generation == 1
        assert repair.subject_id == original.subject_id and repair.status == "queued"
        assert sess.get(AgentBudgetRow, original.task_id).logical_requests == 1
    assert env.service.run_step_generation(recovered["task_id"])["status"] == "succeeded"
    view = env.service.get_workspace(started["conversation_id"], actor_id=env.owner)
    assert len(view["steps"]) == 1 and view["steps"][0]["board"]
    assert view["artifact_recovery"] is None
    assert env.service.recover_lesson(started["conversation_id"], actor_id=env.owner)["status"] == "ready"
    assert len(repair_tasks(env)) == 1


def test_concurrent_recovery_has_one_idempotent_repair_task(env):
    started = missing_first(env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: env.service.recover_lesson(started["conversation_id"], actor_id=env.owner), range(2)))
    assert results[0]["task_id"] == results[1]["task_id"]
    assert len(repair_tasks(env)) == 1


def test_repeated_start_persists_new_confirmation_and_repairs_same_conversation(env):
    started = missing_first(env)
    root = consent(env)
    env.service.disclosures.revoke(root.content_root_grant_id, actor_id=env.owner)
    repeated = approve(env, tts=True, prewarm=True)
    assert repeated["conversation_id"] == started["conversation_id"]
    assert repeated["task_id"] != started["task_id"] and len(repair_tasks(env)) == 1
    assert consent(env).authorization_id != root.authorization_id
    assert env.service.run_step_generation(repeated["task_id"])["status"] == "succeeded"


def test_recovery_confirmation_creates_repair_instead_of_retrying_success(env):
    started = missing_first(env)
    result = approve(env, purpose="recover", conversation_id=started["conversation_id"])
    assert result["status"] == "queued" and result["task_id"] != started["task_id"]
    with env.db.session() as sess:
        assert sess.get(LearningTaskRow, started["task_id"]).generation == 1
    assert env.service.run_step_generation(result["task_id"])["status"] == "succeeded"


def test_two_completed_repairs_without_artifacts_exhaust_limit(env):
    started = missing_first(env)
    for number in (1, 2):
        result = env.service.recover_lesson(started["conversation_id"], actor_id=env.owner)
        assert result["artifact_recovery"]["attempts"] == number
        lease = env.service.tasks.claim(result["task_id"], lease_owner="synthetic-corruption")
        env.service.tasks.submit_result(lease, stage="generate", status="succeeded")
    exhausted = env.service.recover_lesson(started["conversation_id"], actor_id=env.owner)
    assert exhausted["status"] == "unavailable"
    assert exhausted["artifact_recovery"]["status"] == "exhausted"
    assert not exhausted["artifact_recovery"]["available"]
    assert len(repair_tasks(env)) == 2
    view = env.service.get_workspace(started["conversation_id"], actor_id=env.owner)
    assert view["required_action"] is None and view["artifact_recovery"]["status"] == "exhausted"


def test_failed_repairs_are_not_unbounded_normal_retries(env):
    started = missing_first(env)
    for _ in range(2):
        result = approve(env, purpose="recover", conversation_id=started["conversation_id"])
        lease = env.service.tasks.claim(result["task_id"], lease_owner="synthetic-failure")
        env.service.tasks.submit_result(lease, stage="generate", status="failed", error_code="OUTPUT_INVALID")
    result = approve(env, purpose="recover", conversation_id=started["conversation_id"])
    assert result["artifact_recovery"]["status"] == "exhausted"
    assert len(repair_tasks(env)) == 2
    assert all(task.status == "failed" and task.generation == 1 for task in repair_tasks(env))
    workspace = env.service.get_workspace(started["conversation_id"], actor_id=env.owner)
    assert workspace["required_action"] is None
    assert all(not task["retryable"] and not task["recovery"]["available"] and task["required_action"] is None
        for task in workspace["queued_tasks"] if task["task_id"] in {row.task_id for row in repair_tasks(env)})


def test_revoked_deleted_and_stale_targets_cannot_create_repair(env):
    started = missing_first(env)
    root = consent(env)
    env.service.disclosures.revoke(root.content_root_grant_id, actor_id=env.owner)
    with pytest.raises(Slice1Error):
        env.service.recover_lesson(started["conversation_id"], actor_id=env.owner)
    with pytest.raises(Exception) as caught:
        env.service.recover_lesson(started["conversation_id"], actor_id="other-owner")
    assert caught.value.code == "conversation_not_found"
    assert repair_tasks(env) == [] and env.calls == []


def test_second_success_without_artifact_is_repaired_after_first(env):
    started = missing_first(env, second=True)
    repair = env.service.recover_lesson(started["conversation_id"], actor_id=env.owner)
    assert env.service.run_step_generation(repair["task_id"])["status"] == "succeeded"
    tasks = repair_tasks(env)
    assert len(tasks) == 2
    second = next(task for task in tasks if task.subject_id.endswith(":2:1"))
    assert env.service.run_step_generation(second.task_id)["status"] == "succeeded"
    view = env.service.get_workspace(started["conversation_id"], actor_id=env.owner)
    assert [step["step_no"] for step in view["steps"]] == [1, 2]


def test_missing_board_repair_preserves_existing_text_and_scene_versions(env):
    started = approve(env)
    assert env.service.run_step_generation(started["task_id"])["status"] == "succeeded"
    with env.db.session() as sess:
        step = sess.scalar(select(TeachingStepRow).where(TeachingStepRow.conversation_id == started["conversation_id"]))
        original = (step.step_id, step.text, step.text_version_no, step.source_task_id)
        step.board_scene_id = None
    result = env.service.recover_lesson(started["conversation_id"], actor_id=env.owner)
    assert env.service.run_step_generation(result["task_id"])["status"] == "succeeded"
    with env.db.session() as sess:
        step = sess.get(TeachingStepRow, original[0])
        assert (step.step_id, step.text, step.text_version_no, step.source_task_id) == original
        assert step.board_scene_id
        assert sess.scalar(select(func.count()).select_from(BoardSceneRow).where(
            BoardSceneRow.conversation_id == started["conversation_id"], BoardSceneRow.step_no == 1)) == 2


def test_workspace_tts_authorized_is_independent_from_saved_config(env):
    started = approve(env)
    view = env.service.get_workspace(started["conversation_id"], actor_id=env.owner)
    assert view["tts_config"] and not view["tts_authorized"]
    approve(env, purpose="recover", conversation_id=started["conversation_id"], tts=True)
    assert env.service.get_workspace(started["conversation_id"], actor_id=env.owner)["tts_authorized"]


@pytest.mark.parametrize("change", ["child_revoke", "delete", "outline"])
def test_repair_respects_source_child_grant_and_target_fences(env, change):
    from backend.planning.models import utcnow
    started = missing_first(env)
    with env.db.session() as sess:
        if change == "child_revoke":
            source = sess.scalar(select(DisclosureGrantRow).where(
                DisclosureGrantRow.task_id == started["task_id"], DisclosureGrantRow.content_category == "query"))
            source.revoked_at = utcnow()
        elif change == "delete":
            sess.get(ConversationRow, started["conversation_id"]).deleted_at = utcnow()
        else:
            sess.get(LessonRow, env.lesson).status_preparation = "generating"
            from backend.planning.models import CourseRow
            sess.get(CourseRow, env.course).current_outline_version_id = None
    with pytest.raises(Exception):
        env.service.recover_lesson(started["conversation_id"], actor_id=env.owner)
    assert repair_tasks(env) == [] and env.calls == []


def test_repair_publication_checks_lease_and_does_not_publish_cancelled_task(env):
    started = missing_first(env)
    result = env.service.recover_lesson(started["conversation_id"], actor_id=env.owner)
    with env.db.session() as sess:
        sess.get(LearningTaskRow, result["task_id"]).cancel_requested = True
    from backend.tasks.service import LeaseError
    with pytest.raises(LeaseError):
        env.service.run_step_generation(result["task_id"])
    with env.db.session() as sess:
        assert not sess.scalar(select(TeachingStepRow.step_id).where(
            TeachingStepRow.conversation_id == started["conversation_id"]))
        assert sess.get(AgentBudgetRow, started["task_id"]).logical_requests == 1
    assert env.calls == []


def test_repair_cannot_replace_revoked_source_child_grant(env):
    started = missing_first(env, second=True)
    with env.db.session() as sess:
        child = sess.scalar(select(LearningTaskRow).where(
            LearningTaskRow.subject_id == started["conversation_id"] + ":2:1"))
        grant = sess.scalar(select(DisclosureGrantRow).where(
            DisclosureGrantRow.task_id == child.task_id,
            DisclosureGrantRow.content_category == "query"))
        from backend.planning.models import utcnow, ConversationRow
        grant.revoked_at = utcnow()
    with pytest.raises(Exception) as caught:
        with env.db.session() as sess:
            conv = sess.get(ConversationRow, started["conversation_id"])
            env.service._ensure_artifact_repair(sess, conv, 2)
    assert caught.value.code.lower() == "disclosure_revoked"
    assert repair_tasks(env) == [] and env.calls == []
