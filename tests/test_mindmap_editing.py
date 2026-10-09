"""Synthetic planning edit and layout authorization contracts (ADR-027)."""

from sqlalchemy import func, select
from common_fixtures import make_user, auth_headers
from test_course_library import database, env, draft, confirm, get_route  # noqa: F401
from backend.tasks.service import LearningTaskRow


def test_confirmed_edit_is_rejected_without_mutating_version(env):
    rid, first = draft(env)
    confirm(env, first)
    before = get_route(env, rid)
    response = env[0].post(f"/api/planning/route/{rid}/edit-draft", headers=env[3])
    assert response.status_code == 409 and response.json()["error_code"] == "PLANNING_LOCKED"
    assert get_route(env, rid) == before


def test_draft_edit_and_layout_are_owner_scoped_and_do_not_generate(env):
    rid, editable = draft(env)
    _, token = make_user(env[0], env[1].db)
    other = auth_headers(token)
    key = f"{editable['draft_id']}:{editable['revision']}"
    layout = {"offsets": {"stage_0course_0": {"dx": 30, "dy": 20}}}
    assert env[0].put(f"/api/planning/viewstates/draft/{key}", headers=env[3], json={"canvas": layout}).status_code == 200
    assert env[0].get(f"/api/planning/viewstates/draft/{key}", headers=env[3]).json()["canvas"] == layout
    assert env[0].put(f"/api/planning/viewstates/draft/{key}", headers=other, json={"canvas": layout}).status_code == 404
    assert env[0].get(f"/api/planning/viewstates/draft/{key}", headers=other).status_code == 404
    with env[1].db.session() as sess:
        assert sess.scalar(select(func.count()).select_from(LearningTaskRow).where(LearningTaskRow.owner_user_id == env[2])) == 0


def test_scene_black_white_print_fonts_and_edit_metadata(env):
    rid, item = draft(env); confirm(env, item)
    scene = env[0].get(f"/api/routes/{rid}/scene", headers=env[3]).json()
    nodes = [e for e in scene["elements"] if e.get("customData", {}).get("role") == "node"]
    assert nodes and all(n["backgroundColor"] == "#ffffff" and n["strokeColor"] == "#000000" for n in nodes)
    assert all(n["customData"].get("title") and n["customData"].get("path") for n in nodes)
    texts = [e for e in scene["elements"] if e["type"] == "text"]
    assert all(e["fontFamily"] == 2 and e["strokeColor"] == "#000000" for e in texts)
    links = [e for e in scene["elements"] if e["type"] == "line"]
    assert all(e["customData"].get("parent_id") and e["customData"].get("child_id") for e in links)
