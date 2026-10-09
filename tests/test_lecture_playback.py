"""Presentation checkpoints and owned, idempotent upgrade of synthetic legacy sessions."""
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from pydantic_ai.exceptions import ModelRetry
from backend.teaching.learning_outputs import validate_new_blueprint
from backend.teaching.pedagogy import lightweight_plan, prepared_plan, plan_key
from backend.teaching.learning_models import LessonBlueprintRow, PreparedLessonPlanRow
from backend.planning.models import ConversationRow, LessonProgressRow
from sqlalchemy import select
from test_teaching_assessments import database, env, lesson  # noqa: F401
from common_fixtures import make_user, auth_headers


def legacy(env):
    _, _, start = lesson(env)
    state = env[1]
    with state.db.session() as sess:
        row = sess.get(LessonBlueprintRow, start["conversation_id"])
        plan = lightweight_plan(json.loads(row.context_json))
        for stage in plan.stages:
            stage.segments = []
        row.plan_json = plan.model_dump_json()
        row.context_json = json.dumps({k:v for k,v in json.loads(row.context_json).items() if not k.startswith("_")})
    return start


def test_new_validation_and_cache_never_accept_legacy_or_duplicate_purposes(env):
    start = legacy(env)
    state = env[1]
    with state.db.session() as sess:
        row = sess.get(LessonBlueprintRow, start["conversation_id"])
        from backend.teaching.learning_outputs import LessonBlueprint
        plan = LessonBlueprint.model_validate_json(row.plan_json)
        context = json.loads(row.context_json)
        conv = sess.get(ConversationRow, start["conversation_id"])
    with pytest.raises(ModelRetry):
        validate_new_blueprint(None, plan)
    key = plan_key(state.teaching, conv, context)
    with state.db.session() as sess:
        cached = sess.get(PreparedLessonPlanRow, key)
        if cached:
            cached.plan_json = plan.model_dump_json()
        else:
            sess.add(PreparedLessonPlanRow(cache_key=key,owner_user_id=conv.owner_user_id,
                course_id=conv.course_id,lesson_id=conv.lesson_id,plan_json=plan.model_dump_json(),source="agent"))
    assert len(prepared_plan(state.teaching, conv, context).stages[0].segments) == 2
    plan = lightweight_plan(context)
    plan.stages[0].segments[1].purpose = plan.stages[0].segments[0].purpose
    with pytest.raises(ModelRetry):
        validate_new_blueprint(None, plan)


def test_upgrade_preserves_history_is_owned_and_concurrently_idempotent(env):
    start = legacy(env)
    client, state, user, headers = env
    ident = start["conversation_id"]
    assert client.post(f"/api/teaching/tasks/{start['task_id']}/run",headers=headers).json()["status"] == "succeeded"
    before = client.get(f"/api/teaching/{ident}",headers=headers).json()
    assert before["needs_upgrade"] and before["planned_steps"] == 4
    _, stranger_token = make_user(client, state.db)
    assert client.post(f"/api/teaching/{ident}/upgrade",headers=auth_headers(stranger_token)).status_code == 404
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _:state.teaching.upgrade_lesson(ident,actor_id=user),range(2)))
    assert results[0]["conversation_id"] == results[1]["conversation_id"] != ident
    upgraded = results[0]
    assert client.post(f"/api/teaching/{ident}/upgrade",headers=headers).json()["conversation_id"] == upgraded["conversation_id"]
    original = client.get(f"/api/teaching/{ident}",headers=headers).json()
    assert original["session_state"] == "archived" and not original["needs_upgrade"]
    assert original["steps"] == before["steps"]
    fresh = client.get(f"/api/teaching/{upgraded['conversation_id']}",headers=headers).json()
    assert fresh["planned_steps"] == 7 and not fresh["needs_upgrade"] and fresh["steps"] == []
    assert client.post(f"/api/teaching/{upgraded['conversation_id']}/upgrade",headers=headers).json()["conversation_id"] == upgraded["conversation_id"]
    with state.db.session() as sess:
        progress=sess.scalar(select(LessonProgressRow).where(LessonProgressRow.lesson_id==fresh["lesson_id"],LessonProgressRow.owner_user_id==user))
        assert progress.learning_state != "user_completed"
    from backend.teaching.pedagogy import plan_lesson
    conv=state.teaching._conversation(upgraded["conversation_id"],user)
    _,context=plan_lesson(state.teaching,conv,upgraded["task_id"],None)
    assert not any(key.startswith("_") for key in context)


def test_checkpoint_returns_only_sanitized_presentation_and_pause_gates_prefetch(env):
    _,_,start=lesson(env)
    client,state,user,headers=env
    ident=start["conversation_id"]
    client.post(f"/api/teaching/{ident}/checkpoint",headers=headers,json={"client_seq":1,"step_no":1,"position_ms":0,
        "board_state":{"private":"synthetic-only","presentation":{"visible_chars":12,"paused":True,"text_complete":False,"extra":"omit"}}})
    assert client.post(f"/api/teaching/tasks/{start['task_id']}/run",headers=headers).json()["status"]=="succeeded"
    view=client.get(f"/api/teaching/{ident}",headers=headers).json()
    assert view["last_checkpoint"]["presentation"]=={"visible_chars":12,"paused":True,"text_complete":False}
    assert view["last_checkpoint"]["saved_at"]
    assert not any(t["step_no"]==2 for t in view["queued_tasks"])
    client.post(f"/api/teaching/{ident}/checkpoint",headers=headers,json={"client_seq":2,"step_no":1,"position_ms":0,
        "board_state":{"presentation":{"visible_chars":12,"paused":False,"text_complete":False}}})
    assert any(t["step_no"]==2 for t in client.get(f"/api/teaching/{ident}",headers=headers).json()["queued_tasks"])
    assert state.teaching._presentation({"presentation":{"visible_chars":True,"paused":"true","text_complete":1}})=={}
