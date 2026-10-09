"""Synthetic PostgreSQL integration coverage for the grouped library and duplicate consent."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, func

from common_fixtures import auth_headers, make_user
from backend.logging_audit.db import Database
from backend.logging_audit.repositories import PgAuditLog
from backend.slice0_app import Slice0State
from backend.slice3_app import Slice3State, create_slice3_app
from backend.planning.drafts import DraftService
from backend.planning.errors import PlanningDuplicate
from backend.planning.library import canonical_content, fingerprint
from backend.tasks.service import LearningTaskRow
from backend.planning.models import (
    CourseRow, LearningRouteRow, LessonContentVersionRow, LessonProgressRow,
    PlanningConfirmationRow, RouteVersionRow,
)

PAYLOAD = {"title": "合成后端课程", "stages": [{"title": "基础", "courses": [
    {"title": "合成数据库", "goal": "理解数据", "units": [{"title": "关系模型", "lessons": [
        {"title": "数据表", "goal": "理解表"}, {"title": "关系", "goal": "理解关系"}]}],
     "prerequisites": [{"from_index": 0, "to_index": 1}]},
    {"title": "合成操作系统", "goal": "理解调度", "units": [{"title": "进程", "lessons": [
        {"title": "调度", "goal": "理解调度算法"}]}]},
]}], "prerequisites": [{"from_index": 0, "to_index": 1}]}


@pytest.fixture(scope="module")
def database():
    db = Database()
    db.create_all()
    yield db
    db.dispose()


@pytest.fixture
def env(database):
    state = Slice3State(database, PgAuditLog(database))
    app = create_slice3_app(state)
    app.state.slice0 = Slice0State(database, PgAuditLog(database))
    client = TestClient(app)
    user, token = make_user(client, database)
    return client, state, user, auth_headers(token)


def draft(env, payload=None, kind="route", target=None):
    client, state, user, headers = env
    body = deepcopy(payload or PAYLOAD)
    if target is None:
        url = "/api/planning/routes" if kind == "route" else "/api/planning/courses"
        created = client.post(url, headers=headers, json={"title": body.get("title", "合成课程"), "input": "合成学习目标"})
        assert created.status_code == 201, created.text
        target = created.json()["route_id" if kind == "route" else "course_id"]
    item = state.drafts.find_or_create_draft(owner_user_id=user, draft_kind=kind, target_id=target,
        base_version_id=None, initial_content=body)
    return target, item


def confirm(env, item, **extra):
    return env[0].post(f"/api/planning/drafts/{item['draft_id']}/confirm", headers=env[3],
        json={"revision": item["revision"], "idempotency_key": uuid.uuid4().hex, **extra})


def get_library(env):
    response = env[0].get("/api/planning/library", headers=env[3])
    assert response.status_code == 200, response.text
    return response.json()["data"]


def get_route(env, rid, version=None):
    url = f"/api/routes/{rid}/overview" + (f"?version_id={version}" if version else "")
    r = env[0].get(url, headers=env[3])
    assert r.status_code == 200, r.text
    return r.json()


def test_group_and_independent_course(env):
    rid, item = draft(env)
    assert confirm(env, item).status_code == 200
    independent, _ = draft(env, {"title": "合成独立课程", "units": []}, "course_outline")
    data = get_library(env)
    assert {(x["kind"], x["id"]) for x in data} == {("route", rid), ("course", independent)}
    group = next(x for x in data if x["kind"] == "route")
    assert group["course_count"] == 2 and group["course_completed"] == 0
    assert "percentage" not in group
    assert get_route(env, rid)["title"] == PAYLOAD["title"]


def test_duplicate_warn_force_and_revision_idempotency(env):
    _, original = draft(env)
    assert confirm(env, original).status_code == 200
    other, duplicate = draft(env)
    warning = confirm(env, duplicate)
    assert warning.status_code == 409, warning.text
    assert warning.json()["error_code"] == "PLANNING_DUPLICATE"
    assert len(warning.json()["matches"]) == 1
    assert len(get_library(env)) == 1
    fp = warning.json()["fingerprint"]
    stale = confirm(env, duplicate, allow_duplicate=True, duplicate_fingerprint="v1:stale")
    assert stale.status_code == 409 and stale.json()["error_code"] == "DUPLICATE_ACK_STALE"
    forced = confirm(env, duplicate, allow_duplicate=True, duplicate_fingerprint=fp)
    assert forced.status_code == 200, forced.text
    retried = confirm(env, duplicate)
    assert retried.status_code == 200 and retried.json()["version_id"] == forced.json()["version_id"]
    assert retried.json()["deduped"] and len(get_library(env)) == 2
    assert get_route(env, other)["course_count"] == 2
    with env[1].db.session() as sess:
        assert sess.scalar(select(func.count()).select_from(PlanningConfirmationRow).where(
            PlanningConfirmationRow.owner_user_id == env[2])) == 2


def test_permission_stales_after_save(env):
    _, first = draft(env)
    confirm(env, first)
    _, second = draft(env)
    fp = confirm(env, second).json()["fingerprint"]
    revised = deepcopy(PAYLOAD); revised["stages"][0]["courses"][0]["goal"] = "新目标"
    saved = env[1].drafts.save_draft(second["draft_id"], actor_id=env[2], revision=second["revision"], content=revised)
    assert confirm(env, second, allow_duplicate=True, duplicate_fingerprint=fp).status_code == 409
    assert confirm(env, saved, allow_duplicate=True, duplicate_fingerprint=fp).json()["error_code"] == "DUPLICATE_ACK_STALE"
    assert confirm(env, saved).status_code == 200


def test_changed_content_not_duplicate(env):
    _, first = draft(env); confirm(env, first)
    changed = deepcopy(PAYLOAD); changed["stages"][0]["courses"][0]["goal"] = "另一个学习目标"
    _, second = draft(env, changed)
    assert confirm(env, second).status_code == 200


def test_noop_confirmation_and_confirmed_structure_lock(env):
    from backend.planning.errors import PlanningError
    rid, first = draft(env); initial = confirm(env, first).json()
    assert confirm(env, first).json()["version_id"] == initial["version_id"]
    with pytest.raises(PlanningError) as caught:
        env[1].drafts.save_draft(first["draft_id"], actor_id=env[2], revision=first["revision"], content=deepcopy(PAYLOAD))
    assert caught.value.code == "PLANNING_LOCKED"
    assert get_route(env, rid, initial["version_id"])["title"] == PAYLOAD["title"]


def test_locked_detach_and_delete_route_and_course(env):
    rid, item = draft(env); confirm(env, item)
    courses = get_route(env, rid)["stages"][0]["courses"]
    one, two = courses[0]["course_id"], courses[1]["course_id"]
    detached = env[0].post(f"/api/routes/{rid}/courses/{one}/detach", headers=env[3])
    assert detached.status_code == 409 and detached.json()["error_code"] == "PLANNING_LOCKED"
    removed = env[0].delete(f"/api/planning/courses/{two}", headers=env[3])
    assert removed.status_code == 200, removed.text
    scene = env[0].get(f"/api/routes/{rid}/scene", headers=env[3])
    assert scene.status_code == 200 and courses[1]["title"] not in scene.text
    assert env[0].delete(f"/api/planning/routes/{rid}", headers=env[3]).status_code == 200
    assert any(c["id"] == one for c in get_library(env))


def test_started_not_completed_and_version_match(env):
    rid, item = draft(env); confirm(env, item)
    course = get_route(env, rid)["stages"][0]["courses"][0]
    lessons = course["units"][0]["lessons"]
    with env[1].db.session() as sess:
        for lesson in lessons:
            content_id = uuid.uuid4().hex
            sess.add(LessonContentVersionRow(content_version_id=content_id, lesson_id=lesson["lesson_id"], content_version_no=1))
            sess.add(LessonProgressRow(progress_id=uuid.uuid4().hex, owner_user_id=env[2], lesson_id=lesson["lesson_id"],
                lesson_content_version_id=content_id, learning_state="in_progress"))
    ov = env[1].overview.course_overview(course["course_id"], actor_id=env[2])
    assert ov["lesson_started"] == 2 and ov["lesson_completed"] == 0 and not ov["completed"]
    with env[1].db.session() as sess:
        for row in sess.execute(select(LessonProgressRow).where(LessonProgressRow.owner_user_id == env[2])).scalars():
            row.learning_state = "user_completed"
    assert env[1].overview.course_overview(course["course_id"], actor_id=env[2])["completed"]
    with env[1].db.session() as sess:
        sess.add(LessonContentVersionRow(content_version_id=uuid.uuid4().hex, lesson_id=lessons[0]["lesson_id"], content_version_no=2))
    ov = env[1].overview.course_overview(course["course_id"], actor_id=env[2])
    assert ov["lesson_completed"] == 1 and not ov["completed"]


def test_owner_guest_context_and_input_isolation(env):
    rid, item = draft(env); confirm(env, item)
    client, state, _, headers = env
    _, token = make_user(client, state.db); other = auth_headers(token)
    assert client.get("/api/planning/library", headers=other).json()["data"] == []
    assert client.get(f"/api/routes/{rid}/overview", headers=other).status_code == 404
    assert client.post(f"/api/planning/drafts/{item['draft_id']}/duplicates", headers=other, json={"revision": 1}).status_code == 404
    assert client.post("/api/planning/input-matches", headers=other, json={"input": "合成学习目标"}).json()["matches"] == []
    assert client.post("/api/planning/input-matches", headers=headers, json={"input": "合成学习目标"}).json()["matches"]
    guest = TestClient(client.app)
    assert guest.get("/api/planning/library").json()["data"] == []
    course = get_route(env, rid)["stages"][0]["courses"][0]["course_id"]
    assert client.get(f"/api/courses/{course}/overview?route_id=invalid", headers=headers).status_code == 404


def test_parallel_confirmations(env):
    _, one = draft(env); _, two = draft(env)
    service = DraftService(env[1].db)
    def run(item):
        try:
            service.confirm_draft(item["draft_id"], actor_id=env[2], revision=1, idempotency_key=uuid.uuid4().hex)
            return "created"
        except PlanningDuplicate:
            return "duplicate"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [one, two]))
    assert sorted(results) == ["created", "duplicate"]
    assert len(get_library(env)) == 1


def test_query_count_and_no_generation(env):
    rid, item = draft(env); confirm(env, item)
    with env[1].db.session() as sess:
        before = sess.scalar(select(func.count()).select_from(LearningTaskRow).where(LearningTaskRow.owner_user_id == env[2]))
        for n in range(80):
            sess.add(CourseRow(course_id=uuid.uuid4().hex, owner_user_id=env[2], title_ref=f"合成规模课程{n}"))
    calls = []
    def track(*args): calls.append(1)
    event.listen(env[1].db.engine, "before_cursor_execute", track)
    try:
        env[1].overview.list_library(actor_id=env[2])
    finally:
        event.remove(env[1].db.engine, "before_cursor_execute", track)
    assert len(calls) <= 16
    with env[1].db.session() as sess:
        assert sess.scalar(select(func.count()).select_from(LearningTaskRow).where(LearningTaskRow.owner_user_id == env[2])) == before
    assert any("合成操作系统" in c.get("search_titles", []) for c in get_library(env))


def test_legacy_without_fingerprint_and_snapshot(env):
    rid, item = draft(env); result = confirm(env, item).json()
    with env[1].db.session() as sess:
        version = sess.get(RouteVersionRow, result["version_id"])
        version.content_fingerprint = None
        version.content_snapshot_json = None
        version.title_snapshot = ""
    _, repeated = draft(env)
    assert confirm(env, repeated).status_code == 409


def test_structural_normalization_preserves_order_and_ignores_layout():
    value = deepcopy(PAYLOAD); value["title"] = "  合成后端课程  "; value["layout"] = {"x": 123}
    identity = canonical_content("route", PAYLOAD, "", "self_study")
    assert fingerprint(identity) == fingerprint(canonical_content("route", value, "", "self_study"))
    value["stages"][0]["courses"].reverse()
    assert fingerprint(identity) != fingerprint(canonical_content("route", value, "", "self_study"))


def test_idempotency_key_cannot_return_another_owners_version(env):
    _, item = draft(env)
    key = uuid.uuid4().hex
    first = env[1].drafts.confirm_draft(item["draft_id"], actor_id=env[2], revision=1, idempotency_key=key)
    user, token = make_user(env[0], env[1].db)
    other_env = (env[0], env[1], user, auth_headers(token))
    _, other = draft(other_env)
    second = env[1].drafts.confirm_draft(other["draft_id"], actor_id=user, revision=1, idempotency_key=key)
    assert first["version_id"] != second["version_id"]


def test_cross_owner_node_filtered_and_deleted_target_cannot_confirm(env):
    rid, item = draft(env); confirm(env, item)
    user, token = make_user(env[0], env[1].db)
    other_env = (env[0], env[1], user, auth_headers(token))
    foreign_id, _ = draft(other_env, {"title": "别人的合成课程", "units": []}, "course_outline")
    from backend.planning.models import RouteCourseNodeRow
    with env[1].db.session() as sess:
        version = sess.get(LearningRouteRow, rid).current_route_version_id
        node = sess.execute(select(RouteCourseNodeRow).where(RouteCourseNodeRow.route_version_id == version)).scalars().first()
        node.course_id = foreign_id
    assert "别人的合成课程" not in env[0].get(f"/api/routes/{rid}/scene", headers=env[3]).text
    assert get_route(env, rid)["course_count"] == 1
    assert env[0].delete(f"/api/planning/routes/{rid}", headers=env[3]).status_code == 200
    assert confirm(env, item).status_code == 404


def test_read_snapshot_concurrent_with_course_deletion(env):
    rid, item = draft(env); confirm(env, item)
    course = get_route(env, rid)["stages"][0]["courses"][0]
    entered, deleted = Event(), Event()
    def reader():
        entered.set()
        # This read overlaps the deletion and may observe either valid snapshot.
        initial = env[1].overview.route_overview(rid, actor_id=env[2])
        assert initial["course_count"] in (1, 2)
        assert deleted.wait(10)
        for _ in range(4):
            snapshot = env[1].overview.route_overview(rid, actor_id=env[2])
            assert snapshot["course_count"] == 1
            assert course["title"] not in str(snapshot)
    def deleter():
        assert entered.wait(10)
        env[1].deletion.delete_course(course["course_id"], actor_id=env[2])
        deleted.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        read = pool.submit(reader); remove = pool.submit(deleter)
        remove.result(timeout=15); read.result(timeout=15)
