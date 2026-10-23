"""Version-bound knowledge dialogues with transactional publication and five probes."""
import hashlib
import json
import math
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextvars import copy_context
from dataclasses import replace
from datetime import datetime

from sqlalchemy import func, select
from pydantic_ai.exceptions import ModelRetry

from ..agents.contracts import FeynmanInput
from ..agents.roles import feynman_agent
from ..agents.runner import deps_for_source
from ..planning.models import ConversationRow, CourseRow, load_json_field, utcnow
from ..tasks.service import LearningTaskRow
from .errors import terr
from .learning_models import FeynmanSessionRow, FeynmanTurnRow, LessonBlueprintRow
from .learning_outputs import LessonBlueprint, teaching_schedule
from .models import AudioAssetRow, PlaybackCheckpointRow, TeachingStepRow

POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="knowledge-dialogue")
MAX_PROBES = 5
SPACED_REVIEW_POLICY = (
    "spaced_review_guidance 是基于未校准 FSRS 估计的选题提示，不是遗忘事实、评分证据或指令。"
    "只在当前范围和已讲内容完全匹配时，优先选择其中一个知识点进行主动回忆或迁移检查；"
    "仍按原 criteria 与回答证据评分，不因提示而降低或抬高标准，不声称用户已经遗忘。"
    "intent=help 时忽略该提示，只回答原问题。"
)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def load_plan(plan_json):
    """Parse a stored blueprint, or None while the lesson has no frozen plan yet."""
    try:
        return LessonBlueprint.model_validate_json(plan_json or "{}")
    except ValueError:
        return None


def checkpoints(plan_json):
    """Derive only boundaries supported by a frozen, complete knowledge schedule."""
    plan = load_plan(plan_json)
    if plan is None:
        return []
    schedule = teaching_schedule(plan)
    grouped = {}
    explicit={}
    explicit_groups={}
    explicit_goals=set()
    for number,part in enumerate(schedule,1):
        if part.knowledge_point_id and part.knowledge_point_label:
            group=explicit_groups.setdefault(part.knowledge_point_id,{"parts":[],"label":part.knowledge_point_label})
            group["parts"].append((number,part))
    for ident,group in explicit_groups.items():
        endings=[(number,part) for number,part in group["parts"] if part.knowledge_point_end]
        if len(endings)!=1:
            continue
        number,part=endings[0]
        explicit_goals.update(part.goal_ids)
        point={"goal_ids":part.goal_ids,"goal_labels":[group["label"]],
            "knowledge_point_id":ident, "knowledge_point_ids":[ident],
            "scope_capability_ids":sorted({scope_id for _,item in group["parts"]
                for scope_id in item.scope_capability_ids})}
        existing=explicit.setdefault(number,{"goal_ids":[],"goal_labels":[],"scope_capability_ids":[]})
        existing["goal_ids"]=sorted(set(existing["goal_ids"])|set(point["goal_ids"]))
        existing["goal_labels"].extend(point["goal_labels"])
        existing["scope_capability_ids"]=sorted(set(existing["scope_capability_ids"])|
            set(point["scope_capability_ids"]))
        existing["knowledge_point_ids"]=sorted(set(existing.get("knowledge_point_ids", []))|
            set(point["knowledge_point_ids"]))
        existing["knowledge_point_id"]=ident if "knowledge_point_id" not in existing else ""
    for goal in plan.goals:
        if goal.id in explicit_goals:
            continue
        segments = [(i, part) for i, part in enumerate(schedule, 1)
                    if goal.id in part.goal_ids and part.phase != "summary"]
        if not {"intuition", "concept", "example"} <= {part.phase for _, part in segments}:
            continue
        number = max(i for i, _ in segments)
        grouped.setdefault(number, []).append(goal)
    derived={number:{"goal_ids":[g.id for g in goals],"goal_labels":[g.label for g in goals]}
        for number,goals in grouped.items()}
    derived.update(explicit)
    return [{"checkpoint_id": "knowledge_" + hashlib.sha256(
        (plan_json + str(number)).encode()).hexdigest()[:24],
        "after_step_no": number, **point} for number,point in sorted(derived.items())]


def checkpoint_concept_ids(context):
    point_ids = sorted({ident for ident in context.get("knowledge_point_ids", [])
                        if isinstance(ident, str) and ident})
    if len(point_ids) == 1:
        return point_ids
    if point_ids:
        return []
    capability_ids = sorted({ident for ident in context.get("scope_capability_ids", [])
                             if isinstance(ident, str) and ident})
    return capability_ids if len(capability_ids) == 1 else []


def review_topics(schedule, taught, goals):
    labels = {goal.id: goal.label for goal in goals}
    topics = {}
    for source in taught:
        step_no = source.get("step_no")
        if not isinstance(step_no, int) or not 1 <= step_no <= len(schedule):
            continue
        segment = schedule[step_no - 1]
        topic = segment.knowledge_point_label or "、".join(
            labels[goal_id] for goal_id in segment.goal_ids if goal_id in labels)
        if not topic:
            continue
        concepts = {segment.knowledge_point_id, *segment.scope_capability_ids}
        for concept in concepts:
            if concept:
                topics[concept] = topic[:120]
    return topics


