"""切片 3 T05 测试：草稿乐观锁、确认事务、幂等、路线展开大纲、教学门槛。"""

from __future__ import annotations

import uuid

import pytest

from common_fixtures import (
    OUTLINE_OK_PAYLOAD,
    ROUTE_OK_PAYLOAD,
    SkeletonThenOutlineAdapter,
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
from backend.planning.errors import PlanningError  # noqa: E402
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
    slice0 = Slice0State(database, PgAuditLog(database))
    app.state.slice0 = slice0
    client = TestClient(app)
    return {"client": client, "state": state, "container": container, "database": database}


def _confirmed_route(env, *, cycle: bool = False, empty: bool = False, confirm_first: bool = True):
    """生成并确认一条路线，返回 (user_id, token, route_id, course_ids)。"""
    client = env["client"]
    user_id, token = make_user(client, env["database"])
    route_id = client.post(
        "/api/planning/routes", headers=auth_headers(token),
        json={"title": "确认路线", "scenario": "self_study"},
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
    if not confirm_first:
        return user_id, token, route_id, []
    confirmed = client.post(
        f"/api/planning/drafts/{body['draft']['draft_id']}/confirm", headers=auth_headers(token),
        json={"revision": draft["revision"], "idempotency_key": f"c{uuid.uuid4().hex[:8]}"},
    )
    assert confirmed.status_code == 200, confirmed.text
    overview = client.get(f"/api/routes/{route_id}/overview", headers=auth_headers(token)).json()
    course_ids = [c["course_id"] for s in overview["stages"] for c in s["courses"]]
    return user_id, token, route_id, course_ids


class TestDraftOptimisticLock:
    def test_revision_mismatch_rejected(self, env) -> None:
        user_id, token, route_id, _ = _confirmed_route(env, confirm_first=False)
        client = env["client"]
        # 直接构造草稿内容保存
        draft = env["state"].drafts.find_or_create_draft(
            owner_user_id=user_id, draft_kind="route", target_id=route_id, base_version_id=None,
        )
        content = draft["content"] or ROUTE_OK_PAYLOAD
        # 旧 revision 保存 → 409 DRAFT_REVISION_MISMATCH
        r = client.put(
            f"/api/planning/drafts/{draft['draft_id']}", headers=auth_headers(token),
            json={"revision": draft["revision"], "content": content},
        )
        assert r.status_code == 200  # 正确 revision 成功
        new_revision = r.json()["revision"]
        r2 = client.put(
            f"/api/planning/drafts/{draft['draft_id']}", headers=auth_headers(token),
            json={"revision": draft["revision"], "content": content},
        )
        assert r2.status_code == 409
        assert r2.json()["error_code"] == "DRAFT_REVISION_MISMATCH"
        # 服务端 revision 只前进一次
        assert new_revision == draft["revision"] + 1

    def test_cross_user_save_denied(self, env) -> None:
        user_id, token, route_id, _ = _confirmed_route(env, confirm_first=False)
        draft = env["state"].drafts.find_or_create_draft(
            owner_user_id=user_id, draft_kind="route", target_id=route_id, base_version_id=None,
        )
        other_id, other_token = make_user(env["client"], env["database"])
        r = env["client"].put(
            f"/api/planning/drafts/{draft['draft_id']}", headers=auth_headers(other_token),
            json={"revision": draft["revision"], "content": {}},
        )
        assert r.status_code == 404  # 越权以 not_found 呈现，不泄露存在性

    def test_save_failed_visible(self, env) -> None:
        user_id, token, route_id, _ = _confirmed_route(env, confirm_first=False)
        draft = env["state"].drafts.find_or_create_draft(
            owner_user_id=user_id, draft_kind="route", target_id=route_id, base_version_id=None,
        )
        # 结构无效内容 → 保存失败（内容校验拒绝）
        r = env["client"].put(
            f"/api/planning/drafts/{draft['draft_id']}", headers=auth_headers(token),
            json={"revision": draft["revision"], "content": {"stages": "not-a-list"}},
        )
        assert r.status_code == 400
        assert r.json()["error_code"] == "ROUTE_STRUCTURE_INVALID"


class TestConfirmTransaction:
    def test_confirm_creates_version_and_pointer(self, env) -> None:
        user_id, token, route_id, _ = _confirmed_route(env)
        client = env["client"]
        overview = client.get(f"/api/routes/{route_id}/overview", headers=auth_headers(token)).json()
        assert overview["has_version"] is True
        assert overview["version_no"] == 1
        course_count = sum(s["course_count"] for s in overview["stages"])
        assert course_count == 4
        # 确认产生课程壳
        assert all(c["course_id"] for s in overview["stages"] for c in s["courses"])

    def test_confirm_idempotent_same_key(self, env) -> None:
        user_id, token, route_id, _ = _confirmed_route(env, confirm_first=False)
        client = env["client"]
        # 新草稿 + 同幂等键确认两次 → 返回既有版本号，不产生重复有效版本
        draft = env["state"].drafts.find_or_create_draft(
            owner_user_id=user_id, draft_kind="route", target_id=route_id, base_version_id=None,
        )
        idem = f"confirm-{uuid.uuid4().hex[:8]}"
        r1 = client.post(
            f"/api/planning/drafts/{draft['draft_id']}/confirm", headers=auth_headers(token),
            json={"revision": draft["revision"], "idempotency_key": idem},
        )
        assert r1.status_code == 200, r1.text
        r2 = client.post(
            f"/api/planning/drafts/{draft['draft_id']}/confirm", headers=auth_headers(token),
            json={"revision": draft["revision"], "idempotency_key": idem},
        )
        assert r2.status_code == 200
        assert r2.json()["deduped"] is True
        assert r2.json()["version_id"] == r1.json()["version_id"]
        # 版本号未重复增长
        overview = client.get(f"/api/routes/{route_id}/overview", headers=auth_headers(token)).json()
        assert overview["version_no"] == r1.json()["version_no"]

    def test_confirm_with_stale_revision_fails(self, env) -> None:
        user_id, token, route_id, _ = _confirmed_route(env, confirm_first=False)
        draft = env["state"].drafts.find_or_create_draft(
            owner_user_id=user_id, draft_kind="route", target_id=route_id, base_version_id=None,
        )
        r = env["client"].post(
            f"/api/planning/drafts/{draft['draft_id']}/confirm", headers=auth_headers(token),
            json={"revision": draft["revision"] + 99, "idempotency_key": f"c{uuid.uuid4().hex[:8]}"},
        )
        assert r.status_code == 409
        assert r.json()["error_code"] == "DRAFT_REVISION_MISMATCH"

    def test_confirm_cycle_rejected_wholly(self, env) -> None:
        user_id, token, route_id, _ = _confirmed_route(env, confirm_first=False)
        client = env["client"]
        draft = env["state"].drafts.find_or_create_draft(
            owner_user_id=user_id, draft_kind="route", target_id=route_id, base_version_id=None,
        )
        cycle_content = {
            "stages": [{"title": "s", "courses": [{"title": "a"}, {"title": "b"}]}],
            "prerequisites": [{"from_index": 0, "to_index": 1}, {"from_index": 1, "to_index": 0}],
        }
        # 含环内容在保存阶段即被拒绝（DAG 校验先于确认事务）
        r = client.put(
            f"/api/planning/drafts/{draft['draft_id']}", headers=auth_headers(token),
            json={"revision": draft["revision"], "content": cycle_content},
        )
        assert r.status_code == 400
        assert r.json()["error_code"] == "DAG_CYCLE"
        # 整体不生效：路线当前版本不变
        overview = client.get(f"/api/routes/{route_id}/overview", headers=auth_headers(token)).json()
        assert overview["version_no"] is None

    def test_confirm_empty_outline_rejected(self, env) -> None:
        user_id, token, route_id, course_ids = _confirmed_route(env)
        client = env["client"]
        course_id = course_ids[0]
        draft = env["state"].drafts.find_or_create_draft(
            owner_user_id=user_id, draft_kind="course_outline", target_id=course_id,
            base_version_id=None, initial_content=OUTLINE_OK_PAYLOAD,
        )
        # 空单元
        r = client.put(
            f"/api/planning/drafts/{draft['draft_id']}", headers=auth_headers(token),
            json={"revision": draft["revision"], "content": {"units": [{"title": "u", "lessons": []}]}},
        )
        assert r.status_code == 400
        assert r.json()["error_code"] == "INVALID_OUTLINE_STRUCTURE"

    def test_outline_confirm_updates_pointer_and_gates_teaching(self, env) -> None:
        user_id, token, route_id, course_ids = _confirmed_route(env)
        client = env["client"]
        course_id = course_ids[0]
        # 未确认大纲 → 教学门槛生效
        r = client.post(
            f"/api/courses/{course_id}/lessons/les_x/start", headers=auth_headers(token)
        )
        assert r.status_code == 409
        assert r.json()["error_code"] == "LESSON_UNLOCK_REQUIRED"
        overview = client.get(f"/api/courses/{course_id}/overview", headers=auth_headers(token)).json()
        assert overview["teaching_unlocked"] is False

        # 确认大纲
        draft = env["state"].drafts.find_or_create_draft(
            owner_user_id=user_id, draft_kind="course_outline", target_id=course_id,
            base_version_id=None, initial_content=OUTLINE_OK_PAYLOAD,
        )
        confirmed = client.post(
            f"/api/planning/drafts/{draft['draft_id']}/confirm", headers=auth_headers(token),
            json={"revision": draft["revision"], "idempotency_key": f"c{uuid.uuid4().hex[:8]}"},
        )
        assert confirmed.status_code == 200, confirmed.text
        overview = client.get(f"/api/courses/{course_id}/overview", headers=auth_headers(token)).json()
        assert overview["teaching_unlocked"] is True
        assert overview["progress"]["lesson_total"] == 4


class TestRouteToOutlineExpansion:
    def test_outline_inherits_route_source(self, env) -> None:
        user_id, token, route_id, course_ids = _confirmed_route(env)
        client = env["client"]
        course_id = course_ids[0]
        # 路线展开：来源记录在课程壳上（route confirm 时写入）
        from sqlalchemy import select

        from backend.planning.models import CourseRow, OutlineVersionRow

        with env["database"].session() as sess:
            course = sess.get(CourseRow, course_id)
            source_version = course.source_route_version_id
            assert source_version
        # 确认大纲后版本记录继承来源
        draft = env["state"].drafts.find_or_create_draft(
            owner_user_id=user_id, draft_kind="course_outline", target_id=course_id,
            base_version_id=None, initial_content=OUTLINE_OK_PAYLOAD,
        )
        confirmed = client.post(
            f"/api/planning/drafts/{draft['draft_id']}/confirm", headers=auth_headers(token),
            json={"revision": draft["revision"], "idempotency_key": f"c{uuid.uuid4().hex[:8]}"},
        ).json()
        with env["database"].session() as sess:
            outline = sess.get(OutlineVersionRow, confirmed["version_id"])
            assert outline.source_route_version_id == source_version


class TestScopeSwitch:
    def test_switch_keeps_input_new_draft(self, env) -> None:
        user_id, token, route_id, _ = _confirmed_route(env, confirm_first=False)
        client = env["client"]
        draft = env["state"].drafts.find_or_create_draft(
            owner_user_id=user_id, draft_kind="route", target_id=route_id, base_version_id=None,
        )
        r = client.post(
            f"/api/planning/drafts/{draft['draft_id']}/scope", headers=auth_headers(token),
            json={"planning_scope": "course"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # 输入保留：新草稿承接旧内容
        assert body["draft"]["content"]["stages"] == draft["content"]["stages"]
        assert body["draft"]["target_id"] != route_id
        assert body["draft"]["draft_kind"] == "course_outline"
        assert body["new_kind"] == "course_outline"


class TestRouteConfirmMaterializesOutlines:
    def test_confirm_route_with_units_materializes_course_outlines(self, env) -> None:
        """四层草稿确认：逐课落不可变大纲版本，路线场景保持到讲次粒度。"""
        client = env["client"]
        user_id, token = make_user(client, env["database"])
        route_id = client.post(
            "/api/planning/routes", headers=auth_headers(token),
            json={"title": "四层路线", "scenario": "self_study"},
        ).json()["route_id"]
        env["container"]["model"] = SkeletonThenOutlineAdapter(
            payload=ROUTE_OK_PAYLOAD, outline_payload=OUTLINE_OK_PAYLOAD,
        )
        env["container"]["search"] = SyntheticSearchAdapter()
        task_id = client.post(
            "/api/planning/tasks", headers=auth_headers(token),
            json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "route_generate",
                  "planning_scope": "route", "target_route_id": route_id},
        ).json()["task_id"]
        body = run_route_generate(client, token, task_id)
        assert body["task_status"] == "succeeded", body
        draft_id = body["draft"]["draft_id"]
        draft = env["state"].drafts.get_draft(draft_id, actor_id=user_id)
        confirmed = client.post(
            f"/api/planning/drafts/{draft_id}/confirm", headers=auth_headers(token),
            json={"revision": draft["revision"], "idempotency_key": f"c{uuid.uuid4().hex[:8]}"},
        )
        assert confirmed.status_code == 200, confirmed.text

        # 路线场景四层：根卡 + 4 课程 + 单元 + 讲次（les_ 前缀来自确认落库的讲次行）
        scene = client.get(f"/api/routes/{route_id}/scene", headers=auth_headers(token)).json()
        keys = set(scene["node_positions"].keys())
        assert "route_root" in keys
        assert any(k.startswith("unit_") for k in keys)
        assert any(k.startswith("les_") for k in keys)

        # 每门课都拿到当前大纲版本，且课程场景到讲次粒度
        overview = client.get(f"/api/routes/{route_id}/overview", headers=auth_headers(token)).json()
        course_ids = [c["course_id"] for s in overview["stages"] for c in s["courses"]]
        assert len(course_ids) == 4
        for course_id in course_ids:
            course_scene = client.get(
                f"/api/courses/{course_id}/scene", headers=auth_headers(token),
            ).json()
            pos = course_scene["node_positions"]
            assert "course_root" in pos
            assert any(k.startswith("unit_") for k in pos)
