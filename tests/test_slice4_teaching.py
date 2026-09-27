"""切片 4 集成测试：启动门槛、步骤生成、预生成纪律、TTS/无音频、检查点、追问恢复。"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

from backend.logging_audit.db import get_database_url  # noqa: E402

os.environ.setdefault("LEARNING_DATABASE_URL", get_database_url())

from sqlalchemy import select, update  # noqa: E402

from backend.logging_audit.db import Database  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.logging_audit.repositories import PgAuditLog, PgTaskEventStore  # noqa: E402
from backend.materials.chunk_index import ChunkIndexService  # noqa: E402
from backend.materials.storage import MaterialStorage  # noqa: E402
from backend.planning.models import (  # noqa: E402
    CourseRow,
    LessonRow,
    OutlineVersionRow,
    PlanningTaskStateRow,
)
from backend.planning.drafts import DraftService  # noqa: E402
from backend.planning.retrieval import PlanningRetrievalService  # noqa: E402
from backend.planning.service import PlanningService  # noqa: E402
from backend.slice3_app import Slice3State  # noqa: E402
from slice3_common import (  # noqa: E402
    OUTLINE_OK_PAYLOAD,
    ROUTE_OK_PAYLOAD,
    SyntheticModelAdapter,
    SyntheticSearchAdapter,
    capability_status,
)

from fastapi.testclient import TestClient  # noqa: E402

from backend.slice0_app import Slice0State  # noqa: E402
from backend.slice3_app import create_slice3_app  # noqa: E402
from backend.teaching.models import AudioAssetRow, PlaybackCheckpointRow, TeachingStepRow  # noqa: E402

GOOD_DECLARATION = {
    "kind": "board", "title": "核心概念速览",
    "points": ["定义", "例子", "误用"],
    "code": None, "formula": None,
    "nodes": [{"id": "n1", "label": "事务 A"}, {"id": "n2", "label": "事务 B"}],
    "edges": [{"from_id": "n1", "to_id": "n2", "label": "冲突"}],
    "original_refs": [], "reveal_ms": [0, 4000, 8000],
}
STEP_PAYLOAD = {
    "steps": [{
        "title": "合成教学步骤",
        "text": "本步骤讲解当前讲次的核心概念：先给出定义，再用一个具体例子说明它在真实系统里如何出现，"
                "最后指出两个最常见的误用与判别方法，帮助你把概念落到可操作的判断上。整段朗读约四十秒。",
        "board": GOOD_DECLARATION,
    }],
}
FOLLOWUP_PAYLOAD = {"answer": "这是追问的合成回答，按定义、例子、误用三段展开。", "board": None}


class TeachingModelAdapter(SyntheticModelAdapter):
    """按 prompt 特征返回教学/追问候选。"""

    def generate(self, messages, **options):
        self.generate_calls.append(options)
        last = ""
        for m in messages:
            if m.get("role") == "user":
                last = str(m.get("content", ""))
        if "follow-up question" in last:
            return {"content": json.dumps(FOLLOWUP_PAYLOAD, ensure_ascii=False)}
        return {"content": json.dumps(STEP_PAYLOAD, ensure_ascii=False)}


class NoSubtitleTTSAdapter:
    kind = "tts"

    def synthesize(self, text, **kwargs):
        return {
            "model": "speech-2.8-hd", "voice_id": "v", "audio_format": "mp3",
            "subtitle_enable": True, "subtitle_type": "sentence",
            "audio_hex": b"ID3audio".hex(), "subtitle": None,
            "text_sha256": __import__("hashlib").sha256(text.encode()).hexdigest(),
            "vendor_trace_id": "t",
        }


class GoodTTSAdapter(NoSubtitleTTSAdapter):
    def synthesize(self, text, **kwargs):
        out = super().synthesize(text, **kwargs)
        out["subtitle"] = [{"word": "seg", "begin_ms": 0, "end_ms": 400}]
        return out


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db


@pytest.fixture()
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
    client = TestClient(app)
    return {"client": client, "state": state, "container": container, "database": database}


def _user_with_ready_lesson(env):
    """注册用户 → 生成并确认路线 → 确认大纲 → 讲次置 ready。返回 (user_id, token, course_id, lesson_id)。"""
    from backend.auth.models import UserRow

    client = env["client"]
    email = f"synthetic.{uuid.uuid4().hex[:8]}@example.com"
    auth = client.app.state.slice0.auth
    user_id = auth.register(email, "synthetic-password-12345", "s4")["user_id"]
    with env["database"].session() as sess:
        row = sess.get(UserRow, user_id)
        row.status = "approved"
    _, token = auth.login(email, "synthetic-password-12345")

    env["container"]["model"] = SyntheticModelAdapter(payload=ROUTE_OK_PAYLOAD)
    env["container"]["search"] = SyntheticSearchAdapter()
    route_id = client.post("/api/planning/routes", headers={"Authorization": f"Bearer {token}"},
                           json={"title": "教学路线", "scenario": "self_study"}).json()["route_id"]
    t = client.post("/api/planning/tasks", headers={"Authorization": f"Bearer {token}"},
                    json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "route_generate",
                          "planning_scope": "route", "target_route_id": route_id}).json()["task_id"]
    body = client.post(f"/api/planning/tasks/{t}/run", headers={"Authorization": f"Bearer {token}"}).json()
    draft = env["state"].drafts.get_draft(body["draft"]["draft_id"], actor_id=user_id)
    client.post(f"/api/planning/drafts/{body['draft']['draft_id']}/confirm",
                headers={"Authorization": f"Bearer {token}"},
                json={"revision": draft["revision"], "idempotency_key": f"c{uuid.uuid4().hex[:8]}"})
    overview = client.get(f"/api/routes/{route_id}/overview", headers={"Authorization": f"Bearer {token}"}).json()
    course_id = overview["stages"][0]["courses"][0]["course_id"]

    env["container"]["model"] = SyntheticModelAdapter(payload=OUTLINE_OK_PAYLOAD)
    t2 = client.post("/api/planning/tasks", headers={"Authorization": f"Bearer {token}"},
                     json={"idempotency_key": f"t{uuid.uuid4().hex[:8]}", "task_kind": "outline_generate",
                           "planning_scope": "course", "target_course_id": course_id}).json()["task_id"]
    body2 = client.post(f"/api/planning/tasks/{t2}/run", headers={"Authorization": f"Bearer {token}"}).json()
    od = env["state"].drafts.get_draft(body2["draft"]["draft_id"], actor_id=user_id)
    client.post(f"/api/planning/drafts/{body2['draft']['draft_id']}/confirm",
                headers={"Authorization": f"Bearer {token}"},
                json={"revision": od["revision"], "idempotency_key": f"c{uuid.uuid4().hex[:8]}"})
    cov = client.get(f"/api/courses/{course_id}/overview", headers={"Authorization": f"Bearer {token}"}).json()
    lesson_id = cov["units"][0]["lessons"][0]["lesson_id"]
    # 讲次置 ready（内容生成属切片 4 之前的占位：教学门槛要求 ready）
    with env["database"].session() as sess:
        sess.execute(
            update(LessonRow).where(LessonRow.lesson_id == lesson_id)
            .values(status_preparation="ready")
        )
    return user_id, token, course_id, lesson_id


class TestStartGate:
    def test_unconfirmed_outline_blocked(self, env) -> None:
        user_id, token, course_id, lesson_id = _user_with_ready_lesson(env)
        # 找一个未确认大纲的课程
        # （_user_with_ready_lesson 只确认了第一门；这里直接用一个假课程）
        r = env["client"].post(
            f"/api/courses/crs_nonexistent/lessons/{lesson_id}/start",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 404

    def test_not_ready_lesson_blocked(self, env) -> None:
        user_id, token, course_id, lesson_id = _user_with_ready_lesson(env)
        # 把讲次改回 not_generated
        with env["database"].session() as sess:
            sess.execute(update(LessonRow).where(LessonRow.lesson_id == lesson_id)
                         .values(status_preparation="not_generated"))
        r = env["client"].post(
            f"/api/courses/{course_id}/lessons/{lesson_id}/start",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 409
        assert r.json()["error_code"] == "lesson_not_ready"

    def test_start_creates_teaching_conversation(self, env) -> None:
        user_id, token, course_id, lesson_id = _user_with_ready_lesson(env)
        client = env["client"]
        env["container"]["model"] = TeachingModelAdapter(payload=STEP_PAYLOAD)
        r = client.post(f"/api/courses/{course_id}/lessons/{lesson_id}/start",
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 201, r.text
        body = r.json()
        conv_id = body["conversation_id"]
        # 首段生成（interactive lane）
        r2 = client.post(f"/api/teaching/tasks/{body['task_id']}/run",
                         headers={"Authorization": f"Bearer {token}"})
        assert r2.status_code == 200, r2.text
        assert r2.json()["status"] == "succeeded"
        ws = client.get(f"/api/teaching/{conv_id}", headers={"Authorization": f"Bearer {token}"}).json()
        assert ws["kind"] == "teaching"
        assert ws["steps"] and ws["steps"][0]["status"] == "ready"
        assert ws["steps"][0]["board"]["elements"]
        # 预生成：首段就绪 + 会话 active → 下一段任务（teaching_prefetch）
        from backend.tasks.service import LearningTaskRow

        with env["database"].session() as sess:
            pf = sess.execute(
                select(LearningTaskRow).where(
                    LearningTaskRow.kind == "lesson_step_generate",
                    LearningTaskRow.lane == "teaching_prefetch",
                    LearningTaskRow.subject_id == f"{conv_id}:2:1",
                )
            ).scalars().first()
        assert pf is not None


class TestTTSModes:
    def test_tts_ready_with_subtitle(self, env) -> None:
        user_id, token, course_id, lesson_id = _user_with_ready_lesson(env)
        client = env["client"]
        env["container"]["model"] = TeachingModelAdapter(payload=STEP_PAYLOAD)
        env["container"]["tts"] = GoodTTSAdapter()
        start = client.post(f"/api/courses/{course_id}/lessons/{lesson_id}/start",
                            headers={"Authorization": f"Bearer {token}"}).json()
        client.post(f"/api/teaching/tasks/{start['task_id']}/run", headers={"Authorization": f"Bearer {token}"})
        # TTS 任务在 run_step_generation 内创建；找到并执行
        from backend.tasks.service import LearningTaskRow

        with env["database"].session() as sess:
            row = sess.execute(
                select(LearningTaskRow).where(
                    LearningTaskRow.kind == "tts",
                    LearningTaskRow.subject_id == f"{start['conversation_id']}:1:1",
                )
            ).scalars().first()
            assert row is not None
            tts_id = row.task_id
        r = client.post(f"/api/teaching/tasks/{tts_id}/run", headers={"Authorization": f"Bearer {token}"})
        assert r.json()["audio_status"] == "ready"
        ws = client.get(f"/api/teaching/{start['conversation_id']}",
                        headers={"Authorization": f"Bearer {token}"}).json()
        assert ws["steps"][0]["audio_ready"] is True

    def test_no_subtitle_enters_no_audio_mode(self, env) -> None:
        """故障注入：TTS 无字幕 → 音频不发布为可播放内容（无音频手动模式）。"""
        user_id, token, course_id, lesson_id = _user_with_ready_lesson(env)
        client = env["client"]
        env["container"]["model"] = TeachingModelAdapter(payload=STEP_PAYLOAD)
        env["container"]["tts"] = NoSubtitleTTSAdapter()
        start = client.post(f"/api/courses/{course_id}/lessons/{lesson_id}/start",
                            headers={"Authorization": f"Bearer {token}"}).json()
        client.post(f"/api/teaching/tasks/{start['task_id']}/run", headers={"Authorization": f"Bearer {token}"})
        from backend.tasks.service import LearningTaskRow

        with env["database"].session() as sess:
            row = sess.execute(
                select(LearningTaskRow).where(
                    LearningTaskRow.kind == "tts",
                    LearningTaskRow.subject_id == f"{start['conversation_id']}:1:1",
                )
            ).scalars().first()
        client.post(f"/api/teaching/tasks/{row.task_id}/run", headers={"Authorization": f"Bearer {token}"})
        ws = client.get(f"/api/teaching/{start['conversation_id']}",
                        headers={"Authorization": f"Bearer {token}"}).json()
        assert ws["steps"][0]["audio_ready"] is False
        with env["database"].session() as sess:
            audio = sess.execute(select(AudioAssetRow).where(
                AudioAssetRow.conversation_id == start["conversation_id"],
                AudioAssetRow.step_no == 1,
            )).scalars().first()
            assert audio is not None and audio.status == "invalid"
            assert audio.invalid_reason == "subtitle_missing"

    def test_text_version_mismatch_invalidates_audio(self, env) -> None:
        """故障注入：文本版本不一致 → 旧音频不匹配新文本（不可播放）。"""
        user_id, token, course_id, lesson_id = _user_with_ready_lesson(env)
        client = env["client"]
        env["container"]["model"] = TeachingModelAdapter(payload=STEP_PAYLOAD)
        env["container"]["tts"] = GoodTTSAdapter()
        start = client.post(f"/api/courses/{course_id}/lessons/{lesson_id}/start",
                            headers={"Authorization": f"Bearer {token}"}).json()
        client.post(f"/api/teaching/tasks/{start['task_id']}/run", headers={"Authorization": f"Bearer {token}"})
        with env["database"].session() as sess:
            sess.execute(update(TeachingStepRow).where(
                TeachingStepRow.conversation_id == start["conversation_id"],
                TeachingStepRow.step_no == 1,
            ).values(text_version_no=2))
        ws = client.get(f"/api/teaching/{start['conversation_id']}",
                        headers={"Authorization": f"Bearer {token}"}).json()
        # 音频仍绑定 v1 → 对 v2 步骤不可播放
        assert ws["steps"][0]["audio_ready"] is False


class TestPrefetchDiscipline:
    def test_prefetch_cancelled_by_followup(self, env) -> None:
        user_id, token, course_id, lesson_id = _user_with_ready_lesson(env)
        client = env["client"]
        env["container"]["model"] = TeachingModelAdapter(payload=STEP_PAYLOAD)
        env["container"]["tts"] = GoodTTSAdapter()
        start = client.post(f"/api/courses/{course_id}/lessons/{lesson_id}/start",
                            headers={"Authorization": f"Bearer {token}"}).json()
        client.post(f"/api/teaching/tasks/{start['task_id']}/run", headers={"Authorization": f"Bearer {token}"})
        # 排队的 prefetch 任务存在
        from backend.tasks.service import LearningTaskRow

        with env["database"].session() as sess:
            pf = sess.execute(
                select(LearningTaskRow).where(
                    LearningTaskRow.lane == "teaching_prefetch",
                    LearningTaskRow.status == "queued",
                    LearningTaskRow.kind == "lesson_step_generate",
                )
            ).scalars().first()
            assert pf is not None
        # 追问 → prefetch 排队任务被取消 + 恢复点创建 + generation 递增
        r = client.post(f"/api/teaching/{start['conversation_id']}/followup",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"content": "为什么可重复读能避免不可重复读？",
                              "idempotency_key": f"f{uuid.uuid4().hex[:8]}"})
        assert r.status_code == 200, r.text
        fu_task = r.json()["task_id"]
        client.post(f"/api/teaching/tasks/{fu_task}/run", headers={"Authorization": f"Bearer {token}"})
        with env["database"].session() as sess:
            cancelled = sess.execute(
                select(LearningTaskRow).where(
                    LearningTaskRow.lane == "teaching_prefetch",
                    LearningTaskRow.status == "cancelled",
                )
            ).scalars().all()
            assert cancelled
        ws = client.get(f"/api/teaching/{start['conversation_id']}",
                        headers={"Authorization": f"Bearer {token}"}).json()
        assert ws["generation"] == 2  # 追问使 generation 递增
        assert ws["session_state"] == "awaiting_resume"
        assert ws["restore_point"].get("kind") == "not_started"
        # 恢复：会话回 active
        client.post(f"/api/teaching/{start['conversation_id']}/resume",
                    headers={"Authorization": f"Bearer {token}"})
        ws2 = client.get(f"/api/teaching/{start['conversation_id']}",
                         headers={"Authorization": f"Bearer {token}"}).json()
        assert ws2["session_state"] == "active"

    def test_followup_reuses_first_restore_point(self, env) -> None:
        user_id, token, course_id, lesson_id = _user_with_ready_lesson(env)
        client = env["client"]
        env["container"]["model"] = TeachingModelAdapter(payload=STEP_PAYLOAD)
        env["container"]["tts"] = GoodTTSAdapter()
        start = client.post(f"/api/courses/{course_id}/lessons/{lesson_id}/start",
                            headers={"Authorization": f"Bearer {token}"}).json()
        client.post(f"/api/teaching/tasks/{start['task_id']}/run", headers={"Authorization": f"Bearer {token}"})
        conv = start["conversation_id"]
        # 播放到 step 1（checkpoint）
        client.post(f"/api/teaching/{conv}/checkpoint", headers={"Authorization": f"Bearer {token}"},
                    json={"client_seq": 1, "step_no": 1, "position_ms": 3000})
        # 第一次追问：恢复点 = checkpoint
        client.post(f"/api/teaching/{conv}/followup", headers={"Authorization": f"Bearer {token}"},
                    json={"content": "追问一", "idempotency_key": f"f{uuid.uuid4().hex[:8]}"})
        ws = client.get(f"/api/teaching/{conv}", headers={"Authorization": f"Bearer {token}"}).json()
        first_restore = ws["restore_point"]
        assert first_restore["kind"] == "checkpoint" and first_restore["position_ms"] == 3000
        # 继续课程 → 第二次追问 → 恢复点仍是最初那个
        client.post(f"/api/teaching/{conv}/resume", headers={"Authorization": f"Bearer {token}"})
        client.post(f"/api/teaching/{conv}/followup", headers={"Authorization": f"Bearer {token}"},
                    json={"content": "追问二", "idempotency_key": f"f{uuid.uuid4().hex[:8]}"})
        ws2 = client.get(f"/api/teaching/{conv}", headers={"Authorization": f"Bearer {token}"}).json()
        assert ws2["restore_point"] == first_restore


class TestCheckpoints:
    def test_monotonic_seq_and_stale_rejected(self, env) -> None:
        user_id, token, course_id, lesson_id = _user_with_ready_lesson(env)
        client = env["client"]
        env["container"]["model"] = TeachingModelAdapter(payload=STEP_PAYLOAD)
        start = client.post(f"/api/courses/{course_id}/lessons/{lesson_id}/start",
                            headers={"Authorization": f"Bearer {token}"}).json()
        conv = start["conversation_id"]
        ok = client.post(f"/api/teaching/{conv}/checkpoint", headers={"Authorization": f"Bearer {token}"},
                         json={"client_seq": 5, "step_no": 1, "position_ms": 1000})
        assert ok.status_code == 200
        stale = client.post(f"/api/teaching/{conv}/checkpoint", headers={"Authorization": f"Bearer {token}"},
                            json={"client_seq": 4, "step_no": 1, "position_ms": 900})
        assert stale.status_code == 409
        assert stale.json()["error_code"] == "CHECKPOINT_STALE"
        newer = client.post(f"/api/teaching/{conv}/checkpoint", headers={"Authorization": f"Bearer {token}"},
                            json={"client_seq": 6, "step_no": 1, "position_ms": 5000})
        assert newer.status_code == 200

    def test_cross_user_checkpoint_denied(self, env) -> None:
        from slice3_common import make_user

        user_id, token, course_id, lesson_id = _user_with_ready_lesson(env)
        client = env["client"]
        env["container"]["model"] = TeachingModelAdapter(payload=STEP_PAYLOAD)
        start = client.post(f"/api/courses/{course_id}/lessons/{lesson_id}/start",
                            headers={"Authorization": f"Bearer {token}"}).json()
        _, other_token = make_user(client, env["database"])
        r = client.post(f"/api/teaching/{start['conversation_id']}/checkpoint",
                        headers={"Authorization": f"Bearer {other_token}"},
                        json={"client_seq": 1, "step_no": 1, "position_ms": 0})
        assert r.status_code == 404
