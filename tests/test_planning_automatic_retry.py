"""Synthetic fault experiments for durable course structure auto-repair; no provider traffic."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from backend.agents.task_binding import AgentBudgetRow
from backend.logging_audit.models import TaskEventRow
from backend.planning.errors import error
from backend.planning.models import PlanningTaskStateRow
from backend.planning.service import PlanningService
from backend.tasks.service import LearningTaskRow, utcnow
from common_fixtures import OUTLINE_OK_PAYLOAD
from test_planning_authorization import database, env, prepare, grants, mutate


@pytest.fixture(autouse=True)
def isolate_background_queue(database):
    with database.session() as sess:
        sess.execute(update(PlanningTaskStateRow).values(background_enabled=False))
    yield
    with database.session() as sess:
        sess.execute(update(PlanningTaskStateRow).values(background_enabled=False))


def partial_route(env):
    # These tests drive the dispatcher through tick(); keep the background thread
    # from racing a second auto-resume between assertions.
    prepare(env, automatic=True, dispatch=False)
    planning = env.state.planning
    lease = env.state.tasks.claim(env.task, lease_owner='synthetic-initial-worker')
    courses = [{'title': f'合成课程{i}', 'goal': f'合成目标{i}',
        'units': deepcopy(OUTLINE_OK_PAYLOAD['units']) if i < 4 else [],
        'prerequisites': [], 'refinement_status': 'ready' if i < 4 else 'failed',
        **({} if i < 4 else {'refinement_error': 'INVALID_OUTLINE_STRUCTURE'})} for i in range(6)]
    content = {'stages': [{'title': '合成阶段', 'courses': courses}], 'prerequisites': [], 'structure_version': 2}
    applied = env.state.drafts.apply_candidate(owner_user_id=env.owner, target_id=env.route,
        candidate_kind='route_generate', content=content, source_task_id=env.task, lease=lease)
    with env.db.session() as sess:
        row = sess.get(PlanningTaskStateRow, env.task)
        row.draft_id = applied['draft_id']
        row.scope_signals_json = json.dumps({'structure_revision': applied['draft_revision']})
        if sess.get(AgentBudgetRow, env.task) is None:
            sess.add(AgentBudgetRow(task_id=env.task))
    env.state.tasks.submit_result(lease, stage='generate', status='partial', duration_ms=0, preserve_partial=True)
    return planning, applied['draft_id'], content


def state(env):
    return env.state.planning.get_planning_state(env.task, actor_id=env.owner)


def tick(env):
    background = env.state.planning.background
    background.tick()
    for future in list(background.pending.values()):
        future.result(timeout=15)


def test_background_repairs_only_two_missing_courses_without_page_or_new_grant(env, monkeypatch):
    planning, draft_id, original = partial_route(env)
    calls = []
    def refine(adapter, task_state, course, prompt, budget, **kwargs):
        calls.append(course.title)
        return deepcopy(OUTLINE_OK_PAYLOAD)
    monkeypatch.setattr(planning, '_refine_course_units', refine)
    grant = grants(env)[0]['grant_id']
    with env.db.session() as sess:
        budget = sess.get(AgentBudgetRow, env.task)
        budget.logical_requests = budget.wire_requests = 12
        deadline = sess.get(LearningTaskRow, env.task).deadline_at
    assert state(env)['auto_retry_pending']
    tick(env)
    current = state(env)
    assert current['task_status'] == 'succeeded' and current['structure_status'] == 'ready'
    assert current['task_id'] == env.task and current['auto_retry_count'] == 0
    assert calls == ['合成课程4', '合成课程5']
    updated = env.state.drafts.get_draft(draft_id, actor_id=env.owner)['content']
    assert updated['stages'][0]['courses'][:4] == original['stages'][0]['courses'][:4]
    assert grants(env)[0]['grant_id'] == grant and len(grants(env)) == 1
    with env.db.session() as sess:
        budget = sess.get(AgentBudgetRow, env.task)
        assert budget.logical_requests == budget.wire_requests == 12
        assert sess.get(LearningTaskRow, env.task).deadline_at == deadline


def test_persistent_failure_stops_after_exactly_five_extra_rounds_and_cannot_confirm(env, monkeypatch):
    planning, draft_id, _ = partial_route(env)
    calls = []
    def fail(*args, **kwargs):
        calls.append(args[2].title)
        raise error('MODEL_TIMEOUT')
    monkeypatch.setattr(planning, '_refine_course_units', fail)
    for count in range(1, 6):
        tick(env)
        assert state(env)['auto_retry_count'] == count
        assert state(env)['task_status'] == 'partial'
    tick(env)
    assert len(calls) == 10 and state(env)['auto_retry_exhausted']
    assert not state(env)['auto_retry_pending'] and state(env)['auto_retry_limit'] == 5
    draft = env.state.drafts.get_draft(draft_id, actor_id=env.owner)
    assert [c['refinement_error'] for c in draft['content']['stages'][0]['courses'][4:]] == ['MODEL_TIMEOUT'] * 2
    rejected = env.client.post(f'/api/planning/drafts/{draft_id}/confirm', json={
        'revision': draft['revision'], 'idempotency_key': 'synthetic-incomplete-confirm'})
    assert rejected.status_code == 400
    with env.db.session() as sess:
        events = list(sess.scalars(select(TaskEventRow).where(TaskEventRow.task_id == env.task,
            TaskEventRow.status == 'retry_scheduled')))
        assert len(events) == 5


def test_concurrent_partial_resume_consumes_one_round_and_reads_are_pure(env):
    planning, draft_id, _ = partial_route(env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: planning.workspace.resume(env.task, env.owner,
            automatic=True, dispatch=False), range(2)))
    assert all(r['auto_retry_count'] == 1 for r in results)
    for _ in range(3):
        planning.get_planning_state(env.task, actor_id=env.owner)
        planning.workspace.current(env.owner)
    assert state(env)['auto_retry_count'] == 1 and state(env)['generation'] == 2
    assert env.wire.calls == []


@pytest.mark.parametrize('reason', ['deadline', 'revoked', 'edited', 'cancelled', 'confirmed', 'budget'])
def test_automatic_path_stops_at_authorization_version_and_task_fences(env, monkeypatch, reason):
    planning, draft_id, _ = partial_route(env)
    calls = []
    original_refine = planning._refine_course_units
    monkeypatch.setattr(planning, '_refine_course_units', lambda *a, **k: calls.append(1))
    if reason == 'deadline':
        mutate(env, LearningTaskRow, env.task, deadline_at=utcnow() - timedelta(seconds=1))
    elif reason == 'revoked':
        env.disclosures.revoke(grants(env)[0]['grant_id'], actor_id=env.owner)
    elif reason == 'edited':
        draft = env.state.drafts.get_draft(draft_id, actor_id=env.owner)
        env.state.drafts.save_draft(draft_id, actor_id=env.owner,
            revision=draft['revision'], content=draft['content'])
    elif reason == 'cancelled':
        planning.workspace.abandon(env.task, env.owner)
    elif reason == 'confirmed':
        # Mark the target immutable without fabricating a confirmed partial outline.
        from backend.planning.models import LearningRouteRow
        mutate(env, LearningRouteRow, env.route, current_route_version_id='synthetic-confirmed-version')
    elif reason == 'budget':
        with env.db.session() as sess:
            budget = sess.get(AgentBudgetRow, env.task)
            budget.logical_requests = budget.request_limit
        # Exercise the real SDK budget check instead of the stub above.
        monkeypatch.setattr(planning, '_refine_course_units', original_refine)
    tick(env)
    assert calls == [] and env.wire.calls == []
    assert state(env)['task_status'] != 'succeeded'
    if reason == 'edited':
        assert state(env)['error_code'] == 'DRAFT_REVISION_MISMATCH'
    if reason == 'revoked':
        assert state(env)['error_code'] == 'DISCLOSURE_REVOKED'
    if reason == 'budget':
        assert state(env)['error_code'] == 'AGENT_LOOP_LIMIT'


@pytest.mark.parametrize('code', ['MODEL_TIMEOUT', 'MODEL_CONNECTION_ERROR', 'MODEL_PROVIDER_ERROR', 'OUTPUT_INVALID'])
def test_course_failure_keeps_original_controlled_error_in_draft_and_events(code):
    events = []
    def fail(*args, **kwargs):
        raise error(code)
    planning = SimpleNamespace(prompts=SimpleNamespace(resolve_for_task=lambda *a: {'template': 'synthetic'}),
        _require_model_adapter=lambda *a: object(), _refine_course_units=fail, _state=lambda *a: None,
        events=SimpleNamespace(record=lambda *a, **k: events.append(k)))
    content = {'stages': [{'courses': [{'title': '合成课程'}]}]}
    PlanningService._fill_route_units(planning, SimpleNamespace(owner_user_id='synthetic'), 'task_synthetic', content)
    assert content['stages'][0]['courses'][0]['refinement_error'] == code
    assert events[0]['error_code'] == code


def test_newer_planning_task_prevents_old_partial_from_spending_budget(env, monkeypatch):
    planning, draft_id, _ = partial_route(env)
    from test_planning_authorization import make_task
    _, new_task = make_task(env, kind='planning_revise', target=env.route)
    calls = []
    monkeypatch.setattr(planning, '_refine_course_units', lambda *a, **k: calls.append(1))
    tick(env)
    assert calls == [] and state(env)['auto_retry_count'] == 0
    assert not state(env)['auto_retry_pending']


def test_real_sdk_refinement_failure_is_observable_with_task_identity(env):
    planning, draft_id, _ = partial_route(env)
    original = env.wire.handle
    def malformed(request):
        response = original(request)
        body = json.loads(response.content)
        body['choices'][0]['message']['content'] = '{"units":[{"lessons":[]}]}'
        return type(response)(200, json=body)
    env.wire.handle = malformed
    # The transport callback resolves the handler on construction for this run.
    from backend.logging_audit.logger import MemoryLogStream, StructuredLogger
    from backend.logging_audit.metrics import get_model_call_metrics
    metrics = get_model_call_metrics()
    stream = MemoryLogStream()
    metrics.attach_logger(StructuredLogger('api', 'test', stream=stream))
    before = metrics.emit_errors
    try:
        tick(env)
    finally:
        metrics.attach_logger(None)
    failures = [r for r in stream.records() if r['event'] == 'model.call.failed']
    assert len(failures) == 2 and metrics.emit_errors == before
    assert all(r['stage'] == 'generate' and r['task_id'] == env.task for r in failures)
    assert all(r['error_code'] == 'OUTPUT_INVALID' and r['requests'] == 3 for r in failures)
    assert all('UnexpectedModelBehavior <- ToolRetryError <- ValidationError[' in r['reason'] for r in failures)
    assert all('missing@units.0.title' not in r['reason'] for r in failures)
    assert len(env.wire.calls) == 6
