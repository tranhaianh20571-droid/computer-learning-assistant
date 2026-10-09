"""切片 3 T06 测试：场景转换稳定映射、视图状态、拖动不改业务顺序的后端口径。"""

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
    body = run_route_generate(client, token, task_id)
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
        # 业务节点全覆盖：1 大标题根卡 + 4 课程（阶段不再单独成卡）
        assert len(scene["node_positions"]) == 5
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

    def test_draft_scene_strips_legacy_internal_ids(self, env) -> None:
        """读路径兜底：剥除上线前生成的存量草稿预览时也不泄漏内部 id（含 goal）。"""
        import json as _json

        from backend.planning.models import PlanningDraftRow

        user_id, token = make_user(env["client"], env["database"])
        draft_id = f"draft_{uuid.uuid4().hex[:12]}"
        dirty = {
            "stages": [{"title": "基础构建：route_8a471d50aada 核心概念", "courses": [
                {"title": "route_8a471d50aada 基础导论", "goal": "route_deadbeef1234 了解基本操作"},
            ]}],
            "prerequisites": [],
        }
        with env["database"].session() as sess:
            sess.add(PlanningDraftRow(
                draft_id=draft_id, owner_user_id=user_id, draft_kind="route",
                target_id="route_legacy001", revision=1,
                content_json=_json.dumps(dirty, ensure_ascii=False),
            ))
            sess.commit()
        r = env["client"].get(
            f"/api/planning/drafts/{draft_id}/scene", headers=auth_headers(token),
        )
        assert r.status_code == 200, r.text
        texts = [el.get("text", "") for el in r.json()["elements"] if el.get("type") == "text"]
        assert texts
        assert all("route_" not in t for t in texts)


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
        )
        assert other.status_code == 404


