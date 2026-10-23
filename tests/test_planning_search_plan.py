"""Real search orchestration with synthetic transport and owned database rows."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from test_anysearch_service import env, setup, Adapter
from backend.planning.errors import PlanningError
from backend.planning.models import PlanningTaskStateRow, CourseRow
from backend.planning.service import PlanningService
from backend.search.service import SearchService
from backend.search.repository import SearchCallRow, SearchTaskBudgetRow
from backend.tasks.service import LearningTaskRow, Lease, LeaseError


def execute(env, task, queries, adapters):
    def factory(owner, ident):
        def transport(*_):
            adapter = Adapter()
            adapters.append(adapter)
            return adapter
        return SearchService(env.db, env.configs, owner_user_id=owner, task_id=ident,
            adapter_factory=transport)
    planning = SimpleNamespace(db=env.db, tasks=env.tasks, search_adapter_factory=factory,
        _owned_target_title=lambda _: "Synthetic algebra")
    with env.db.session() as sess:
        state = sess.get(PlanningTaskStateRow, task)
        row = sess.get(LearningTaskRow, task)
        lease = Lease(row.task_id, row.attempt_id, row.lease_token, row.lease_owner,
            row.generation, row.attempt, row.lease_until, row.deadline_at, row.owner_user_id)
    plan = SimpleNamespace(coverage_areas=[SimpleNamespace(search_query=q) for q in queries])
    return PlanningService._attempt_search_plan(planning, task, lease, state, plan)


def test_coverage_queries_reach_real_service_with_distinct_shared_slots(env):
    task, _, _ = setup(env)
    adapters = []
    queries = [f"Synthetic algebra topic {i}" for i in range(9)]
    outcome, binding, _ = execute(env, task, queries, adapters)
    assert outcome.state == "succeeded"
    assert [row["status"] for row in outcome.queries] == ["succeeded"] * 3 + ["skipped"] * 6
    assert all(row["reason"] == "search_budget_limit" for row in outcome.queries[3:])
    assert len(adapters) == 3 and sum(a.calls for a in adapters) == 3
    assert len(binding.bindings) == 3
    binding.check_state()
    with env.db.session() as sess:
        calls = list(sess.scalars(select(SearchCallRow).where(SearchCallRow.task_id == task)))
        budget = sess.get(SearchTaskBudgetRow, task)
        assert sorted(c.logical_slot for c in calls) == [1, 2, 3]
        assert len({(c.config_id, c.config_version) for c in calls}) == 1
        assert (budget.logical_count, budget.wire_count) == (3, 3)
    again, _, _ = execute(env, task, queries, adapters)
    assert again.state == "failed" and again.error_code == "SEARCH_INTERRUPTED"
    assert len(adapters) == 3


@pytest.mark.parametrize("change", ["query", "snapshot", "title", "config", "cancel"])
def test_plan_changes_and_unapproved_queries_cannot_send_or_publish(env, change):
    task, course, view = setup(env)
    adapters = []
    _, binding, _ = execute(env, task, ["Synthetic algebra topic"], adapters)
    if change == "query":
        runner = SearchService(env.db, env.configs, owner_user_id=env.owner, task_id=task,
            adapter_factory=lambda *_: pytest.fail("unapproved query reached transport"))
        with pytest.raises(PlanningError):
            runner.attempt("Synthetic unrelated query", logical_slot=2)
        return
    with env.db.session() as sess:
        if change == "snapshot":
            row = sess.get(PlanningTaskStateRow, task)
            signals = json.loads(row.scope_signals_json)
            signals["search_plan_v1"]["queries"].append("Synthetic other topic")
            row.scope_signals_json = json.dumps(signals)
        elif change == "title":
            sess.get(CourseRow, course).title_ref = "Synthetic different target"
        elif change == "cancel":
            sess.get(LearningTaskRow, task).cancel_requested = True
    if change == "config":
        env.configs.update(view["config_id"], actor_id=env.owner, is_admin=False,
            credentials={"api_key": "synthetic-rotated-key"})
    with pytest.raises((PlanningError, LeaseError)) as caught:
        binding.check_state()
    assert getattr(caught.value, "code", None) is not None
    with env.db.session() as sess, pytest.raises((PlanningError, LeaseError)):
        binding.check_in_session(sess)
    assert sum(a.calls for a in adapters) == 1
