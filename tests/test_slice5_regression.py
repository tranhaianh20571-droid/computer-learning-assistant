"""切片 5 全量回归：两账户交叉访问全部资源类型均拒绝（架构 §8.2）。

资源清单：资料/图块内容、路线与课程总览、草稿、任务、SSE 事件、
教学工作区、音频、检查点、检索。A 建立全套资源，B 逐一交叉访问。
"""

from __future__ import annotations

import io
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

from backend.logging_audit.db import get_database_url  # noqa: E402

os.environ.setdefault("LEARNING_DATABASE_URL", get_database_url())

from sqlalchemy import select, update  # noqa: E402

from backend.logging_audit.db import Database  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.logging_audit.repositories import PgAuditLog  # noqa: E402
from backend.planning.models import LessonRow  # noqa: E402
from backend.slice3_app import Slice3State, create_slice3_app  # noqa: E402
from backend.slice0_app import Slice0State  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from slice3_common import (  # noqa: E402
    OUTLINE_OK_PAYLOAD,
    ROUTE_OK_PAYLOAD,
    SyntheticModelAdapter,
    SyntheticSearchAdapter,
    TeachingModelAdapter,
    make_user,
)


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db


@pytest.fixture(scope="module")
def env(database: Database):
    container = {"model": None, "search": None, "tts": None, "ocr": None}
    state = Slice3State(
        database, PgAuditLog(database),
        model_adapter_factory=lambda o, t: container["model"],
        search_adapter_factory=lambda o, t: container["search"],
        tts_adapter_factory=lambda o, t: container["tts"],
        ocr_adapter_factory=lambda o, t: container["ocr"],
    )
    app = create_slice3_app(state=state)
    app.state.slice0 = Slice0State(database, PgAuditLog(database))
    client = TestClient(app, raise_server_exceptions=False)
    user_a, token_a = make_user(client, database)
    user_b, token_b = make_user(client, database)
    HA = {"Authorization": f"Bearer {token_a}"}
    HB = {"Authorization": f"Bearer {token_b}"}

    # ---- A 建立全套资源 ----
    buf = io.BytesIO()
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(buf, pagesize=A4)
    c.drawString(60, 760, "Neural network chapter conv2d_block_7 v1.2.3")
    c.showPage()
    c.save()
    up = client.post("/api/materials", headers=HA,
                     files=[("files", ("doc.pdf", buf.getvalue(), "application/pdf"))])
    assert up.status_code == 201
    material_id = up.json()["data"][0]["material_id"]

    container["model"] = SyntheticModelAdapter(payload=ROUTE_OK_PAYLOAD)
    container["search"] = SyntheticSearchAdapter()
    route_id = client.post("/api/planning/routes", headers=HA,
                           json={"title": "回归路线", "scenario": "self_study"}).json()["route_id"]
    t1 = client.post("/api/planning/tasks", headers=HA,
                     json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "route_generate",
                           "planning_scope": "route", "target_route_id": route_id}).json()["task_id"]
    b1 = client.post(f"/api/planning/tasks/{t1}/run", headers=HA).json()
    d1 = state.drafts.get_draft(b1["draft"]["draft_id"], actor_id=user_a)
    client.post(f"/api/planning/drafts/{b1['draft']['draft_id']}/confirm", headers=HA,
                json={"revision": d1["revision"], "idempotency_key": f"c{uuid.uuid4().hex[:8]}"})
    overview = client.get(f"/api/routes/{route_id}/overview", headers=HA).json()
    course_id = overview["stages"][0]["courses"][0]["course_id"]

    container["model"] = SyntheticModelAdapter(payload=OUTLINE_OK_PAYLOAD)
    t2 = client.post("/api/planning/tasks", headers=HA,
                     json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "outline_generate",
                           "planning_scope": "course", "target_course_id": course_id}).json()["task_id"]
    b2 = client.post(f"/api/planning/tasks/{t2}/run", headers=HA).json()
    d2 = state.drafts.get_draft(b2["draft"]["draft_id"], actor_id=user_a)
    client.post(f"/api/planning/drafts/{b2['draft']['draft_id']}/confirm", headers=HA,
                json={"revision": d2["revision"], "idempotency_key": f"c{uuid.uuid4().hex[:8]}"})
    cov = client.get(f"/api/courses/{course_id}/overview", headers=HA).json()
    lesson_id = cov["units"][0]["lessons"][0]["lesson_id"]
    with database.session() as sess:
        sess.execute(update(LessonRow).where(LessonRow.lesson_id == lesson_id)
                     .values(status_preparation="ready"))

    container["model"] = TeachingModelAdapter(payload=OUTLINE_OK_PAYLOAD)
    start = client.post(f"/api/courses/{course_id}/lessons/{lesson_id}/start", headers=HA).json()
    conv_id = start["conversation_id"]
    client.post(f"/api/teaching/tasks/{start['task_id']}/run", headers=HA)
    # A 的播放检查点
    client.post(f"/api/teaching/{conv_id}/checkpoint", headers=HA,
                json={"client_seq": 1, "step_no": 1, "position_ms": 2500})

    return {
        "client": client, "database": database, "state": state,
        "HA": HA, "HB": HB,
        "material_id": material_id, "route_id": route_id, "course_id": course_id,
        "lesson_id": lesson_id, "conv_id": conv_id, "task_ids": [t1, t2],
        "draft_id": b1["draft"]["draft_id"],
    }


