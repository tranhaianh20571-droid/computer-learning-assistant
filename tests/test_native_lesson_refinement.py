"""Offline persistent refinement for future plans, never frozen teaching content."""
import json
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import create_engine, select, func
from sqlalchemy.pool import StaticPool

from backend.agents.task_binding import AgentBudgetRow
from backend.auth.models import UserRow
from backend.teaching.unit_scope import UnitScopeRow
from backend.logging_audit.db import Database
from backend.logging_audit.models import TaskStateRow, TaskEventRow
from backend.logging_audit.repositories import PgTaskEventStore
from backend.planning.models import (CourseRow, OutlineVersionRow, CourseUnitRow, LessonRow,
    LessonContentVersionRow, ConversationRow, PlanningTaskStateRow, RouteVersionRow)
from backend.prompts.service import PromptService, PromptBindingRow
from backend.tasks.service import TaskService, LearningTaskRow, TaskAttemptRow
from backend.teaching.learning_models import LessonBlueprintRow, PreparedLessonPlanRow
from backend.teaching.models import TeachingStepRow
from backend.teaching.pedagogy import lightweight_plan, lesson_context, plan_key, prepared_plan, plan_lesson
from backend.teaching.preparation import LessonPreparation
from backend.teaching.service import TeachingService


@pytest.fixture
def environment():
    db = Database('sqlite+pysqlite:///:memory:')
    db.engine.dispose()
    db.engine = create_engine('sqlite+pysqlite:///:memory:', poolclass=StaticPool,
        connect_args={'check_same_thread': False})
    db._factory.configure(bind=db.engine)
    for model in (UserRow, UnitScopeRow, CourseRow, OutlineVersionRow, CourseUnitRow, LessonRow, LessonContentVersionRow,
                  ConversationRow, PlanningTaskStateRow, RouteVersionRow, LessonBlueprintRow,
                  PreparedLessonPlanRow, TeachingStepRow, PromptBindingRow, LearningTaskRow,
                  AgentBudgetRow, TaskAttemptRow, TaskStateRow, TaskEventRow):
        model.__table__.create(db.engine)
    events = PgTaskEventStore(db)
    tasks = TaskService(db, events)
    prompts = PromptService(db)
    calls = []
    state = SimpleNamespace(config_version=1, callback=None, invalid=False)
    service = TeachingService(db, tasks, events, prompts, SimpleNamespace(), None, None)
    def model_request(messages, info):
        calls.append(info.model_request_parameters.output_mode)
        if state.callback:
            state.callback()
        plan = lightweight_plan(context)
        plan.stages[-1].purpose = 'Refined future summary'
        if state.invalid:
            plan.goals = []
        return ModelResponse(parts=[TextPart(plan.model_dump_json())])
    def factory(owner, task_id):
        return FunctionModel(model_request, profile={'supports_json_schema_output': True,
            'supports_tools': True, 'supports_text_output': True})
    factory.config_for_owner = lambda owner: {'config_id': 'synthetic', 'config_version': state.config_version}
    service.model_adapter_factory = factory
    service.preparation = LessonPreparation(service)
    source = tasks.create_task(owner_user_id='owner', kind='generic', subject_id='source',
        idempotency_key='source')['task_id']
    prompts.bind_for_task(source, 'lesson_blueprint_v1')
    with db.session() as sess:
        sess.add(CourseRow(course_id='course', owner_user_id='owner', title_ref='Synthetic course',
            current_outline_version_id='outline'))
        sess.add(OutlineVersionRow(outline_version_id='outline', course_id='course'))
        sess.add(CourseUnitRow(unit_id='unit', outline_version_id='outline', title_ref='Synthetic unit'))
        sess.add(LessonRow(lesson_id='lesson', unit_id='unit', outline_version_id='outline',
            title_ref='Synthetic lesson', goal_ref='Understand synthetic state'))
        sess.add(LessonContentVersionRow(content_version_id='content', lesson_id='lesson'))
        conv = ConversationRow(conversation_id='conversation', owner_user_id='owner', kind='teaching',
            generation=1, course_id='course', lesson_id='lesson', outline_version_id='outline',
            lesson_content_version_id='content')
        sess.add(conv)
    context = lesson_context(service, conv)
    frozen = lightweight_plan(context).model_dump_json()
    with db.session() as sess:
        sess.add(LessonBlueprintRow(conversation_id='conversation', owner_user_id='owner', lesson_id='lesson',
            outline_version_id='outline', content_version_id='content', plan_json=frozen,
            context_json=json.dumps(context), source_task_id=source))
        sess.add(TeachingStepRow(step_id='first', conversation_id='conversation', step_no=1,
            generation=1, text='Synthetic frozen first text', status='ready', source_task_id=source))
    yield service, conv, context, calls, state, frozen
    db.dispose()