class TestLayoutConstants:
    def test_layout_constants_indent_tree_v2(self) -> None:
        # 字体度量为 T02 锁定值；几何常数为 v2 横向缩进树布局
        assert LAYOUT["font_size"] == 16
        assert LAYOUT["max_lines"] == 2
        assert LAYOUT["stage_w"] == 220
        assert LAYOUT["course_w"] == 300
        assert LAYOUT["level_gap"] == 72 and LAYOUT["row_gap"] == 18
        assert LAYOUT["accent_w"] == 0

    def test_indent_tree_geometry_parent_centered(self) -> None:
        payload = {
            "stages": [
                {"id": "s1", "title": "阶段一", "courses": [
                    {"id": "c1", "title": "课程一", "goal": "目标一"},
                    {"id": "c2", "title": "课程二"},
                ]},
            ],
            "prerequisites": [],
        }
        scene = route_scene(payload)
        pos = scene["node_positions"]
        # 大标题根卡在最左列；子级在父级右侧：x = margin_x + stage_w + level_gap
        assert pos["route_root"]["x"] == LAYOUT["margin_x"]
        assert pos["c1"]["x"] == LAYOUT["margin_x"] + LAYOUT["stage_w"] + LAYOUT["level_gap"]
        # 子级纵向堆叠（row_gap 间隔），父按子树垂直居中
        assert pos["c2"]["y"] >= pos["c1"]["y"] + pos["c1"]["h"] + LAYOUT["row_gap"] - 1e-6
        span_mid = (pos["c1"]["y"] + pos["c2"]["y"] + pos["c2"]["h"]) / 2
        assert abs((pos["route_root"]["y"] + pos["route_root"]["h"] / 2) - span_mid) <= 1

    def test_cards_rounded_with_accent_bar_and_goal_desc(self) -> None:
        payload = {
            "stages": [
                {"id": "s1", "title": "阶段一", "courses": [
                    {"id": "c1", "title": "课程一", "goal": "掌握关系模型"},
                ]},
            ],
            "prerequisites": [],
        }
        scene = route_scene(payload)
        cards = [el for el in scene["elements"]
                 if el["type"] == "rectangle" and el["id"].startswith("node_")]
        assert cards and all(el.get("roundness", {}).get("type") == 2 for el in cards)
        bars = [el for el in scene["elements"]
                if el["type"] == "rectangle" and el["id"].startswith("acc_")]
        assert not bars, "黑白节点不使用彩色强调条"
        descs = [el for el in scene["elements"]
                 if el["type"] == "text" and el["fontSize"] == LAYOUT["desc_size"]]
        assert descs and all(el["strokeColor"] == "#000000" for el in descs)

    def test_prereq_arrow_orthogonal_no_crossing(self) -> None:
        payload = {
            "stages": [
                {"id": "s1", "title": "阶段一", "courses": [
                    {"id": "c1", "title": "课程一"}, {"id": "c2", "title": "课程二"},
                ]},
                {"id": "s2", "title": "阶段二", "courses": [
                    {"id": "c3", "title": "课程三"}, {"id": "c4", "title": "课程四"},
                ]},
            ],
            "prerequisites": [{"from": "c1", "to": "c3"}],
        }
        scene = route_scene(payload)
        pos = scene["node_positions"]
        max_right = max(p["x"] + p["w"] for p in pos.values())
        arrow = [el for el in scene["elements"] if el["type"] == "arrow"][0]
        xs = [arrow["x"]] + [arrow["x"] + pt[0] for pt in arrow["points"]]
        ys = [arrow["y"]] + [arrow["y"] + pt[1] for pt in arrow["points"]]
        # 每段都轴对齐（正交折线），走廊在所有节点右侧
        for i in range(len(xs) - 1):
            assert xs[i] == xs[i + 1] or ys[i] == ys[i + 1]
        assert min(xs) >= max_right
        assert arrow["strokeStyle"] == "dashed" and arrow.get("endArrowhead") == "arrow"

    def test_children_ordered_by_prereq_topology(self) -> None:
        payload = {
            "stages": [
                {"id": "s1", "title": "阶段一", "courses": [{"id": "c1", "title": "课程一"}]},
                {"id": "s2", "title": "阶段二", "courses": [
                    {"id": "c3", "title": "后修课程"}, {"id": "c2", "title": "先修课程"},
                ]},
            ],
            "prerequisites": [{"from": "c2", "to": "c3"}],
        }
        scene = route_scene(payload)
        pos = scene["node_positions"]
        # c2 是 c3 的先修：画布纵序 c2 在 c3 上方（业务 order_index 不受影响）
        assert pos["c2"]["y"] < pos["c3"]["y"]

    def test_outline_scene_long_lesson_no_overflow(self) -> None:
        from backend.planning.scene import measure_text_width, wrap_label

        payload = {
            "units": [{"id": "u1", "title": "单元", "lessons": [
                {"id": "l1", "title": "事务隔离级别与并发异常：脏读、不可重复读、幻读的判定（长中文讲次标题）"}
            ]}],
            "prerequisites": [],
        }
        scene = outline_scene(payload)
        inner = LAYOUT["lesson_w"] - 2 * LAYOUT["node_inner_pad"] - LAYOUT["accent_w"]
        texts = [el for el in scene["elements"] if el["type"] == "text"]
        assert texts
        for el in texts:
            for line in el["text"].split("\n"):
                assert measure_text_width(line, el["fontSize"]) <= inner
            assert len(el["text"].split("\n")) <= LAYOUT["max_lines"]
        # wrap_label 截断省略号
        lines = wrap_label(payload["units"][0]["lessons"][0]["title"], inner)
        assert lines[-1].endswith("…")

    def test_route_scene_four_levels_with_units(self) -> None:
        payload = {
            "title": "调度学习路线",
            "stages": [{"id": "s1", "title": "阶段一", "courses": [
                {"id": "c1", "title": "操作系统调度", "goal": "能实现调度器", "units": [
                    {"id": "u1", "title": "调度基础", "lessons": [
                        {"id": "l1", "title": "进程与线程", "goal": "能区分进程线程"},
                    ]},
                ]},
            ]}],
            "prerequisites": [],
        }
        scene = route_scene(payload)
        pos = scene["node_positions"]
        # 四层节点齐备：大标题根卡 + 课程 + 单元 + 讲次
        assert set(pos) == {"route_root", "c1", "u1", "l1"}
        # 列序：大标题最左 → 课程 → 单元 → 讲次
        xs = [pos["route_root"]["x"], pos["c1"]["x"], pos["u1"]["x"], pos["l1"]["x"]]
        assert xs == sorted(xs)
        assert pos["route_root"]["x"] == LAYOUT["margin_x"]
        # 根卡回退：无 title 时使用默认名
        fallback = route_scene({"stages": payload["stages"], "prerequisites": []})
        root_texts = [el["text"] for el in fallback["elements"]
                      if el["type"] == "text" and el["text"] == "学习路线"]
        assert root_texts

    def test_outline_scene_has_course_root(self) -> None:
        payload = {
            "title": "数据库基础",
            "units": [{"id": "u1", "title": "单元", "lessons": [{"id": "l1", "title": "第 1 讲"}]}],
            "prerequisites": [],
        }
        scene = outline_scene(payload)
        assert set(scene["node_positions"]) == {"course_root", "u1", "l1"}
        # 无 title 时根卡回退为「课程」
        fallback = outline_scene({"units": payload["units"], "prerequisites": []})
        texts = [el["text"] for el in fallback["elements"] if el["type"] == "text"]
        assert "课程" in texts
