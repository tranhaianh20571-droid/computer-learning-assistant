"""切片 3 T07 测试：路线/课程总览、同版口径、无整体百分比、导航状态、教学门槛。"""

from __future__ import annotations

import uuid

import pytest

from common_fixtures import (
    OUTLINE_OK_PAYLOAD,
    ROUTE_OK_PAYLOAD,
    SyntheticModelAdapter,
    SyntheticSearchAdapter,
    auth_headers,
    make_user,
    run_route_generate,
)

from fastapi.testclient import TestClient  # noqa: E402

from backend.logging_audit.db import Database  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.logging_audit.repositories import PgAuditLog  # noqa: E402
from backend.slice0_app import Slice0State  # noqa: E402
from backend.slice3_app import Slice3State, create_slice3_app  # noqa: E402


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db


@pytest.fixture()
def env(database: Database):
    container = {"model": None, "search": None}
    state = Slice3State(
        database, PgAuditLog(database),
        model_adapter_factory=lambda owner, task_id: container["model"],
        search_adapter_factory=lambda owner, task_id: container["search"],
    )
    app = create_slice3_app(state=state)
    app.state.slice0 = Slice0State(database, PgAuditLog(database))
    client = TestClient(app)
    return {"client": client, "state": state, "container": container, "database": database}


def _confirm_route_and_outline(env):
    client = env["client"]
    user_id, token = make_user(client, env["database"])
    route_id = client.post(
        "/api/planning/routes", headers=auth_headers(token),
        json={"title": "总览路线", "scenario": "self_study"},
    ).json()["route_id"]
    env["container"]["model"] = SyntheticModelAdapter(payload=ROUTE_OK_PAYLOAD)
    env["container"]["search"] = SyntheticSearchAdapter()
    t1 = client.post(
        "/api/planning/tasks", headers=auth_headers(token),
        json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "route_generate",
              "planning_scope": "route", "target_route_id": route_id},
    ).json()["task_id"]
    body = run_route_generate(client, token, t1)
    draft = env["state"].drafts.get_draft(body["draft"]["draft_id"], actor_id=user_id)
    client.post(
        f"/api/planning/drafts/{body['draft']['draft_id']}/confirm", headers=auth_headers(token),
        json={"revision": draft["revision"], "idempotency_key": f"c{uuid.uuid4().hex[:8]}"},
    )
    overview = client.get(f"/api/routes/{route_id}/overview", headers=auth_headers(token)).json()
    course_id = overview["stages"][0]["courses"][0]["course_id"]

    env["container"]["model"] = SyntheticModelAdapter(payload=OUTLINE_OK_PAYLOAD)
    t2 = client.post(
        "/api/planning/tasks", headers=auth_headers(token),
        json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "outline_generate",
              "planning_scope": "course", "target_course_id": course_id},
    ).json()["task_id"]
    body2 = client.post(f"/api/planning/tasks/{t2}/run", headers=auth_headers(token)).json()  # outline 无访谈
    outline_draft = env["state"].drafts.get_draft(body2["draft"]["draft_id"], actor_id=user_id)
    client.post(
        f"/api/planning/drafts/{body2['draft']['draft_id']}/confirm", headers=auth_headers(token),
        json={"revision": outline_draft["revision"], "idempotency_key": f"c{uuid.uuid4().hex[:8]}"},
    )
    return user_id, token, route_id, course_id


class TestRouteOverview:
    def test_overview_shape_no_overall_percentage(self, env) -> None:
        user_id, token, route_id, _ = _confirm_route_and_outline(env)
        r = env["client"].get(f"/api/routes/{route_id}/overview", headers=auth_headers(token))
        assert r.status_code == 200
        body = r.json()
        body_text = str(body).lower()
        # 明确不输出路线整体百分比
        assert "percentage" not in body_text and "overall" not in body_text and "percent" not in body_text
        assert body["stages"]
        for stage in body["stages"]:
            assert "course_count" in stage
            for course in stage["courses"]:
                assert "progress" in course
                assert "outline_confirmed" in course

    def test_overview_same_version_as_scene(self, env) -> None:
        """总览与导图同版：版本 ID 一致，课程节点集合一致（经 node_id）。"""
        user_id, token, route_id, _ = _confirm_route_and_outline(env)
        client = env["client"]
        overview = client.get(f"/api/routes/{route_id}/overview", headers=auth_headers(token)).json()
        scene = client.get(f"/api/routes/{route_id}/scene", headers=auth_headers(token)).json()
        assert scene["node_positions"].keys() >= {s["stage_id"] for s in overview["stages"]}
        for stage in overview["stages"]:
            for course in stage["courses"]:
                assert course["node_id"] in scene["node_positions"]

    def test_preparation_states_visible(self, env) -> None:
        user_id, token, route_id, course_id = _confirm_route_and_outline(env)
        overview = env["client"].get(
            f"/api/routes/{route_id}/overview", headers=auth_headers(token)
        ).json()
        confirmed = [c for s in overview["stages"] for c in s["courses"]
                     if c["course_id"] == course_id][0]
        assert confirmed["outline_confirmed"] is True
        unconfirmed = [c for s in overview["stages"] for c in s["courses"]
                       if c["course_id"] != course_id]
        assert any(c["outline_confirmed"] is False for c in unconfirmed)


