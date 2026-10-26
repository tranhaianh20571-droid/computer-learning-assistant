"""Whole-course preparation: real persistence/leases, synthetic provider responses."""
# ruff: noqa: F811

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from backend.agents.task_binding import AgentBudgetRow, bind_task_deps
from backend.planning.models import (
    ConversationRow,
    CourseRow,
    LessonProgressRow,
    utcnow,
)
from backend.slice1.config_service import ConfigService
from backend.slice1.disclosure_service import DisclosureService
from backend.tasks.service import LearningTaskRow
from backend.teaching.course_preparation import (
    CoursePreparation,
    aware,
    debit_wire,
)
from backend.teaching.learning_models import PreparedFirstSegmentRow
from backend.teaching.preparation_models import (
    CoursePreparationRow,
    PreparationConnectionRow,
    PreparationItemRow,
)
from sqlalchemy import func, select
from test_course_library import confirm, draft, env  # noqa: F401
from test_native_teaching_authorization import isolated_scheduler  # noqa: F401
from test_teaching_assessments import LearningAdapter


@pytest.fixture
def database(isolated_scheduler):
    """A whole-queue test must not drain earlier modules' outstanding courses."""
    return isolated_scheduler.db


@pytest.fixture
def prepared(env, monkeypatch):
    _, state, owner, _ = env
    service = state.teaching
    configs = ConfigService(state.db, encryption_key="synthetic-course-preparation")
    model = configs.create(
        actor_id=owner,
        is_admin=False,
        kind="content_model",
        protocol="openai",
        endpoint="https://model.example.test/v1",
        model_name="synthetic",
        credentials={"api_key": "synthetic-key"},
    )
    service.configs, service.disclosures = (
        configs,
        DisclosureService(state.db, state.audit),
    )
    service.authorization.automatic_enabled = True
    calls = []

    def factory(current, task_id):
        view = configs.get(model["config_id"], actor_id=owner, is_admin=False)

        class Adapter(LearningAdapter):
            def generate(self, messages, **options):
                calls.append(task_id)
                return super().generate(messages, **options)

        source = Adapter()
        source.config_id, source.config_version = (
            view["config_id"],
            view["config_version"],
        )
        source.endpoint = view["endpoint"]
        source.agent_deps = bind_task_deps(
            state.db,
            source,
            actor_id=current,
            task_id=task_id,
            disclosures=service.disclosures,
        )
        return source

    factory.config_for_owner = lambda current: model if current == owner else None
    service.model_adapter_factory = factory
    payload = {
        "title": "Synthetic background course",
        "units": [
            {
                "title": "Synthetic unit one",
                "lessons": [
                    {
                        "title": "Read a value",
                        "goal": "Read a value using a unique key.",
                    },
                    {
                        "title": "Update a value",
                        "goal": "Update a value using a unique key.",
                    },
                ],
            },
            {
                "title": "Synthetic unit two",
                "lessons": [
                    {"title": "Check a key", "goal": "Check that a unique key exists."}
                ],
            },
        ],
    }
    course, proposal = draft(env, payload, kind="course_outline")
    assert confirm(env, proposal).status_code == 200
    flow = service.course_preparation
    try:
        yield SimpleNamespace(
            env=env,
            state=state,
            service=service,
            owner=owner,
            course=course,
            flow=flow,
            configs=configs,
            model=model,
            calls=calls,
        )
    finally:
        state.assessments.scopes.pool.shutdown(wait=True, cancel_futures=True)
        with state.db.session() as sess:
            for item in sess.scalars(
                select(PreparationItemRow).where(PreparationItemRow.course_id == course)
            ):
                item.state = "cancelled"
                task = sess.get(LearningTaskRow, item.task_id) if item.task_id else None
                if task and task.status in ("queued", "leased", "running"):
                    task.cancel_requested = True
                    service.tasks._terminal(
                        sess,
                        task,
                        status="cancelled",
                        error_code="TASK_CANCELLED",
                        now=utcnow(),
                    )


def test_invalid_credentials_pause_sibling_units_on_the_same_connection(prepared):
    p = prepared
    p.flow.reconcile()
    scopes = [r for r in items(p) if r.kind == "scope"]
    task = p.service.tasks.get_task(scopes[0].task_id)
    p.service.tasks.fail_queued(
        task["task_id"],
        expected_generation=task["generation"],
        expected_revision=task["revision"],
        error_code="MODEL_CREDENTIAL_INVALID",
    )
    due(p)
    p.flow.reconcile()
    with p.state.db.session() as sess:
        other = sess.get(LearningTaskRow, scopes[1].task_id)
        assert not p.flow.eligible(sess, other)
        lesson = next(
            r.lesson_id
            for r in items(p)
            if r.unit_id == scopes[1].unit_id and r.lesson_id
        )
        assert p.flow.view(sess, p.course, lesson, p.owner) == {
            "state": "action_required",
            "action": "settings",
        }
    assert not p.calls


