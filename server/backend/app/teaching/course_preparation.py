"""Persisted preparation DAG, bounded recovery and provider connection budgets."""

import hashlib
import json
import os
import random
from datetime import UTC, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from ..logging_audit.models import TaskStateRow
from ..planning.models import (
    CourseRow,
    CourseUnitRow,
    LessonRow,
    OutlineVersionRow,
    utcnow,
)
from ..tasks.service import LearningTaskRow
from .learning_models import PreparedFirstSegmentRow
from .preparation_models import (
    CoursePreparationRow,
    PreparationConnectionRow,
    PreparationItemRow,
)

TRANSIENT = frozenset(
    {
        "MODEL_TIMEOUT",
        "MODEL_CONNECTION_ERROR",
        "MODEL_PROVIDER_ERROR",
        "MODEL_RATE_LIMITED",
        "OUTPUT_INVALID",
        "TTS_TIMEOUT",
        "TASK_EXPIRED",
        "TASK_LEASE_EXPIRED",
        "TASK_DEADLINE_EXCEEDED",
        "LEASE_LOST",
        "TASK_STORAGE_BUSY",
        "WORKER_RECOVERY_FAILED",
        "timeout",
        "connection_error",
    }
)
SETTINGS = frozenset(
    {
        "MODEL_CREDENTIAL_INVALID",
        "MODEL_CAPABILITY_UNAVAILABLE",
        "MODEL_QUOTA_EXCEEDED",
        "CONFIG_NOT_FOUND",
        "config_not_found",
        "credential_invalid",
        "capability_unavailable",
        "CREDENTIAL_INVALID",
        "CAPABILITY_UNAVAILABLE",
        "CAPABILITY_TEST_FAILED",
        "DISCLOSURE_REQUIRED",
        "DISCLOSURE_REVOKED",
        "DISCLOSURE_EXPIRED",
        "disclosure_required",
        "disclosure_revoked",
        "disclosure_expired",
    }
)
ACTIVE = ("queued", "leased", "running")
RESOURCE_WAITS = {
    "storage": "PREPARATION_STORAGE_WAIT",
    "memory": "PREPARATION_MEMORY_WAIT",
    "capacity": "PREPARATION_CAPACITY_WAIT",
}
CONNECTION_SETTINGS = frozenset(
    {
        "MODEL_CREDENTIAL_INVALID",
        "CREDENTIAL_INVALID",
        "MODEL_QUOTA_EXCEEDED",
        "MODEL_CAPABILITY_UNAVAILABLE",
        "CAPABILITY_UNAVAILABLE",
    }
)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def register_course(sess, course):
    """Called in the outline publication transaction; never invokes a provider."""
    if not course.current_outline_version_id or course.deleted_at:
        return
    version = course.current_outline_version_id
    outline = sess.get(OutlineVersionRow, version)
    if not outline or outline.course_id != course.course_id:
        return
    if sess.get(CoursePreparationRow, version):
        return
    sess.add(
        CoursePreparationRow(
            outline_version_id=version,
            course_id=course.course_id,
            owner_user_id=course.owner_user_id,
        )
    )
    sess.flush()
    units = list(
        sess.scalars(
            select(CourseUnitRow)
            .where(CourseUnitRow.outline_version_id == version)
            .order_by(CourseUnitRow.order_index)
        )
    )
    for unit in units:
        targets = [("scope", None, unit.order_index * 100)]
        targets.extend(
            ("first", lesson.lesson_id, unit.order_index * 100 + lesson.order_index + 1)
            for lesson in sess.scalars(
                select(LessonRow)
                .where(LessonRow.unit_id == unit.unit_id)
                .order_by(LessonRow.order_index)
            )
        )
        for kind, lesson_id, priority in targets:
            sess.add(
                PreparationItemRow(
                    item_id=digest([version, kind, lesson_id or unit.unit_id]),
                    outline_version_id=version,
                    course_id=course.course_id,
                    owner_user_id=course.owner_user_id,
                    unit_id=unit.unit_id,
                    lesson_id=lesson_id,
                    kind=kind,
                    priority=priority,
                )
            )


def connection_key(owner, config):
    return digest([owner, config["config_id"], config["config_version"]])


