"""切片 5 参考负载采集：两账户并发 × 固定样例各 30 次。

架构 §8 待核对 4：正常网络下首段可读文字 P95 ≤ 30 秒、音频可播放 P95 ≤ 60 秒；
统计口径为第 29 个最小值（sorted[28]）。

本脚本在本地以合成模型/TTS 适配器驱动完整服务端管线（任务创建、租约围栏、
生成、校验、落库、事件），测量的是**服务端管线开销**，不含真实模型/TTS
供应商延迟；正式云部署时以同一脚本在真实凭据下重跑并回填证据。

用法（仓库根）：
    LEARNING_DEV_SYNTHETIC_MODEL=1 uv run --project server/backend \
        python scripts/slice5_p95_harness.py --samples 30 --out /tmp/slice5-p95.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))
sys.path.insert(0, "tests")

from backend.logging_audit.db import get_database_url  # noqa: E402

os_environ = __import__("os").environ
os_environ.setdefault("LEARNING_DATABASE_URL", get_database_url())

from fastapi.testclient import TestClient  # noqa: E402

from backend.logging_audit.db import Database  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.logging_audit.repositories import PgAuditLog  # noqa: E402
from backend.slice3_app import Slice3State, create_slice3_app  # noqa: E402
from backend.slice0_app import Slice0State  # noqa: E402
from slice3_common import (  # noqa: E402
    OUTLINE_OK_PAYLOAD,
    ROUTE_OK_PAYLOAD,
    SyntheticModelAdapter,
    SyntheticSearchAdapter,
    TeachingModelAdapter,
    make_user,
)


def build_client(database: Database, container: dict) -> TestClient:
    state = Slice3State(
        database, PgAuditLog(database),
        model_adapter_factory=lambda o, t: container["model"],
        search_adapter_factory=lambda o, t: container["search"],
        tts_adapter_factory=lambda o, t: container["tts"],
        ocr_adapter_factory=lambda o, t: container["ocr"],
    )
    app = create_slice3_app(state=state)
    app.state.slice0 = Slice0State(database, PgAuditLog(database))
    return TestClient(app, raise_server_exceptions=False)


def p29(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[28] if len(ordered) >= 29 else ordered[-1]


def measure_first_text(client: TestClient, headers: dict, course_id: str,
                       lesson_provider, db: Database) -> float:
    """首段可读文字：开始学习 → 步骤生成 → 工作区出现 ready 文字。"""
    start_ts = time.monotonic()
    lesson_id = lesson_provider()
    start = client.post(f"/api/courses/{course_id}/lessons/{lesson_id}/start", headers=headers)
    assert start.status_code == 201, start.text
    start_body = start.json()
    conv_id = start_body["conversation_id"]
    client.post(f"/api/teaching/tasks/{start_body['task_id']}/run", headers=headers)
    ws = client.get(f"/api/teaching/{conv_id}", headers=headers).json()
    ready = [s for s in ws["steps"] if s["status"] == "ready"]
    assert ready, "first step must be ready"
    return time.monotonic() - start_ts


def measure_audio_playable(client: TestClient, headers: dict, course_id: str,
                           lesson_provider, db: Database) -> float:
    """音频可播放：开始学习 → 步骤生成 → TTS → 音频就绪。"""
    start_ts = time.monotonic()
    lesson_id = lesson_provider()
    start = client.post(f"/api/courses/{course_id}/lessons/{lesson_id}/start", headers=headers)
    assert start.status_code == 201, start.text
    start_body = start.json()
    conv_id = start_body["conversation_id"]
    client.post(f"/api/teaching/tasks/{start_body['task_id']}/run", headers=headers)
    from sqlalchemy import select

    from backend.tasks.service import LearningTaskRow

    with db.session() as sess:
        tts = sess.execute(
            select(LearningTaskRow).where(
                LearningTaskRow.kind == "tts",
                LearningTaskRow.subject_id == f"{conv_id}:1:1",
            )
        ).scalars().first()
    if tts is not None:
        client.post(f"/api/teaching/tasks/{tts.task_id}/run", headers=headers)
    ws = client.get(f"/api/teaching/{conv_id}", headers=headers).json()
    assert ws["steps"] and ws["steps"][0]["audio_ready"], "audio must be ready"
    return time.monotonic() - start_ts




GOOD_DECLARATION_S4 = {
    "kind": "board", "title": "核心概念速览",
    "points": ["定义", "例子", "误用"],
    "code": None, "formula": None,
    "nodes": [{"id": "n1", "label": "事务 A"}, {"id": "n2", "label": "事务 B"}],
    "edges": [{"from_id": "n1", "to_id": "n2", "label": "冲突"}],
    "original_refs": [], "reveal_ms": [0, 4000, 8000],
}
TEACHING_STEP_PAYLOAD = {
    "steps": [{
        "title": "合成教学步骤",
        "text": "本步骤讲解当前讲次的核心概念：先给出定义，再用一个具体例子说明它在真实系统里如何出现，"
                "最后指出两个最常见的误用与判别方法，帮助你把概念落到可操作的判断上。整段朗读约四十秒。",
        "board": GOOD_DECLARATION_S4,
    }],
}
FOLLOWUP_PAYLOAD = {"answer": "这是追问的合成回答，按定义、例子、误用三段展开。", "board": None}


class RealBridgedAdapter(SyntheticModelAdapter):
    """把固定 payload 作为用户追问发往真实模型，验证真实供应商往返延迟。

    结构化契约仍由切片冻结的输出模型约束；本适配器取回模型文本后
    提取 JSON（容忍围栏），只保留往返耗时语义（与真实管线同型）。
    """

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        import sys as _sys
        from app.slice1.adapters.openai import OpenAIAdapter

        self._real = OpenAIAdapter(
            endpoint=os.environ["LEARNING_REAL_MODEL_ENDPOINT"],
            model_name=os.environ.get("LEARNING_REAL_MODEL_NAME", ""),
            credentials={"api_key": os.environ["LEARNING_REAL_MODEL_KEY"]},
        )

    def probe(self, capability: str):
        from types import SimpleNamespace

        return SimpleNamespace(capability=capability, state="available")

    def generate(self, messages, **options):
        import json as _json

        self.generate_calls.append(options)
        last = ""
        for m in messages:
            if m.get("role") == "user":
                last = str(m.get("content", ""))
        payload = (FOLLOWUP_PAYLOAD if "follow-up question" in last
                   else TEACHING_STEP_PAYLOAD if "ONE short teaching step" in last
                   else OUTLINE_OK_PAYLOAD if ("outline" in last or "大纲" in last)
                   else ROUTE_OK_PAYLOAD)
        prompt = ("请严格原样输出以下 JSON，不要任何其他文字：\n"
                  + _json.dumps(payload, ensure_ascii=False))
        raw = self._real.generate(
            [{"role": "user", "content": prompt}], max_tokens=4096)
        content = raw.get("content", "") if isinstance(raw, dict) else str(raw or "")
        # 容忍 Markdown 围栏 / 前后噪声：提取首个 JSON 对象
        import re as _re

        match = _re.search(r"\{.*\}", content, _re.DOTALL)
        parsed = None
        if match:
            try:
                parsed = _json.loads(match.group(0))
            except ValueError:
                parsed = None
        return {"content": _json.dumps(parsed, ensure_ascii=False) if parsed else content}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--out", default="slice5-p95.json")
    parser.add_argument("--real", action="store_true",
                        help="use real model/tts adapters from LEARNING_REAL_* env")
    args = parser.parse_args()

    real_mode = "--real" in sys.argv
    container = {"model": None, "search": None, "tts": None, "ocr": None}
    container["search"] = SyntheticSearchAdapter()
    if real_mode:
        from app.slice1.adapters.minimax import MiniMaxTTSAdapter
        from app.slice1.adapters.openai import OpenAIAdapter

    if real_mode:
        from app.slice1.adapters.minimax import MiniMaxTTSAdapter

        container["tts"] = MiniMaxTTSAdapter(
            endpoint=os.environ.get("LEARNING_REAL_TTS_ENDPOINT", "https://api.minimax.cn"),
            model_name=os.environ.get("LEARNING_REAL_TTS_MODEL", "speech-2.8-hd"),
            credentials={"api_key": os.environ["LEARNING_REAL_TTS_KEY"]},
            voice_id=os.environ.get("LEARNING_REAL_TTS_VOICE", "male-qn-qingse"),
        )

    class HarnessTTS:
        """合成 TTS：hex 音频 + 句级字幕（可播放）。"""

        kind = "tts"
        protocol = "minimax"

        def synthesize(self, text, **kwargs):
            import hashlib as _h

            return {
                "model": "speech-2.8-hd", "voice_id": "p95",
                "audio_format": "mp3", "subtitle_enable": True,
                "subtitle_type": "sentence",
                "audio_hex": (b"ID3" + text.encode()).hex(),
                "subtitle": [{"word": "seg", "begin_ms": 0, "end_ms": 400}],
                "text_sha256": _h.sha256(text.encode()).hexdigest(),
                "vendor_trace_id": "p95",
            }

    container["tts"] = HarnessTTS()
    # 阶段切换：路线/大纲准备阶段用规划样例；教学测量阶段切教学样例
    container["model"] = SyntheticModelAdapter(payload=ROUTE_OK_PAYLOAD)

    database = Database()
    database.create_all()
    install_audit_triggers(database.engine)

    client_a = build_client(database, container)
    client_b = build_client(database, container)
    user_a, token_a = make_user(client_a, database)
    user_b, token_b = make_user(client_b, database)
    HA = {"Authorization": f"Bearer {token_a}"}
    HB = {"Authorization": f"Bearer {token_b}"}

    # 准备阶段 1：两账户各生成并确认路线
    route_ids: dict[str, str] = {}
    for client, headers, uid, tag in ((client_a, HA, user_a, "a"), (client_b, HB, user_b, "b")):
        container["model"] = (RealBridgedAdapter(payload=ROUTE_OK_PAYLOAD)
                              if real_mode else SyntheticModelAdapter(payload=ROUTE_OK_PAYLOAD))
        route_id = client.post("/api/planning/routes", headers=headers,
                               json={"title": f"P95 路线 {tag}", "scenario": "self_study"}).json()["route_id"]
        t = client.post("/api/planning/tasks", headers=headers,
                        json={"idempotency_key": f"p95-{uuid.uuid4().hex[:8]}", "task_kind": "route_generate",
                              "planning_scope": "route", "target_route_id": route_id}).json()["task_id"]
        body = client.post(f"/api/planning/tasks/{t}/run", headers=headers).json()
        assert body.get("draft"), body
        draft = client.app.state.slice3.drafts.get_draft(body["draft"]["draft_id"], actor_id=uid)
        client.post(f"/api/planning/drafts/{body['draft']['draft_id']}/confirm", headers=headers,
                    json={"revision": draft["revision"], "idempotency_key": f"c{uuid.uuid4().hex[:8]}"})
        route_ids[uid] = route_id

    # 准备阶段 2：由路线展开大纲并确认
    course_ids: dict[str, str] = {}
    for client, headers, uid in ((client_a, HA, user_a), (client_b, HB, user_b)):
        container["model"] = (RealBridgedAdapter(payload=OUTLINE_OK_PAYLOAD)
                              if real_mode else SyntheticModelAdapter(payload=OUTLINE_OK_PAYLOAD))
        route_id = route_ids[uid]
        overview = client.get(f"/api/routes/{route_id}/overview", headers=headers).json()
        course_id = overview["stages"][0]["courses"][0]["course_id"]
        outline_task = client.post("/api/planning/tasks", headers=headers,
                                   json={"idempotency_key": f"p95-{uuid.uuid4().hex[:8]}",
                                         "task_kind": "outline_generate", "planning_scope": "course",
                                         "target_course_id": course_id}).json()
        obody = client.post(f"/api/planning/tasks/{outline_task['task_id']}/run", headers=headers).json()
        assert obody.get("draft"), obody
        odraft = client.app.state.slice3.drafts.get_draft(obody["draft"]["draft_id"], actor_id=uid)
        oconf = client.post(f"/api/planning/drafts/{obody['draft']['draft_id']}/confirm", headers=headers,
                            json={"revision": odraft["revision"], "idempotency_key": f"c{uuid.uuid4().hex[:8]}"})
        assert oconf.status_code == 200, oconf.text
        cov = client.get(f"/api/courses/{course_id}/overview", headers=headers).json()
        assert cov["outline_confirmed"], "P95 课程需要已确认大纲"
        course_ids[uid] = course_id

    # 教学测量阶段：切换到教学样例适配器（含 TTS）；--real 用真实桥接
    container["model"] = (RealBridgedAdapter(payload=OUTLINE_OK_PAYLOAD)
                          if real_mode else TeachingModelAdapter(payload=OUTLINE_OK_PAYLOAD))
    # 讲次池：每账户重复使用同一课程的多个讲次（ready 化），避免讲次耗尽
    from backend.planning.models import LessonRow

    def ready_lessons_for(uid: str) -> list[str]:
        with database.session() as sess:
            rows = sess.execute(
                select(LessonRow.lesson_id)
                .where(LessonRow.status_preparation == "ready")
                .limit(args.samples * 2)
            ).scalars().all()
        return list(rows)

    # 所有 ready 讲次平分给两个账户的样例
    def lesson_pool(client: TestClient, headers: dict, uid: str) -> list[str]:
        cov = client.get(f"/api/courses/{course_ids[uid]}/overview", headers=headers).json()
        lessons = [l["lesson_id"] for u in cov["units"] for l in u["lessons"]]
        return lessons

    pool_a = lesson_pool(client_a, HA, user_a)
    pool_b = lesson_pool(client_b, HB, user_b)
    # ready 化（与走查一致：P95 关注管线耗时，讲次内容占位由样例说明）
    with database.session() as sess:
        sess.execute(__import__("sqlalchemy").update(LessonRow).values(status_preparation="ready"))
    n = max(len(pool_a), len(pool_b), 1)
    samples = args.samples

    import sqlalchemy as sa  # 局部：讲次重置用

    sa_update = sa.update
    text_times: list[float] = []
    audio_times: list[float] = []

    def run_pair(i: int):
        la = pool_a[i % len(pool_a)]
        lb = pool_b[i % len(pool_b)]
        t0 = time.monotonic()
        ta = time.monotonic()
        ta_done = measure_first_text(client_a, HA, course_ids[user_a], lambda: la)
        tb_done = time.monotonic()
        return (ta_done - ta, None)

    # 并发：两账户同时执行，各 30 次（文字指标 + 音频指标分别采集）
    def reset_lesson(lesson_id: str) -> None:
        with database.session() as sess:
            sess.execute(
                sa_update(LessonRow).where(LessonRow.lesson_id == lesson_id)
                .values(status_preparation="ready")
            )

    def run_text(idx: int, client: TestClient, headers: dict, uid: str, pool: list[str]):
        lesson = pool[idx % len(pool)]
        try:
            return measure_first_text(client, headers, course_ids[uid], lambda: lesson, database)
        finally:
            reset_lesson(lesson)

    def run_audio(idx: int, client: TestClient, headers: dict, uid: str, pool: list[str]):
        lesson = pool[idx % len(pool)]
        try:
            return measure_audio_playable(client, headers, course_ids[uid], lambda: lesson, database)
        finally:
            reset_lesson(lesson)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(run_text, i, client_a if i % 2 == 0 else client_b,
                        HA if i % 2 == 0 else HB, user_a if i % 2 == 0 else user_b,
                        pool_a if i % 2 == 0 else pool_b)
            for i in range(samples)
        ]
        text_times = [f.result() for f in futures]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(run_audio, i, client_a if i % 2 == 0 else client_b,
                        HA if i % 2 == 0 else HB, user_a if i % 2 == 0 else user_b,
                        pool_a if i % 2 == 0 else pool_b)
            for i in range(samples)
        ]
        audio_times = [f.result() for f in futures]

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "samples": samples,
        "accounts": 2,
        "note": "本地合成适配器：测得服务端管线开销；正式部署需以真实凭据重跑",
        "first_text_seconds": {
            "p29_min": round(p29(text_times), 3),
            "median": round(statistics.median(text_times), 3),
            "max": round(max(text_times), 3),
            "threshold": 30.0,
            "pass": p29(text_times) <= 30.0,
        },
        "audio_playable_seconds": {
            "p29_min": round(p29(audio_times), 3),
            "median": round(statistics.median(audio_times), 3),
            "max": round(max(audio_times), 3),
            "threshold": 60.0,
            "pass": p29(audio_times) <= 60.0,
        },
        "raw": {"text": [round(t, 3) for t in text_times],
                "audio": [round(t, 3) for t in audio_times]},
    }
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "raw"}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