def items(p):
    with p.state.db.session() as sess:
        return list(
            sess.scalars(
                select(PreparationItemRow)
                .where(PreparationItemRow.course_id == p.course)
                .order_by(PreparationItemRow.priority)
            )
        )


@pytest.mark.parametrize("pointer", ["missing", "foreign"])
def test_stale_legacy_outline_does_not_abort_other_course_registration(prepared, pointer):
    p = prepared
    with p.state.db.session() as sess:
        own_outline = sess.get(CourseRow, p.course).current_outline_version_id
        sess.add(CourseRow(course_id="synthetic-stale-course", owner_user_id=p.owner,
            title_ref="Synthetic stale course",
            current_outline_version_id="synthetic-missing-outline" if pointer == "missing" else own_outline))
    p.flow.reconcile()
    assert any(row.task_id for row in items(p) if row.kind == "scope")
    with p.state.db.session() as sess:
        assert not sess.scalar(select(CoursePreparationRow).where(
            CoursePreparationRow.course_id == "synthetic-stale-course"))


def due(p):
    with p.state.db.session() as sess:
        for item in sess.scalars(
            select(PreparationItemRow).where(PreparationItemRow.course_id == p.course)
        ):
            if item.state not in ("paused", "action_required", "cancelled"):
                item.next_run_at = utcnow() - timedelta(seconds=1)


def advance(p, rounds=8):
    for _ in range(rounds):
        due(p)
        p.flow.reconcile()
        for item in items(p):
            if not item.task_id or item.state not in ("running", "waiting"):
                continue
            with p.state.db.session() as sess:
                task = sess.get(LearningTaskRow, item.task_id)
                runnable = task and task.status == "queued"
            if runnable:
                if task.kind == "lesson_step_generate":
                    p.service.run_step_generation(item.task_id)
                elif item.kind == "scope":
                    p.state.assessments.scopes._run(item.task_id)
                else:
                    p.service.preparation.run(item.task_id)


def test_confirmation_atomically_registers_all_units_and_lessons_without_learning(
    prepared,
):
    p = prepared
    rows = items(p)
    assert len(rows) == 5
    assert [r.kind for r in rows].count("scope") == 2
    assert [r.kind for r in rows].count("first") == 3
    assert all(r.task_id is None for r in rows)
    with p.state.db.session() as sess:
        assert sess.scalar(
            select(CoursePreparationRow).where(
                CoursePreparationRow.course_id == p.course
            )
        )
        assert not sess.scalar(
            select(ConversationRow.conversation_id).where(
                ConversationRow.course_id == p.course
            )
        )
        assert not sess.scalar(
            select(LessonProgressRow.progress_id).where(
                LessonProgressRow.owner_user_id == p.owner
            )
        )


def test_all_units_and_first_segments_finish_without_opening_any_course(prepared):
    p = prepared
    advance(p)
    assert {r.state for r in items(p)} == {"ready"}
    with p.state.db.session() as sess:
        cached = list(
            sess.scalars(
                select(PreparedFirstSegmentRow).where(
                    PreparedFirstSegmentRow.course_id == p.course
                )
            )
        )
        assert len(cached) == 3 and all(
            r.persistent and r.candidate_json != "{}" for r in cached
        )
        assert not sess.scalar(
            select(ConversationRow.conversation_id).where(
                ConversationRow.course_id == p.course
            )
        )
    before = len(p.calls)
    advance(p, 2)
    assert len(p.calls) == before


def test_scope_dependency_and_failed_unit_do_not_block_other_units(prepared):
    p = prepared
    p.flow.reconcile()
    rows = items(p)
    assert all(r.task_id is None for r in rows if r.kind == "first")
    broken = next(r for r in rows if r.kind == "scope")
    info = p.service.tasks.get_task(broken.task_id)
    p.service.tasks.fail_queued(
        broken.task_id,
        expected_generation=info["generation"],
        expected_revision=info["revision"],
        error_code="MODEL_PROVIDER_ERROR",
    )
    advance(p, 1)
    with p.state.db.session() as sess:
        task = sess.get(LearningTaskRow, broken.task_id)
        assert task.status == "failed"
    assert any(
        r.task_id
        and r.unit_id != broken.unit_id
        and p.service.tasks.get_task(r.task_id)["status"] == "succeeded"
        for r in items(p)
    )
    assert all(
        r.task_id is None
        for r in items(p)
        if r.kind == "first" and r.unit_id == broken.unit_id
    )


