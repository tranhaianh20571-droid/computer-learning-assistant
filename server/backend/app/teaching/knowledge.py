"""Deterministic completion from owned, version-bound presentation evidence."""
import hashlib
import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ..planning.models import ConversationRow, CourseRow, LessonRow, MessageRow, load_json_field
from .errors import terr
from .feynman import checkpoints
from .learning_models import (ChannelPresentationRow, FeynmanSessionRow, LessonBlueprintRow,
                              SegmentCoverageRow, TaughtKnowledgeRow)
from .learning_outputs import LessonBlueprint, teaching_schedule
from .models import AudioAssetRow, BoardSceneRow, TeachingStepRow

Count = Annotated[int, Field(strict=True, ge=0, le=120000)]


class CoverageIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    step_id: str = Field(min_length=1, max_length=64)
    text_version: int = Field(strict=True, ge=1)
    audio_id: str = Field(default="", max_length=64)
    client_seq: int = Field(strict=True, ge=1, le=9007199254740991)
    intervals: list[tuple[Count, Count]] = Field(default_factory=list, max_length=100)
    visible_chars: Count
    board_complete: bool

    @model_validator(mode="after")
    def ordered(self):
        if any(start >= end for start, end in self.intervals):
            raise ValueError("nonempty forward intervals required")
        return self


class KnowledgeCompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    checkpoint_id: str = Field(min_length=1, max_length=64)
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ChannelIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    generation: int = Field(strict=True, ge=1)
    client_seq: int = Field(strict=True, ge=1, le=9007199254740991)
    visible_chars: Count
    position_ms: Count
    audio_id: str = Field(default="", max_length=64)
    paused: bool = True


class LessonTitleIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    title: str = Field(min_length=1, max_length=200)
    expected_title: str = Field(max_length=200)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def merge_intervals(values):
    merged = []
    for start, end in sorted(values, key=lambda pair: (pair[0], pair[1])):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