def connection(sess, key, owner):
    sess.execute(
        insert(PreparationConnectionRow)
        .values(
            connection_key=key,
            owner_user_id=owner,
            failures=0,
            budget_day="",
            wire_requests=0,
        )
        .on_conflict_do_nothing()
    )
    row = sess.get(
        PreparationConnectionRow, key, with_for_update=True, populate_existing=True
    )
    probe = sess.get(LearningTaskRow, row.probe_task_id) if row.probe_task_id else None
    if row.probe_task_id and (not probe or probe.status not in ACTIVE):
        event = sess.get(TaskStateRow, probe.task_id) if probe else None
        blocked = bool(
            event
            and event.error_code in CONNECTION_SETTINGS
            and row.cooldown_until
            and aware(row.cooldown_until) > utcnow()
        )
        if not blocked:
            row.probe_task_id = None
        if not blocked and probe and probe.status == "succeeded":
            row.failures, row.cooldown_until = 0, None
    return row


def debit_wire(sess, task, *, config=None):
    """Shared with the native adapter; atomic actual-wire accounting survives retries."""
    if sess.bind.dialect.name != "postgresql":
        return
    item = sess.scalar(
        select(PreparationItemRow).where(PreparationItemRow.task_id == task.task_id)
    )
    if not item:
        return
    key = connection_key(task.owner_user_id, config) if config else item.connection_key
    row = connection(sess, key, task.owner_user_id)
    now = utcnow()
    if row.cooldown_until:
        if aware(row.cooldown_until) > now or row.probe_task_id not in (
            None,
            task.task_id,
        ):
            from ..planning.errors import error

            raise error("PREPARATION_CONNECTION_WAIT")
        row.probe_task_id = task.task_id
    day = now.date().isoformat()
    if row.budget_day != day:
        row.budget_day, row.wire_requests = day, 0
    limit = max(0, int(os.environ.get("COURSE_PREPARATION_DAILY_REQUEST_LIMIT", "500")))
    if row.wire_requests >= limit:
        from ..planning.errors import error

        raise error("PREPARATION_BUDGET_WAIT")
    row.wire_requests += 1
    row.updated_at = now