@pytest.mark.parametrize("retry_code",["MODEL_TIMEOUT","TASK_DEADLINE_EXCEEDED","MODEL_PROVIDER_ERROR"])
def test_retry_survives_restart_and_keeps_task_budget_and_backoff(prepared,retry_code):
    p = prepared
    p.flow.reconcile()
    item = next(r for r in items(p) if r.kind == "scope")
    info = p.service.tasks.get_task(item.task_id)
    p.service.tasks.fail_queued(
        item.task_id,
        expected_generation=info["generation"],
        expected_revision=info["revision"],
        error_code=retry_code,
    )
    p.flow = CoursePreparation(p.service)
    due(p)
    p.flow.reconcile()
    row = next(r for r in items(p) if r.item_id == item.item_id)
    assert (
        row.state == "retrying"
        and row.failures == 1
        and aware(row.next_run_at) > utcnow()
    )
    with p.state.db.session() as sess:
        sess.get(AgentBudgetRow, item.task_id).wire_requests = 2
    p.flow.reconcile()
    assert p.service.tasks.get_task(item.task_id)["status"] == "failed"
    due(p)
    p.flow.reconcile()
    assert p.service.tasks.get_task(item.task_id)["status"] == "queued"
    with p.state.db.session() as sess:
        assert sess.get(AgentBudgetRow, item.task_id).wire_requests == 2


def test_persistent_segments_survive_old_cache_entry_and_ttl_limits(prepared):
    p = prepared
    advance(p)
    with p.state.db.session() as sess:
        for row in sess.scalars(
            select(PreparedFirstSegmentRow).where(
                PreparedFirstSegmentRow.course_id == p.course
            )
        ):
            row.expires_at = utcnow() - timedelta(days=30)
    p.service.preparation.max_entries = 1
    p.service.preparation.max_bytes = 1
    p.service.preparation.cleanup()
    p.service.preparation.enforce_limits(p.owner)
    with p.state.db.session() as sess:
        assert (
            sess.scalar(
                select(func.count())
                .select_from(PreparedFirstSegmentRow)
                .where(PreparedFirstSegmentRow.course_id == p.course)
            )
            == 3
        )


def test_clicking_ready_lesson_reuses_prepared_artifacts(prepared):
    p = prepared
    advance(p)
    lesson = next(r.lesson_id for r in items(p) if r.kind == "first")
    before = len(p.calls)
    result = p.env[0].post(
        f"/api/courses/{p.course}/lessons/{lesson}/start", headers=p.env[3]
    )
    assert result.status_code == 201, result.text
    assert result.json()["cache_hit"]
    view = p.service.get_workspace(result.json()["conversation_id"], actor_id=p.owner)
    assert view["steps"][0]["text"] and len(p.calls) == before
    again = p.env[0].post(
        f"/api/courses/{p.course}/lessons/{lesson}/start", headers=p.env[3]
    )
    assert again.json()["conversation_id"] == result.json()["conversation_id"]
    assert len(p.calls) == before


def test_connection_circuit_and_daily_wire_budget_are_persistent(prepared, monkeypatch):
    p = prepared
    p.flow.reconcile()
    row = next(r for r in items(p) if r.task_id)
    with p.state.db.session() as sess:
        item = sess.get(PreparationItemRow, row.item_id)
        for _ in range(3):
            p.flow._failure(sess, item, "MODEL_PROVIDER_ERROR")
        gate = sess.get(PreparationConnectionRow, item.connection_key)
        assert aware(gate.cooldown_until) > utcnow()
    with p.state.db.session() as sess:
        task = sess.get(LearningTaskRow, row.task_id)
        with pytest.raises(Exception, match="PREPARATION_CONNECTION_WAIT"):
            debit_wire(sess, task)
    with p.state.db.session() as sess:
        gate = sess.get(PreparationConnectionRow, row.connection_key)
        gate.cooldown_until = utcnow() - timedelta(seconds=1)
        gate.probe_task_id = None
    monkeypatch.setenv("COURSE_PREPARATION_DAILY_REQUEST_LIMIT", "1")
    with p.state.db.session() as sess:
        debit_wire(sess, sess.get(LearningTaskRow, row.task_id))
    with p.state.db.session() as sess:
        with pytest.raises(Exception, match="PREPARATION_BUDGET_WAIT"):
            debit_wire(sess, sess.get(LearningTaskRow, row.task_id))
    with p.state.db.session() as sess:
        assert sess.get(PreparationConnectionRow, row.connection_key).wire_requests == 1


