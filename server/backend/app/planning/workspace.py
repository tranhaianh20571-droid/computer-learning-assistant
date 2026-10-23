"""Durable owner-scoped planning recovery. Reads never launch model work."""
import json
from datetime import timedelta
from sqlalchemy import select, or_
from ..logging_audit.models import TaskStateRow
from ..tasks.service import LearningTaskRow
from .models import PlanningTaskStateRow, LearningRouteRow, CourseRow, load_json_field, utcnow
from .locking import planning_target
from .errors import error

from .retry_policy import AUTO_RETRY_LIMIT, automatic_retry_eligible, missing_courses


class PlanningWorkspace:
    def __init__(self, planning):
        self.planning = planning
        self.db = planning.db

    def current(self, owner):
        with self.db.session() as sess:
            row = sess.scalar(select(PlanningTaskStateRow).where(PlanningTaskStateRow.owner_user_id == owner)
                              .order_by(PlanningTaskStateRow.created_at.desc(), PlanningTaskStateRow.task_id.desc()).limit(1))
            if not row or row.abandoned_at:
                return {"planning": None}
            parent = sess.get(CourseRow if row.target_kind == "course" else LearningRouteRow, row.target_id)
            if not parent or parent.deleted_at:
                return {"planning": None}
            ident = row.task_id
        return {"planning": self.planning.get_planning_state(ident, actor_id=owner)}

    def _owned(self, sess, ident, owner, *, lock=False):
        row = sess.get(PlanningTaskStateRow, ident, with_for_update=lock)
        if not row or row.owner_user_id != owner:
            raise error("task_not_found", status=404)
        return row

    def resume(self, ident, owner, *, automatic=False, dispatch=True):
        # Target -> task -> planning -> draft is the publication/abandon lock order.
        with self.db.session() as sess:
            original = self._owned(sess, ident, owner)
            _, confirmed = planning_target(sess, original.target_kind, original.target_id, owner)
            if original.abandoned_at:
                raise error("TASK_CANCELLED", status=409)
            if confirmed:
                return self.planning.get_planning_state(ident, actor_id=owner)
            planning_target(sess, original.target_kind, original.target_id, owner, mutable=True)
            task = sess.get(LearningTaskRow, ident, with_for_update=True)
            row = self._owned(sess, ident, owner, lock=True)
            sess.refresh(row)
            if row.abandoned_at or task.cancel_requested or task.status == "cancelled":
                raise error("TASK_CANCELLED", status=409)
            if row.wait_state or task.status in ("succeeded", "leased", "running"):
                return self.planning.get_planning_state(ident, actor_id=owner)
            from .models import PlanningDraftRow
            draft = sess.get(PlanningDraftRow, row.draft_id, with_for_update=True) if row.draft_id else None
            content = load_json_field(draft.content_json, {}) if draft else {}
            event = sess.get(TaskStateRow, ident)
            now = utcnow()
            expired = task.deadline_at is not None and task.deadline_at <= now
            retryable = task.status in ("failed", "expired", "partial") or (task.status == "queued" and expired)
            if automatic:
                latest = sess.scalar(select(PlanningTaskStateRow.task_id).where(
                    PlanningTaskStateRow.owner_user_id == owner,
                    PlanningTaskStateRow.target_id == row.target_id,
                ).order_by(PlanningTaskStateRow.created_at.desc(), PlanningTaskStateRow.task_id.desc()).limit(1))
                if latest != ident:
                    return self.planning.get_planning_state(ident, actor_id=owner)
                if retryable and (row.auto_retry_count >= AUTO_RETRY_LIMIT or not automatic_retry_eligible(
                        task, row, event.error_code if event else None, content)):
                    return self.planning.get_planning_state(ident, actor_id=owner)
            if task.status == "partial" and not missing_courses(content):
                return self.planning.get_planning_state(ident, actor_id=owner)
            if self.planning.authorization is not None:
                self.planning.authorization.prepare(sess, task, row, owner,
                    allow_refresh=not automatic or (task.status == "queued" and task.attempt == 0))
            if retryable:
                signals = load_json_field(row.scope_signals_json, {})
                if draft and missing_courses(content):
                    expected = signals.get("structure_revision")
                    if automatic and expected is not None and expected != draft.revision:
                        raise error("DRAFT_REVISION_MISMATCH", status=409)
                    signals["refine_draft"] = {"draft_id": draft.draft_id, "revision": draft.revision}
                    signals["structure_revision"] = draft.revision
                    row.scope_signals_json = json.dumps(signals)
                if automatic:
                    row.auto_retry_count += 1
                task.generation += 1
                if not (draft and missing_courses(content)):
                    row.process_json = "{}"
                task.status = "queued"
                task.lease_token = task.lease_owner = task.lease_until = None
                # Only an explicit action may establish a new task deadline.
                if not automatic:
                    task.deadline_at = now + timedelta(hours=1)
                task.updated_at = now
                self._event(sess, task, "retry_scheduled")
            row.background_enabled = True
        if dispatch:
            self.planning.background.start()
            self.planning.background.wake.set()
        return self.planning.get_planning_state(ident, actor_id=owner)

    def suspend_automatic(self, ident, owner, code):
        """Surface a controlled reauthorization/conflict failure without another model run."""
        from ..logging_audit.contracts import normalize_error_code
        with self.db.session() as sess:
            original = self._owned(sess, ident, owner)
            planning_target(sess, original.target_kind, original.target_id, owner, mutable=True)
            task = sess.get(LearningTaskRow, ident, with_for_update=True)
            row = self._owned(sess, ident, owner, lock=True)
            if task.status not in ("failed", "partial") or row.abandoned_at or task.cancel_requested:
                return
            row.background_enabled = False
            if code is not None:
                task.status = "failed"
                self._event(sess, task, "failed", error_code=normalize_error_code(code))

    def _event(self, sess, task, status, *, error_code=None):
        state = sess.get(TaskStateRow, task.task_id, with_for_update=True)
        code = "TASK_CANCELLED" if status == "cancelled" else error_code
        event_name = "task.stage.failed" if status == "failed" else "task." + status
        self.planning.events._append(sess, state, event_name=event_name, stage="generate",
            status=status, error_code=code, generation=task.generation, attempt=task.attempt)
        state.status = status
        state.error_code = code
        state.generation = task.generation
        state.attempt = task.attempt
        state.revision += 1

    def abandon(self, ident, owner):
        with self.db.session() as sess:
            original = self._owned(sess, ident, owner)
            if original.abandoned_at:
                return {"task_id": ident, "abandoned": True}
            planning_target(sess, original.target_kind, original.target_id, owner, mutable=True)
            related = list(sess.scalars(select(PlanningTaskStateRow).where(
                PlanningTaskStateRow.owner_user_id == owner,
                PlanningTaskStateRow.target_id == original.target_id)))
            ids = [r.task_id for r in related]
            tasks = list(sess.scalars(select(LearningTaskRow).where(
                LearningTaskRow.owner_user_id == owner,
                or_(LearningTaskRow.task_id.in_(ids),
                    (LearningTaskRow.subject_id == original.target_id) & LearningTaskRow.kind.in_(["planning_dialogue", "course_cover"])))
                                      .order_by(LearningTaskRow.task_id).with_for_update()))
            now = utcnow()
            if self.planning.audit:
                self.planning.audit.append("config.changed", actor_id=owner, object_type="planning_task",
                                           object_id=ident, result="abandoned")
            for task in tasks:
                task.cancel_requested = True
                task.status = "cancelled"
                task.generation += 1
                task.lease_token = task.lease_owner = task.lease_until = None
                task.updated_at = now
                self._event(sess, task, "cancelled")
            for row in related:
                sess.refresh(row, with_for_update=True)
                row.abandoned_at = now
                row.background_enabled = False
                row.wait_state = None
                row.answer_draft_json = "[]"
                row.process_json = "{}"
                row.answer_draft_revision += 1
        return {"task_id": ident, "abandoned": True, "task_ids": ids}

    def save_answers(self, ident, owner, revision, answers):
        with self.db.session() as sess:
            row = self._owned(sess, ident, owner, lock=True)
            if row.abandoned_at:
                raise error("TASK_CANCELLED", status=409)
            if row.wait_state != "awaiting_intake":
                raise error("state_invalid", status=409)
            if row.answer_draft_revision != revision:
                raise error("DRAFT_REVISION_MISMATCH", status=409)
            questions = {q["question_id"]: q for q in load_json_field(row.intake_json, {}).get("questions", [])}
            seen = set()
            clean = []
            for answer in answers:
                ident_q = answer.get("question_id")
                if not isinstance(ident_q, str):
                    raise error("invalid_field", status=400)
                selections = answer.get("selections", [])
                free = answer.get("free_text", "")
                question = questions.get(ident_q)
                if not question or ident_q in seen or not isinstance(selections, list) or not isinstance(free, str):
                    raise error("invalid_field", status=400)
                keys = {o["key"] for o in question["options"]}
                if any(not isinstance(s, str) or s not in keys for s in selections) or len(set(selections)) != len(selections):
                    raise error("invalid_field", status=400)
                if len(free) > 1000 or (question["kind"] == "single" and len(selections) > 1):
                    raise error("invalid_field", status=400)
                seen.add(ident_q)
                time_answer={}
                if question.get("kind")=="time_budget":
                    from .intake import validate_time_answer
                    if selections or free or answer.get("skipped"):
                        raise error("invalid_field", "学习时间安排不可跳过。")
                    time_answer["time_budget"]=validate_time_answer(answer.get("time_budget",{}),partial=True)
                clean.append({"question_id": ident_q, "selections": selections,
                    "free_text": "".join(c for c in free if c >= " " or c in "\n\t"),
                    "skipped": bool(answer.get("skipped")), "editing_free": bool(answer.get("editing_free")),
                    **time_answer})
            row.answer_draft_json = json.dumps(clean, ensure_ascii=False)
            row.answer_draft_revision += 1
            return {"revision": row.answer_draft_revision, "answers": clean}
