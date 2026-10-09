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

from ..agents.contracts import TeachingStepInput, FollowupInput
from ..agents.runner import deps_for_source

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
    LessonContentVersionRow,
    LessonProgressRow,
    MessageRow,
    OutlineVersionRow,
    load_json_field,
    utcnow,
)
from ..prompts.service import PromptService, PromptUnavailable
from ..tasks.service import TaskService, LeaseError
from .models import AudioAssetRow, BoardSceneRow, PlaybackCheckpointRow, TeachingStepRow
from .outputs import (
    FollowupCandidate,
    LessonPlanCandidate,
    StepCandidate,
    declaration_to_scene,
    validate_step_duration,
)

from .errors import TeachingError, terr
from .learning_models import LessonBlueprintRow
from .learning_outputs import LessonBlueprint, teaching_schedule
from .pedagogy import plan_lesson, previous_teaching, lesson_context, prepared_plan
from ..planning.retrieval import PlanningRetrievalService

TEACHING_PROMPT = "teaching_step_v1"
FOLLOWUP_PROMPT = "followup_v1"

MAX_STEPS_PER_LESSON = 24
MAX_ARTIFACT_REPAIRS = 2


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
        disclosures=None,
    ) -> None:
        self.db = db
        self.tasks = tasks
        self.events = events
        self.prompts = prompts
        self.storage = storage
        self.model_adapter_factory = model_adapter_factory
        self.tts_adapter_factory = tts_adapter_factory
        self.disclosures = disclosures
        from .authorization import TeachingAuthorization
        self.authorization = TeachingAuthorization(self)
        self.retrieval = PlanningRetrievalService(db)
        from .preparation import LessonPreparation
        self.preparation = LessonPreparation(self)
        self.server_managed=False
        self.configs=None
        self.background=None
        self.observed_sessions={}

    # ------------------------------------------------------------------
    # 启动讲次（教学门槛）
    # ------------------------------------------------------------------
    def start_lesson(self, course_id: str, lesson_id: str, *, actor_id: str,
                     _upgrade_from: str | None = None, _confirmation: dict | None = None) -> dict:
        with self.db.session() as sess:
            confirmation = self.authorization.prepare_confirmation(sess, course_id, lesson_id, actor_id,
                **_confirmation) if _confirmation else None
            source = None
            if _upgrade_from:
                source = sess.get(ConversationRow, _upgrade_from, with_for_update=True)
                if not source or source.owner_user_id != actor_id or source.deleted_at:
                    raise terr("conversation_not_found", status=404)
                if source.course_id != course_id or source.lesson_id != lesson_id:
                    raise terr("conversation_not_found", status=404)
                candidates = sess.scalars(select(LessonBlueprintRow).where(
                    LessonBlueprintRow.owner_user_id == actor_id, LessonBlueprintRow.lesson_id == lesson_id))
                for candidate in candidates:
                    if load_json_field(candidate.context_json, {}).get("_upgraded_from") == _upgrade_from:
                        upgraded = sess.get(ConversationRow, candidate.conversation_id)
                        if upgraded and not upgraded.deleted_at:
                            return {"conversation_id": candidate.conversation_id,
                                    "task_id": candidate.source_task_id, "lesson_id": lesson_id}
                from .learning_outputs import validate_new_blueprint
                from pydantic_ai.exceptions import ModelRetry
                original = sess.get(LessonBlueprintRow, _upgrade_from)
                if original:
                    try:
                        validate_new_blueprint(None, LessonBlueprint.model_validate_json(original.plan_json))
                        return {"conversation_id": _upgrade_from, "task_id": None, "lesson_id": lesson_id}
                    except (ValueError, ModelRetry):
                        pass
                if source.session_state != "active":
                    raise terr("STALE_RESULT_DISCARDED", status=409)
            course = sess.get(CourseRow, course_id, with_for_update=True)
            if course is None or course.owner_user_id != actor_id or course.deleted_at is not None:
                raise terr("course_not_found", status=404)
            if not course.current_outline_version_id:
                raise terr("LESSON_UNLOCK_REQUIRED", "outline not confirmed", status=409)
            if source and source.outline_version_id != course.current_outline_version_id:
                raise terr("LESSON_UNLOCK_REQUIRED", status=409)
            lesson = sess.get(LessonRow, lesson_id)
            if lesson is None or lesson.outline_version_id != course.current_outline_version_id:
                raise terr("lesson_not_found", status=404)
            from types import SimpleNamespace
            if not confirmation:
                self.authorization.require(sess, source or SimpleNamespace(owner_user_id=actor_id,
                    course_id=course_id, lesson_id=lesson_id, outline_version_id=course.current_outline_version_id,
                    conversation_id=None))
            warm=self.preparation.claim_for_start(sess,course,lesson) if not _upgrade_from else None
            if warm and warm.consumed_conversation_id:
                previous=sess.get(ConversationRow,warm.consumed_conversation_id)
                if previous and not previous.deleted_at and previous.session_state in ("active","paused","awaiting_resume"):
                    return self._reuse_lesson(sess, previous, lesson, confirmation)
            if not _upgrade_from and (lesson.status_preparation == "generating" or self.authorization.automatic_enabled):
                existing = sess.execute(select(ConversationRow).where(ConversationRow.owner_user_id == actor_id,
                    ConversationRow.lesson_id == lesson_id, ConversationRow.deleted_at.is_(None),
                    ConversationRow.outline_version_id == course.current_outline_version_id,
                    ConversationRow.session_state.in_(("active", "paused", "awaiting_resume")))
                    .order_by(ConversationRow.created_at.desc())).scalars().first()
                if existing:
                    return self._reuse_lesson(sess, existing, lesson, confirmation)
            version = sess.execute(select(LessonContentVersionRow).where(LessonContentVersionRow.lesson_id == lesson_id)
                .order_by(LessonContentVersionRow.content_version_no.desc())).scalars().first()
            if version is None:
                version = LessonContentVersionRow(content_version_id=f"lcv_{uuid.uuid4().hex[:12]}",
                    lesson_id=lesson_id, content_version_no=1, goal_ref=lesson.goal_ref,
                    scope_ref=course.current_outline_version_id)
                sess.add(version); sess.flush()
            progress = sess.execute(select(LessonProgressRow).where(LessonProgressRow.owner_user_id == actor_id,
                LessonProgressRow.lesson_id == lesson_id)).scalars().first()
            if progress is None:
                sess.add(LessonProgressRow(progress_id=f"prog_{uuid.uuid4().hex[:12]}",owner_user_id=actor_id,
                    lesson_id=lesson_id,lesson_content_version_id=version.content_version_id,learning_state="in_progress"))
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
                lesson_content_version_id=version.content_version_id,
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
                deadline_seconds=780 if warm and warm.status in ('queued','generating') else 180,
                session=sess,
            )
            if confirmation:
                self.authorization.record(sess, confirmation, actor_id, created["task_id"], conv_id)
            else:
                self.authorization.bind_task(sess, conv, created["task_id"])
            context = lesson_context(self, conv)
            conv.material_snapshot_json = json.dumps(context.get("material_ids", []))
            plan = LessonBlueprint.model_validate_json(warm.plan_json) if warm else prepared_plan(self, conv, context)
            if warm:
                context=json.loads(warm.context_json)
                context["_first_cache_id"]=warm.cache_id
                warm.consumed_conversation_id=conv_id
            context["_presentation_contract"] = 2
            if source:
                context["_upgraded_from"] = source.conversation_id
                source.session_state = "archived"
            sess.add(LessonBlueprintRow(conversation_id=conv_id,owner_user_id=actor_id,lesson_id=lesson_id,
                outline_version_id=course.current_outline_version_id,content_version_id=version.content_version_id,
                plan_json=plan.model_dump_json(),context_json=json.dumps(context,ensure_ascii=False),source_task_id=created["task_id"]))
            sess.commit()
        try:
            from time import monotonic
            self.observed_sessions[conv_id]=monotonic()
            if warm:
                self.prompts.clone_for_task(created["task_id"],warm.task_id,TEACHING_PROMPT)
                self.prompts.clone_for_task(created["task_id"],warm.task_id,"lesson_blueprint_v1")
                with self.db.session() as sess:
                    from ..tasks.service import LearningTaskRow
                    preparation_task=sess.get(LearningTaskRow,warm.task_id)
                    if preparation_task and preparation_task.status=="queued":preparation_task.lane="interactive"
                if warm.status in ("ready","text_ready"):
                    self.run_step_generation(created["task_id"])
            else:
                self.prompts.bind_for_task(created["task_id"], TEACHING_PROMPT, allow_fallback=True)
                self.prompts.bind_for_task(created["task_id"], "lesson_blueprint_v1", allow_fallback=True)
        except PromptUnavailable as exc:
            raise terr("PROMPT_UNAVAILABLE", exc.reason, status=409)
        return {
            "conversation_id": conv_id,
            "task_id": created["task_id"],
            "lesson_id": lesson_id,
            "started_at": utcnow().isoformat(),
            "cache_hit":bool(warm and warm.status in ("ready","text_ready")),
            "preparation_status":warm.status if warm else "cold",
        }

    def _step_artifact(self, sess, conv, step_no):
        generation = 1 if getattr(conv, "kind", "teaching") == "teaching" else conv.generation
        step = sess.scalars(select(TeachingStepRow).where(
            TeachingStepRow.conversation_id == conv.conversation_id,
            TeachingStepRow.generation == generation, TeachingStepRow.step_no == step_no)).first()
        board = sess.get(BoardSceneRow, step.board_scene_id) if step and step.board_scene_id else None
        ready = bool(step and step.text and step.status == "ready" and board
            and board.conversation_id == conv.conversation_id and board.generation == generation
            and board.step_no == step_no)
        return step, board, ready

    def _artifact_recovery(self, sess, conv, step_no=1):
        """Read-only integrity view; success requires owned text and its board."""
        from ..tasks.service import LearningTaskRow
        generation = 1 if getattr(conv, "kind", "teaching") == "teaching" else conv.generation
        subject = f"{conv.conversation_id}:{step_no}:{generation}"
        tasks = list(sess.scalars(select(LearningTaskRow).where(
            LearningTaskRow.owner_user_id == conv.owner_user_id,
            LearningTaskRow.kind == "lesson_step_generate", LearningTaskRow.subject_id == subject)
            .order_by(LearningTaskRow.created_at, LearningTaskRow.task_id)))
        source = next((task for task in tasks if task.status == "succeeded"), None)
        if self._step_artifact(sess, conv, step_no)[2] or not source:
            return None
        repairs = [task for task in tasks if task.idempotency_key.startswith("artifact-repair:")]
        pending = next((task for task in tasks if task.status in ("queued", "leased", "running")), None)
        cancelled = bool(source.cancel_requested or repairs and
            (repairs[-1].cancel_requested or repairs[-1].status == "cancelled"))
        available = not pending and not cancelled and len(repairs) < MAX_ARTIFACT_REPAIRS
        return {"status": "repairing" if pending else "missing" if available else "exhausted",
            "error_code": "OUTPUT_INVALID", "step_no": step_no,
            "source_task_id": source.task_id, "task_id": pending.task_id if pending else None,
            "available": bool(available), "required_action": "retry" if available else None,
            "attempts": len(repairs), "max_attempts": MAX_ARTIFACT_REPAIRS}

    def _ensure_artifact_repair(self, sess, conv, step_no=1):
        """Create a separate bounded task under the conversation row lock."""
        current = sess.get(ConversationRow, conv.conversation_id, with_for_update=True, populate_existing=True)
        if not current or current.deleted_at or current.owner_user_id != conv.owner_user_id:
            raise terr("conversation_not_found", status=404)
        course = sess.get(CourseRow, current.course_id)
        if not course or course.deleted_at or course.current_outline_version_id != current.outline_version_id:
            raise terr("STALE_RESULT_DISCARDED", status=409)
        state = self._artifact_recovery(sess, current, step_no)
        if not state or state["status"] == "repairing":
            return state
        if not state["available"] or current.session_state != "active":
            return state | {"available": False, "required_action": None}
        self.authorization.require(sess, current)
        self._check_authorization(state["source_task_id"], session=sess)
        generation = 1 if current.kind == "teaching" else current.generation
        created = self.tasks.create_task(owner_user_id=current.owner_user_id, kind="lesson_step_generate",
            subject_id=f"{current.conversation_id}:{step_no}:{generation}", stage="generate", lane="interactive",
            idempotency_key=f"artifact-repair:{current.conversation_id}:{step_no}:{generation}:{state['attempts'] + 1}",
            deadline_seconds=180, session=sess)
        self.authorization.bind_task(sess, current, created["task_id"])
        from ..prompts.service import PromptBindingRow
        blueprint = sess.get(LessonBlueprintRow, current.conversation_id)
        for name in (TEACHING_PROMPT, "lesson_blueprint_v1"):
            source_id = blueprint.source_task_id if name == "lesson_blueprint_v1" and blueprint else state["source_task_id"]
            self.prompts.resolve_for_task(source_id, name)
            source = sess.scalars(select(PromptBindingRow).where(PromptBindingRow.task_id == source_id,
                PromptBindingRow.prompt_name == name).order_by(PromptBindingRow.created_at)).first()
            sess.add(PromptBindingRow(binding_id="pbind_" + uuid.uuid4().hex[:12], task_id=created["task_id"],
                prompt_name=name, source=source.source, langfuse_version=source.langfuse_version,
                template_snapshot=source.template_snapshot, template_sha256=source.template_sha256,
                model_config_version=source.model_config_version, output_schema_version=source.output_schema_version,
                tools_version=source.tools_version, agent_code_version=source.agent_code_version))
        lesson = sess.get(LessonRow, current.lesson_id)
        if lesson:
            lesson.status_preparation = "generating"
        from time import monotonic
        self.observed_sessions[current.conversation_id] = monotonic()
        return state | {"status": "repairing", "task_id": created["task_id"], "available": False,
            "required_action": None, "attempts": state["attempts"] + 1}

    def _reuse_lesson(self, sess, conv, lesson, confirmation=None):
        """Reuse a session without dropping a fresh approval or missing results."""
        from ..tasks.service import LearningTaskRow
        blueprint = sess.get(LessonBlueprintRow, conv.conversation_id)
        task = sess.get(LearningTaskRow, blueprint.source_task_id) if blueprint else None
        if not task:
            task = sess.scalars(select(LearningTaskRow).where(
                LearningTaskRow.owner_user_id == conv.owner_user_id,
                LearningTaskRow.subject_id == f"{conv.conversation_id}:1:1",
                LearningTaskRow.kind == "lesson_step_generate").order_by(LearningTaskRow.created_at)).first()
        if not task:
            raise terr("task_not_found", status=404)
        if confirmation and getattr(self.authorization, "automatic_enabled", False):
            request = self.authorization.automatic_request(conv.course_id, conv.lesson_id,
                actor_id=conv.owner_user_id, purpose="recover", conversation_id=conv.conversation_id, session=sess)
            confirmation = self.authorization.prepare_confirmation(sess, conv.course_id, conv.lesson_id,
                conv.owner_user_id, **request)
        if confirmation:
            from ..slice1.errors import Slice1Error
            try:
                self.authorization.require(sess, conv)
                if confirmation["tts_confirmed"]:
                    self.authorization.require(sess, conv, audio=True)
                if confirmation["prewarm_opt_in"]:
                    self.authorization.require(sess, conv, prewarm=True)
            except Slice1Error:
                self.authorization.record(sess, confirmation, conv.owner_user_id, task.task_id, conv.conversation_id)
        self.authorization.bind_task(sess, conv, task.task_id)
        state = self._ensure_artifact_repair(sess, conv)
        from time import monotonic
        self.observed_sessions[conv.conversation_id] = monotonic()
        return {"conversation_id": conv.conversation_id,
            "task_id": state["task_id"] if state and state["task_id"] else task.task_id,
            "lesson_id": lesson.lesson_id, "cache_hit": self._step_artifact(sess, conv, 1)[2],
            "artifact_recovery": state, "status": "queued" if state and state["status"] == "repairing" else task.status}

    def recover_lesson(self, conversation_id: str, *, actor_id: str) -> dict:
        """Explicit recovery with existing authorization; never retry a success row."""
        conv = self._conversation(conversation_id, actor_id)
        with self.db.session() as sess:
            self.authorization.require(sess, conv)
            state = self._ensure_artifact_repair(sess, conv)
        return {"conversation_id": conversation_id, "lesson_id": conv.lesson_id,
            "task_id": state["task_id"] if state else None,
            "status": "queued" if state and state["status"] == "repairing" else "unavailable" if state else "ready",
            "artifact_recovery": state}

    def preflight_lesson(self, course_id, lesson_id, *, actor_id, purpose="start", conversation_id=None):
        return self.authorization.preflight(course_id, lesson_id, actor_id=actor_id,
            purpose=purpose, conversation_id=conversation_id)

    def confirm_lesson(self, course_id, lesson_id, *, actor_id, fingerprint,
                       content_confirmed, tts_confirmed=False, prewarm_opt_in=False,
                       purpose="start", conversation_id=None):
        confirmation = {"fingerprint": fingerprint, "content_confirmed": content_confirmed,
            "tts_confirmed": tts_confirmed, "prewarm_opt_in": prewarm_opt_in,
            "purpose": purpose, "conversation_id": conversation_id}
        if purpose == "prewarm":
            return self.preparation.prepare_course(course_id, actor_id, _confirmation=confirmation)
        if conversation_id:
            from ..tasks.service import LearningTaskRow
            with self.db.session() as sess:
                snapshot = self.authorization.prepare_confirmation(sess, course_id, lesson_id, actor_id, **confirmation)
                conv = sess.get(ConversationRow, conversation_id, with_for_update=True)
                tasks = list(sess.scalars(select(LearningTaskRow).where(
                    LearningTaskRow.owner_user_id == actor_id,
                    LearningTaskRow.subject_id.like(conversation_id + ":%"),
                    LearningTaskRow.kind.in_(("lesson_step_generate", "lesson_plan_refine", "tts")))
                    .order_by(LearningTaskRow.created_at)))
                root_task = next((task for task in tasks if task.kind == "lesson_step_generate"), None)
                if root_task is None:
                    raise terr("task_not_found", status=404)
                self.authorization.record(sess, snapshot, actor_id, root_task.task_id, conversation_id)
                retry_ids = []
                for task in tasks:
                    audio = task.kind == "tts"
                    if audio and not snapshot["tts_confirmed"]:
                        continue
                    if task.task_id != root_task.task_id:
                        self.authorization.bind_task(sess, conv, task.task_id, audio=audio)
                    config = snapshot["tts" if audio else "content_model"]
                    task.model_config_version = f"{config['config_id']}:{config['config_version']}"
                    if (task.status in ("failed", "expired") and not task.cancel_requested
                            and not task.idempotency_key.startswith("artifact-repair:")):
                        retry_ids.append(task.task_id)
                repair = self._ensure_artifact_repair(sess, conv)
            for task_id in retry_ids:
                self.tasks.requeue_for_retry(task_id, actor_id=actor_id)
            from time import monotonic
            self.observed_sessions[conversation_id] = monotonic()
            repair_task = repair["task_id"] if repair else None
            return {"conversation_id": conversation_id, "task_id": repair_task or (retry_ids[0] if retry_ids else root_task.task_id),
                "lesson_id": lesson_id, "status": "queued" if repair_task or retry_ids else root_task.status,
                "artifact_recovery": repair}
        return self.start_lesson(course_id, lesson_id, actor_id=actor_id, _confirmation=confirmation)

    def _check_authorization(self, task_id, *, audio=False, session=None):
        if session is not None:
            from ..tasks.service import LearningTaskRow
            task = session.get(LearningTaskRow, task_id, populate_existing=True)
            self.authorization.check_task(session, task, audio=audio)
            return
        with self.db.session() as sess:
            self._check_authorization(task_id, audio=audio, session=sess)

    def upgrade_lesson(self, conversation_id: str, *, actor_id: str) -> dict:
        conv = self._conversation(conversation_id, actor_id)
        return self.start_lesson(conv.course_id, conv.lesson_id, actor_id=actor_id,
                                 _upgrade_from=conversation_id)

    def _generate_one(self,conv,task_id,adapter,step_no,*,lease=None,prepared=None):
        owner=conv.owner_user_id;conv_id=conv.conversation_id
        blueprint, context = prepared or plan_lesson(self, conv, task_id, adapter)
        schedule = teaching_schedule(blueprint)
        if step_no > len(schedule): raise terr("OUTPUT_INVALID", "教学已结束")
        phase = schedule[step_no-1]
        existing_board = self._stage_blocks(conv_id,schedule,step_no,phase.stage_no)
        previous = previous_teaching(self, conv_id, step_no)
        with self.db.session() as sess:
            existing_step = self._step_artifact(sess, conv, step_no)[0]
            preserved_text = existing_step.text if existing_step and existing_step.text else None
        prompt = self.prompts.resolve_for_task(task_id, TEACHING_PROMPT)["template_snapshot"]
        from ..agents.roles import teaching_agent
        from ..agents.tools import lesson_step_output_validator
        from pydantic_ai.exceptions import ModelRetry
        def validate(ctx, output):
            lesson_step_output_validator(ctx, output)
            if len(output.steps) != 1: raise ModelRetry("只输出当前一个步骤。")
            item = output.steps[0]
            if preserved_text is not None and item.text != preserved_text:
                raise ModelRetry("已有讲解文字必须原样保留，仅补齐缺失的板书。")
            for block in item.board.blocks:
                if block.id in existing_board and block.model_dump()!=existing_board[block.id]:
                    raise ModelRetry("同阶段已有板书块ID的内容不能改写；引用旧块须原样保留，新内容使用新ID。")
            if any(ref.material_id not in context.get("material_ids",[]) for ref in item.board.original_refs):
                raise ModelRetry("原图引用必须来自本讲已选择的资料。")
            if item.phase and item.phase != phase.phase: raise ModelRetry("phase 与当前计划阶段不符。")
            if item.covered_goal_ids and set(item.covered_goal_ids) != set(phase.goal_ids):
                raise ModelRetry("覆盖目标与当前阶段不符。")
            if item.lesson_finished and step_no != len(schedule): raise ModelRetry("尚未到最后小结，不能结束。")
            return output
        payload={"existing_board":existing_board,"context":context,"blueprint":blueprint.model_dump(),"current_stage":phase.model_dump(),
                 "previous":previous,"is_final":step_no==len(schedule)}
        if preserved_text is not None:
            payload["preserved_text"] = preserved_text
            payload["repair_policy"] = "Keep preserved_text verbatim; generate only the missing board for that text."
        if context.get("material_ids"):
            # One deterministic retrieval, no model round-trip for getters.
            result = self.retrieval.assemble(owner_user_id=owner,material_ids=context["material_ids"],
                                             query=context["lesson_goal"])
            payload["selected_material_context"] = {"refs":result.refs,"excerpts":result.excerpts}
            adapter.omit_prompt_trace = True  # retrieved textbook text cannot enter content trace
        options = {"output_validator":validate}
        if prepared is not None:
            options["max_requests"]=2
            payload["preferred_text_length"]="160-240 characters"
        status = getattr(adapter,"capability_status",{}).get("streaming",{})
        state = status.get("state") if isinstance(status,dict) else status
        if lease is not None and getattr(adapter,"supports_text_stream",False) and state != "unavailable":
            from .content_stream import PreviewWriter
            options["on_delta"] = PreviewWriter(self,lease,conv,step_no,stage_no=phase.stage_no,
                stage_segment_no=phase.stage_segment_no,existing_blocks=existing_board)
        run=teaching_agent.generate_step(adapter,TeachingStepInput.from_messages([{"role":"system","content":prompt},
            {"role":"user","content":json.dumps(payload,ensure_ascii=False)}]),deps_for_source(adapter),**options)
        return blueprint,context,phase,run

    # ------------------------------------------------------------------
    # 步骤生成 worker（首段 interactive / 下一段 teaching_prefetch）
    # ------------------------------------------------------------------
    def run_step_generation(self, task_id: str, *, lease_owner: str = "teaching-worker") -> dict:
        task_info=self.tasks.get_task(task_id)
        if task_info["status"] == "succeeded":
            conv_id, step_raw, _ = task_info["subject_id"].split(":", 2)
            conv = self._conversation(conv_id, task_info["owner_user_id"])
            with self.db.session() as sess:
                if not self._step_artifact(sess, conv, int(step_raw))[2]:
                    return {"status": "failed", "error_code": "OUTPUT_INVALID", "artifact_missing": True,
                        "required_action": "retry"}
            return {"status": "succeeded"}
        if self.preparation.waiting(task_info):return {"status":"preparing"}
        started = monotonic_ms()
        adapter = None
        lease = self.tasks.claim(task_id, lease_owner=lease_owner, lease_seconds=120, allow_reclaim_active=False)
        try:
            task = self.tasks.get_task(task_id)
            owner = task["owner_user_id"]
            conv_id, _, rest = task["subject_id"].partition(":")
            step_raw, _, gen_raw = rest.partition(":")
            step_no, generation = int(step_raw or "1"), int(gen_raw or "1")
            conv = self._conversation(conv_id, owner)
            if conv.session_state != "active": raise terr("STALE_RESULT_DISCARDED")
            cached=self.preparation.for_conversation(conv,step_no)
            adapter = None if cached else self._model_adapter(owner, task_id)
            if adapter is None and not cached: raise terr("MODEL_CAPABILITY_UNAVAILABLE")
            from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
            from contextvars import copy_context
            def generate():
                cached=self.preparation.for_conversation(conv,step_no)
                if cached:
                    from types import SimpleNamespace
                    from .outputs import StepCandidate
                    blueprint=LessonBlueprint.model_validate_json(cached.plan_json)
                    context={key:value for key,value in json.loads(cached.context_json).items() if not key.startswith("_")}
                    phase=teaching_schedule(blueprint)[0]
                    return blueprint,context,phase,SimpleNamespace(payload=LessonPlanCandidate(steps=[StepCandidate.model_validate_json(cached.candidate_json)]),mode="cache")
                return self._generate_one(conv,task_id,adapter,step_no,lease=lease)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(copy_context().run,generate)
                while True:
                    try: blueprint,context,phase,run=future.result(timeout=30);break
                    except FutureTimeout:self.tasks.heartbeat(lease,lease_seconds=120)
            candidate=run.payload.steps[0]
            schedule=teaching_schedule(blueprint)
            duration_ms=validate_step_duration(candidate.text)
            step_id=f"tstep_{uuid.uuid4().hex[:12]}";scene_id=f"bscene_{uuid.uuid4().hex[:12]}"
            def publish(sess):
                nonlocal step_id, scene_id
                self._check_authorization(task_id, session=sess)
                current=sess.get(ConversationRow,conv_id,with_for_update=True)
                if not current or current.deleted_at or current.session_state!="active" or current.generation != conv.generation: raise terr("STALE_RESULT_DISCARDED")
                course=sess.get(CourseRow,current.course_id)
                if not course or course.deleted_at:raise terr("course_not_found",status=404)
                if sess.get(LessonBlueprintRow,conv_id) is None:
                    sess.add(LessonBlueprintRow(conversation_id=conv_id,owner_user_id=owner,lesson_id=current.lesson_id,
                        outline_version_id=current.outline_version_id,content_version_id=current.lesson_content_version_id,
                        plan_json=blueprint.model_dump_json(),context_json=json.dumps(context,ensure_ascii=False),source_task_id=task_id))
                else:
                    planned = sess.get(LessonBlueprintRow, conv_id)
                    if not load_json_field(planned.plan_json, {}).get("stages"):
                        planned.plan_json = blueprint.model_dump_json()
                exists=sess.execute(select(TeachingStepRow).where(TeachingStepRow.conversation_id==conv_id,
                    TeachingStepRow.generation==generation,TeachingStepRow.step_no==step_no)).scalars().first()
                if exists:
                    step_id = exists.step_id
                    if self._step_artifact(sess, current, step_no)[2]:
                        scene_id = exists.board_scene_id
                if not self._step_artifact(sess, current, step_no)[2]:
                    if exists and exists.text and exists.text != candidate.text:
                        raise terr("OUTPUT_INVALID", "repair must preserve existing text")
                    scene=declaration_to_scene(candidate.board)
                    scene.update(stage_no=phase.stage_no,stage_segment_no=phase.stage_segment_no,
                        stage_finished=phase.stage_segment_no==phase.stage_segment_count,
                        board_scope=f"{conv_id}:stage:{phase.stage_no}")
                    scene["beats"]=[{"id":b.id,"unit_id":b.id,"text":b.text,"block_ids":[block.id for block in b.blocks],"highlight_ids":b.highlight_ids} for b in candidate.beats]
                    scene_version = (sess.scalar(select(func.max(BoardSceneRow.scene_version_no)).where(
                        BoardSceneRow.conversation_id == conv_id, BoardSceneRow.generation == generation,
                        BoardSceneRow.step_no == step_no)) or 0) + 1
                    sess.add(BoardSceneRow(scene_id=scene_id,conversation_id=conv_id,generation=generation,step_no=step_no,
                        scene_version_no=scene_version,declaration_json=candidate.board.model_dump_json(),scene_json=json.dumps(scene,ensure_ascii=False)))
                    if exists:
                        exists.board_scene_id = scene_id
                        exists.status = "ready"
                        if not exists.text:
                            exists.text = candidate.text
                    else:
                        sess.add(TeachingStepRow(step_id=step_id,conversation_id=conv_id,generation=generation,step_no=step_no,
                            text_version_no=1,title=candidate.title,text=candidate.text,status="ready",board_scene_id=scene_id,
                            source_task_id=task_id,phase=phase.phase,covered_goal_ids_json=json.dumps(phase.goal_ids),
                            lesson_finished=step_no==len(schedule)))
                lesson=sess.get(LessonRow,current.lesson_id)
                if lesson:lesson.status_preparation="ready"
            receipt=self.tasks.submit_result(lease,stage="generate",status="succeeded",duration_ms=monotonic_ms()-started,publish=publish)
            if not receipt["accepted"]: return {"status":"stale_discarded"}
            self.preparation.attach_audio(conv_id,owner)
            if step_no==1:
                try:self.preparation.prepare_next(conv_id,owner)
                except Exception:pass
            with self.db.session() as sess:self._ensure_prefetch(sess,conv_id,owner,step_no+1,generation)
            if not self.authorization.enforced and self.tts_adapter_factory is not None and (not getattr(self.tts_adapter_factory,"uses_saved_config",False) or (getattr(self.tts_adapter_factory,"has_fallback",False) and not self.tts_adapter_factory.config_for_owner(owner))):
                self.tasks.create_task(owner_user_id=owner,kind="tts",subject_id=f"{conv_id}:{step_no}:{generation}",
                    idempotency_key=f"tts:{conv_id}:{step_no}:{generation}:v1",stage="tts",lane="teaching_prefetch",deadline_seconds=90)
            return {"status":"succeeded","step_no":step_no,"step_id":step_id,"board_scene_id":scene_id,"mode":run.mode}
        except LeaseError:
            self.tasks.recover_worker_failure(lease,stage="generate",error_code="LEASE_LOST")
            return {"status":"stale_discarded","error_code":"STALE_RESULT_DISCARDED"}
        except Exception as exc:
            code = self._controlled_failure(exc)
            self.tasks.submit_result(lease,stage="generate",status="failed",error_code=code,duration_ms=monotonic_ms()-started)
            return {"status":"failed","error_code":code}

        finally:
            if adapter is not None and hasattr(adapter,"close"):
                adapter.close()

    def _stage_blocks(self,conv_id,schedule,step_no,stage_no):
        from .outputs import BoardDeclaration
        from .board_blocks import presentation_blocks
        with self.db.session() as sess:
            scenes=list(sess.scalars(select(BoardSceneRow).where(BoardSceneRow.conversation_id==conv_id,
                BoardSceneRow.generation==1,BoardSceneRow.step_no<step_no,BoardSceneRow.step_no>0)
                .order_by(BoardSceneRow.step_no)))
            result={}
            for scene in scenes:
                if scene.step_no>len(schedule) or schedule[scene.step_no-1].stage_no!=stage_no:continue
                for block in presentation_blocks(BoardDeclaration.model_validate_json(scene.declaration_json)):
                    from .board_blocks import BLOCK_ADAPTER
                    result[block["id"]]=BLOCK_ADAPTER.validate_python(block).model_dump()
            return result

    def _ensure_prefetch(self, sess, conv_id: str, owner: str, next_no: int, generation: int) -> None:
        """预生成窗口=1：当前段就绪且会话 active 时才创建下一段任务。"""
        conv = sess.get(ConversationRow, conv_id)
        if conv is None or conv.session_state != "active":
            return
        blueprint = sess.get(LessonBlueprintRow, conv_id)
        if blueprint and load_json_field(blueprint.plan_json, {}).get("stages") and next_no > len(teaching_schedule(LessonBlueprint.model_validate_json(blueprint.plan_json))):
            return
        if next_no > MAX_STEPS_PER_LESSON:
            return
        checkpoint = sess.execute(select(PlaybackCheckpointRow).where(PlaybackCheckpointRow.conversation_id == conv_id)
            .order_by(PlaybackCheckpointRow.client_seq.desc())).scalars().first()
        if checkpoint and self._presentation(load_json_field(checkpoint.board_state_json, {})).get("paused"):
            return
        if next_no > max(1, checkpoint.step_no if checkpoint else 1) + 1:
            return
        generation = 1 if conv.kind == "teaching" else generation
        from ..planning.models import LessonRow

        lesson = sess.get(LessonRow, conv.lesson_id) if conv.lesson_id else None
        _ = lesson
        if self._ensure_artifact_repair(sess, conv, next_no) is not None:
            return
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
        idem = f"step:{conv_id}:{next_no}:{generation}" + (f":resume:{conv.generation}" if conv.generation > 1 else "")
        self.authorization.require(sess, conv)
        created = self.tasks.create_task(
            owner_user_id=owner, kind="lesson_step_generate", subject_id=f"{conv_id}:{next_no}:{generation}",
            idempotency_key=idem, stage="generate", lane="teaching_prefetch", deadline_seconds=180,
            session=sess)
        self.authorization.bind_task(sess, conv, created["task_id"])
        if blueprint:
            self.prompts.clone_for_task(created["task_id"], blueprint.source_task_id, TEACHING_PROMPT)
        else:
            self.prompts.bind_for_task(created["task_id"], TEACHING_PROMPT, allow_fallback=True)

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

    @staticmethod
    def _controlled_failure(exc):
        from ..agents.limits import to_controlled_error
        if isinstance(exc,PromptUnavailable):
            return "PROMPT_UNAVAILABLE"
        if getattr(exc,"code",None):
            from ..logging_audit.contracts import normalize_error_code
            try:
                return normalize_error_code(exc.code)
            except Exception:
                return "INTERNAL_ERROR"
        return to_controlled_error(exc).code

    def _model_adapter(self, owner: str, task_id: str):
        self._check_authorization(task_id)
        if self.model_adapter_factory is None:
            return None
        source = self.model_adapter_factory(owner, task_id)
        if source is not None and self.authorization.enforced:
            from dataclasses import replace
            bound = deps_for_source(source)
            check = bound.check_state
            def guarded():
                self._check_authorization(task_id)
                if check:
                    check()
            source.agent_deps = replace(bound, check_state=guarded)
        return source

    def _conversation(self, conv_id: str, owner: str) -> ConversationRow:
        with self.db.session() as sess:
            conv = sess.get(ConversationRow, conv_id)
            if conv is None or conv.owner_user_id != owner or conv.deleted_at:
                raise terr("conversation_not_found", status=404)
            sess.expunge(conv)
            return conv

    # ------------------------------------------------------------------
    # TTS worker
    # ------------------------------------------------------------------
    def ensure_audio(self,conversation_id: str,*,actor_id: str,step_no: int) -> dict:
        conv=self._conversation(conversation_id,actor_id)
        if conv.session_state!="active":raise terr("STALE_RESULT_DISCARDED",status=409)
        factory=self.tts_adapter_factory
        view=factory.config_for_owner(actor_id) if getattr(factory,"config_for_owner",None) else None
        if not view:return {"status":"unconfigured","task_id":None}
        if step_no==1 and self.preparation.pending_audio_for(conv):return {"status":"preparing","task_id":None}
        with self.db.session() as sess:
            checkpoint=sess.scalars(select(PlaybackCheckpointRow).where(PlaybackCheckpointRow.conversation_id==conversation_id)
                .order_by(PlaybackCheckpointRow.client_seq.desc())).first()
            if step_no>max(1,checkpoint.step_no if checkpoint else 1)+1:raise terr("step_not_found",status=404)
            step=sess.scalars(select(TeachingStepRow).where(TeachingStepRow.conversation_id==conversation_id,
                TeachingStepRow.generation==1,TeachingStepRow.step_no==step_no)).first()
            if not step:raise terr("step_not_found",status=404)
            linked=sess.get(AudioAssetRow,step.audio_id) if step.audio_id else None
            if linked and linked.status=="ready" and linked.text_version_no==step.text_version_no and linked.text_sha256==hashlib.sha256(step.text.encode("utf8")).hexdigest():
                return {"status":"ready","task_id":None}
            text_version=step.text_version_no
            self.authorization.require(sess, conv, audio=True)
            created=self.tasks.create_task(owner_user_id=actor_id,kind="tts",subject_id=f"{conversation_id}:{step_no}:1",
                idempotency_key=f"tts:{conversation_id}:{step_no}:1:v{text_version}:{view['config_id']}:{view['config_version']}",
                stage="tts",lane="teaching_prefetch",deadline_seconds=120,
                model_config_version=f"{view['config_id']}:{view['config_version']}", session=sess)
            if created["deduped"]:
                self._check_authorization(created["task_id"], audio=True, session=sess)
            else:
                self.authorization.bind_task(sess, conv, created["task_id"], audio=True)
        return {"status":created["status"],"task_id":created["task_id"]}

    def run_tts(self, task_id: str, *, lease_owner: str = "tts-worker") -> dict:
        lease = self.tasks.claim(task_id, lease_owner=lease_owner, lease_seconds=120,allow_reclaim_active=False)
        adapter = None
        try:
            task = self.tasks.get_task(task_id)
            owner = task["owner_user_id"]
            self._check_authorization(task_id, audio=True)
            conv_id, step_raw, gen_raw = task["subject_id"].split(":")
            step_no, generation = int(step_raw), int(gen_raw)
            conv = self._conversation(conv_id, owner)
            if conv.session_state != "active": raise terr("STALE_RESULT_DISCARDED")
            with self.db.session() as sess:
                step = sess.scalars(select(TeachingStepRow).where(TeachingStepRow.conversation_id==conv_id,
                    TeachingStepRow.generation==generation, TeachingStepRow.step_no==step_no)).first()
                if not step: raise terr("step_not_found", status=404)
                text, text_version = step.text, step.text_version_no
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            audio_status, reason, synth, audio_bytes = "invalid", "tts_unconfigured", {}, None
            try:
                adapter = self.tts_adapter_factory(owner, task_id) if self.tts_adapter_factory else None
                if adapter:
                    synth = adapter.synthesize(text)
                    subtitle = synth.get("subtitle")
                    import math
                    if not synth.get("audio_hex") or not subtitle:
                        reason = "subtitle_missing"
                    elif not all(isinstance(cue, dict) and isinstance(cue.get("begin_ms"), (int,float))
                        and isinstance(cue.get("end_ms"), (int,float)) and math.isfinite(cue["begin_ms"]) and math.isfinite(cue["end_ms"])
                        and 0<=cue["begin_ms"]<cue["end_ms"]<=120000 for cue in subtitle):
                        reason = "subtitle_missing"
                    else:
                        audio_bytes = bytes.fromhex(synth["audio_hex"])
                        audio_status, reason = "ready", None
            except Exception as exc:
                allowed = {"timeout","credential_invalid","rate_limited","config_not_found","disclosure_required",
                    "disclosure_revoked","disclosure_expired","connection_error","capability_test_failed"}
                audio_status, reason = "failed", getattr(exc,"code", "tts_failed")
                if reason not in allowed: reason = "tts_failed"
            result = {}
            def publish(sess):
                self._check_authorization(task_id, audio=True, session=sess)
                current = sess.get(ConversationRow,conv_id,with_for_update=True)
                if not current or current.deleted_at or current.session_state!="active" or current.generation!=conv.generation:
                    raise terr("STALE_RESULT_DISCARDED")
                saved = sess.get(TeachingStepRow,step.step_id,with_for_update=True)
                if not saved or saved.text_version_no!=text_version or hashlib.sha256(saved.text.encode("utf8")).hexdigest()!=digest:
                    raise terr("STALE_RESULT_DISCARDED")
                if getattr(adapter,"config_id",None):
                    from ..slice1.models import ServiceConfigRow, DisclosureGrantRow
                    config = sess.get(ServiceConfigRow,adapter.config_id,with_for_update=True)
                    if not config or not config.is_active or config.config_version!=adapter.config_version:
                        raise terr("STALE_RESULT_DISCARDED")
                    grant=sess.scalars(select(DisclosureGrantRow).where(DisclosureGrantRow.task_id==task_id,
                        DisclosureGrantRow.config_id==adapter.config_id,DisclosureGrantRow.owner_user_id==owner,
                        DisclosureGrantRow.content_category=="audio_text").order_by(DisclosureGrantRow.granted_at.desc())
                        .with_for_update()).first()
                    if not grant or grant.revoked_at or grant.config_version!=config.config_version:
                        raise terr("STALE_RESULT_DISCARDED")
                    if grant.expires_at:
                        from datetime import timezone
                        expires=grant.expires_at if grant.expires_at.tzinfo else grant.expires_at.replace(tzinfo=timezone.utc)
                        if expires<utcnow():raise terr("STALE_RESULT_DISCARDED")
                audio = sess.scalars(select(AudioAssetRow).where(AudioAssetRow.conversation_id==conv_id,
                    AudioAssetRow.generation==generation,AudioAssetRow.step_no==step_no,AudioAssetRow.text_version_no==text_version)).first()
                if not audio:
                    audio=AudioAssetRow(audio_id=f"aud_{uuid.uuid4().hex[:12]}",conversation_id=conv_id,
                        generation=generation,step_no=step_no,text_version_no=text_version,text_sha256=digest)
                    sess.add(audio)
                if audio.status=="ready" and audio.text_sha256==digest:
                    saved.audio_id=audio.audio_id
                    result.update(audio_id=audio.audio_id,audio_status="ready");return
                audio.status=audio_status;audio.invalid_reason=reason
                if audio_status=="ready":
                    audio.storage_key=f"audio/{conv_id}/{audio.audio_id}.mp3"
                    self.storage.put(audio.storage_key,audio_bytes)
                    audio.audio_format=synth.get("audio_format","mp3")
                    audio.subtitle_json=json.dumps(synth["subtitle"],ensure_ascii=False)
                    audio.duration_ms=int(max(cue["end_ms"] for cue in synth["subtitle"]))
                    saved.audio_id=audio.audio_id
                result.update(audio_id=audio.audio_id,audio_status=audio_status)
            receipt=self.tasks.submit_result(lease,stage="tts",status="succeeded" if audio_status=="ready" else "failed",error_code=None if audio_status=="ready" else "TTS_TIMEOUT" if reason=="timeout" else "INTERNAL_ERROR",duration_ms=0,publish=publish)
            if not receipt["accepted"]:return {"status":"stale_discarded"}
            return {"status":"succeeded" if audio_status=="ready" else "failed",**result}
        except Exception as exc:
            code = self._controlled_failure(exc)
            self.tasks.submit_result(lease,stage="tts",status="failed",error_code=code,duration_ms=0)
            return {"status":"failed","error_code":code}
        finally:
            if adapter and hasattr(adapter,"close"):adapter.close()

    # ------------------------------------------------------------------
    # 工作区读取
    # ------------------------------------------------------------------
    def get_workspace(self, conversation_id: str, *, actor_id: str) -> dict:
        conv = self._conversation(conversation_id, actor_id)
        from time import monotonic
        self.observed_sessions[conversation_id]=monotonic()
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
            blueprint_row=sess.get(LessonBlueprintRow,conversation_id)
            schedule=teaching_schedule(LessonBlueprint.model_validate_json(blueprint_row.plan_json)) if blueprint_row and load_json_field(blueprint_row.plan_json,{}).get("stages") else []
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
            followup_board = None
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
                followup_scene = sess.scalars(select(BoardSceneRow).where(
                    BoardSceneRow.conversation_id==conversation_id,BoardSceneRow.generation==conv.generation,
                    BoardSceneRow.step_no==0).order_by(BoardSceneRow.scene_version_no.desc())).first()
                if followup_scene:
                    followup_board = json.loads(followup_scene.scene_json)
                    if "presentation" not in followup_board:
                        from .outputs import BoardDeclaration
                        followup_board["presentation"]=declaration_to_scene(BoardDeclaration.model_validate_json(
                            followup_scene.declaration_json))["presentation"]
                followup_messages = [
                    {"generation": m.generation, "role": m.role, "content": m.content_ref}
                    for m in msgs
                ]
            step_views = []
            for s in steps:
                scene = scene_by_id.get(s.board_scene_id or "")
                audio_ready = False
                audio = sess.scalars(select(AudioAssetRow).where(AudioAssetRow.conversation_id==conversation_id,
                    AudioAssetRow.generation==s.generation,AudioAssetRow.step_no==s.step_no,
                    AudioAssetRow.text_version_no==s.text_version_no)).first()
                if s.audio_id:
                    linked = sess.get(AudioAssetRow, s.audio_id)
                    audio_ready = bool(linked and linked.status == "ready" and linked.text_version_no==s.text_version_no
                        and linked.text_sha256==hashlib.sha256(s.text.encode("utf8")).hexdigest())
                    if audio_ready:audio=linked
                saved_board = json.loads(scene.scene_json) if scene else None
                if scene and "presentation" not in saved_board:
                    from .outputs import BoardDeclaration
                    saved_board["presentation"]=declaration_to_scene(BoardDeclaration.model_validate_json(
                        scene.declaration_json))["presentation"]
                scheduled = schedule[s.step_no-1] if s.step_no<=len(schedule) else None
                stage_no=scheduled.stage_no if scheduled else s.step_no
                stage_segment_no=scheduled.stage_segment_no if scheduled else 1
                stage_finished=scheduled.stage_segment_no==scheduled.stage_segment_count if scheduled else True
                if saved_board:
                    saved_board.update(stage_no=stage_no,stage_segment_no=stage_segment_no,stage_finished=stage_finished,
                        board_scope=f"{conversation_id}:stage:{stage_no}")
                step_views.append({
                    "stage_no":stage_no,"stage_segment_no":stage_segment_no,"stage_finished":stage_finished,
                    "board_scope":f"{conversation_id}:stage:{stage_no}",
                    "step_no": s.step_no,
                    "step_id": s.step_id,
                    "title": s.title,
                    "text": s.text,
                    "status": s.status,
                    "text_version_no": s.text_version_no,
                    "board": saved_board,
                    "audio_id": s.audio_id,
                    "audio_ready": audio_ready, "phase": s.phase,
                    "audio_status": "ready" if audio_ready else ("invalid" if audio.status=="ready" else audio.status) if audio else "none",
                    "audio_error": ("text_version_mismatch" if audio.status=="ready" else audio.invalid_reason) if audio and not audio_ready else None,
                    "audio_subtitles": load_json_field(audio.subtitle_json,[]) if audio_ready else [],
                    "audio_duration_ms":audio.duration_ms if audio_ready else 0,
                    "covered_goal_ids": load_json_field(s.covered_goal_ids_json, []),
                    "lesson_finished": s.lesson_finished,
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
            from ..tasks.service import LearningTaskRow
            pending_tasks = list(sess.scalars(select(LearningTaskRow).where(LearningTaskRow.owner_user_id == actor_id,
                LearningTaskRow.subject_id.like(conversation_id + ":%"),
                LearningTaskRow.status.in_(("queued", "failed", "leased", "running", "expired")))))
            from ..logging_audit.models import TaskStateRow
            from ..logging_audit.contracts import ERROR_CODES, normalize_error_code
            states = {state.task_id: normalize_error_code(state.error_code) if state.error_code else None
                for state in sess.scalars(select(TaskStateRow).where(
                    TaskStateRow.task_id.in_([t.task_id for t in pending_tasks])))}
            queued = [{"task_id":t.task_id,"kind":t.kind,"status":t.status,"error_code":states.get(t.task_id),
                "attempt":t.attempt,"generation":t.generation,"revision":t.revision,
                "deadline_at":t.deadline_at.isoformat() if t.deadline_at else None,
                "lease_until":t.lease_until.isoformat() if t.lease_until else None,
                "cancel_requested":bool(t.cancel_requested),
                "retryable":bool(t.status in ("failed","expired") and not t.cancel_requested and not t.idempotency_key.startswith("artifact-repair:") and states.get(t.task_id) and ERROR_CODES[states[t.task_id]].retryable),
                "required_action":"authorize" if states.get(t.task_id) in ("DISCLOSURE_REQUIRED","DISCLOSURE_REVOKED","DISCLOSURE_EXPIRED","CONFIG_NOT_FOUND") else "retry" if t.status in ("failed","expired") and not t.cancel_requested and not t.idempotency_key.startswith("artifact-repair:") else None,
                "recovery":{"available":t.status in ("failed","expired") and not t.cancel_requested and not t.idempotency_key.startswith("artifact-repair:"),
                    "requires_confirmation":t.status in ("failed","expired") and not t.cancel_requested and not t.idempotency_key.startswith("artifact-repair:"),
                    "action":"authorize" if states.get(t.task_id) in ("DISCLOSURE_REQUIRED","DISCLOSURE_REVOKED","DISCLOSURE_EXPIRED","CONFIG_NOT_FOUND") else "retry" if t.status in ("failed","expired") and not t.cancel_requested and not t.idempotency_key.startswith("artifact-repair:") else None},
                "step_no":int(t.subject_id.split(":")[1]) if t.subject_id.split(":")[1].isdigit() else 0,
                "followup":":followup:" in t.subject_id} for t in pending_tasks
                if (t.subject_id.split(":")[1].isdigit() and (t.kind == "tts" or conv.session_state == "active"))
                or (":followup:" in t.subject_id and int(t.subject_id.split(":")[-1]) == conv.generation)]
            blueprint = sess.get(LessonBlueprintRow, conversation_id)
            total = len(schedule) if schedule else None
            needs_upgrade = bool(conv.session_state == "active" and
                (not schedule or any(s.stage_segment_count < 2 for s in schedule
                                     if s.phase in ("intuition", "concept", "example"))))
            lesson = sess.get(LessonRow,conv.lesson_id) if conv.lesson_id else None
            artifact_recovery = self._artifact_recovery(sess, conv, 1)
            if artifact_recovery is None and last_checkpoint and last_checkpoint.step_no > 1:
                artifact_recovery = self._artifact_recovery(sess, conv, last_checkpoint.step_no)
            tts_config_view = (self.tts_adapter_factory.config_for_owner(actor_id)
                if getattr(self.tts_adapter_factory, "config_for_owner", None) else None)
            tts_authorized = False
            if tts_config_view or (self.tts_adapter_factory and not self.authorization.enforced):
                from ..slice1.errors import Slice1Error
                try:
                    voice_authorization = self.authorization.require(sess, conv, audio=True)
                    tts_authorized = True
                    if self.authorization.automatic_enabled and voice_authorization:
                        from ..slice1.models import DisclosureGrantRow
                        revoked_audio = sess.scalar(select(DisclosureGrantRow.grant_id).join(LearningTaskRow,
                            DisclosureGrantRow.task_id == LearningTaskRow.task_id).where(
                            LearningTaskRow.owner_user_id == actor_id,
                            LearningTaskRow.subject_id.like(conversation_id + ":%"),
                            DisclosureGrantRow.content_category == "audio_text",
                            DisclosureGrantRow.config_id == voice_authorization.tts_config_id,
                            DisclosureGrantRow.config_version == voice_authorization.tts_config_version,
                            DisclosureGrantRow.revoked_at.is_not(None)).limit(1))
                        tts_authorized = revoked_audio is None
                except Slice1Error:
                    pass
        authorization_required=False
        authorization_error=None
        authorization_preflight=None
        if self.authorization.enforced:
            from ..slice1.errors import Slice1Error
            try:
                with self.db.session() as sess:self.authorization.require(sess,conv)
            except Slice1Error as exc:
                authorization_required=True;authorization_error=exc.code
                try:authorization_preflight=self.preflight_lesson(conv.course_id,conv.lesson_id,
                    actor_id=actor_id,purpose="recover",conversation_id=conversation_id)
                except Exception:authorization_preflight=None
        return {
            "authorization_required":authorization_required,"authorization_error":authorization_error,
            "required_action":"authorize" if authorization_required else "retry" if
                (artifact_recovery and artifact_recovery["available"]) or any(t["required_action"]=="retry" for t in queued) else None,
            "artifact_recovery": artifact_recovery,
            "authorization_preflight":authorization_preflight,
            "server_managed":self.server_managed,
            "tts_authorized": tts_authorized,
            "tts_config": ({"config_id":tts_config_view["config_id"],"config_version":tts_config_view["config_version"]}
                if tts_config_view else None),
            "lesson_title": lesson.title_ref or lesson.goal_ref if lesson else "讲次教学",
            "planned_steps": total,
            "needs_upgrade": needs_upgrade,
            "queued_tasks": queued,
            "lesson_finished": any(s.lesson_finished for s in steps),
            "conversation_id": conversation_id,
            "kind": conv.kind,
            "course_id": conv.course_id,
            "lesson_id": conv.lesson_id,
            "generation": conv.generation,
            "session_state": conv.session_state,
            "restore_point": load_json_field(conv.restore_point_json, {}),
            "followups": followup_messages,
            "followup_board":followup_board,
            "last_checkpoint": (
                {
                    "client_seq": last_checkpoint.client_seq,
                    "step_no": last_checkpoint.step_no,
                    "position_ms": last_checkpoint.position_ms,
                    "presentation": self._presentation(load_json_field(last_checkpoint.board_state_json, {})),
                    "saved_at": last_checkpoint.created_at.isoformat(),
                }
                if last_checkpoint else None
            ),
            "steps": step_views,
        }

    @staticmethod
    def _presentation(board_state):
        value = board_state.get("presentation", {}) if isinstance(board_state, dict) else {}
        if not isinstance(value, dict):
            return {}
        result = {}
        chars = value.get("visible_chars")
        if type(chars) is int:
            result["visible_chars"] = max(0, min(100000, chars))
        for name in ("paused", "text_complete"):
            if type(value.get(name)) is bool:
                result[name] = value[name]
        if isinstance(value.get("audio_id"),str) and value["audio_id"].startswith("aud_") and len(value["audio_id"])<=64:
            result["audio_id"]=value["audio_id"]
        return result

    def complete_lesson(self, conversation_id, *, actor_id):
        with self.db.session() as sess:
            conv=sess.get(ConversationRow,conversation_id,with_for_update=True)
            if not conv or conv.owner_user_id!=actor_id or conv.deleted_at:raise terr("conversation_not_found",status=404)
            course=sess.get(CourseRow,conv.course_id)
            if not course or course.deleted_at or course.current_outline_version_id!=conv.outline_version_id:
                raise terr("course_not_found",status=404)
            final=sess.execute(select(TeachingStepRow).where(TeachingStepRow.conversation_id==conversation_id,
                TeachingStepRow.generation==1,TeachingStepRow.lesson_finished.is_(True))).scalars().first()
            if not final:raise terr("LESSON_UNLOCK_REQUIRED","本讲尚未完整生成。",status=409)
            progress=sess.execute(select(LessonProgressRow).where(LessonProgressRow.owner_user_id==actor_id,
                LessonProgressRow.lesson_id==conv.lesson_id).with_for_update()).scalars().first()
            if not progress:
                progress=LessonProgressRow(progress_id=f"prog_{uuid.uuid4().hex[:12]}",owner_user_id=actor_id,lesson_id=conv.lesson_id)
                sess.add(progress)
            progress.learning_state="user_completed";progress.lesson_content_version_id=conv.lesson_content_version_id
            progress.updated_at=utcnow()
            course_id,lesson_id=conv.course_id,conv.lesson_id
        try:self.preparation.prepare_course(course_id,actor_id)
        except Exception:pass
        return {"completed":True,"course_id":course_id,"lesson_id":lesson_id}

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
            if conv is None or conv.owner_user_id != actor_id or conv.kind != "teaching" or conv.deleted_at:
                raise terr("conversation_not_found", status=404)
            self.authorization.require(sess,conv)
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
                        LearningTaskRow.status.in_(("queued", "leased", "running")),
                        LearningTaskRow.subject_id.like(conversation_id + ":%"),
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
            conv.session_state = "paused"
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
                owner_user_id=actor_id, kind="lesson_step_generate",
                subject_id=f"{conversation_id}:followup:{generation}",
                idempotency_key=idempotency_key or f"followup:{conversation_id}:{generation}",
                stage="generate", lane="interactive", deadline_seconds=120, session=sess)
            self.authorization.bind_task(sess,conv,created["task_id"])
        if created.get("deduped"):
            return {"task_id": created["task_id"], "generation": generation, "deduped": True}
        try:
            self.prompts.bind_for_task(created["task_id"], FOLLOWUP_PROMPT, allow_fallback=True)
        except PromptUnavailable as exc:
            raise terr("PROMPT_UNAVAILABLE", exc.reason, status=409)
        return {"task_id": created["task_id"], "generation": generation}

    def run_followup(self, task_id: str, *, lease_owner: str = "teaching-worker") -> dict:
        started = monotonic_ms()
        adapter = None
        lease = self.tasks.claim(task_id, lease_owner=lease_owner, lease_seconds=120, allow_reclaim_active=False)
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
            from ..agents.roles import teaching_agent

            prompt = self.prompts.resolve_for_task(task_id)
            options = {}
            def validate_followup(ctx, output):
                from pydantic_ai.exceptions import ModelRetry
                allowed = set(load_json_field(conv.material_snapshot_json,[]))
                if output.board:
                    if any(ref.material_id not in allowed for ref in output.board.original_refs):
                        raise ModelRetry("原图引用必须来自本讲已选择的资料。")
                    try:
                        declaration_to_scene(output.board)
                    except PlanningError:
                        raise ModelRetry("板书连线必须引用已经存在的节点。")
                return output
            options["output_validator"] = validate_followup
            saved = getattr(adapter,"capability_status",{}).get("streaming",{})
            state = saved.get("state") if isinstance(saved,dict) else saved
            if getattr(adapter,"supports_text_stream",False) and state != "unavailable":
                from .content_stream import PreviewWriter
                options["on_delta"] = PreviewWriter(self,lease,conv,0,followup=True)
            messages = [
                    {"role": "system", "content": "你是授课教师。只输出符合要求的 JSON。可用beats，片段先输出完整blocks再text；最终answer等于片段text拼接。板书按通用文字、表格、关系图、代码或公式选择，board包含title及format_version=2，片段块由程序汇总。"},
                    {"role": "user", "content": f"{prompt.get('template_snapshot', '')}\n问题：{question_text}"},
                ]
            from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
            from contextvars import copy_context
            def generate():
                return teaching_agent.answer_followup(adapter,FollowupInput.from_messages(messages),deps_for_source(adapter),**options).payload
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(copy_context().run,generate)
                while True:
                    try:
                        answer = future.result(timeout=30)
                        break
                    except FutureTimeout:
                        self.tasks.heartbeat(lease,lease_seconds=120)
            def publish(sess):
                self._check_authorization(task_id, session=sess)
                current = sess.get(ConversationRow,conv_id,with_for_update=True)
                course = sess.get(CourseRow,current.course_id) if current else None
                if not current or current.deleted_at or current.generation != generation or not course or course.deleted_at:
                    raise terr("STALE_RESULT_DISCARDED")
                if answer.board is not None:
                    scene = declaration_to_scene(answer.board)
                    scene["beats"]=[{"id":b.id,"unit_id":b.id,"text":b.text,"block_ids":[block.id for block in b.blocks],"highlight_ids":b.highlight_ids} for b in answer.beats]
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
            result = self.tasks.submit_result(lease, stage="generate", status="succeeded",
                duration_ms=monotonic_ms()-started,publish=publish)
            if not result["accepted"]:
                return {"status":"stale_discarded"}
            return {"status": "succeeded", "generation": generation}
        except LeaseError:
            self.tasks.recover_worker_failure(lease, stage="generate", error_code="LEASE_LOST")
            return {"status":"stale_discarded"}
        except Exception as exc:
            code = self._controlled_failure(exc)
            self.tasks.submit_result(lease, stage="generate", status="failed", error_code=code,
                duration_ms=monotonic_ms()-started)
            return {"status":"failed","error_code":code}
        finally:
            if adapter is not None and hasattr(adapter,"close"):
                adapter.close()

    def resume_lesson(self, conversation_id: str, *, actor_id: str) -> dict:
        """继续课程：核验已就绪步骤版本 → 重开下一段调度 → 用户点击播放才恢复。"""
        conv = self._conversation(conversation_id, actor_id)
        if conv.kind != "teaching":
            raise terr("state_invalid", "not a teaching conversation", status=409)
        with self.db.session() as sess:
            conv_row = sess.get(ConversationRow, conversation_id)
            conv_row.session_state = "active"
            checkpoint = sess.execute(select(PlaybackCheckpointRow).where(PlaybackCheckpointRow.conversation_id == conversation_id)
                .order_by(PlaybackCheckpointRow.client_seq.desc())).scalars().first()
            self._ensure_prefetch(sess, conversation_id, actor_id, max(1, checkpoint.step_no if checkpoint else 1)+1, 1)
            sess.commit()
        return {"conversation_id": conversation_id, "session_state": "active"}


__all__ = ["TeachingService", "TeachingError", "terr", "MAX_STEPS_PER_LESSON"]