def test_failure_limit_and_readonly_user_projection(prepared):
    p = prepared
    row = next(r for r in items(p) if r.kind == "scope")
    with p.state.db.session() as sess:
        item = sess.get(PreparationItemRow, row.item_id)
        for _ in range(4):
            p.flow._failure(sess, item, "OUTPUT_INVALID")
        view = p.flow.view(
            sess,
            p.course,
            next(
                r.lesson_id
                for r in items(p)
                if r.unit_id == row.unit_id and r.lesson_id
            ),
            p.owner,
        )
        assert view == {"state": "paused", "action": "retry"}
        assert item.failures == 4
        assert not p.flow.view(sess, p.course, "missing", "other-owner")
    before = len(p.calls)
    p.flow.reconcile()
    assert len(p.calls) == before


def test_deleted_course_cancels_work_and_late_results_are_rejected(prepared):
    p = prepared
    p.flow.reconcile()
    row = next(r for r in items(p) if r.task_id)
    with p.state.db.session() as sess:
        sess.get(CourseRow, p.course).deleted_at = utcnow()
    p.flow.reconcile()
    assert {r.state for r in items(p)} == {"cancelled"}
    p.state.assessments.scopes._run(row.task_id)
    assert p.service.tasks.get_task(row.task_id)["status"] != "succeeded"
    assert not p.calls


def test_default_mentor_preheat_has_real_persona_and_is_consumable(prepared):
    p = prepared
    from backend.characters.models import CharacterPreferenceRow
    from backend.characters.schemas import Profile
    from backend.characters.service import CharacterService

    cards = CharacterService(p.state.db, p.configs, p.state.audit, p.state.storage)
    card = cards.create(
        p.owner,
        Profile(name="Synthetic patient tutor", personality="Patient and precise"),
    )
    with p.state.db.session() as sess:
        sess.add(
            CharacterPreferenceRow(
                owner_user_id=p.owner, card_id=card["card_id"], revision=1
            )
        )
    advance(p)
    assert {r.state for r in items(p)} == {"ready"}
    lesson = next(r.lesson_id for r in items(p) if r.kind == "first")
    with p.state.db.session() as sess:
        row = sess.scalar(
            select(PreparedFirstSegmentRow).where(
                PreparedFirstSegmentRow.lesson_id == lesson
            )
        )
        assert (
            json.loads(row.context_json)["_prepared_character"]["card_id"]
            == card["card_id"]
        )
    before = len(p.calls)
    result = p.env[0].post(
        f"/api/courses/{p.course}/lessons/{lesson}/start", headers=p.env[3]
    )
    assert result.status_code == 201, result.text
    assert result.json()["cache_hit"]
    view = p.service.get_workspace(result.json()["conversation_id"], actor_id=p.owner)
    assert view["steps"][0]["character"]["card_id"] == card["card_id"]
    assert len(p.calls) == before


def test_scope_review_failure_resumes_validated_candidate_without_repeating_generation(
    prepared, monkeypatch
):
    p = prepared
    original = p.service.model_adapter_factory
    fail = [True]

    def factory(owner, task_id):
        source = original(owner, task_id)
        generate = source.generate

        def wrapped(messages, **options):
            from common_fixtures import synthetic_output_schema

            if fail[0] and "additions" in synthetic_output_schema(options).get(
                "properties", {}
            ):
                from backend.planning.errors import error

                raise error("MODEL_CONNECTION_ERROR")
            return generate(messages, **options)

        source.generate = wrapped
        return source

    factory.config_for_owner = original.config_for_owner
    p.service.model_adapter_factory = factory
    p.flow.reconcile()
    item = next(r for r in items(p) if r.kind == "scope")
    p.state.assessments.scopes._run(item.task_id)
    from backend.teaching.unit_scope import UnitScopeRow

    with p.state.db.session() as sess:
        row = sess.scalar(
            select(UnitScopeRow).where(UnitScopeRow.task_id == item.task_id)
        )
        checkpoint = json.loads(row.scope_json)["_preparation"]
        assert checkpoint["coverage"] and checkpoint["candidate"]
        assert sess.get(AgentBudgetRow, item.task_id).wire_requests == 3
    fail[0] = False
    due(p)
    p.flow.reconcile()
    due(p)
    p.flow.reconcile()
    p.state.assessments.scopes._run(item.task_id)
    assert p.service.tasks.get_task(item.task_id)["status"] == "succeeded"
    with p.state.db.session() as sess:
        assert sess.get(AgentBudgetRow, item.task_id).wire_requests == 4