class CoursePreparation:
    def __init__(self, teaching):
        self.teaching = teaching
        self.db = teaching.db
        self.cursor = ""

    @property
    def enabled(self):
        return (
            self.teaching.preparation.enabled
            and self.teaching.authorization.automatic_enabled
            and getattr(self.teaching, "assessments", None) is not None
        )

    def promote(self, sess, course, lesson):
        if not self.enabled:
            return
        register_course(sess, course)
        for item in sess.scalars(
            select(PreparationItemRow).where(
                PreparationItemRow.outline_version_id
                == course.current_outline_version_id,
                PreparationItemRow.unit_id == lesson.unit_id,
                (PreparationItemRow.kind == "scope")
                | (PreparationItemRow.lesson_id == lesson.lesson_id),
            )
        ):
            item.priority = -2 if item.kind == "scope" else -1
            if item.state not in ("paused", "action_required", "cancelled"):
                item.next_run_at = utcnow()
            task = sess.get(LearningTaskRow, item.task_id) if item.task_id else None
            if task and task.status == "queued":
                task.lane = "interactive"

    def request_resume(self, sess, course, lesson):
        if not self.enabled:
            return
        from ..agents.task_binding import AgentBudgetRow

        for item in sess.scalars(
            select(PreparationItemRow)
            .where(
                PreparationItemRow.outline_version_id
                == course.current_outline_version_id,
                PreparationItemRow.unit_id == lesson.unit_id,
                (PreparationItemRow.kind == "scope")
                | (PreparationItemRow.lesson_id == lesson.lesson_id),
                PreparationItemRow.state == "paused",
            )
            .with_for_update()
        ):
            budget = sess.get(AgentBudgetRow, item.task_id) if item.task_id else None
            if (
                item.failures <= 4
                and item.error_code in TRANSIENT
                and budget
                and max(budget.logical_requests, budget.wire_requests)
                < budget.request_limit
                and self._available(sess, item)
            ):
                item.state, item.next_run_at = "retrying", utcnow()

    def link_started(self, sess, course, lesson, task_id):
        """A cold interactive first segment satisfies the same preparation obligation."""
        if not self.enabled:
            return
        item = sess.get(
            PreparationItemRow,
            digest([course.current_outline_version_id, "first", lesson.lesson_id]),
            with_for_update=True,
        )
        if not item or item.task_id:
            return
        config = self._config(course.owner_user_id, "first")
        if not config:
            return
        item.task_id, item.state = task_id, "running"
        item.connection_key = connection_key(course.owner_user_id, config)

    def reconcile(self, limit=4):
        if not self.enabled:
            return []
        with self.db.session() as sess:
            query = select(CourseRow).where(
                CourseRow.deleted_at.is_(None),
                CourseRow.current_outline_version_id.is_not(None),
                CourseRow.course_id > self.cursor,
            )
            if self._production_filter:
                from ..auth.models import UserRow
                from ..slice1.models import ServiceConfigRow

                configured = select(ServiceConfigRow.config_id).where(
                    ServiceConfigRow.owner_user_id == CourseRow.owner_user_id,
                    ServiceConfigRow.owner_scope == "user",
                    ServiceConfigRow.kind == "content_model",
                    ServiceConfigRow.is_active.is_(True),
                    ServiceConfigRow.encrypted_credentials != "",
                )
                for suffix in ("test", "invalid"):
                    configured = configured.where(
                        func.lower(ServiceConfigRow.endpoint).not_like(
                            "%://%." + suffix
                        ),
                        func.lower(ServiceConfigRow.endpoint).not_like(
                            "%://%." + suffix + "/%"
                        ),
                    )
                query = query.where(
                    configured.exists(),
                    select(UserRow.user_id)
                    .where(
                        UserRow.user_id == CourseRow.owner_user_id,
                        UserRow.status == "approved",
                    )
                    .exists(),
                )
            courses = list(
                sess.scalars(
                    query.order_by(CourseRow.course_id)
                    .limit(max(1, limit))
                    .with_for_update(skip_locked=True)
                )
            )
            for course in courses:
                register_course(sess, course)
            self.cursor = courses[-1].course_id if courses else ""
            # Cancel stale obligations even if the deleted course is never visited again.
            stale = list(
                sess.scalars(
                    select(PreparationItemRow)
                    .join(
                        CourseRow, CourseRow.course_id == PreparationItemRow.course_id
                    )
                    .where(
                        PreparationItemRow.state != "cancelled",
                        (CourseRow.deleted_at.is_not(None))
                        | (
                            CourseRow.current_outline_version_id
                            != PreparationItemRow.outline_version_id
                        ),
                    )
                    .limit(100)
                )
            )
            for item in stale:
                item.state = "cancelled"
                task = sess.get(LearningTaskRow, item.task_id) if item.task_id else None
                if task and task.status in ACTIVE:
                    task.cancel_requested = True
            ids = list(
                sess.scalars(
                    select(PreparationItemRow.item_id)
                    .join(
                        CourseRow, CourseRow.course_id == PreparationItemRow.course_id
                    )
                    .where(
                        CourseRow.deleted_at.is_(None),
                        CourseRow.current_outline_version_id
                        == PreparationItemRow.outline_version_id,
                        PreparationItemRow.state != "cancelled",
                        PreparationItemRow.next_run_at <= utcnow(),
                    )
                    .order_by(
                        PreparationItemRow.priority,
                        PreparationItemRow.next_run_at,
                        PreparationItemRow.item_id,
                    )
                    .limit(32)
                )
            )
        results = []
        for ident in ids:
            try:
                self._advance(ident)
            except Exception as exc:
                code = self.teaching._controlled_failure(exc)
                with self.db.session() as sess:
                    item = sess.get(PreparationItemRow, ident, with_for_update=True)
                    if item:
                        self._failure(sess, item, code)
            results.append({"item_id": ident})
        return results

    def _config(self, owner, kind):
        configs = self.teaching.configs
        if configs:
            from ..agents.model_routing import role_config

            view = role_config(
                configs, owner, "planning" if kind == "scope" else "teaching"
            )
            if self._production_filter and view:
                from urllib.parse import urlsplit

                host = (urlsplit(view.get("endpoint", "")).hostname or "").lower()
                if not host or host.endswith((".test", ".invalid")):
                    return None
            return view
        return self.teaching.preparation._model(owner)

    @property
    def _production_filter(self):
        return (
            self.teaching.server_managed
            and os.environ.get("LEARNING_DEV_SYNTHETIC_MODEL") != "1"
        )

    def eligible(self, sess, task):
        if not self.enabled:
            return True
        item = sess.scalar(
            select(PreparationItemRow).where(PreparationItemRow.task_id == task.task_id)
        )
        if not item:
            return True
        if item.state in ("cancelled", "paused", "action_required", "retrying"):
            return False
        if task.kind == "lesson_first_prepare" and task.status == "queued":
            reason = self.teaching.preparation.resource_wait_reason()
            if reason:
                self._wait_resource(item, reason)
                return False
            if item.error_code in RESOURCE_WAITS.values():
                if aware(item.next_run_at) > utcnow():
                    return False
                item.state, item.error_code = "waiting", None
        gate = sess.get(PreparationConnectionRow, self._gate_key(sess, item))
        probe = (
            sess.get(LearningTaskRow, gate.probe_task_id)
            if gate and gate.probe_task_id
            else None
        )
        return (
            not gate
            or not gate.cooldown_until
            or (
                aware(gate.cooldown_until) <= utcnow()
                and (
                    gate.probe_task_id in (None, task.task_id)
                    or not probe
                    or probe.status not in ACTIVE
                )
            )
        )

    def _wait_resource(self, item, reason):
        code = RESOURCE_WAITS[reason]
        if item.error_code != code:
            from ..logging_audit.metrics import record_task_diagnostic

            record_task_diagnostic(reason="resource_wait", error_code=code,
                                   task_id=item.task_id)
            item.updated_at = utcnow()
        item.state, item.error_code = "waiting", code
        item.next_run_at = utcnow() + timedelta(seconds=30)

    def note_resource_wait(self, task_id, reason):
        if not self.enabled:
            return
        with self.db.session() as sess:
            task = sess.get(LearningTaskRow, task_id)
            item = sess.scalar(select(PreparationItemRow).where(
                PreparationItemRow.task_id == task_id).with_for_update())
            if item and task and task.status == "queued" and not task.cancel_requested:
                self._wait_resource(item, reason)

    def note_failure(self, task_id, exc, *, config=None):
        """Persist only the bounded Retry-After delay, never provider content."""
        import math
        from email.utils import parsedate_to_datetime

        delay = None
        cause = exc
        for _ in range(8):
            if cause is None:
                break
            value = getattr(cause, "retry_after", None)
            try:
                if value:
                    try:
                        seconds = float(value)
                    except TypeError, ValueError:
                        seconds = (
                            aware(parsedate_to_datetime(str(value))) - utcnow()
                        ).total_seconds()
                    if math.isfinite(seconds):
                        delay = max(0, min(86400, seconds))
                elif getattr(cause, "code", "") in (
                    "MODEL_RATE_LIMITED",
                    "rate_limited",
                ):
                    delay = 60
            except TypeError, ValueError, OverflowError:
                pass
            if delay is not None:
                break
            cause = getattr(cause, "__cause__", None)
        if delay is None or not self.enabled:
            return
        with self.db.session() as sess:
            item = sess.scalar(
                select(PreparationItemRow).where(PreparationItemRow.task_id == task_id)
            )
            if not item:
                return
            key = (
                connection_key(item.owner_user_id, config)
                if config
                else item.connection_key
            )
            if not key:
                return
            gate = connection(sess, key, item.owner_user_id)
            until = utcnow() + timedelta(seconds=delay)
            gate.cooldown_until = (
                max(until, aware(gate.cooldown_until)) if gate.cooldown_until else until
            )
            gate.probe_task_id = None

    def _advance(self, ident):
        with self.db.session() as sess:
            item = sess.scalar(
                select(PreparationItemRow)
                .where(PreparationItemRow.item_id == ident)
                .with_for_update(skip_locked=True)
            )
            if (
                not item
                or item.state == "cancelled"
                or aware(item.next_run_at) > utcnow()
            ):
                return
            course = sess.get(CourseRow, item.course_id)
            if (
                not course
                or course.deleted_at
                or course.owner_user_id != item.owner_user_id
                or course.current_outline_version_id != item.outline_version_id
            ):
                item.state = "cancelled"
                return
            now = utcnow()
            item.next_run_at = now + timedelta(seconds=15)
            config = self._config(item.owner_user_id, item.kind)
            if not config:
                item.state, item.error_code = (
                    "action_required",
                    "MODEL_CAPABILITY_UNAVAILABLE",
                )
                return
            key = connection_key(item.owner_user_id, config)
            scopes = self.teaching.assessments.scopes
            scope = scopes.get(item.course_id, item.unit_id, item.owner_user_id)
            if item.kind == "first" and scope["status"] != "succeeded":
                item.state = "waiting"
                return
            if item.kind == "scope":
                inputs = digest(
                    [key, scope.get("input_hash") or self._scope_input(sess, item)]
                )
            else:
                from types import SimpleNamespace

                conv = SimpleNamespace(
                    owner_user_id=item.owner_user_id,
                    course_id=item.course_id,
                    lesson_id=item.lesson_id,
                    outline_version_id=item.outline_version_id,
                    conversation_id="prewarm",
                )
                running_task = (
                    sess.get(LearningTaskRow, item.task_id) if item.task_id else None
                )
                if running_task and running_task.kind == "lesson_step_generate":
                    from ..planning.models import ConversationRow

                    conv = (
                        sess.get(ConversationRow, running_task.subject_id.split(":")[0])
                        or conv
                    )
                text_key, audio_key, _ = self.teaching.preparation.keys(conv)
                inputs = digest([text_key, audio_key])
            if item.input_key and (
                item.input_key != inputs or item.connection_key != key
            ):
                previous = (
                    sess.get(LearningTaskRow, item.task_id) if item.task_id else None
                )
                if previous and previous.status in ACTIVE:
                    previous.cancel_requested = True
                    if (
                        previous.status == "queued"
                        or not previous.lease_until
                        or aware(previous.lease_until) <= now
                    ):
                        self.teaching.tasks._terminal(
                            sess,
                            previous,
                            status="cancelled",
                            error_code="TASK_CANCELLED",
                            now=now,
                        )
                    item.next_run_at = now + timedelta(seconds=30)
                    return
                item.task_id, item.artifact_id = None, None
                item.failures, item.state, item.error_code = 0, "waiting", None
            item.input_key, item.connection_key = inputs, key
            task = sess.get(LearningTaskRow, item.task_id) if item.task_id else None
            if (item.kind == "first" and task and task.kind == "lesson_first_prepare"
                    and task.status in ("failed", "expired")):
                cache = sess.scalar(select(PreparedFirstSegmentRow).where(
                    PreparedFirstSegmentRow.task_id == task.task_id))
                event = sess.get(TaskStateRow, task.task_id)
                if (cache and event
                        and event.error_code == "AGENT_LOOP_LIMIT"):
                    from time import monotonic
                    from ..planning.models import ConversationRow

                    current = (sess.get(ConversationRow, cache.consumed_conversation_id)
                        if cache.consumed_conversation_id else sess.scalar(
                            select(ConversationRow).where(
                                ConversationRow.owner_user_id == item.owner_user_id,
                                ConversationRow.course_id == item.course_id,
                                ConversationRow.lesson_id == item.lesson_id,
                                ConversationRow.outline_version_id == item.outline_version_id,
                                ConversationRow.kind == "teaching",
                                ConversationRow.deleted_at.is_(None),
                                ConversationRow.session_state == "active",
                            ).order_by(ConversationRow.created_at.desc()).limit(1)))
                    if (current and current.owner_user_id == item.owner_user_id
                            and current.course_id == item.course_id
                            and current.lesson_id == item.lesson_id
                            and current.outline_version_id == item.outline_version_id
                            and not current.deleted_at and current.session_state == "active"
                            and monotonic() - self.teaching.observed_sessions.get(
                                current.conversation_id, 0) <= 90):
                        foreground = sess.scalar(select(LearningTaskRow).where(
                            LearningTaskRow.owner_user_id == item.owner_user_id,
                            LearningTaskRow.kind == "lesson_step_generate",
                            LearningTaskRow.subject_id == f"{current.conversation_id}:1:1",
                            LearningTaskRow.cancel_requested.is_(False),
                            LearningTaskRow.status.in_(("queued", "failed", "expired")),
                            LearningTaskRow.attempt < 3,
                        ))
                        if foreground:
                            # Use the already authorized classroom task; never refill prewarm.
                            item.task_id, task = foreground.task_id, foreground
                            item.state, item.error_code = "retrying", None
            resource_wait = item.error_code in RESOURCE_WAITS.values()
            if item.kind == "first" and (
                not task or task.status == "queued" or resource_wait
            ):
                reason = self.teaching.preparation.resource_wait_reason()
                if reason:
                    self._wait_resource(item, reason)
                    return
                if resource_wait:
                    item.state = "retrying" if task and task.status in (
                        "failed", "expired") else "waiting"
                    item.error_code = None
            # Recover once from the former two-request cap, retaining the total budget.
            if (item.kind == "first" and item.state == "paused"
                    and item.error_code == "AGENT_LOOP_LIMIT" and task
                    and task.status == "failed" and task.attempt == 1):
                from ..agents.task_binding import AgentBudgetRow

                budget = sess.get(AgentBudgetRow, task.task_id)
                if (budget and budget.request_limit == 8
                        and max(budget.logical_requests, budget.wire_requests) == 2):
                    item.state = "retrying"
            if item.state in ("paused", "action_required") and item.task_id:
                item.next_run_at = now + timedelta(seconds=60)
                return
            if scope["status"] == "succeeded" and item.kind == "scope":
                if item.state != "ready":
                    self._success(sess, item)
                item.task_id, item.artifact_id, item.state = (
                    scope["task_id"],
                    scope["scope_id"],
                    "ready",
                )
                item.next_run_at = now + timedelta(minutes=5)
                return
            task = sess.get(LearningTaskRow, item.task_id) if item.task_id else None
            if task:
                if task.status == "succeeded":
                    if item.kind == "first":
                        if task.kind == "lesson_step_generate":
                            from ..planning.models import ConversationRow

                            current = sess.get(
                                ConversationRow, task.subject_id.split(":")[0]
                            )
                            step, _, valid = (
                                self.teaching._step_artifact(sess, current, 1)
                                if current
                                else (None, None, False)
                            )
                            if valid:
                                item.artifact_id, item.state = step.step_id, "ready"
                                item.next_run_at = now + timedelta(minutes=5)
                                return
                            if current:
                                repair = self.teaching._ensure_artifact_repair(
                                    sess, current
                                )
                                if (
                                    repair
                                    and repair.get("status") == "repairing"
                                    and repair.get("task_id")
                                ):
                                    from ..agents.task_binding import AgentBudgetRow

                                    previous = sess.get(AgentBudgetRow, task.task_id)
                                    replacement = sess.get(
                                        AgentBudgetRow,
                                        repair["task_id"],
                                        with_for_update=True,
                                    )
                                    if (
                                        previous
                                        and replacement
                                        and not (
                                            replacement.logical_requests
                                            or replacement.wire_requests
                                            or replacement.tool_calls
                                        )
                                    ):
                                        for name in (
                                            "logical_requests",
                                            "wire_requests",
                                            "tool_calls",
                                        ):
                                            setattr(
                                                replacement,
                                                name,
                                                getattr(previous, name),
                                            )
                                        replacement.request_limit = min(
                                            previous.request_limit, 8
                                        )
                                    item.task_id, item.state = (
                                        repair["task_id"],
                                        "running",
                                    )
                                else:
                                    item.state, item.error_code = (
                                        "paused",
                                        "WORKER_RECOVERY_FAILED",
                                    )
                                return
                        cache = sess.scalar(
                            select(PreparedFirstSegmentRow).where(
                                PreparedFirstSegmentRow.task_id == task.task_id
                            )
                        )
                        if not cache or cache.candidate_json == "{}":
                            self._failure(sess, item, "WORKER_RECOVERY_FAILED")
                            return
                        item.artifact_id = cache.cache_id
                        if (
                            cache.error_code
                            and cache.audio_key
                            and not cache.audio_object_key
                        ):
                            self._failure(sess, item, cache.error_code)
                            return
                    if item.state != "ready":
                        self._success(sess, item)
                    item.state, item.error_code = "ready", None
                    item.next_run_at = now + timedelta(minutes=5)
                    return
                if task.status in ACTIVE:
                    if task.deadline_at and aware(task.deadline_at) <= now:
                        self.teaching.tasks.sweep_expired_tasks(kinds=[task.kind])
                        sess.refresh(task)
                    else:
                        item.state = "waiting" if task.status == "queued" else "running"
                        return
                if task.status == "cancelled" or task.cancel_requested:
                    item.state = "cancelled"
                    return
                event = sess.get(TaskStateRow, task.task_id)
                code = (
                    event.error_code
                    if event and event.error_code
                    else "WORKER_RECOVERY_FAILED"
                )
                if item.state != "retrying":
                    self._failure(sess, item, code)
                    return
                if not self._available(sess, item):
                    return
                # Same task and AgentBudgetRow; never replenish requests by replacing a task.
                self.teaching.tasks.requeue_for_retry(
                    task.task_id,
                    actor_id=item.owner_user_id,
                    deadline_seconds=86400,
                )
                sess.refresh(task)
                cache = (
                    sess.scalar(
                        select(PreparedFirstSegmentRow).where(
                            PreparedFirstSegmentRow.task_id == task.task_id
                        )
                    )
                    if item.kind == "first"
                    else None
                )
                if cache:
                    cache.status, cache.error_code = "queued", None
                item.state = "running"
                return
            if not self._available(sess, item):
                return
            pending = sess.scalar(
                select(LearningTaskRow.task_id)
                .where(
                    LearningTaskRow.kind.in_(
                        ("unit_scope_generate", "lesson_first_prepare")
                    ),
                    LearningTaskRow.status.in_(ACTIVE),
                    LearningTaskRow.cancel_requested.is_(False),
                    LearningTaskRow.deadline_at > now,
                )
                .offset(7)
                .limit(1)
            )
            if pending:
                return
            if item.kind == "scope":
                created = scopes.generate(
                    item.course_id, item.unit_id, item.owner_user_id, dispatch=False
                )
            else:
                created = self.teaching.preparation.prepare_course(
                    item.course_id,
                    item.owner_user_id,
                    lesson_id=item.lesson_id,
                    persistent=True,
                )
            if not created.get("task_id"):
                item.state = (
                    "action_required"
                    if created["status"] in ("unconfigured", "authorization_required")
                    else "waiting"
                )
                item.error_code = created.get("error_code")
                return
            item.task_id, item.state = created["task_id"], "running"
            task = sess.get(LearningTaskRow, item.task_id)
            if item.priority < 0 and task.status == "queued":
                task.lane = "interactive"
            if item.kind == "first":
                cache = sess.scalar(
                    select(PreparedFirstSegmentRow).where(
                        PreparedFirstSegmentRow.task_id == item.task_id
                    )
                )
                if cache:
                    item.artifact_id = cache.cache_id
            gate = connection(sess, key, item.owner_user_id)
            if gate.cooldown_until:
                gate.probe_task_id = item.task_id

    def _scope_input(self, sess, item):
        from ..materials.study_sources import digest as source_digest

        course = sess.get(CourseRow, item.course_id)
        unit = sess.get(CourseUnitRow, item.unit_id)
        return source_digest(
            self.teaching.assessments.scopes.context(
                sess, course, unit, item.owner_user_id
            )
        )

    def _available(self, sess, item):
        row = connection(sess, self._gate_key(sess, item), item.owner_user_id)
        now = utcnow()
        if row.cooldown_until and (
            aware(row.cooldown_until) > now
            or row.probe_task_id not in (None, item.task_id)
        ):
            item.next_run_at = max(
                now + timedelta(seconds=15), aware(row.cooldown_until)
            )
            return False
        limit = max(
            0, int(os.environ.get("COURSE_PREPARATION_DAILY_REQUEST_LIMIT", "500"))
        )
        if (
            not limit
            or row.budget_day == now.date().isoformat()
            and row.wire_requests >= limit
        ):
            item.next_run_at = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            return False
        return True

    def _failure(self, sess, item, code):
        now = utcnow()
        if code in RESOURCE_WAITS.values():
            self._wait_resource(item, next(
                reason for reason, value in RESOURCE_WAITS.items() if value == code))
            return
        item.error_code, item.updated_at = code, now
        if code in ("PREPARATION_BUDGET_WAIT", "PREPARATION_CONNECTION_WAIT"):
            item.state = "retrying"
            item.next_run_at = now + timedelta(seconds=30)
            if item.connection_key:
                self._available(sess, item)
            return
        item.failures += 1
        if code in SETTINGS:
            item.state = "action_required"
            if code in CONNECTION_SETTINGS and item.task_id and item.connection_key:
                gate = connection(sess, self._gate_key(sess, item), item.owner_user_id)
                gate.cooldown_until = now + timedelta(days=1)
                gate.probe_task_id = item.task_id
        elif code not in TRANSIENT or item.failures > 3:
            item.state = "paused"
        else:
            item.state = "retrying"
            item.next_run_at = now + timedelta(
                seconds=(10, 30, 120)[item.failures - 1] + random.uniform(0, 3)
            )
        if item.connection_key and code in TRANSIENT - {
            "OUTPUT_INVALID",
            "TASK_STORAGE_BUSY",
        }:
            gate = connection(sess, self._gate_key(sess, item), item.owner_user_id)
            gate.failures += 1
            gate.probe_task_id = None
            if gate.failures >= 3:
                until = now + timedelta(minutes=15)
                gate.cooldown_until = (
                    max(until, aware(gate.cooldown_until))
                    if gate.cooldown_until
                    else until
                )

    def _success(self, sess, item):
        if item.connection_key:
            row = connection(sess, self._gate_key(sess, item), item.owner_user_id)
            if row.cooldown_until and row.probe_task_id != item.task_id:
                return
            row.failures, row.cooldown_until, row.probe_task_id = 0, None, None

    def _gate_key(self, sess, item):
        task = sess.get(LearningTaskRow, item.task_id) if item.task_id else None
        voice = (
            self.teaching.preparation._audio(item.owner_user_id)
            if (task and task.stage == "tts" and item.kind == "first")
            else None
        )
        return (
            connection_key(item.owner_user_id, voice) if voice else item.connection_key
        )

    def view(self, sess, course_id, lesson_id, owner):
        if not self.enabled:
            return None
        course = sess.get(CourseRow, course_id)
        lesson = sess.get(LessonRow, lesson_id)
        if (
            not course
            or course.owner_user_id != owner
            or course.deleted_at
            or not lesson
        ):
            return None
        items = list(
            sess.scalars(
                select(PreparationItemRow).where(
                    PreparationItemRow.owner_user_id == owner,
                    PreparationItemRow.course_id == course_id,
                    PreparationItemRow.outline_version_id
                    == course.current_outline_version_id,
                    PreparationItemRow.unit_id == lesson.unit_id,
                    (PreparationItemRow.kind == "scope")
                    | (PreparationItemRow.lesson_id == lesson_id),
                )
            )
        )
        if not items:
            return None
        for reason, code in RESOURCE_WAITS.items():
            if any(item.error_code == code and item.state == "waiting" for item in items):
                return {"state": "paused", "action": None, "reason": reason}
        for item in items:
            if not item.connection_key:
                continue
            gate = sess.get(PreparationConnectionRow, self._gate_key(sess, item))
            event = (
                sess.get(TaskStateRow, gate.probe_task_id)
                if gate and gate.probe_task_id
                else None
            )
            if (
                gate
                and gate.cooldown_until
                and aware(gate.cooldown_until) > utcnow()
                and event
                and event.error_code in CONNECTION_SETTINGS
            ):
                return {
                    "state": "action_required",
                    "action": "settings",
                    **(
                        {"reason": "quota"}
                        if event.error_code == "MODEL_QUOTA_EXCEEDED"
                        else {}
                    ),
                }
        for state in ("action_required", "paused", "retrying", "waiting", "running"):
            if any(item.state == state for item in items):
                return {
                    "state": "preparing" if state in ("waiting", "running") else state,
                    "action": "settings"
                    if state == "action_required"
                    else "retry"
                    if state == "paused"
                    else None,
                    **(
                        {"reason": "storage"}
                        if any(
                            item.error_code == "PREPARATION_STORAGE_WAIT"
                            for item in items
                        )
                        else {}
                    ),
                }
        return {"state": "ready", "action": None}
