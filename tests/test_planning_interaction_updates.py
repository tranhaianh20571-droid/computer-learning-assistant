"""Owned synthetic planning revisions, retry episodes and preserved search evidence."""
from copy import deepcopy
import json

from sqlalchemy import select

from backend.planning.models import PlanningTaskStateRow
from backend.slice1.models import DisclosureGrantRow
from test_planning_authorization import database, env, make_config
from test_course_library import PAYLOAD
from test_planning_automatic_retry import partial_route, tick


def test_dialogue_prepares_pinned_model_and_adopts_native_ai_revision(env, monkeypatch):
    features = env.state.features
    monkeypatch.setattr(features, "kick", lambda task: None)
    draft = env.state.drafts.find_or_create_draft(owner_user_id=env.owner,
        draft_kind="route", target_id=env.route, base_version_id=None,
        initial_content=deepcopy(PAYLOAD))
    with env.db.session() as session:
        session.get(PlanningTaskStateRow, env.task).draft_id = draft["draft_id"]
    result = env.client.post(f"/api/planning/drafts/{draft['draft_id']}/dialogue", json={
        "revision": draft["revision"], "content": "修改第一门课程名称",
        "idempotency_key": "synthetic-dialogue"})
    assert result.status_code == 202, result.text
    task_id = result.json()["task_id"]
    with env.db.session() as session:
        grant = session.scalar(select(DisclosureGrantRow).where(DisclosureGrantRow.task_id == task_id))
        assert grant and grant.config_id == env.config["config_id"]
    make_config(env, endpoint="https://newer.synthetic.test/v1")
    features._run(task_id)
    assert env.state.tasks.get_task(task_id)["status"] == "succeeded"
    turn = features.dialogue(draft["draft_id"], env.owner)["turns"][-1]
    assert turn["result"]["candidate"]["stages"][0]["courses"][0]["title"] == "合成更新后的课程"
    assert all(item["host"] == "planning.example.test" for item in env.wire.calls)
    adopted = features.decide(task_id, env.owner, draft["revision"], True)
    assert adopted["draft"]["revision"] == draft["revision"] + 1
    assert adopted["draft"]["content"]["stages"][0]["courses"][0]["title"] == "合成更新后的课程"


def test_structure_repair_keeps_search_result_and_clears_successful_retry_count(env):
    planning, _, _ = partial_route(env)
    with env.db.session() as session:
        row = session.get(PlanningTaskStateRow, env.task)
        row.process_json = json.dumps({
            "search": {"status": "succeeded", "result_count": 10,
                "sources": [{"title": "Synthetic reference", "url": "https://example.test/ref",
                    "domain": "example.test", "used": True}]},
            "steps": [{"key": "search", "label": "搜索参考来源", "status": "succeeded"}],
            "_config_versions": {}, "_material_revisions": []})
    tick(env)
    current = planning.get_planning_state(env.task, actor_id=env.owner)
    assert current["task_status"] == "succeeded" and current["auto_retry_count"] == 0
    from backend.planning.process import read_process
    process = read_process(planning, env.task, env.owner)["process"]
    assert process["search"]["status"] == "succeeded"
    assert process["search"]["result_count"] == 10 and process["search"]["sources"][0]["used"]