def test_configuration_repair_rebinds_pending_scope_without_resetting_spend(prepared):
    p = prepared
    p.flow.reconcile()
    item = next(r for r in items(p) if r.kind == "scope")
    info = p.service.tasks.get_task(item.task_id)
    p.service.tasks.fail_queued(
        item.task_id,
        expected_generation=info["generation"],
        expected_revision=info["revision"],
        error_code="MODEL_CREDENTIAL_INVALID",
    )
    with p.state.db.session() as sess:
        sess.get(AgentBudgetRow, item.task_id).wire_requests = 1
    due(p)
    p.flow.reconcile()
    assert (
        next(r for r in items(p) if r.item_id == item.item_id).state
        == "action_required"
    )
    p.configs.update(
        p.model["config_id"],
        actor_id=p.owner,
        is_admin=False,
        credentials={"api_key": "synthetic-repaired-key"},
    )
    with p.state.db.session() as sess:
        sess.get(PreparationItemRow, item.item_id).next_run_at = utcnow() - timedelta(
            seconds=1
        )
    p.flow.reconcile()
    rebound = next(r for r in items(p) if r.item_id == item.item_id)
    assert rebound.task_id != item.task_id
    with p.state.db.session() as sess:
        assert sess.get(AgentBudgetRow, rebound.task_id).wire_requests == 1
    p.state.assessments.scopes._run(rebound.task_id)
    assert p.service.tasks.get_task(rebound.task_id)["status"] == "succeeded"


def test_click_during_scope_generation_uses_one_interactive_first_task(prepared):
    p = prepared
    lesson = next(r.lesson_id for r in items(p) if r.kind == "first")
    url = f"/api/courses/{p.course}/lessons/{lesson}/start"
    started = p.env[0].post(url, headers=p.env[3])
    assert started.status_code == 201, started.text
    repeated = p.env[0].post(url, headers=p.env[3])
    assert repeated.json()["conversation_id"] == started.json()["conversation_id"]
    advance(p)
    item = next(r for r in items(p) if r.lesson_id == lesson)
    assert item.state == "ready" and item.task_id == started.json()["task_id"]
    assert p.calls.count(item.task_id) == 1
    with p.state.db.session() as sess:
        assert not sess.scalar(
            select(PreparedFirstSegmentRow).where(
                PreparedFirstSegmentRow.lesson_id == lesson
            )
        )


def test_audio_failure_only_retries_audio_and_keeps_text(prepared):
    p = prepared
    voice = p.configs.create(
        actor_id=p.owner,
        is_admin=False,
        kind="tts",
        protocol="minimax",
        endpoint="https://voice.example.test",
        model_name="synthetic-voice",
        credentials={"api_key": "synthetic-voice-key", "voice_id": "synthetic-voice"},
    )
    speech = []

    def tts(owner, task_id):
        def synthesize(text):
            speech.append(task_id)
            if len(speech) == 1:
                from backend.slice1.errors import error

                raise error("timeout")
            return {
                "audio_hex": b"synthetic-audio".hex(),
                "subtitle": [{"text": text, "begin_ms": 0, "end_ms": 1000}],
                "audio_format": "mp3",
            }

        return SimpleNamespace(synthesize=synthesize)

    tts.config_for_owner = lambda owner: voice if owner == p.owner else None
    tts.disclosures = p.service.disclosures
    p.service.tts_adapter_factory = tts
    advance(p)
    assert {r.state for r in items(p)} == {"ready"}
    assert len(speech) == 4
    for item in items(p):
        if item.kind == "first":
            assert p.calls.count(item.task_id) == 1


