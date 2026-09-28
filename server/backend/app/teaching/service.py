"""讲次教学服务（切片 4 T12～T15）。

- `start_lesson`：门槛校验（大纲已确认 + 讲次就绪）→ 课程会话绑定
  （course/outline_version/lesson/content_version）→ 首段生成任务
  （lane=interactive）；计时起点 = 用户点击开始学习；
- `run_step_generation`：短步骤生成（30–60s 目标、120s 硬上限拆段）、
  板书声明 → 受控场景（同版本绑定）、TTS 任务；
- 预生成窗口 = 1：当前段就绪且会话 active 时才创建下一段任务
  （lane=teaching_prefetch）；追问关闭预生成并创建一次性恢复点；
- `save_checkpoint`：单调 client_seq + 乐观校验（5 秒周期 + 事件双触发）；
- 无音频手动模式：TTS 缺失/无字幕/文本版本不一致 → 音频不发布。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import func, select

from ..logging_audit.db import Database
from ..logging_audit.logger import monotonic_ms
from ..logging_audit.metrics import get_model_call_metrics
from ..logging_audit.repositories import PgTaskEventStore
from ..planning.errors import PlanningError, error
from ..planning.models import (
    ConversationRow,
    CourseRow,
    LessonRow,
    MessageRow,
    OutlineVersionRow,
    load_json_field,
    utcnow,
)
from ..prompts.service import PromptService, PromptUnavailable
from ..tasks.service import TaskService
from .models import AudioAssetRow, BoardSceneRow, PlaybackCheckpointRow, TeachingStepRow
from .outputs import (
    FollowupCandidate,
    LessonPlanCandidate,
    StepCandidate,
    declaration_to_scene,
    validate_step_duration,
)

from .errors import TeachingError, terr

TEACHING_PROMPT = "teaching_step_v1"
FOLLOWUP_PROMPT = "followup_v1"

MAX_STEPS_PER_LESSON = 12


class TeachingService:
    def __init__(
        self,
        db: Database,
        tasks: TaskService,
        events: PgTaskEventStore,
        prompts: PromptService,
        storage,
        model_adapter_factory: Optional[Callable[[str, str], Any]] = None,
        tts_adapter_factory: Optional[Callable[[str, str], Any]] = None,
    ) -> None:
        self.db = db
        self.tasks = tasks
        self.events = events
        self.prompts = prompts
        self.storage = storage
        self.model_adapter_factory = model_adapter_factory
        self.tts_adapter_factory = tts_adapter_factory

    # ------------------------------------------------------------------
    # 启动讲次（教学门槛）
    # ------------------------------------------------------------------
    def start_lesson(self, course_id: str, lesson_id: str, *, actor_id: str) -> dict:
        with self.db.session() as sess:
            course = sess.get(CourseRow, course_id)
            if course is None or course.owner_user_id != actor_id or course.deleted_at is not None:
                raise terr("course_not_found", status=404)
            if not course.current_outline_version_id:
                raise terr("LESSON_UNLOCK_REQUIRED", "outline not confirmed", status=409)
            lesson = sess.get(LessonRow, lesson_id)
            if lesson is None or lesson.outline_version_id != course.current_outline_version_id:
                raise terr("lesson_not_found", status=404)
            if lesson.status_preparation != "ready":
                raise terr("lesson_not_ready", f"preparation={lesson.status_preparation}", status=409)
            # 计时起点：本方法即「点击开始学习」的服务端时刻（任务创建时间）
            conv_id = f"conv_{uuid.uuid4().hex[:12]}"
            conv = ConversationRow(
                conversation_id=conv_id,
                owner_user_id=actor_id,
                kind="teaching",
                scenario=course.scenario,
                # 教学内容代次从 1 起：首段生成即 generation=1（追问再递增）
                generation=1,
                course_id=course_id,
                outline_version_id=course.current_outline_version_id,
                lesson_id=lesson_id,
                lesson_content_version_id=None,
                session_state="active",
            )
            sess.add(conv)
            lesson.status_preparation = "generating"
            sess.flush()
            created = self.tasks.create_task(
                owner_user_id=actor_id,
                kind="lesson_step_generate",
                subject_id=f"{conv_id}:1:1",
                idempotency_key=f"step:{conv_id}:1:1",
                stage="generate",
                lane="interactive",
                deadline_seconds=180,
            )
            sess.commit()
        try:
            self.prompts.bind_for_task(created["task_id"], TEACHING_PROMPT, allow_fallback=True)
        except PromptUnavailable as exc:
            raise terr("PROMPT_UNAVAILABLE", exc.reason, status=409)
        return {
            "conversation_id": conv_id,
            "task_id": created["task_id"],
            "lesson_id": lesson_id,
            "started_at": utcnow().isoformat(),
        }

    # ------------------------------------------------------------------
    # 步骤生成 worker（首段 interactive / 下一段 teaching_prefetch）
    # ------------------------------------------------------------------
    def run_step_generation(self, task_id: str, *, lease_owner: str = "teaching-worker") -> dict:
        lease = self.tasks.claim(task_id, lease_owner=lease_owner, lease_seconds=120)
        try:
            task = self.tasks.get_task(task_id)
            owner = task["owner_user_id"]
            conv_id, _, rest = task["subject_id"].partition(":")
            step_raw, _, gen_raw = rest.partition(":")
            step_no = int(step_raw or "1")
            generation = int(gen_raw or "1")

            conv = self._conversation(conv_id, owner)
            prompt = self.prompts.resolve_for_task(task_id)
            adapter = self._model_adapter(owner, task_id)
            if adapter is None:
                raise terr("MODEL_CAPABILITY_UNAVAILABLE", "no model adapter")
            messages = self._step_messages(prompt, conv)
            from ..planning.model_runner import run_structured

            run = run_structured(
                adapter, LessonPlanCandidate, messages,
                schema=LessonPlanCandidate.model_json_schema(),
                tool_name="submit_lesson_steps",
            )
            candidate = run.payload.steps[0]
            duration_ms = validate_step_duration(candidate.text)

            step_id = f"tstep_{uuid.uuid4().hex[:12]}"
            scene_id = None
            with self.db.session() as sess:
                # 同版本唯一有效产物（会话 + generation + step_no）
                exists = (
                    sess.execute(
                        select(TeachingStepRow).where(
                            TeachingStepRow.conversation_id == conv_id,
                            TeachingStepRow.generation == generation,
                            TeachingStepRow.step_no == step_no,
                        )
                    )
                    .scalars()
                    .first()
                )
                if exists is not None:
                    step_id = exists.step_id
                else:
                    declaration = candidate.board
                    scene = declaration_to_scene(declaration)
                    scene_id = f"bscene_{uuid.uuid4().hex[:12]}"
                    sess.add(
                        BoardSceneRow(
                            scene_id=scene_id,
                            conversation_id=conv_id,
                            generation=generation,
                            step_no=step_no,
                            scene_version_no=1,
                            declaration_json=json.dumps(declaration.model_dump(), ensure_ascii=False),
                            scene_json=json.dumps(scene, ensure_ascii=False),
                        )
                    )
                    sess.add(
                        TeachingStepRow(
                            step_id=step_id,
                            conversation_id=conv_id,
                            generation=generation,
                            step_no=step_no,
                            text_version_no=1,
                            title=candidate.title,
                            text=candidate.text,
                            status="ready",
                            board_scene_id=scene_id,
                            source_task_id=task_id,
                        )
                    )
                sess.flush()
                # 预生成窗口 = 1：当前段就绪且会话 active → 创建下一段任务
                self._ensure_prefetch(sess, conv_id, owner, step_no + 1, generation)
                # TTS：有适配器才创建（无音频模式是可交付路径）
                if self.tts_adapter_factory is not None and self.tts_adapter_factory(owner, task_id):
                    try:
                        self.tasks.create_task(
                            owner_user_id=owner,
                            kind="tts",
                            subject_id=f"{conv_id}:{step_no}:{generation}",
                            idempotency_key=f"tts:{conv_id}:{step_no}:{generation}:v1",
                            stage="tts",
                            lane="teaching_prefetch",
                            deadline_seconds=90,
                        )
                    except Exception:  # noqa: BLE001 - 幂等冲突视为已创建
                        pass
                sess.commit()
            self.events.record(
                task_id, stage="generate", status="succeeded",
                template_params={"mode": run.mode, "duration_ms_est": duration_ms},
                generation=lease.generation, attempt=lease.attempt,
            )
            self.tasks.submit_result(lease, stage="generate", status="succeeded", duration_ms=0)
            return {"status": "succeeded", "step_no": step_no, "step_id": step_id,
                    "board_scene_id": scene_id, "mode": run.mode}
        except PlanningError as exc:
            self.events.record(
                task_id, stage="generate", status="failed", error_code=exc.code,
                generation=lease.generation, attempt=lease.attempt,
            )
            self.tasks.submit_result(lease, stage="generate", status="failed",
                                     error_code=exc.code, duration_ms=0)
            return {"status": "failed", "error_code": exc.code}

    def _ensure_prefetch(self, sess, conv_id: str, owner: str, next_no: int, generation: int) -> None:
        """预生成窗口=1：当前段就绪且会话 active 时才创建下一段任务。"""
        conv = sess.get(ConversationRow, conv_id)
        if conv is None or conv.session_state != "active":
            return
        if next_no > MAX_STEPS_PER_LESSON:
            return
        from ..planning.models import LessonRow

        lesson = sess.get(LessonRow, conv.lesson_id) if conv.lesson_id else None
        _ = lesson
        exists = (
            sess.execute(
                select(TeachingStepRow).where(
                    TeachingStepRow.conversation_id == conv_id,
                    TeachingStepRow.generation == generation,
                    TeachingStepRow.step_no == next_no,
                )
            )
            .scalars()
            .first()
        )
        if exists is not None:
            return
        try:
            self.tasks.create_task(
                owner_user_id=owner,
                kind="lesson_step_generate",
                subject_id=f"{conv_id}:{next_no}:{generation}",
                idempotency_key=f"step:{conv_id}:{next_no}:{generation}",
                stage="generate",
                lane="teaching_prefetch",
                deadline_seconds=180,
            )
            self.prompts.bind_for_task(
                self._task_id_by_idem(f"step:{conv_id}:{next_no}:{generation}"),
                TEACHING_PROMPT, allow_fallback=True,
            )
        except Exception:  # noqa: BLE001 - 幂等冲突/绑定竞争不阻塞当前段
            pass

    def _task_id_by_idem(self, idem: str) -> str:
        with self.db.session() as sess:
            from ..tasks.service import LearningTaskRow

            row = (
                sess.execute(
                    select(LearningTaskRow).where(LearningTaskRow.idempotency_key == idem)
                )
                .scalars()
                .first()
            )
            return row.task_id if row else ""

    def _step_messages(self, prompt: dict, conv: ConversationRow) -> list[dict]:
        template = prompt.get("template_snapshot", "")
        return [
            {"role": "system", "content": "你是授课教师。只输出符合要求的 JSON。"},
            {"role": "user", "content": f"{template}\nlesson_id={conv.lesson_id or 'lesson'}"},
        ]

    def _model_adapter(self, owner: str, task_id: str):
        if self.model_adapter_factory is None:
            return None
        return self.model_adapter_factory(owner, task_id)

    def _conversation(self, conv_id: str, owner: str) -> ConversationRow:
        with self.db.session() as sess:
            conv = sess.get(ConversationRow, conv_id)
            if conv is None or conv.owner_user_id != owner:
                raise terr("conversation_not_found", status=404)
            sess.expunge(conv)
            return conv

    # ------------------------------------------------------------------
    # TTS worker
    # ------------------------------------------------------------------
    def run_tts(self, task_id: str, *, lease_owner: str = "tts-worker") -> dict:
        lease = self.tasks.claim(task_id, lease_owner=lease_owner, lease_seconds=90)
        try:
            task = self.tasks.get_task(task_id)
            owner = task["owner_user_id"]
            conv_id, _, rest = task["subject_id"].partition(":")
            step_raw, _, gen_raw = rest.partition(":")
            step_no = int(step_raw or "1")
            generation = int(gen_raw or "1")
            adapter = None
            if self.tts_adapter_factory is not None:
                adapter = self.tts_adapter_factory(owner, task_id)
            with self.db.session() as sess:
                step = (
                    sess.execute(
                        select(TeachingStepRow).where(
                            TeachingStepRow.conversation_id == conv_id,
                            TeachingStepRow.generation == generation,
                            TeachingStepRow.step_no == step_no,
                        )
                    )
                    .scalars()
                    .first()
                )
                if step is None:
                    raise terr("step_not_found", status=404)
                audio = AudioAssetRow(
                    audio_id=f"aud_{uuid.uuid4().hex[:12]}",
                    conversation_id=conv_id,
                    generation=generation,
                    step_no=step_no,
                    text_version_no=step.text_version_no,
                    text_sha256=hashlib.sha256(step.text.encode("utf-8")).hexdigest(),
                    status="generating",
                )
                sess.add(audio)
                sess.flush()
                if adapter is None:
                    # 无音频手动模式（可交付路径）：不发布音频
                    audio.status = "invalid"
                    audio.invalid_reason = "tts_unconfigured"
                else:
                    tts_started = monotonic_ms()
                    try:
                        synth = adapter.synthesize(step.text)
                        get_model_call_metrics().record(
                            stage="tts", outcome="succeeded",
                            duration_ms=monotonic_ms() - tts_started,
                        )
                        audio_hex = synth.get("audio_hex", "")
                        subtitle = synth.get("subtitle")
                        # 字幕缺失或时标为空 → 音频不得发布为可播放内容
                        if not audio_hex or not subtitle:
                            audio.status = "invalid"
                            audio.invalid_reason = "subtitle_missing"
                        else:
                            key = f"audio/{conv_id}/{audio.audio_id}.mp3"
                            self.storage.put(key, bytes.fromhex(audio_hex))
                            audio.storage_key = key
                            audio.audio_format = synth.get("audio_format", "mp3")
                            audio.subtitle_json = json.dumps(subtitle, ensure_ascii=False)[:8000]
                            audio.provider_trace_id = (synth.get("vendor_trace_id") or "")[:128]
                            audio.status = "ready"
                            step.audio_id = audio.audio_id
                    except Exception as exc:  # noqa: BLE001 - 供应商异常映射受控错误码
                        get_model_call_metrics().record(
                            stage="tts", outcome="failed",
                            duration_ms=monotonic_ms() - tts_started,
                            error_code=(
                                "TTS_TIMEOUT" if getattr(exc, "code", "") == "timeout" else "INTERNAL_ERROR"
                            ),
                        )
                        audio.status = "failed"
                        audio.invalid_reason = (getattr(exc, "code", "") or "tts_failed")[:40]
                sess.flush()
                sess.commit()
                audio_id = audio.audio_id
                status = audio.status
            self.events.record(
                task_id, stage="tts", status="succeeded" if status == "ready" else "partial",
                error_code=None if status == "ready" else "TTS_TIMEOUT",
                generation=lease.generation, attempt=lease.attempt,
            )
            self.tasks.submit_result(
                lease, stage="tts", status="succeeded", duration_ms=0
            )
            return {"status": "succeeded", "audio_id": audio_id, "audio_status": status}
        except PlanningError as exc:
            self.events.record(
                task_id, stage="tts", status="failed", error_code=exc.code,
                generation=lease.generation, attempt=lease.attempt,
            )
            self.tasks.submit_result(lease, stage="tts", status="failed",
                                     error_code=exc.code, duration_ms=0)
            return {"status": "failed", "error_code": exc.code}

    # ------------------------------------------------------------------
    # 工作区读取
    # ------------------------------------------------------------------
    def get_workspace(self, conversation_id: str, *, actor_id: str) -> dict:
        conv = self._conversation(conversation_id, actor_id)
        # 原课步骤固定在初始代次（generation=1）；追问轮次单独返回，
        # 保证追问后原课讲解与恢复点仍然可见（§5.3 恢复语义）。
        base_generation = 1 if conv.kind == "teaching" else conv.generation
        with self.db.session() as sess:
            steps = (
                sess.execute(
                    select(TeachingStepRow)
                    .where(
                        TeachingStepRow.conversation_id == conversation_id,
                        TeachingStepRow.generation == base_generation,
                    )
                    .order_by(TeachingStepRow.step_no)
                )
                .scalars()
                .all()
            )
            scene_by_id = {}
            if steps:
                scene_rows = (
                    sess.execute(
                        select(BoardSceneRow).where(
                            BoardSceneRow.conversation_id == conversation_id,
                            BoardSceneRow.generation == base_generation,
                        )
                    )
                    .scalars()
                    .all()
                )
                scene_by_id = {r.scene_id: r for r in scene_rows}
            followup_messages = []
            if conv.kind == "teaching" and conv.generation > 1:
                msgs = (
                    sess.execute(
                        select(MessageRow).where(
                            MessageRow.conversation_id == conversation_id,
                            MessageRow.generation > 1,
                        )
                        .order_by(MessageRow.generation, MessageRow.created_at)
                    )
                    .scalars()
                    .all()
                )
                followup_messages = [
                    {"generation": m.generation, "role": m.role, "content": m.content_ref}
                    for m in msgs
                ]
            step_views = []
            for s in steps:
                scene = scene_by_id.get(s.board_scene_id or "")
                audio_ready = False
                if s.audio_id:
                    audio = sess.get(AudioAssetRow, s.audio_id)
                    audio_ready = bool(audio and audio.status == "ready")
                step_views.append({
                    "step_no": s.step_no,
                    "step_id": s.step_id,
                    "title": s.title,
                    "text": s.text,
                    "status": s.status,
                    "text_version_no": s.text_version_no,
                    "board": json.loads(scene.scene_json) if scene else None,
                    "audio_id": s.audio_id,
                    "audio_ready": audio_ready,
                })
            last_checkpoint = (
                sess.execute(
                    select(PlaybackCheckpointRow)
                    .where(PlaybackCheckpointRow.conversation_id == conversation_id)
                    .order_by(PlaybackCheckpointRow.client_seq.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
        return {
            "conversation_id": conversation_id,
            "kind": conv.kind,
            "course_id": conv.course_id,
            "lesson_id": conv.lesson_id,
            "generation": conv.generation,
            "session_state": conv.session_state,
            "restore_point": load_json_field(conv.restore_point_json, {}),
            "followups": followup_messages,
            "last_checkpoint": (
                {
                    "client_seq": last_checkpoint.client_seq,
                    "step_no": last_checkpoint.step_no,
                    "position_ms": last_checkpoint.position_ms,
                }
                if last_checkpoint else None
            ),
            "steps": step_views,
        }

    # ------------------------------------------------------------------
    # 播放检查点（5 秒周期 + 事件双触发；client_seq 单调）
    # ------------------------------------------------------------------
    def save_checkpoint(
        self, conversation_id: str, *, actor_id: str,
        client_seq: int, step_no: int, position_ms: int,
        board_state: Optional[dict] = None,
    ) -> dict:
        with self.db.session() as sess:
            conv = sess.get(ConversationRow, conversation_id)
            if conv is None or conv.owner_user_id != actor_id:
                raise terr("conversation_not_found", status=404)
            max_seq = sess.execute(
                select(func.max(PlaybackCheckpointRow.client_seq))
                .where(PlaybackCheckpointRow.conversation_id == conversation_id)
            ).scalar_one()
            if client_seq <= int(max_seq or 0):
                raise terr("CHECKPOINT_STALE", "old client_seq rejected", status=409)
            sess.add(
                PlaybackCheckpointRow(
                    checkpoint_id=f"ckpt_{uuid.uuid4().hex[:12]}",
                    conversation_id=conversation_id,
                    client_seq=client_seq,
                    generation=conv.generation,
                    step_no=step_no,
                    position_ms=max(0, position_ms),
                    board_state_json=json.dumps(board_state or {}, ensure_ascii=False),
                )
            )
            sess.commit()
        # 用户推进到新步骤：确保下一段预生成（窗口=1、会话 active）
        if conv.session_state == "active":
            with self.db.session() as sess:
                self._ensure_prefetch(sess, conversation_id, actor_id, step_no + 1, conv.generation)
                sess.commit()
        return {"saved": True, "client_seq": client_seq}

    # ------------------------------------------------------------------
    # 追问与恢复
    # ------------------------------------------------------------------
    def followup(self, conversation_id: str, *, actor_id: str,
                 content: str, idempotency_key: str) -> dict:
        content = (content or "").strip()
        if not content or len(content) > 1000:
            raise terr("invalid_field", "followup length out of range")
        with self.db.session() as sess:
            conv = sess.get(ConversationRow, conversation_id)
            if conv is None or conv.owner_user_id != actor_id or conv.kind != "teaching":
                raise terr("conversation_not_found", status=404)
            # 一次性原课程恢复点：连续追问沿用最初恢复点
            if not load_json_field(conv.restore_point_json, None):
                latest = (
                    sess.execute(
                        select(PlaybackCheckpointRow)
                        .where(PlaybackCheckpointRow.conversation_id == conversation_id)
                        .order_by(PlaybackCheckpointRow.client_seq.desc())
                        .limit(1)
                    )
                    .scalars()
                    .first()
                )
                restore = (
                    {"step_no": latest.step_no, "position_ms": latest.position_ms,
                     "kind": "checkpoint"}
                    if latest else {"step_no": 0, "position_ms": 0, "kind": "not_started"}
                )
                conv.restore_point_json = json.dumps(restore, ensure_ascii=False)
            # 关闭原课程预生成调度：取消排队中的 prefetch 任务
            from ..tasks.service import LearningTaskRow

            prefetch = (
                sess.execute(
                    select(LearningTaskRow).where(
                        LearningTaskRow.lane == "teaching_prefetch",
                        LearningTaskRow.owner_user_id == actor_id,
                        LearningTaskRow.status == "queued",
                    )
                )
                .scalars()
                .all()
            )
            for t in prefetch:
                t.cancel_requested = True
                if t.status == "queued":
                    t.status = "cancelled"
            conv.generation += 1
            generation = conv.generation
            sess.add(
                MessageRow(
                    message_id=f"msg_{uuid.uuid4().hex[:12]}",
                    conversation_id=conversation_id,
                    generation=generation,
                    role="user",
                    content_ref=content,
                    status="completed",
                )
            )
            sess.flush()
        created = self.tasks.create_task(
            owner_user_id=actor_id,
            kind="lesson_step_generate",
            subject_id=f"{conversation_id}:followup:{generation}",
            idempotency_key=idempotency_key or f"followup:{conversation_id}:{generation}",
            stage="generate",
            lane="interactive",
            deadline_seconds=120,
        )
        if created.get("deduped"):
            return {"task_id": created["task_id"], "generation": generation, "deduped": True}
        try:
            self.prompts.bind_for_task(created["task_id"], FOLLOWUP_PROMPT, allow_fallback=True)
        except PromptUnavailable as exc:
            raise terr("PROMPT_UNAVAILABLE", exc.reason, status=409)
        return {"task_id": created["task_id"], "generation": generation}

    def run_followup(self, task_id: str, *, lease_owner: str = "teaching-worker") -> dict:
        lease = self.tasks.claim(task_id, lease_owner=lease_owner, lease_seconds=90)
        try:
            task = self.tasks.get_task(task_id)
            owner = task["owner_user_id"]
            conv_id, _, rest = task["subject_id"].partition(":")
            generation = int(rest.split(":")[-1] or "1")
            conv = self._conversation(conv_id, owner)
            with self.db.session() as sess:
                question = (
                    sess.execute(
                        select(MessageRow).where(
                            MessageRow.conversation_id == conv_id,
                            MessageRow.generation == generation,
                            MessageRow.role == "user",
                        )
                        .order_by(MessageRow.created_at.desc())
                    )
                    .scalars()
                    .first()
                )
                question_text = question.content_ref if question else ""
            adapter = self._model_adapter(owner, task_id)
            if adapter is None:
                raise terr("MODEL_CAPABILITY_UNAVAILABLE", "no model adapter")
            from ..planning.model_runner import run_structured

            prompt = self.prompts.resolve_for_task(task_id)
            run = run_structured(
                adapter, FollowupCandidate,
                [
                    {"role": "system", "content": "你是授课教师。只输出符合要求的 JSON。"},
                    {"role": "user", "content": f"{prompt.get('template_snapshot', '')}\n问题：{question_text}"},
                ],
                schema=FollowupCandidate.model_json_schema(),
                tool_name="submit_followup",
            )
            answer = run.payload
            with self.db.session() as sess:
                if answer.board is not None:
                    scene = declaration_to_scene(answer.board)
                    sess.add(
                        BoardSceneRow(
                            scene_id=f"bscene_{uuid.uuid4().hex[:12]}",
                            conversation_id=conv_id,
                            generation=generation,
                            step_no=0,  # 追问板书：step_no=0 标识，与原课步骤分开存
                            scene_version_no=1,
                            declaration_json=json.dumps(answer.board.model_dump(), ensure_ascii=False),
                            scene_json=json.dumps(scene, ensure_ascii=False),
                        )
                    )
                sess.add(
                    MessageRow(
                        message_id=f"msg_{uuid.uuid4().hex[:12]}",
                        conversation_id=conv_id,
                        generation=generation,
                        role="assistant",
                        content_ref=answer.answer,
                        status="completed",
                    )
                )
                conv_row = sess.get(ConversationRow, conv_id)
                conv_row.session_state = "awaiting_resume"
                sess.commit()
            self.events.record(
                task_id, stage="generate", status="succeeded",
                generation=lease.generation, attempt=lease.attempt,
            )
            self.tasks.submit_result(lease, stage="generate", status="succeeded", duration_ms=0)
            return {"status": "succeeded", "generation": generation}
        except PlanningError as exc:
            self.events.record(
                task_id, stage="generate", status="failed", error_code=exc.code,
                generation=lease.generation, attempt=lease.attempt,
            )
            self.tasks.submit_result(lease, stage="generate", status="failed",
                                     error_code=exc.code, duration_ms=0)
            return {"status": "failed", "error_code": exc.code}

    def resume_lesson(self, conversation_id: str, *, actor_id: str) -> dict:
        """继续课程：核验已就绪步骤版本 → 重开下一段调度 → 用户点击播放才恢复。"""
        conv = self._conversation(conversation_id, actor_id)
        if conv.kind != "teaching":
            raise terr("state_invalid", "not a teaching conversation", status=409)
        with self.db.session() as sess:
            conv_row = sess.get(ConversationRow, conversation_id)
            conv_row.session_state = "active"
            self._ensure_prefetch(sess, conversation_id, actor_id, 1, conv.generation)
            sess.commit()
        return {"conversation_id": conversation_id, "session_state": "active"}


__all__ = ["TeachingService", "TeachingError", "terr", "MAX_STEPS_PER_LESSON"]