def queue(env):
    return env[0].preparation.queue_refinement('conversation', 'owner')['task_id']


def assert_frozen(env):
    service, _, _, _, _, frozen = env
    with service.db.session() as sess:
        assert sess.get(LessonBlueprintRow, 'conversation').plan_json == frozen
        assert sess.get(TeachingStepRow, 'first').text == 'Synthetic frozen first text'


def test_enqueue_is_persistent_idempotent_and_does_not_call_model(environment):
    service, conv, context, calls, _, _ = environment
    task_id = queue(environment)
    assert queue(environment) == task_id
    assert calls == []
    with service.db.session() as sess:
        task = sess.get(LearningTaskRow, task_id)
        assert task.kind == 'lesson_plan_refine' and task.lane == 'maintenance'
        assert task.subject_id == 'conversation:' + plan_key(service, conv, context)
        assert task.status == 'queued' and task.model_config_version == 'synthetic:1'
        assert sess.get(AgentBudgetRow, task_id).request_limit == 3
        assert service.preparation.refinement_eligible(sess, task)
    assert service.prompts.resolve_for_task(task_id, 'lesson_blueprint_v1')['template_sha256'] == (
        service.prompts.load_local_template('lesson_blueprint_v1')['sha256'])
    assert_frozen(environment)


def test_native_worker_writes_only_future_cache_with_durable_budget(environment):
    service, conv, context, calls, _, _ = environment
    task_id = queue(environment)
    assert service.preparation.run_refinement(task_id) == {'status': 'succeeded'}
    assert calls == ['native']
    with service.db.session() as sess:
        cache = sess.get(PreparedLessonPlanRow, plan_key(service, conv, context))
        assert cache.source == 'agent' and cache.owner_user_id == 'owner'
        budget = sess.get(AgentBudgetRow, task_id)
        assert budget.logical_requests == 1 and budget.request_limit == 3
        assert not service.preparation.refinement_eligible(sess, sess.get(LearningTaskRow, task_id))
    assert prepared_plan(service, conv, context).stages[-1].purpose == 'Refined future summary'
    assert plan_lesson(service, conv, task_id, None)[0].stages[-1].purpose != 'Refined future summary'
    assert service.preparation.queue_refinement('conversation', 'owner') == {'status': 'ready'}
    assert_frozen(environment)


@pytest.mark.parametrize('change', ['owner', 'course_delete', 'outline', 'conversation_delete',
    'inactive', 'content', 'context', 'config', 'cancel', 'template'])
def test_changed_domain_lease_or_cache_version_cannot_publish(environment, change):
    service, _, _, calls, state, _ = environment
    task_id = queue(environment)
    def change_after_model():
        state.callback = None
        with service.db.session() as sess:
            if change == 'owner':sess.get(CourseRow, 'course').owner_user_id = 'other'
            elif change == 'course_delete':
                from backend.planning.models import utcnow
                sess.get(CourseRow, 'course').deleted_at = utcnow()
            elif change == 'outline':sess.get(CourseRow, 'course').current_outline_version_id = 'other'
            elif change == 'conversation_delete':
                from backend.planning.models import utcnow
                sess.get(ConversationRow, 'conversation').deleted_at = utcnow()
            elif change == 'inactive':sess.get(ConversationRow, 'conversation').session_state = 'paused'
            elif change == 'content':
                sess.add(LessonContentVersionRow(content_version_id='content2', lesson_id='lesson', content_version_no=2))
            elif change == 'context':sess.get(LessonRow, 'lesson').goal_ref = 'changed'
            elif change == 'config':state.config_version = 2
            elif change == 'cancel':sess.get(LearningTaskRow, task_id).cancel_requested = True
            elif change == 'template':
                service.prompts.load_local_template = lambda name: {'sha256': 'changed'}
    state.callback = change_after_model
    assert service.preparation.run_refinement(task_id)['status'] in ('failed', 'stale_discarded')
    assert calls == ['native']
    with service.db.session() as sess:
        assert sess.scalar(select(func.count()).select_from(PreparedLessonPlanRow)) == 0
    assert_frozen(environment)