def spaced_review_policy(context, settings, owner):
    concepts = checkpoint_concept_ids(context)
    return bool(settings and settings.enabled and not owner.startswith("guest_")
        and len(concepts) == 1)


def lesson_checkpoint(plan_json):
    plan = load_plan(plan_json)
    if plan is None:
        return None
    return {"checkpoint_id": "lesson_" + hashlib.sha256(plan_json.encode()).hexdigest()[:24],
            "scope": "lesson", "protocol_version": 2, "persona_isolation_version": 1,
            "after_step_no": len(teaching_schedule(plan)),
            "goal_ids": [goal.id for goal in plan.goals],
            "goal_labels": [goal.label for goal in plan.goals]}


def prompt_name(context, operation):
    suffix = "v2" if context.get("protocol_version") == 2 else "v1"
    return f"feynman_{'question' if operation == 'open' else 'evaluate'}_{suffix}"


class FeynmanService:
    def __init__(self, teaching):
        self.teaching = teaching
        self.db = teaching.db
        self.tasks = teaching.tasks
        self.lock = threading.Lock()
        self.running = set()

    def verify_source(self,sess,conv,context):
        from ..materials.study_sources import verify_sources
        verify_sources(sess, conv.owner_user_id, context.get("source_snapshot", {}))
        plan=sess.get(LessonBlueprintRow,conv.conversation_id,populate_existing=True)
        if (context["generation"]!=conv.generation or not plan
            or hashlib.sha256(plan.plan_json.encode()).hexdigest()!=context["plan_hash"]
            or conv.lesson_content_version_id!=context["content_version_id"]):
            raise terr("STALE_RESULT_DISCARDED")
        for source in context["taught"]:
            step=sess.scalar(select(TeachingStepRow).where(TeachingStepRow.conversation_id==conv.conversation_id,
                TeachingStepRow.generation==1,TeachingStepRow.step_no==source["step_no"]).execution_options(populate_existing=True))
            if not step or step.text!=source["text"] or step.text_version_no!=source["text_version"]:
                raise terr("STALE_RESULT_DISCARDED")
        if context.get("mentor"):
            from ..characters.models import CharacterCardRow
            mentor=sess.get(CharacterCardRow,context["mentor"]["card_id"],populate_existing=True)
            if not mentor or mentor.deleted_at:
                raise terr("DISCLOSURE_REVOKED")

    def owned(self, sess, conversation_id, owner, *, lock=False):
        conv = sess.get(ConversationRow, conversation_id, with_for_update=lock)
        course = sess.get(CourseRow, conv.course_id) if conv else None
        if (not conv or conv.owner_user_id != owner or conv.deleted_at or conv.kind != "teaching"
                or not course or course.deleted_at or course.current_outline_version_id != conv.outline_version_id):
            raise terr("conversation_not_found", status=404)
        self.teaching.authorization.require(sess, conv)
        return conv

    def boundaries(self, sess, conv):
        row = sess.get(LessonBlueprintRow, conv.conversation_id)
        if not row:
            return []
        points = checkpoints(row.plan_json)
        from ..planning.models import LessonProgressRow
        progress = sess.scalar(select(LessonProgressRow).where(
            LessonProgressRow.owner_user_id == conv.owner_user_id,
            LessonProgressRow.lesson_id == conv.lesson_id))
        if not (progress and progress.learning_state == "user_completed"
                and progress.lesson_content_version_id == conv.lesson_content_version_id):
            final = lesson_checkpoint(row.plan_json)
            if final:
                points.append(final)
        return points

    def require_lesson_result(self, sess, conv):
        plan = sess.get(LessonBlueprintRow, conv.conversation_id)
        point = lesson_checkpoint(plan.plan_json) if plan else None
        row = sess.scalar(select(FeynmanSessionRow).where(
            FeynmanSessionRow.conversation_id == conv.conversation_id,
            FeynmanSessionRow.checkpoint_id == point["checkpoint_id"])) if point else None
        if not row or row.state != "closed" or row.result not in ("understood", "review", "unverified"):
            raise terr("state_invalid", "请先完成本讲末尾的费曼对话测验。", status=409)
        self.verify_source(sess, conv, load_json_field(row.context_json, {}))
        answered = sess.scalar(select(FeynmanTurnRow.turn_id).where(
            FeynmanTurnRow.session_id == row.session_id, FeynmanTurnRow.kind == "question",
            FeynmanTurnRow.answer != "", FeynmanTurnRow.evaluation_json != "{}"))
        if not answered:
            raise terr("state_invalid", "请先提交本讲测验回答并完成评价。", status=409)

    def guard(self, sess, conv, target):
        latest = sess.scalar(select(PlaybackCheckpointRow).where(
            PlaybackCheckpointRow.conversation_id == conv.conversation_id)
            .order_by(PlaybackCheckpointRow.client_seq.desc()))
        previous = latest.step_no if latest else 0
        rows = {row.checkpoint_id: row for row in sess.scalars(select(FeynmanSessionRow).where(
            FeynmanSessionRow.conversation_id == conv.conversation_id))}
        for boundary in self.boundaries(sess, conv):
            row = rows.get(boundary["checkpoint_id"])
            if boundary["after_step_no"] < target and (
                    row and row.state != "closed" or not row and boundary["after_step_no"] >= previous):
                message = ("请先完成本讲费曼对话测验。" if boundary.get("scope") == "lesson"
                           else "请先完成或跳过当前知识点问答。")
                raise terr("state_invalid", message, status=409)

    def blocks(self, sess, conv, target):
        try:
            self.guard(sess, conv, target)
            return False
        except Exception as exc:
            if getattr(exc, "code", "") == "state_invalid":
                return True
            raise

    def _rows(self, sess, conversation_id, owner, *, lock=False):
        conv = self.owned(sess, conversation_id, owner, lock=lock)
        query = select(FeynmanSessionRow).where(
            FeynmanSessionRow.conversation_id == conversation_id,
            FeynmanSessionRow.state != "closed").order_by(FeynmanSessionRow.created_at.desc())
        row = sess.scalar(query.with_for_update() if lock else query)
        return conv, row

    def _new_task(self, sess, conv, row, turn, operation, key):
        task = self.tasks.create_task(owner_user_id=conv.owner_user_id, kind="feynman",
            subject_id=f"{conv.conversation_id}:feynman:{row.session_id}:{turn.turn_id}:{operation}",
            idempotency_key=key, stage="generate", lane="interactive",
            deadline_seconds=600 if load_json_field(row.context_json, {}).get("protocol_version") == 2 else 180,
            session=sess)
        self.teaching.authorization.bind_task(sess, conv, task["task_id"])
        consent=self.teaching.authorization.require(sess,conv)
        if consent:
            task_row=sess.get(LearningTaskRow,task["task_id"])
            self.teaching._character_disclosure(sess,task_row,consent.content_config_id,consent.content_config_version,prepare=True)
        row.task_id = task["task_id"]
        return task["task_id"]

    def start(self, conversation_id, owner, checkpoint_id):
        with self.db.session() as sess:
            conv, active = self._rows(sess, conversation_id, owner, lock=True)
            if active:
                return self._project(sess, active)
            boundary = next((b for b in self.boundaries(sess, conv) if b["checkpoint_id"] == checkpoint_id), None)
            if not boundary:
                raise terr("step_not_found", status=404)
            exists = sess.scalar(select(FeynmanSessionRow).where(
                FeynmanSessionRow.conversation_id == conversation_id,
                FeynmanSessionRow.checkpoint_id == checkpoint_id))
            if exists:
                return self._project(sess, exists)
            number = boundary["after_step_no"]
            self.guard(sess, conv, number)
            position = sess.scalar(select(PlaybackCheckpointRow).where(
                PlaybackCheckpointRow.conversation_id == conversation_id)
                .order_by(PlaybackCheckpointRow.client_seq.desc()))
            if not position or position.step_no != number:
                raise terr("state_invalid", "尚未到达该知识点。", status=409)
            steps = list(sess.scalars(select(TeachingStepRow).where(
                TeachingStepRow.conversation_id == conversation_id, TeachingStepRow.generation == 1,
                TeachingStepRow.step_no <= number, TeachingStepRow.status == "ready")
                .order_by(TeachingStepRow.step_no)))
            plan_row=sess.get(LessonBlueprintRow,conversation_id)
            schedule=teaching_schedule(LessonBlueprint.model_validate_json(plan_row.plan_json))
            relevant = [step for step in steps if set(load_json_field(step.covered_goal_ids_json, []))
                        & set(boundary["goal_ids"])]
            if boundary.get("knowledge_point_id"):
                relevant=[step for step in relevant if schedule[step.step_no-1].knowledge_point_id==boundary["knowledge_point_id"]]
            if not relevant or relevant[-1].step_no != number:
                raise terr("state_invalid", "知识点内容尚未就绪。", status=409)
            if boundary.get("scope") == "lesson" and (
                    len(steps) != number or not steps[-1].lesson_finished):
                raise terr("state_invalid", "本讲内容尚未完整就绪。", status=409)
            if not self.teaching._presentation(load_json_field(position.board_state_json, {})).get("text_complete"):
                raise terr("state_invalid", "请先完成当前讲解。", status=409)
            context = {**boundary, "taught": [{"step_no": step.step_no, "text": step.text,
                "text_version": step.text_version_no} for step in relevant]}
            if context.get("protocol_version") != 2 and getattr(self.teaching, "memory", None):
                from ..memory.models import MemorySettingsRow
                setting = sess.get(MemorySettingsRow, owner)
                if spaced_review_policy(context, setting, owner):
                    context["protocol_version"] = 2
            lesson_data = load_json_field(plan_row.context_json, {})
            if lesson_data.get("source_snapshot"):
                from ..materials.study_sources import source_candidates
                context["source_snapshot"] = lesson_data["source_snapshot"]
                context["source_candidates"] = source_candidates(sess, owner, context["source_snapshot"],
                    [{"lesson_id": conv.lesson_id}], feynman=True)
            from ..characters.service import segment_character
            character = segment_character(sess,conversation_id,number)
            if character and not context.get("persona_isolation_version"):
                context["mentor"]={"card_id":character["card_id"],"revision":character["revision"],
                    "name":character["profile"].get("name",""),"style":character["profile"].get("personality","")}
            factory = self.teaching.tts_adapter_factory
            audio = bool(factory and (factory.config_for_owner(owner)
                if getattr(factory, "config_for_owner", None) else not self.teaching.authorization.enforced))
            consent = self.teaching.authorization.require(sess, conv)
            if consent and not consent.tts_root_grant_id:
                audio = False
            if audio:
                self.teaching.authorization.require(sess, conv, audio=True)
                last=relevant[-1]
                track=sess.get(AudioAssetRow,last.audio_id) if last.audio_id else None
                if track and position.position_ms<max(0,track.duration_ms-250):
                    raise terr("state_invalid","请先听完当前知识点。",status=409)
            context["audio_required"] = audio
            context["generation"] = conv.generation
            context["plan_hash"] = hashlib.sha256(plan_row.plan_json.encode()).hexdigest()
            context["content_version_id"] = conv.lesson_content_version_id
            row = FeynmanSessionRow(session_id="fey_" + uuid.uuid4().hex,
                conversation_id=conversation_id, owner_user_id=owner, checkpoint_id=checkpoint_id,
                after_step_no=number, source_hash=hashlib.sha256(encode(context).encode()).hexdigest(),
                context_json=encode(context))
            sess.add(row)
            sess.flush()
            turn = FeynmanTurnRow(turn_id="fturn_" + uuid.uuid4().hex,
                session_id=row.session_id, turn_no=0, kind="question")
            sess.add(turn)
            sess.flush()
            task_id = self._new_task(sess, conv, row, turn, "open", f"feynman-open:{row.session_id}")
        self._bind(task_id, prompt_name(context, "open"))
        self.kick(task_id)
        return self.view(conversation_id, owner)["active"]

    def view(self, conversation_id, owner):
        pending = []
        with self.db.session() as sess:
            conv, row = self._rows(sess, conversation_id, owner)
            boundaries = self.boundaries(sess, conv)
            resolved = {r.checkpoint_id for r in sess.scalars(select(FeynmanSessionRow).where(
                FeynmanSessionRow.conversation_id == conversation_id, FeynmanSessionRow.state == "closed"))}
            for boundary in boundaries:
                boundary["resolved"] = boundary["checkpoint_id"] in resolved
            completed=list(sess.scalars(select(FeynmanSessionRow).where(
                FeynmanSessionRow.conversation_id==conversation_id,FeynmanSessionRow.state=="closed")
                .order_by(FeynmanSessionRow.created_at)))
            result = {"checkpoints": boundaries, "active": self._project(sess, row) if row else None,
                "completed":[self._project(sess,item) for item in completed]}
            if row:
                turns = list(sess.scalars(select(FeynmanTurnRow).where(FeynmanTurnRow.session_id == row.session_id)))
                ids = [row.task_id, *[t.audio_task_id for t in turns]]
                for ident in filter(None, ids):
                    task = sess.get(LearningTaskRow, ident)
                    if task and not task.cancel_requested and (task.status in ("queued", "expired")
                            or task.status in ("leased", "running") and task.lease_until and task.lease_until <= utcnow()):
                        pending.append(ident)
        for ident in pending:
            self.kick(ident)
        return result

    def _project(self, sess, row):
        turns = list(sess.scalars(select(FeynmanTurnRow).where(
            FeynmanTurnRow.session_id == row.session_id).order_by(FeynmanTurnRow.turn_no)))
        history = []
        for turn in turns:
            audio = sess.get(AudioAssetRow, turn.audio_id) if turn.audio_id else None
            if turn.published:
                history.append({"id": turn.turn_id, "role": "assistant", "text": turn.public_text,
                    "source": load_json_field(turn.evaluation_json, {}).get("_source"),
                    "audio_id": turn.audio_id, "subtitles": load_json_field(audio.subtitle_json, []) if audio else [],
                    "playback": load_json_field(turn.playback_json, {})})
            if turn.answer:
                history.append({"id": turn.turn_id + ":answer", "role": "user", "text": turn.answer})
        question = next((t for t in reversed(turns) if t.kind == "question" and t.published), None)
        task_ids = [row.task_id, *[t.audio_task_id for t in turns]]
        failed = next((task for ident in reversed(task_ids) if ident
            and (task := sess.get(LearningTaskRow, ident)) and task.status in ("failed", "expired")), None)
        context = load_json_field(row.context_json, {})
        from .feynman_protocol import learning_record, state_from_turns
        flow = state_from_turns(turns) if context.get("protocol_version") == 2 else None
        return {"session_id": row.session_id, "checkpoint_id": row.checkpoint_id,
            "scope": context.get("scope", "knowledge"), "protocol_version": context.get("protocol_version", 1),
            "learning_state": flow,
            "learning_record": learning_record(turns, flow) if flow else None,
            "after_step_no": row.after_step_no, "revision": row.revision, "state": row.state,
            "probe_count": row.probe_count, "max_probes": MAX_PROBES, "result": row.result,
            "question_id": question.turn_id if question else None, "history": history,
            "audio_required": load_json_field(row.context_json, {}).get("audio_required", False),
            "retry_available": bool(failed), "task_id": failed.task_id if failed else row.task_id}

    def answer(self, conversation_id, owner, question_id, revision, content, key, *, help=False):
        content = content.strip()
        if not content or len(content) > 3000:
            raise terr("invalid_field")
        with self.db.session() as sess:
            conv, row = self._rows(sess, conversation_id, owner, lock=True)
            turn = sess.get(FeynmanTurnRow, question_id)
            if not row or not turn or turn.session_id != row.session_id:
                raise terr("step_not_found", status=404)
            duplicate = sess.scalar(select(FeynmanTurnRow).where(
                FeynmanTurnRow.session_id == row.session_id, FeynmanTurnRow.request_key == key))
            if duplicate and duplicate.answer == content:
                return {"task_id": row.task_id}
            if duplicate:
                raise terr("state_invalid", "同一请求不能提交不同内容。", status=409)
            if row.revision != revision or row.state != "waiting" or turn.kind != "question" or turn.answer:
                raise terr("state_invalid", "题目状态已经变化，请刷新后继续。", status=409)
            if load_json_field(row.context_json, {}).get("audio_required") and not load_json_field(turn.playback_json, {}).get("ended"):
                raise terr("state_invalid", "请先听完当前问题。", status=409)
            latest=sess.scalar(select(FeynmanTurnRow).where(FeynmanTurnRow.session_id==row.session_id,
                FeynmanTurnRow.published.is_(True)).order_by(FeynmanTurnRow.turn_no.desc()))
            if (load_json_field(row.context_json,{}).get("audio_required") and latest
                and not load_json_field(latest.playback_json,{}).get("ended")):
                raise terr("state_invalid","请先听完当前解释。",status=409)
            if help:
                number = max(t.turn_no for t in sess.scalars(select(FeynmanTurnRow).where(
                    FeynmanTurnRow.session_id == row.session_id))) + 1
                turn = FeynmanTurnRow(turn_id="fturn_" + uuid.uuid4().hex, session_id=row.session_id,
                    turn_no=number, kind="help", answer=content, request_key=key)
                sess.add(turn)
                sess.flush()
            else:
                turn.answer = content
                turn.request_key = key
                turn.evaluation_json = encode({**load_json_field(turn.evaluation_json, {}),
                                               "_answered_at": utcnow().isoformat()})
            row.state = "evaluating"
            row.revision += 1
            task_id = self._new_task(sess, conv, row, turn, "help" if help else "answer",
                f"feynman-answer:{turn.turn_id}:{key}")
            context = load_json_field(row.context_json, {})
        self._bind(task_id, prompt_name(context, "answer"))
        self.kick(task_id)
        return {"task_id": task_id}

    def action(self, conversation_id, owner, revision, action):
        with self.db.session() as sess:
            conv, row = self._rows(sess, conversation_id, owner, lock=True)
            if not row:
                raise terr("state_invalid", status=409)
            if row.revision != revision:
                raise terr("state_invalid", status=409)
            if action == "skip" and load_json_field(row.context_json, {}).get("scope") == "lesson":
                raise terr("state_invalid", "本讲费曼环节可暂停后继续，不能跳过并完成本讲。", status=409)
            if action == "continue" and row.state != "feedback":
                raise terr("state_invalid", status=409)
            if action == "continue" and load_json_field(row.context_json, {}).get("audio_required"):
                final = sess.scalar(select(FeynmanTurnRow).where(FeynmanTurnRow.session_id == row.session_id)
                    .order_by(FeynmanTurnRow.turn_no.desc()))
                if not final or not load_json_field(final.playback_json, {}).get("ended"):
                    raise terr("state_invalid", "请先听完反馈。", status=409)
            row.state = "closed"
            row.result = "skipped" if action == "skip" else row.result
            row.revision += 1
            return {"state": row.state, "result": row.result}

    def playback(self, conversation_id, owner, turn_id, client_seq, position_ms, ended):
        with self.db.session() as sess:
            _, row = self._rows(sess, conversation_id, owner, lock=True)
            turn = sess.get(FeynmanTurnRow, turn_id)
            if not row or not turn or turn.session_id != row.session_id or not turn.published:
                raise terr("step_not_found", status=404)
            previous = load_json_field(turn.playback_json, {})
            if client_seq <= previous.get("client_seq", 0):
                return {"saved": False}
            audio = sess.get(AudioAssetRow, turn.audio_id) if turn.audio_id else None
            done = bool(ended and (not audio or position_ms >= max(0, audio.duration_ms - 250)))
            turn.playback_json = encode({"client_seq": client_seq, "position_ms": max(0, position_ms),
                "ended": bool(previous.get("ended") or done)})
            return {"saved": True}

    def retry(self, conversation_id, owner):
        with self.db.session() as sess:
            _, row = self._rows(sess, conversation_id, owner, lock=True)
            if not row:
                raise terr("state_invalid", status=409)
            view = self._project(sess, row)
            if not view["retry_available"]:
                return view
            task_id = view["task_id"]
            self.tasks.requeue_for_retry(task_id, actor_id=owner)
        self.kick(task_id)
        return {"task_id": task_id}

    def _bind(self, task_id, name):
        from ..prompts.service import PromptBindingRow
        with self.db.session() as sess:
            exists = sess.scalar(select(PromptBindingRow.binding_id).where(PromptBindingRow.task_id == task_id))
        if not exists:
            self.teaching.prompts.bind_for_task(task_id, name,
                output_schema_version="feynman-2" if name.endswith("_v2") else "feynman-1")

    def kick(self, task_id):
        with self.lock:
            if task_id in self.running:
                return
            self.running.add(task_id)
        POOL.submit(copy_context().run, self.run, task_id)

    def run(self, task_id):
        lease, adapter, storage_key = None, None, None
        try:
            task = self.tasks.get_task(task_id)
            if task["status"] in ("succeeded", "cancelled"):
                return {"status": task["status"]}
            if task["status"] == "expired":
                self.tasks.requeue_for_retry(task_id, actor_id=task["owner_user_id"])
            lease = self.tasks.claim(task_id, lease_owner="feynman-worker", lease_seconds=120, allow_reclaim_active=False)
            conv_id, _, sid, tid, operation = task["subject_id"].split(":")
            audio = task["kind"] == "feynman_audio"
            self.teaching._check_authorization(task_id, audio=audio)
            with self.db.session() as sess:
                conv = self.owned(sess, conv_id, task["owner_user_id"])
                row, turn = sess.get(FeynmanSessionRow, sid), sess.get(FeynmanTurnRow, tid)
                if not row or not turn or row.state == "closed" or (
                        turn.audio_task_id if audio else row.task_id) != task_id:
                    raise terr("STALE_RESULT_DISCARDED")
                revision = row.revision
                context = load_json_field(row.context_json, {})
                self.verify_source(sess,conv,context)
                turns = list(sess.scalars(select(FeynmanTurnRow).where(
                    FeynmanTurnRow.session_id == sid).order_by(FeynmanTurnRow.turn_no)))
                history = [{"question": t.question, "answer": t.answer, "feedback": t.feedback,
                            "intent": t.kind} for t in turns]
                payload = {**context, "dialogue": history, "probe_count": row.probe_count,
                    "max_probes": MAX_PROBES, "intent": operation,
                    "evaluation_criteria": load_json_field(turn.criteria_json, [])}
                protocol_v2 = context.get("protocol_version") == 2
                from .feynman_protocol import state_from_turns, validate_evaluation
                flow = state_from_turns(turns) if protocol_v2 else None
                if flow:
                    payload["learning_state"] = flow
                if (protocol_v2 and operation in ("open", "answer")
                        and getattr(self.teaching, "memory", None)):
                    try:
                        from ..memory.learning import context as learning_context
                        from ..memory.learning import select_review_targets
                        plan = LessonBlueprint.model_validate_json(
                            sess.get(LessonBlueprintRow, conv_id).plan_json)
                        schedule = teaching_schedule(plan)
                        topics = review_topics(schedule, context.get("taught", []), plan.goals)
                        if topics:
                            learned = learning_context(sess, task["owner_user_id"], conv.course_id,
                                lesson=conv.lesson_id, content_version=context.get("content_version_id", ""),
                                limit=max(8, len(topics)))
                            guidance = select_review_targets(learned, topics)
                            if guidance:
                                payload["spaced_review_guidance"] = guidance
                    except Exception:
                        # Optional review guidance must never block a teaching turn.
                        pass
                asked_action = load_json_field(turn.evaluation_json, {}).get("_prompt_action", "explain")
                text = turn.public_text
            with ThreadPoolExecutor(max_workers=1) as pool:
                if audio:
                    adapter = self.teaching.tts_adapter_factory(task["owner_user_id"], task_id)
                    if not adapter:
                        raise terr("MODEL_CAPABILITY_UNAVAILABLE")
                    future = pool.submit(copy_context().run, adapter.synthesize, text)
                else:
                    adapter = self.teaching._model_adapter(task["owner_user_id"], task_id)
                    if not adapter:
                        raise terr("MODEL_CAPABILITY_UNAVAILABLE")
                    name = prompt_name(context, operation)
                    self._bind(task_id, name)
                    prompt = self.teaching.prompts.resolve_for_task(task_id, name)["template_snapshot"]
                    if operation == "help":
                        payload.pop("spaced_review_guidance", None)
                    elif payload.get("spaced_review_guidance"):
                        prompt += "\n" + SPACED_REVIEW_POLICY
                    from .assessment_contract import ASSESSMENT_POLICY
                    prompt += "\n" + ASSESSMENT_POLICY
                    if operation == "open":
                        from ..materials.study_sources import QUESTION_POLICY
                        prompt += "\n" + QUESTION_POLICY
                    from ..characters.service import segment_character
                    with self.db.session() as sess:
                        character = segment_character(sess, conv_id, row.after_step_no)
                    isolated = bool(context.get("persona_isolation_version"))
                    if character and not isolated:
                        from ..characters.expression import content_expression
                        from ..characters.delivery import DELIVERY_POLICY, delivery_context, expression_payload
                        prompt += "\n人物表达约束：" + DELIVERY_POLICY
                        expression = content_expression(self.db, adapter, character["profile"],
                            binding=(conv_id, row.after_step_no))
                        payload["character_expression"] = expression_payload(character["profile"], expression,
                            event="question" if operation == "open" else "unsure" if operation == "help" else "feedback",
                            continuity=delivery_context(self.db, conv_id, row.after_step_no, history=history))
                    elif not isolated and getattr(self.teaching, "memory", None):
                        payload["personal_memory_data"] = self.teaching.memory.context(
                            task["owner_user_id"], course=conv.course_id, lesson=conv.lesson_id)
                    def validate(ctx, candidate):
                        if operation == "open":
                            from ..materials.study_sources import validate_source_choices
                            validate_source_choices([candidate], context.get("source_candidates", []),
                                candidate.source_rejections, feynman=True)
                        if protocol_v2 and operation != "open":
                            return validate_evaluation(candidate, flow, intent=operation,
                                count=row.probe_count, max_probes=MAX_PROBES)
                        if operation != "open" and row.probe_count >= MAX_PROBES and candidate.decision == "probe":
                            raise ModelRetry("已达到五次追问上限，必须给出反馈和补讲，不得提出新题。")
                        if operation == "help" and candidate.next_question:
                            raise ModelRetry("当前是求助，只解释原题，不新增正式问题。")
                        if operation == "answer" and row.probe_count < MAX_PROBES and candidate.decision == "review":
                            raise ModelRetry("尚未达到五次上限，仍有缺口时应继续一次针对性追问。")
                        return candidate
                    method = feynman_agent.question if operation == "open" else feynman_agent.evaluate
                    options = {"protocol_version": 2} if protocol_v2 and operation != "open" else {}
                    question_deps = replace(deps_for_source(adapter), omit_content_trace=True)
                    if operation == "open" and context.get("source_snapshot"):
                        from ..materials.question_retrieval import (
                            POLICY as QUESTION_SEARCH_POLICY, conversation_question_search)
                        candidate_pool = context.setdefault("source_candidates", [])
                        tool = conversation_question_search(self.teaching, question_deps, conv_id,
                            task_id, lease, context["source_snapshot"],
                            [{"lesson_id": conv.lesson_id, "goal": "解释当前已讲知识"}],
                            candidate_pool, purpose="assessment", feynman=True)
                        if tool:
                            prompt += "\n" + QUESTION_SEARCH_POLICY
                            options.setdefault("tools", []).append(tool)
                            payload["source_candidates"] = candidate_pool
                    if character and not isolated:
                        from ..characters.persona import reference_tool
                        tool=reference_tool(character["profile"])
                        if tool: options.setdefault("tools", []).append(tool)
                    future = pool.submit(copy_context().run, method, adapter, FeynmanInput.from_messages([
                        {"role": "system", "content": prompt}, {"role": "user", "content": encode(payload)}]),
                        question_deps,
                        output_validator=validate, **options)
                while True:
                    try:
                        candidate = future.result(timeout=30)
                        break
                    except TimeoutError:
                        self.tasks.heartbeat(lease, lease_seconds=120)
            if audio:
                cues = candidate.get("subtitle")
                if (not cues or not all(isinstance(c, dict) and isinstance(c.get("begin_ms"), (int, float))
                    and isinstance(c.get("end_ms"), (int, float)) and math.isfinite(c["begin_ms"])
                    and math.isfinite(c["end_ms"]) and 0 <= c["begin_ms"] < c["end_ms"] <= 120000 for c in cues)):
                    raise terr("OUTPUT_INVALID")
                data = bytes.fromhex(candidate.get("audio_hex", ""))
                if not data or len(data) > 8 * 1024 * 1024:
                    raise terr("OUTPUT_INVALID")
                audio_id = "aud_" + uuid.uuid4().hex
                storage_key = f"audio/{conv_id}/{audio_id}.bin"
                self.teaching.storage.put(storage_key, data)
            else:
                candidate = candidate.payload
            created_audio = []
            def publish(sess):
                self.teaching._check_authorization(task_id, audio=audio, session=sess)
                current_conv = self.owned(sess, conv_id, task["owner_user_id"], lock=True)
                self.verify_source(sess,current_conv,context)
                current = sess.get(FeynmanSessionRow, sid, with_for_update=True)
                saved = sess.get(FeynmanTurnRow, tid)
                if (current.revision != revision or current.state == "closed"
                    or current_conv.generation != context["generation"]):
                    raise terr("STALE_RESULT_DISCARDED")
                if audio:
                    audio_version = (sess.scalar(select(func.max(AudioAssetRow.text_version_no)).where(
                        AudioAssetRow.conversation_id == conv_id, AudioAssetRow.step_no == -1)) or 0) + 1
                    sess.add(AudioAssetRow(audio_id=audio_id, conversation_id=conv_id, generation=1,
                        step_no=-1, text_version_no=audio_version,
                        text_sha256=hashlib.sha256(text.encode()).hexdigest(), storage_key=storage_key,
                        audio_format=candidate.get("audio_format", "mp3"), duration_ms=int(max(c["end_ms"] for c in cues)),
                        subtitle_json=encode(cues), status="ready"))
                    sess.flush()
                    saved.audio_id = audio_id
                    self._release(current, saved)
                    return
                if operation == "open":
                    from ..materials.study_sources import attach_provenance
                    saved.question, saved.criteria_json = candidate.question, encode(candidate.criteria)
                    saved.evaluation_json = encode({"_source": attach_provenance(
                        candidate, context.get("source_candidates", []))})
                    saved.public_text = candidate.question
                    output = saved
                elif operation == "help":
                    saved.feedback = saved.public_text = candidate.feedback
                    if protocol_v2:
                        saved.evaluation_json = encode({**candidate.model_dump(), "_accepted_answer": True,
                            "_source": load_json_field(saved.evaluation_json, {}).get("_source")})
                    output = saved
                else:
                    saved.feedback = candidate.feedback
                    evaluation = candidate.model_dump()
                    if protocol_v2:
                        evaluation.update(_accepted_answer=True, _answered_action=asked_action,
                            _prompt_action=asked_action,
                            _assisted=bool(flow["help_level"] or flow["mode"] == "learn"))
                    answered_at = load_json_field(saved.evaluation_json, {}).get("_answered_at")
                    saved.evaluation_json = encode({**evaluation, "_answered_at": answered_at,
                        "_source": load_json_field(saved.evaluation_json, {}).get("_source")})
                    if getattr(self.teaching, "memory", None):
                        from ..memory.service import enqueue
                        evidence_payload = {"question": saved.question, "answer": saved.answer,
                             "feedback": candidate.feedback,
                             **({"scope": "lesson", "lesson_id": current_conv.lesson_id,
                                 "assisted": evaluation["_assisted"], "phase": asked_action,
                                 "dimension_evidence": evaluation["dimension_evidence"],
                                 "review_items": evaluation["review_items"],
                                 "knowledge_point_ids": checkpoint_concept_ids(context),
                                 "content_version": current_conv.lesson_content_version_id,
                                 "learning_result": candidate.decision} if protocol_v2 else {})}
                        source_id = enqueue(sess, current_conv.owner_user_id, "course_feynman", tid,
                            evidence_payload,
                            course=current_conv.course_id)
                        if protocol_v2 and answered_at:
                            from ..memory.learning import record_feynman
                            record_feynman(sess, current_conv.owner_user_id, current_conv.course_id,
                                tid, datetime.fromisoformat(answered_at), evidence_payload, [source_id])
                        from ..characters.service import segment_character
                        character = segment_character(sess, conv_id, current.after_step_no)
                        if source_id and character:
                            enqueue(sess, current_conv.owner_user_id, "character_feedback_candidate", source_id,
                                {"source_id": source_id}, course=current_conv.course_id, card=character["card_id"])
                    number = max(t.turn_no for t in sess.scalars(select(FeynmanTurnRow).where(
                        FeynmanTurnRow.session_id == sid))) + 1
                    probing = candidate.decision == "probe"
                    if probing and current.probe_count >= MAX_PROBES:
                        raise terr("OUTPUT_INVALID")
                    output = FeynmanTurnRow(turn_id="fturn_" + uuid.uuid4().hex, session_id=sid,
                        turn_no=number, kind="question" if probing else "feedback",
                        question=candidate.next_question, criteria_json=saved.criteria_json,
                        evaluation_json=encode({**candidate.model_dump(),
                            **({"_prompt_action": candidate.action} if protocol_v2 else {})}),
                        public_text=candidate.feedback + ("\n\n" + candidate.next_question if probing else ""))
                    sess.add(output)
                    if not probing:
                        current.result = "understood" if candidate.decision == "understood" else "review" if candidate.decision == "review" else "unverified"
                if context.get("persona_isolation_version"):
                    from ..characters.service import segment_character
                    from ..characters.delivery import assessment_salutation
                    bound_character = segment_character(sess, conv_id, row.after_step_no)
                    if bound_character:
                        output.public_text = assessment_salutation(bound_character["profile"]) + output.public_text
                sess.flush()
                current.state = "audio_pending"
                if context["audio_required"]:
                    factory = self.teaching.tts_adapter_factory
                    config = factory.config_for_owner(task["owner_user_id"]) if getattr(factory, "config_for_owner", None) else None
                    queued = self.tasks.create_task(owner_user_id=task["owner_user_id"], kind="feynman_audio",
                        subject_id=f"{conv_id}:feynman:{sid}:{output.turn_id}:audio",
                        idempotency_key=f"feynman-audio:{output.turn_id}", stage="tts", lane="interactive",
                        deadline_seconds=180, model_config_version=f"{config['config_id']}:{config['config_version']}" if config else None,
                        session=sess)
                    self.teaching.authorization.bind_task(sess, current_conv, queued["task_id"], audio=True)
                    output.audio_task_id = queued["task_id"]
                    created_audio.append(queued["task_id"])
                else:
                    self._release(current, output)
            result = self.tasks.submit_result(lease, stage="tts" if audio else "generate",
                status="succeeded", publish=publish)
            if not result.get("accepted") and storage_key:
                self.teaching.storage.delete(storage_key)
            for ident in created_audio:
                self.kick(ident)
            return {"status": "succeeded" if result.get("accepted") else "stale_discarded"}
        except Exception as exc:
            if storage_key:
                self.teaching.storage.delete(storage_key)
            if lease:
                self.tasks.submit_result(lease, stage="tts" if task["kind"] == "feynman_audio" else "generate",
                    status="failed", error_code=self.teaching._controlled_failure(exc))
            return {"status": "failed", "error_code": self.teaching._controlled_failure(exc)}
        finally:
            if adapter and hasattr(adapter, "close"):
                adapter.close()
            with self.lock:
                self.running.discard(task_id)

    def _release(self, row, turn):
        if turn.published:
            return
        if turn.kind == "question" and turn.turn_no > 0:
            if row.probe_count >= MAX_PROBES:
                raise terr("OUTPUT_INVALID")
            row.probe_count += 1
        turn.published = True
        row.state = "feedback" if turn.kind == "feedback" else "waiting"
        row.revision += 1