def test_outline_publication_rollback_cannot_leave_preparation_obligations(
    prepared, monkeypatch
):
    p = prepared
    course, proposal = draft(
        p.env,
        {
            "title": "Synthetic rollback course",
            "units": [
                {"title": "Unit", "lessons": [{"title": "Read", "goal": "Read a key."}]}
            ],
        },
        kind="course_outline",
    )
    original = p.state.drafts._confirm_outline

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic publication interruption")

    monkeypatch.setattr(p.state.drafts, "_confirm_outline", fail)
    with pytest.raises(RuntimeError):
        p.state.drafts.confirm_draft(
            proposal["draft_id"],
            actor_id=p.owner,
            revision=proposal["revision"],
            idempotency_key="synthetic-rollback",
        )
    with p.state.db.session() as sess:
        assert sess.get(CourseRow, course).current_outline_version_id is None
        assert not sess.scalar(
            select(CoursePreparationRow).where(CoursePreparationRow.course_id == course)
        )
        assert not sess.scalar(
            select(PreparationItemRow).where(PreparationItemRow.course_id == course)
        )


def test_provider_retry_after_blocks_connection_despite_unrelated_success(prepared):
    p = prepared
    p.flow.reconcile()
    scoped = [r for r in items(p) if r.kind == "scope"]
    from backend.planning.errors import error

    failure = error("MODEL_RATE_LIMITED")
    failure.retry_after = "3600"
    p.flow.note_failure(scoped[0].task_id, failure)
    with p.state.db.session() as sess:
        first = sess.get(PreparationItemRow, scoped[0].item_id)
        p.flow._failure(sess, first, "MODEL_RATE_LIMITED")
        other = sess.get(PreparationItemRow, scoped[1].item_id)
        p.flow._success(sess, other)
        gate = sess.get(PreparationConnectionRow, first.connection_key)
        assert aware(gate.cooldown_until) > utcnow() + timedelta(minutes=59)
        assert not p.flow.eligible(sess, sess.get(LearningTaskRow, other.task_id))


def test_production_reconciliation_keeps_reserved_synthetic_endpoints_off_the_wire(
    prepared,
):
    p = prepared
    p.service.server_managed = True
    p.flow.reconcile()
    assert not p.calls
    assert all(not row.task_id for row in items(p))
    assert {row.state for row in items(p)} == {"action_required"}


def test_real_dispatcher_prepares_all_without_observed_classrooms(prepared):
    p = prepared
    from backend.teaching.background import TeachingBackground

    worker = TeachingBackground(p.service)
    try:
        for _ in range(24):
            due(p)
            worker._last_reconcile = 0
            worker.tick()
            for _, future, _ in list(worker.pending.values()):
                future.result(timeout=15)
            if {r.state for r in items(p)} == {"ready"}:
                break
        assert {r.state for r in items(p)} == {"ready"}
        assert not p.service.observed_sessions
    finally:
        worker.close()


def test_new_learning_memory_does_not_regenerate_ready_course_artifacts(
    prepared, monkeypatch
):
    p = prepared
    advance(p)
    before = len(p.calls)
    monkeypatch.setattr(p.service.memory, "version", lambda owner: 999)
    advance(p, 2)
    assert len(p.calls) == before
    assert {r.state for r in items(p)} == {"ready"}


def test_missing_board_of_interactive_first_segment_recovers_without_new_learning_session(
    prepared,
):
    p = prepared
    from backend.teaching.models import TeachingStepRow

    first = next(r for r in items(p) if r.kind == "first")
    started = (
        p.env[0]
        .post(
            f"/api/courses/{p.course}/lessons/{first.lesson_id}/start", headers=p.env[3]
        )
        .json()
    )
    advance(p)
    with p.state.db.session() as sess:
        step = sess.scalar(
            select(TeachingStepRow).where(
                TeachingStepRow.conversation_id == started["conversation_id"],
                TeachingStepRow.step_no == 1,
            )
        )
        text = step.text
        step.board_scene_id = None
        sess.get(PreparationItemRow, first.item_id).next_run_at = utcnow() - timedelta(
            seconds=1
        )
    p.flow.reconcile()
    repaired = next(r for r in items(p) if r.item_id == first.item_id)
    assert repaired.state == "running" and repaired.task_id != started["task_id"]
    assert p.service.run_step_generation(repaired.task_id)["status"] == "succeeded"
    view = p.service.get_workspace(started["conversation_id"], actor_id=p.owner)
    assert view["steps"][0]["text"] == text and view["steps"][0]["board"]


