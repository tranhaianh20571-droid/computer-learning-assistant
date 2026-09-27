"""切片 3 T06 测试：场景转换稳定映射、视图状态、拖动不改业务顺序的后端口径。"""

from __future__ import annotations

import uuid

import pytest

from slice3_common import (
    OUTLINE_OK_PAYLOAD,
    ROUTE_OK_PAYLOAD,
    SyntheticModelAdapter,
    SyntheticSearchAdapter,
    auth_headers,
    make_user,
)

from backend.planning.scene import (  # noqa: E402
    LAYOUT,
    outline_scene,
    route_scene,
)
from backend.slice0_app import Slice0State  # noqa: E402
from backend.slice3_app import Slice3State, create_slice3_app  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from backend.logging_audit.db import Database  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.logging_audit.repositories import PgAuditLog  # noqa: E402


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


def _confirmed_route(env):
    client = env["client"]
    user_id, token = make_user(client, env["database"])
    route_id = client.post(
        "/api/planning/routes", headers=auth_headers(token),
        json={"title": "场景路线", "scenario": "self_study"},
    ).json()["route_id"]
    env["container"]["model"] = SyntheticModelAdapter(payload=ROUTE_OK_PAYLOAD)
    env["container"]["search"] = SyntheticSearchAdapter()
    task_id = client.post(
        "/api/planning/tasks", headers=auth_headers(token),
        json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "route_generate",
              "planning_scope": "route", "target_route_id": route_id},
    ).json()["task_id"]
    body = client.post(f"/api/planning/tasks/{task_id}/run", headers=auth_headers(token)).json()
    draft = env["state"].drafts.get_draft(body["draft"]["draft_id"], actor_id=user_id)
    client.post(
        f"/api/planning/drafts/{body['draft']['draft_id']}/confirm", headers=auth_headers(token),
        json={"revision": draft["revision"], "idempotency_key": f"c{uuid.uuid4().hex[:8]}"},
    )
    return user_id, token, route_id


class TestSceneAPI:
    def test_route_scene_from_confirmed_version(self, env) -> None:
        user_id, token, route_id = _confirmed_route(env)
        r = env["client"].get(f"/api/routes/{route_id}/scene", headers=auth_headers(token))
        assert r.status_code == 200, r.text
        scene = r.json()
        types = {el["type"] for el in scene["elements"]}
        assert types <= {"rectangle", "ellipse", "text", "arrow", "line"}
        # 业务节点全覆盖：2 阶段 + 4 课程
        assert len(scene["node_positions"]) == 6
        # 先修边为虚线箭头且与树形框分层
        arrows = [el for el in scene["elements"] if el["type"] == "arrow"]
        assert arrows and all(a.get("strokeStyle") == "dashed" for a in arrows)

    def test_scene_deterministic_across_calls(self, env) -> None:
        user_id, token, route_id = _confirmed_route(env)
        s1 = env["client"].get(f"/api/routes/{route_id}/scene", headers=auth_headers(token)).json()
        s2 = env["client"].get(f"/api/routes/{route_id}/scene", headers=auth_headers(token)).json()
        assert s1 == s2

    def test_scene_requires_confirmed_version(self, env) -> None:
        user_id, token = make_user(env["client"], env["database"])
        route_id = env["client"].post(
            "/api/planning/routes", headers=auth_headers(token),
            json={"title": "未确认", "scenario": "self_study"},
        ).json()["route_id"]
        r = env["client"].get(f"/api/routes/{route_id}/scene", headers=auth_headers(token))
        assert r.status_code == 404

    def test_cross_user_scene_denied(self, env) -> None:
        _, token, route_id = _confirmed_route(env)
        _, other_token = make_user(env["client"], env["database"])
        r = env["client"].get(f"/api/routes/{route_id}/scene", headers=auth_headers(other_token))
        assert r.status_code == 404


class TestViewState:
    def test_viewstate_roundtrip_and_isolation(self, env) -> None:
        user_id, token, route_id = _confirmed_route(env)
        client = env["client"]
        canvas = {"scrollX": 12.5, "scrollY": -3, "zoom": 0.8, "collapsed": ["stg_a"]}
        r = client.put(
            f"/api/planning/viewstates/route/{route_id}", headers=auth_headers(token),
            json={"canvas": canvas},
        )
        assert r.status_code == 200
        got = client.get(
            f"/api/planning/viewstates/route/{route_id}", headers=auth_headers(token)
        ).json()
        assert got["canvas"] == canvas

        # 视图状态不承载业务顺序：order_index 只来自版本数据
        overview = client.get(f"/api/routes/{route_id}/overview", headers=auth_headers(token)).json()
        first_stage = overview["stages"][0]
        assert first_stage["order_index"] == 0
        assert [c["order_index"] for c in first_stage["courses"]] == [0, 1]

        # 其他用户视角为空
        _, other_token = make_user(client, env["database"])
        other = client.get(
            f"/api/planning/viewstates/route/{route_id}", headers=auth_headers(other_token)
        ).json()
        assert other["canvas"] == {}


class TestLayoutConstants:
    def test_layout_matches_t02_spike(self) -> None:
        assert LAYOUT["font_size"] == 16
        assert LAYOUT["max_lines"] == 2
        assert LAYOUT["stage_w"] == 260 and LAYOUT["stage_h"] == 56
        assert LAYOUT["course_w"] == 220 and LAYOUT["course_h"] == 64
        assert LAYOUT["column_gap"] == 64 and LAYOUT["row_gap"] == 24

    def test_outline_scene_long_lesson_no_overflow(self) -> None:
        from backend.planning.scene import measure_text_width, wrap_label

        payload = {
            "units": [{"id": "u1", "title": "单元", "lessons": [
                {"id": "l1", "title": "事务隔离级别与并发异常：脏读、不可重复读、幻读的判定（长中文讲次标题）"}
            ]}],
            "prerequisites": [],
        }
        scene = outline_scene(payload)
        inner = LAYOUT["lesson_w"] - 2 * LAYOUT["node_inner_pad"]
        texts = [el for el in scene["elements"] if el["type"] == "text"]
        assert texts
        for el in texts:
            for line in el["text"].split("\n"):
                assert measure_text_width(line) <= inner
            assert len(el["text"].split("\n")) <= LAYOUT["max_lines"]
        # wrap_label 截断省略号
        lines = wrap_label(payload["units"][0]["lessons"][0]["title"], inner)
        assert lines[-1].endswith("…")