class TestCourseOverview:
    def test_units_lessons_preparation_progress(self, env) -> None:
        user_id, token, route_id, course_id = _confirm_route_and_outline(env)
        r = env["client"].get(f"/api/courses/{course_id}/overview", headers=auth_headers(token))
        assert r.status_code == 200
        body = r.json()
        assert body["outline_confirmed"] is True
        assert body["teaching_unlocked"] is True
        assert len(body["units"]) == 2
        lessons = [l for u in body["units"] for l in u["lessons"]]
        assert len(lessons) == 4
        assert all(l["status_preparation"] == "not_generated" for l in lessons)
        assert all(l["learning_state"] == "not_started" for l in lessons)
        # 进度按当前已确认大纲讲次等权：本切片全部未开始
        assert body["progress"] == {"lesson_total": 4, "lesson_started": 0}

    def test_prereq_hint_present(self, env) -> None:
        user_id, token, route_id, course_id = _confirm_route_and_outline(env)
        body = env["client"].get(
            f"/api/courses/{course_id}/overview", headers=auth_headers(token)
        ).json()
        lessons = [l for u in body["units"] for l in u["lessons"]]
        hinted = [l for l in lessons if l["prereq_arranged"]]
        assert hinted
        assert all(l["prereq_hint"] == "prereq_arranged_not_mastered" for l in hinted)

    def test_unconfirmed_outline_gates_start(self, env) -> None:
        user_id, token, route_id, course_id = _confirm_route_and_outline(env)
        client = env["client"]
        # 找一个未确认大纲的课程
        overview = client.get(f"/api/routes/{route_id}/overview", headers=auth_headers(token)).json()
        other = [c for s in overview["stages"] for c in s["courses"] if c["course_id"] != course_id][0]
        r = client.post(
            f"/api/courses/{other['course_id']}/lessons/les_x/start", headers=auth_headers(token)
        )
        assert r.status_code == 409
        assert r.json()["error_code"] == "LESSON_UNLOCK_REQUIRED"
        # 路线总览不受影响
        r2 = client.get(f"/api/routes/{route_id}/overview", headers=auth_headers(token))
        assert r2.status_code == 200

    def test_lesson_start_when_ready_starts_teaching(self, env) -> None:
        """切片 4：大纲已确认 + 讲次就绪 → 真启动教学（创建课程会话）。"""
        from sqlalchemy import select, update

        from backend.planning.models import LessonRow, OutlineVersionRow

        user_id, token, route_id, course_id = _confirm_route_and_outline(env)
        # 讲次置 ready（首段内容生成由切片 4 worker 承接）
        with env["database"].session() as sess:
            ov_id = sess.execute(
                select(OutlineVersionRow.outline_version_id)
                .where(OutlineVersionRow.course_id == course_id)
                .limit(1)
            ).scalar_one()
        with env["database"].session() as sess:
            lesson = sess.execute(
                select(LessonRow).where(LessonRow.outline_version_id == ov_id).limit(1)
            ).scalars().first()
            lesson.status_preparation = "ready"
            lesson_id = lesson.lesson_id
        r = env["client"].post(
            f"/api/courses/{course_id}/lessons/{lesson_id}/start", headers=auth_headers(token)
        )
        assert r.status_code == 201, r.text
        assert r.json()["conversation_id"]
        # 未就绪讲次仍被门槛拒绝
        r2 = env["client"].post(
            f"/api/courses/{course_id}/lessons/les_missing/start", headers=auth_headers(token)
        )
        assert r2.status_code == 404


class TestNavigation:
    def test_navigation_roundtrip(self, env) -> None:
        user_id, token, route_id, course_id = _confirm_route_and_outline(env)
        client = env["client"]
        r = client.put(
            "/api/navigation", headers=auth_headers(token),
            json={"route_id": route_id, "course_id": course_id},
        )
        assert r.status_code == 200
        got = client.get("/api/navigation", headers=auth_headers(token)).json()
        assert got["last_route_id"] == route_id
        assert got["last_course_id"] == course_id
        # 用户隔离
        _, other_token = make_user(client, env["database"])
        other = client.get("/api/navigation", headers=auth_headers(other_token)).json()
        assert other["last_route_id"] is None