def test_disk_pressure_preserves_scope_and_automatically_resumes(prepared, monkeypatch):
    p = prepared
    monkeypatch.setattr(p.service.preparation, "resource_wait_reason", lambda: "storage")
    advance(p, 3)
    rows = items(p)
    assert all(r.state == "ready" for r in rows if r.kind == "scope")
    assert all(
        r.state == "waiting" and not r.task_id
        for r in rows
        if r.kind == "first"
    )
    with p.state.db.session() as sess:
        lesson = next(r.lesson_id for r in rows if r.kind == "first")
        view = p.flow.view(sess, p.course, lesson, p.owner)
        assert view == {"state": "paused", "action": None, "reason": "storage"}
        for row in rows:
            sess.get(PreparationItemRow, row.item_id).next_run_at = (
                utcnow() - timedelta(seconds=1)
            )
    monkeypatch.setattr(p.service.preparation, "resource_wait_reason", lambda: None)
    advance(p)
    assert {r.state for r in items(p)} == {"ready"}


def queue_firsts(p):
    p.flow.reconcile()
    for item in items(p):
        if item.kind == "scope":
            p.state.assessments.scopes._run(item.task_id)
            assert p.service.tasks.get_task(item.task_id)["status"] == "succeeded"
    due(p)
    p.flow.reconcile()
    return [item for item in items(p) if item.kind == "first"]


def test_claim_pressure_after_enqueue_survives_expiry_without_model_retry(prepared, monkeypatch):
    p = prepared
    first = queue_firsts(p)[0]
    blocked = SimpleNamespace(permits_heavy=lambda: False, wait_reason=lambda: "storage")
    monkeypatch.setattr(p.service.tasks, "resource_budget", blocked, raising=False)
    assert p.service.preparation.run(first.task_id)["status"] == "queued"
    with p.state.db.session() as sess:
        item = sess.get(PreparationItemRow, first.item_id)
        task = sess.get(LearningTaskRow, first.task_id)
        assert item.state == "waiting" and item.failures == 0
        assert item.error_code == "PREPARATION_STORAGE_WAIT"
        assert task.attempt == 0 and task.status == "queued"
        budget = sess.get(AgentBudgetRow, first.task_id)
        assert budget.logical_requests == budget.wire_requests == 0
        task.deadline_at = utcnow() - timedelta(seconds=1)
    p.service.tasks.sweep_expired_tasks(kinds=["lesson_first_prepare"])
    due(p)
    p.flow.reconcile()
    blocked.permits_heavy, blocked.wait_reason = lambda: True, lambda: None
    advance(p)
    restored = next(item for item in items(p) if item.item_id == first.item_id)
    assert restored.state == "ready" and restored.task_id == first.task_id
    assert restored.failures == 0


@pytest.mark.parametrize("invalid_outputs", [2, 3])
def test_persistent_first_allows_third_validation_request_with_same_total_budget(prepared, monkeypatch, invalid_outputs):
    from common_fixtures import synthetic_output_schema, synthetic_output_response

    p = prepared
    first = queue_firsts(p)[0]
    original_factory = p.service.model_adapter_factory
    requests = []

    def factory(owner, task):
        adapter = original_factory(owner, task)
        original_generate = adapter.generate

        def generate(messages, **options):
            if "steps" in synthetic_output_schema(options).get("properties", {}):
                requests.append(task)
                if len(requests) <= invalid_outputs:
                    return synthetic_output_response({"steps": []}, options)
            return original_generate(messages, **options)

        adapter.generate = generate
        return adapter

    factory.config_for_owner = original_factory.config_for_owner
    monkeypatch.setattr(p.service, "model_adapter_factory", factory)
    assert p.service.preparation.run(first.task_id)["status"] == "succeeded"
    assert len(requests) == invalid_outputs + 1
    with p.state.db.session() as sess:
        budget = sess.get(AgentBudgetRow, first.task_id)
        assert budget.request_limit == 8
        assert budget.logical_requests == budget.wire_requests == invalid_outputs + 1


def test_legacy_two_request_failure_recovers_once_without_refilling_budget(prepared):
    p = prepared
    first = queue_firsts(p)[0]
    lease = p.service.tasks.claim(first.task_id, lease_owner="synthetic-old-prewarm")
    p.service.tasks.submit_result(lease, stage="generate", status="failed",
                                 error_code="AGENT_LOOP_LIMIT")
    with p.state.db.session() as sess:
        budget = sess.get(AgentBudgetRow, first.task_id)
        budget.logical_requests = budget.wire_requests = 2
    due(p)
    p.flow.reconcile()
    assert next(item for item in items(p) if item.item_id == first.item_id).state == "paused"
    with p.state.db.session() as sess:
        sess.get(PreparationItemRow, first.item_id).next_run_at = utcnow() - timedelta(seconds=1)
    p.flow.reconcile()
    assert p.service.tasks.get_task(first.task_id)["status"] == "queued"
    assert p.service.preparation.run(first.task_id)["status"] == "succeeded"
    with p.state.db.session() as sess:
        budget = sess.get(AgentBudgetRow, first.task_id)
        assert budget.logical_requests == budget.wire_requests == 3