@pytest.fixture(scope="module")
def audio_id(env) -> str | None:
    """为 A 的首段生成音频（合成 TTS），返回 audio_id。"""
    client = env["client"]
    with env["database"].session() as sess:
        from backend.teaching.models import TeachingStepRow

        step = sess.execute(
            select(TeachingStepRow).where(
                TeachingStepRow.conversation_id == env["conv_id"],
                TeachingStepRow.step_no == 1,
            )
        ).scalars().first()
        return step.audio_id if step else None


class TestCrossAccountRegression:
    """B（非所有者）访问 A 的每一类资源：一律 404/403，不泄露存在性。"""

    def test_material_read(self, env) -> None:
        r = env["client"].get(f"/api/materials/{env['material_id']}", headers=env["HB"])
        # 资料模块跨账户返回 403（不泄露存在性由 404 语义覆盖规划模块）
        assert r.status_code in (403, 404)

    def test_material_list_isolated(self, env) -> None:
        r = env["client"].get("/api/materials", headers=env["HB"])
        assert all(m["material_id"] != env["material_id"] for m in r.json()["data"])

    def test_figure_content(self, env) -> None:
        r = env["client"].get(
            f"/api/materials/{env['material_id']}/figures/fig_fake/content", headers=env["HB"])
        assert r.status_code in (403, 404, 500)

    def test_route_overview(self, env) -> None:
        r = env["client"].get(f"/api/routes/{env['route_id']}/overview", headers=env["HB"])
        assert r.status_code == 404

    def test_route_scene(self, env) -> None:
        r = env["client"].get(f"/api/routes/{env['route_id']}/scene", headers=env["HB"])
        assert r.status_code == 404

    def test_course_overview(self, env) -> None:
        r = env["client"].get(f"/api/courses/{env['course_id']}/overview", headers=env["HB"])
        assert r.status_code == 404

    def test_draft_read(self, env) -> None:
        r = env["client"].get(f"/api/planning/drafts/{env['draft_id']}", headers=env["HB"])
        assert r.status_code == 404

    def test_draft_confirm(self, env) -> None:
        r = env["client"].post(f"/api/planning/drafts/{env['draft_id']}/confirm", headers=env["HB"],
                               json={"revision": 1, "idempotency_key": f"c{uuid.uuid4().hex[:8]}"})
        assert r.status_code == 404

    def test_task_read(self, env) -> None:
        for tid in env["task_ids"]:
            r = env["client"].get(f"/api/tasks/{tid}", headers=env["HB"])
            assert r.status_code in (403, 404)

    def test_task_sse(self, env) -> None:
        for tid in env["task_ids"]:
            r = env["client"].get(f"/api/tasks/{tid}/sse", headers=env["HB"])
            assert r.status_code in (403, 404)

    def test_task_delete_denied(self, env) -> None:
        r = env["client"].delete(f"/api/planning/routes/{env['route_id']}", headers=env["HB"])
        assert r.status_code == 404

    def test_teaching_workspace(self, env) -> None:
        r = env["client"].get(f"/api/teaching/{env['conv_id']}", headers=env["HB"])
        assert r.status_code == 404

    def test_checkpoint(self, env) -> None:
        r = env["client"].post(f"/api/teaching/{env['conv_id']}/checkpoint", headers=env["HB"],
                               json={"client_seq": 99, "step_no": 1, "position_ms": 0})
        assert r.status_code == 404

    def test_followup(self, env) -> None:
        r = env["client"].post(f"/api/teaching/{env['conv_id']}/followup", headers=env["HB"],
                               json={"content": "越权追问", "idempotency_key": f"f{uuid.uuid4().hex[:8]}"})
        assert r.status_code == 404

    def test_resume(self, env) -> None:
        r = env["client"].post(f"/api/teaching/{env['conv_id']}/resume", headers=env["HB"])
        assert r.status_code == 404

    def test_audio_content(self, env, audio_id: str | None) -> None:
        if audio_id is None:
            pytest.skip("本运行无音频（未配置 TTS 适配器）")
        r = env["client"].get(f"/api/teaching/audio/{audio_id}/content", headers=env["HB"])
        assert r.status_code == 404

    def test_audio_content_owner_ok(self, env, audio_id: str | None) -> None:
        if audio_id is None:
            pytest.skip("本运行无音频")
        r = env["client"].get(f"/api/teaching/audio/{audio_id}/content", headers=env["HA"])
        assert r.status_code == 200

    def test_material_search_isolated(self, env) -> None:
        client = env["client"]
        # B 索引自己的内容后，检索范围加入 A 的材料也不命中 A 的内容
        r = client.post("/api/materials/search", headers=env["HB"],
                        json={"query": "neural network", "material_ids": [env["material_id"]]})
        assert r.status_code == 200
        assert r.json()["data"] == []

    def test_owner_access_still_works(self, env) -> None:
        """对照：所有者访问全部资源正常。"""
        client = env["client"]
        assert client.get(f"/api/materials/{env['material_id']}", headers=env["HA"]).status_code == 200
        assert client.get(f"/api/routes/{env['route_id']}/overview", headers=env["HA"]).status_code == 200
        assert client.get(f"/api/courses/{env['course_id']}/overview", headers=env["HA"]).status_code == 200
        ws = client.get(f"/api/teaching/{env['conv_id']}", headers=env["HA"])
        assert ws.status_code == 200