def test_invalid_output_uses_three_request_budget_and_leaves_frozen_content(environment):
    service, _, _, calls, state, _ = environment
    state.invalid = True
    task_id = queue(environment)
    assert service.preparation.run_refinement(task_id)['status'] == 'failed'
    assert calls == ['native'] * 3
    with service.db.session() as sess:
        assert sess.get(AgentBudgetRow, task_id).logical_requests == 3
        assert sess.get(LearningTaskRow, task_id).status == 'failed'
        assert sess.scalar(select(func.count()).select_from(PreparedLessonPlanRow)) == 0
    assert_frozen(environment)


def test_unpublished_or_foreign_target_never_enqueues(environment):
    service = environment[0]
    assert service.preparation.queue_refinement('conversation', 'other') == {'status': 'unavailable'}
    with service.db.session() as sess:
        sess.delete(sess.get(TeachingStepRow, 'first'))
    assert service.preparation.queue_refinement('conversation', 'owner') == {'status': 'unavailable'}
    assert environment[3] == []


def test_failed_event_write_rolls_back_refined_cache(environment, monkeypatch):
    service = environment[0]
    task_id = queue(environment)
    original = service.events.record
    def fail_success_event(*args, **kwargs):
        original(*args, **kwargs)
        if kwargs.get('status') == 'succeeded':
            raise RuntimeError('synthetic event persistence failure')
    monkeypatch.setattr(service.events, 'record', fail_success_event)
    assert service.preparation.run_refinement(task_id)['status'] == 'failed'
    with service.db.session() as sess:
        assert sess.scalar(select(func.count()).select_from(PreparedLessonPlanRow)) == 0
        assert sess.get(LearningTaskRow, task_id).status != 'succeeded'
    assert_frozen(environment)


def test_missing_prompt_or_unconfigured_model_does_not_enqueue(environment):
    service = environment[0]
    with service.db.session() as sess:
        binding = sess.scalars(select(PromptBindingRow)).first()
        sess.delete(binding)
    assert service.preparation.queue_refinement('conversation', 'owner') == {'status': 'unavailable'}
    service.model_adapter_factory = None
    assert service.preparation.queue_refinement('conversation', 'owner') == {'status': 'unconfigured'}
    assert environment[3] == []


def test_queue_does_not_create_disclosure_grants(environment):
    from backend.slice1.models import DisclosureGrantRow
    service = environment[0]
    DisclosureGrantRow.__table__.create(service.db.engine)
    queue(environment)
    with service.db.session() as sess:
        assert sess.scalar(select(func.count()).select_from(DisclosureGrantRow)) == 0


def test_versioned_cache_does_not_cross_model_content_or_owner(environment):
    service, conv, context, _, state, _ = environment
    original = plan_key(service, conv, context)
    state.config_version += 1
    assert plan_key(service, conv, context) != original
    state.config_version = 1
    with service.db.session() as sess:
        sess.add(LessonContentVersionRow(content_version_id='content2', lesson_id='lesson', content_version_no=2))
    assert plan_key(service, conv, context) != original
    key = plan_key(service, conv, context)
    forged = lightweight_plan(context)
    forged.stages[-1].purpose = 'Unauthorized cache'
    with service.db.session() as sess:
        sess.add(PreparedLessonPlanRow(cache_key=key, owner_user_id='other', course_id='course',
            lesson_id='lesson', plan_json=forged.model_dump_json(), source='agent'))
    assert prepared_plan(service, conv, context).stages[-1].purpose != 'Unauthorized cache'