def test_paused_playback_does_not_block_durable_first_or_cached_first_publication(prepared):
    from backend.teaching.background import TeachingBackground
    from backend.teaching.models import PlaybackCheckpointRow
    from backend.teaching.recovery import recover_observed

    p = prepared
    first = queue_firsts(p)[0]
    started = p.service.start_lesson(p.course, first.lesson_id, actor_id=p.owner)
    lease = p.service.tasks.claim(started["task_id"], lease_owner="synthetic-old-classroom")
    p.service.tasks.submit_result(lease, stage="generate", status="failed", error_code="OUTPUT_INVALID")
    with p.state.db.session() as sess:
        sess.get(LearningTaskRow, started["task_id"]).updated_at = utcnow() - timedelta(minutes=1)
        sess.add(PlaybackCheckpointRow(checkpoint_id="synthetic-paused-persistent-first",
            conversation_id=started["conversation_id"],
            generation=1, step_no=1, position_ms=0, client_seq=1,
            board_state_json=json.dumps({"presentation": {"paused": True}})))
    background = TeachingBackground(p.service)
    try:
        with p.state.db.session() as sess:
            assert background.eligible(sess, sess.get(LearningTaskRow, first.task_id))
        assert p.service.preparation.run(first.task_id)["status"] == "succeeded"
        p.service.observed_sessions[started["conversation_id"]] = __import__("time").monotonic()
        with p.state.db.session() as sess:
            assert background.eligible(sess, sess.get(LearningTaskRow, started["task_id"]))
        recover_observed(background)
        assert p.service.tasks.get_task(started["task_id"])["status"] == "queued"
        assert p.service.run_step_generation(started["task_id"])["status"] == "succeeded"
        workspace = p.service.get_workspace(started["conversation_id"], actor_id=p.owner)
        assert workspace["steps"][0]["text"]
        assert workspace["last_checkpoint"]["presentation"]["paused"] is True
    finally:
        background.close()


@pytest.mark.parametrize("cache_linked", [True, False])
def test_exhausted_prewarm_uses_only_existing_observed_foreground_task(prepared, cache_linked):
    from time import monotonic

    p = prepared
    first = queue_firsts(p)[0]
    started = p.service.start_lesson(p.course, first.lesson_id, actor_id=p.owner)
    lease = p.service.tasks.claim(first.task_id, lease_owner="synthetic-exhausted-prewarm")
    p.service.tasks.submit_result(lease, stage="generate", status="failed", error_code="AGENT_LOOP_LIMIT")
    with p.state.db.session() as sess:
        budget = sess.get(AgentBudgetRow, first.task_id)
        budget.logical_requests = budget.wire_requests = 8
        cache = sess.get(PreparedFirstSegmentRow, first.artifact_id)
        cache.status = "failed"
        if not cache_linked:
            cache.consumed_conversation_id = None
        task = sess.get(LearningTaskRow, started["task_id"])
        task.deadline_at = utcnow() - timedelta(seconds=1)
    p.service.tasks.sweep_expired_tasks(kinds=["lesson_step_generate"])
    p.service.observed_sessions.clear()
    due(p)
    p.flow.reconcile()
    assert next(item for item in items(p) if item.item_id == first.item_id).task_id == first.task_id
    p.service.observed_sessions[started["conversation_id"]] = monotonic()
    with p.state.db.session() as sess:
        sess.get(PreparationItemRow, first.item_id).next_run_at = utcnow() - timedelta(seconds=1)
    p.flow.reconcile()
    replacement = next(item for item in items(p) if item.item_id == first.item_id)
    assert replacement.task_id == started["task_id"] and replacement.state == "running"
    assert p.service.run_step_generation(started["task_id"])["status"] == "succeeded"
    due(p)
    p.flow.reconcile()
    assert p.service.get_workspace(started["conversation_id"], actor_id=p.owner)["steps"][0]["text"]
    with p.state.db.session() as sess:
        old = sess.get(AgentBudgetRow, first.task_id)
        assert old.logical_requests == old.wire_requests == old.request_limit == 8
        assert sess.get(LearningTaskRow, first.task_id).status == "failed"
        assert sess.get(AgentBudgetRow, started["task_id"]).wire_requests > 0