class KnowledgeService:
    def __init__(self, teaching):
        self.teaching, self.db = teaching, teaching.db

    def owned(self, sess, conversation_id, owner, *, lock=False):
        conv = sess.get(ConversationRow, conversation_id, with_for_update=lock)
        course = sess.get(CourseRow, conv.course_id) if conv else None
        lesson = sess.get(LessonRow, conv.lesson_id) if conv else None
        if (not conv or conv.owner_user_id != owner or conv.deleted_at or conv.kind != "teaching"
                or not course or course.owner_user_id != owner or course.deleted_at or not lesson
                or course.current_outline_version_id != conv.outline_version_id
                or lesson.outline_version_id != conv.outline_version_id):
            raise terr("conversation_not_found", status=404)
        return conv, lesson

    def audio_required(self, sess, conv):
        consent = self.teaching.authorization.require(sess, conv)
        if consent:
            return bool(consent.tts_root_grant_id)
        return bool(self.teaching.tts_adapter_factory and not self.teaching.authorization.enforced)

    def coverage(self, conversation_id, owner, value: CoverageIn):
        with self.db.session() as sess:
            conv, _ = self.owned(sess, conversation_id, owner, lock=True)
            required = self.audio_required(sess, conv)
            step = sess.get(TeachingStepRow, value.step_id)
            if (not step or step.conversation_id != conversation_id or step.generation != 1
                    or step.status != "ready" or step.text_version_no != value.text_version
                    or not step.board_scene_id or value.visible_chars > len(step.text)):
                raise terr("state_invalid", status=409)
            limit = len(step.text)
            if required:
                self.teaching.authorization.require(sess, conv, audio=True)
                audio = sess.get(AudioAssetRow, step.audio_id) if step.audio_id else None
                if (not audio or audio.status != "ready" or audio.audio_id != value.audio_id
                        or audio.text_version_no != step.text_version_no
                        or audio.text_sha256 != digest(step.text) or audio.duration_ms <= 0):
                    raise terr("state_invalid", status=409)
                limit = audio.duration_ms
            elif value.audio_id:
                raise terr("state_invalid", status=409)
            if any(end > limit + (250 if required else 0) for _, end in value.intervals):
                raise terr("invalid_field")
            key = (conversation_id, step.step_no, value.text_version, value.audio_id)
            row = sess.get(SegmentCoverageRow, key)
            if row is None:
                row = SegmentCoverageRow(conversation_id=conversation_id, step_no=step.step_no,
                    text_version=value.text_version, audio_id=value.audio_id, client_seq=0,
                    intervals_json="[]", visible_chars=0, board_complete=False)
                sess.add(row)
            if value.client_seq <= row.client_seq:
                return {"saved": True, "client_seq": row.client_seq, "deduped": True}
            row.client_seq = value.client_seq
            intervals = merge_intervals(load_json_field(row.intervals_json, []) + list(value.intervals))
            if len(intervals) > 200:
                raise terr("invalid_field")
            row.intervals_json = json.dumps(intervals)
            row.visible_chars = max(row.visible_chars, value.visible_chars)
            row.board_complete |= value.board_complete and value.visible_chars == len(step.text)
        return {"saved": True, "client_seq": value.client_seq}

    def complete(self, conversation_id, owner, value: KnowledgeCompleteIn):
        with self.db.session() as sess:
            conv, _ = self.owned(sess, conversation_id, owner, lock=True)
            required = self.audio_required(sess, conv)
            plan = sess.get(LessonBlueprintRow, conversation_id)
            if not plan or digest(plan.plan_json) != value.plan_hash:
                raise terr("state_invalid", status=409)
            point = next((item for item in checkpoints(plan.plan_json)
                          if item["checkpoint_id"] == value.checkpoint_id), None)
            if not point:
                raise terr("step_not_found", status=404)
            identity = digest(json.dumps([owner, conv.lesson_id, conv.lesson_content_version_id,
                value.plan_hash, value.checkpoint_id]))
            old = sess.get(TaughtKnowledgeRow, identity)
            if old:
                return {"saved": True, "record_id": old.record_id, "deduped": True}
            schedule = teaching_schedule(LessonBlueprint.model_validate_json(plan.plan_json))
            numbers = [number for number, part in enumerate(schedule, 1)
                if number <= point["after_step_no"] and part.phase != "summary"
                and (part.knowledge_point_id in point["knowledge_point_ids"] if point.get("knowledge_point_ids")
                     else part.knowledge_point_id == point["knowledge_point_id"] if point.get("knowledge_point_id")
                     else bool(set(part.goal_ids) & set(point["goal_ids"])))]
            if not numbers:
                raise terr("state_invalid", status=409)
            expected_scope_ids = set(point.get("scope_capability_ids", []))
            sources, summary = [], ""
            recorded_scope_ids = set()
            for number in numbers:
                step = sess.scalar(select(TeachingStepRow).where(
                    TeachingStepRow.conversation_id == conversation_id,
                    TeachingStepRow.generation == 1, TeachingStepRow.step_no == number))
                if not step or step.status != "ready" or not step.board_scene_id:
                    raise terr("state_invalid", "知识点尚未完整讲完。", status=409)
                audio_id, limit, tolerance = "", len(step.text), 0
                if required:
                    audio = sess.get(AudioAssetRow, step.audio_id) if step.audio_id else None
                    if (not audio or audio.status != "ready" or audio.text_version_no != step.text_version_no
                            or audio.text_sha256 != digest(step.text) or audio.duration_ms <= 0):
                        raise terr("state_invalid", status=409)
                    audio_id, limit, tolerance = audio.audio_id, audio.duration_ms, 250
                coverage = sess.get(SegmentCoverageRow,
                    (conversation_id, number, step.text_version_no, audio_id))
                spans = load_json_field(coverage.intervals_json, []) if coverage else []
                if (not coverage or coverage.visible_chars < len(step.text) or not coverage.board_complete
                        or len(spans) != 1 or spans[0][0] > 1 or spans[0][1] < limit - tolerance):
                    raise terr("state_invalid", "知识点尚未完整讲完。", status=409)
                scene = sess.get(BoardSceneRow, step.board_scene_id)
                if not scene or scene.conversation_id != conversation_id or scene.step_no != number:
                    raise terr("state_invalid", status=409)
                if number == point["after_step_no"]:
                    recap = load_json_field(scene.scene_json, {}).get("knowledge_recap") or {}
                    summary = recap.get("summary", "")[:200]
                sources.append({"step_id": step.step_id, "step_no": number,
                    "text_version": step.text_version_no, "audio_id": audio_id,
                    "scene_id": step.board_scene_id})
                step_scope_ids = set(load_json_field(step.covered_scope_capability_ids_json, []))
                planned_scope_ids = {ident for part in schedule[number - 1:number]
                                     for ident in part.scope_capability_ids}
                if step_scope_ids != planned_scope_ids:
                    raise terr("state_invalid", "讲解步骤未确认覆盖其分配的考纲能力。", status=409)
                recorded_scope_ids.update(step_scope_ids)
            if recorded_scope_ids != expected_scope_ids:
                raise terr("state_invalid", "考纲能力与完整讲授记录不匹配。", status=409)
            from ..planning.models import utcnow
            sess.execute(insert(TaughtKnowledgeRow).values(record_id=identity, owner_user_id=owner,
                course_id=conv.course_id, lesson_id=conv.lesson_id,
                content_version_id=conv.lesson_content_version_id, plan_hash=value.plan_hash,
                checkpoint_id=value.checkpoint_id, conversation_id=conversation_id,
                after_step_no=point["after_step_no"], title="、".join(point["goal_labels"])[:200],
                summary=summary, source_json=json.dumps(sources),
                scope_hash=load_json_field(plan.context_json, {}).get("unit_scope_hash", ""),
                scope_capability_ids_json=json.dumps(sorted(recorded_scope_ids)),
                completed_at=utcnow()
            ).on_conflict_do_nothing(index_elements=["record_id"]))
        return {"saved": True, "record_id": identity}

    def view(self, conversation_id, owner, *, after=0, limit=30):
        with self.db.session() as sess:
            conv, lesson = self.owned(sess, conversation_id, owner)
            plan = sess.get(LessonBlueprintRow, conversation_id)
            plan_hash = digest(plan.plan_json) if plan else ""
            query = select(TaughtKnowledgeRow).where(TaughtKnowledgeRow.owner_user_id == owner,
                TaughtKnowledgeRow.lesson_id == conv.lesson_id,
                TaughtKnowledgeRow.course_id == conv.course_id,
                TaughtKnowledgeRow.content_version_id == conv.lesson_content_version_id,
                TaughtKnowledgeRow.plan_hash == plan_hash)
            rows = list(sess.scalars(query.order_by(TaughtKnowledgeRow.after_step_no)))
            data = []
            for row in rows:
                if row.after_step_no <= after:
                    continue
                quiz = sess.scalar(select(FeynmanSessionRow).where(
                    FeynmanSessionRow.conversation_id == row.conversation_id,
                    FeynmanSessionRow.checkpoint_id == row.checkpoint_id))
                status = ("passed" if quiz.result == "understood" else "skipped" if quiz.result == "skipped"
                          else "review" if quiz.state == "closed" or quiz.result == "review"
                          else "pending") if quiz else "pending"
                data.append({"record_id": row.record_id, "title": row.title, "summary": row.summary,
                    "completed_at": row.completed_at.isoformat(), "assessment": status,
                    "after_step_no": row.after_step_no, "conversation_id": row.conversation_id,
                    "sources": load_json_field(row.source_json, [])})
            return {"lesson_id": conv.lesson_id, "lesson_title": lesson.title_ref or lesson.goal_ref,
                "plan_hash": plan_hash, "total": len(rows), "data": data[:limit],
                "next_after": data[limit - 1]["after_step_no"] if len(data) > limit else None}

    def progress(self, conversation_id, owner):
        with self.db.session() as sess:
            self.owned(sess, conversation_id, owner)
            steps = {row.step_no: row for row in sess.scalars(select(TeachingStepRow).where(
                TeachingStepRow.conversation_id == conversation_id, TeachingStepRow.generation == 1))}
            result = {}
            for row in sess.scalars(select(SegmentCoverageRow).where(
                    SegmentCoverageRow.conversation_id == conversation_id)):
                step = steps.get(row.step_no)
                if (not step or row.text_version != step.text_version_no
                        or row.audio_id and row.audio_id != step.audio_id):
                    continue
                audio = sess.get(AudioAssetRow, row.audio_id) if row.audio_id else None
                limit = audio.duration_ms if audio else len(step.text)
                spans = merge_intervals(load_json_field(row.intervals_json, []))
                fraction = min(1., sum(end-start for start, end in spans)/max(1, limit))
                previous = result.get(str(row.step_no), {})
                if fraction >= previous.get("fraction", 0):
                    result[str(row.step_no)] = {"visible_chars": row.visible_chars, "fraction": fraction,
                        "board_complete": row.board_complete}
            return result

    def rename(self, conversation_id, owner, value: LessonTitleIn):
        with self.db.session() as sess:
            conv, _ = self.owned(sess, conversation_id, owner, lock=True)
            lesson = sess.get(LessonRow, conv.lesson_id, with_for_update=True)
            if (lesson.title_ref or lesson.goal_ref) != value.expected_title:
                raise terr("state_invalid", status=409)
            title = value.title.strip()
            if not title or any(ord(char) < 32 for char in title):
                raise terr("invalid_field")
            lesson.title_ref = title
            audit = getattr(self.teaching.disclosures, "audit", None)
            if audit:
                audit.append("config.changed", actor_id=owner,
                    object_type="lesson", object_id=lesson.lesson_id, result="success", session=sess)
        return {"lesson_title": title}

    def save_channel(self, conversation_id, owner, value: ChannelIn):
        with self.db.session() as sess:
            conv, _ = self.owned(sess, conversation_id, owner, lock=True)
            if conv.generation != value.generation or conv.session_state != "awaiting_resume":
                raise terr("state_invalid", status=409)
            answer = sess.scalar(select(MessageRow).where(MessageRow.conversation_id == conversation_id,
                MessageRow.generation == value.generation, MessageRow.role == "assistant")
                .order_by(MessageRow.created_at.desc()))
            if not answer or value.visible_chars > len(answer.content_ref):
                raise terr("state_invalid", status=409)
            if self.audio_required(sess, conv) and value.visible_chars and not value.audio_id:
                raise terr("state_invalid", status=409)
            key = f"followup:{value.generation}"
            row = sess.get(ChannelPresentationRow, (conversation_id, key))
            if row and value.client_seq <= row.client_seq:
                return {"saved": True, "deduped": True}
            if value.audio_id:
                audio = sess.get(AudioAssetRow, value.audio_id)
                if (not audio or audio.conversation_id != conversation_id or audio.generation != value.generation
                        or audio.step_no != 0 or audio.status != "ready"
                        or audio.text_sha256 != digest(answer.content_ref)
                        or value.position_ms > audio.duration_ms+250):
                    raise terr("state_invalid", status=409)
            if row is None:
                row = ChannelPresentationRow(conversation_id=conversation_id, channel_key=key)
                sess.add(row)
            row.client_seq, row.text_hash = value.client_seq, digest(answer.content_ref)
            row.audio_id, row.visible_chars = value.audio_id, value.visible_chars
            row.position_ms, row.paused = value.position_ms, value.paused
        return {"saved": True}

    def channel(self, sess, conv):
        row = sess.get(ChannelPresentationRow, (conv.conversation_id, f"followup:{conv.generation}"))
        return {"visible_chars": row.visible_chars, "position_ms": row.position_ms,
                "audio_id": row.audio_id, "paused": row.paused, "client_seq": row.client_seq} if row else None

    def channel_state(self, conversation_id, owner):
        with self.db.session() as sess:
            conv, _ = self.owned(sess, conversation_id, owner)
            return self.channel(sess, conv)
