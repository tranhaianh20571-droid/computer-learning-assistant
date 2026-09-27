"""SQLAlchemy 仓储：任务事件与追加式审计（接口与 storage.py 对齐）。"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .audit import (
    ALLOWED_AUDIT_ROLES,
    AuditAccessDeniedError,
    AuditImmutabilityError,
    AuditRecord,
    AuditWriteError,
)
from .contracts import ContractError, validate_error_code, validate_event_name, validate_stage, validate_status
from .db import Database
from .models import AuditLogRow, TaskEventRow, TaskStateRow
from .sanitize import sanitize_string, sanitize_template_params
from .task_events import (
    DEFAULT_EVENT_RETENTION_SECONDS,
    AccessDeniedError,
    EventsExpiredError,
    TaskEvent,
    TaskState,
)

_TASK_EVENT_NAMES = frozenset(
    {
        "task.accepted",
        "task.started",
        "task.stage.succeeded",
        "task.stage.partial",
        "task.stage.failed",
        "task.retry_scheduled",
        "task.cancelled",
        "task.stale_discarded",
    }
)

_AUDIT_EVENTS = frozenset(
    {
        "auth.login.failed",
        "auth.register.submitted",
        "auth.email.verified",
        "auth.password.reset_requested",
        "auth.password.reset_completed",
        "auth.session.revoked",
        "admin.approval.changed",
        "config.changed",
        "external.disclosure.confirmed",
        "connector.bound",
        "connector.revoked",
        "access.denied",
        "resource.delete.requested",
        "cleanup.completed",
        "restore.locked",
        "audit.query.executed",
    }
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class PgTaskEventStore:
    """PostgreSQL 任务事件仓储。"""

    def __init__(self, db: Database, retention_seconds: int = DEFAULT_EVENT_RETENTION_SECONDS) -> None:
        self.db = db
        self.retention_seconds = retention_seconds

    def create_task(
        self,
        task_id: str,
        owner_user_id: str,
        subject_id: str,
        stage: str,
        *,
        generation: int = 1,
        attempt: int = 1,
        progress_total: int = 0,
        template_id: Optional[str] = None,
        template_params: Optional[Mapping[str, Any]] = None,
    ) -> TaskEvent:
        validate_stage(stage)
        with self.db.session() as sess:
            exists = sess.get(TaskStateRow, task_id)
            if exists:
                raise ContractError("DUPLICATE_TASK", f"任务已存在: {task_id}")
            state = TaskStateRow(
                task_id=task_id,
                owner_user_id=owner_user_id,
                subject_id=subject_id,
                stage=stage,
                status="started",
                generation=generation,
                attempt=attempt,
                revision=1,
                progress_current=0,
                progress_total=progress_total,
                created_at=_utc_now(),
            )
            sess.add(state)
            sess.flush()
            event = self._append(
                sess,
                state,
                event_name="task.accepted",
                stage=stage,
                status="started",
                template_id=template_id,
                template_params=dict(template_params or {}),
                progress_total=progress_total,
            )
            sess.commit()
            return event

    def record(
        self,
        task_id: str,
        *,
        stage: str,
        status: str,
        error_code: Optional[str] = None,
        progress_current: Optional[int] = None,
        progress_total: Optional[int] = None,
        template_id: Optional[str] = None,
        template_params: Optional[Mapping[str, Any]] = None,
        duration_ms: Optional[int] = None,
        generation: Optional[int] = None,
        attempt: Optional[int] = None,
        actor_id: Optional[str] = None,
        diagnostics_only: bool = False,
    ) -> TaskEvent:
        validate_stage(stage)
        validate_status(status)
        if error_code is not None:
            validate_error_code(error_code)

        event_name = {
            "started": "task.started",
            "succeeded": "task.stage.succeeded",
            "partial": "task.stage.partial",
            "failed": "task.stage.failed",
            "retry_scheduled": "task.retry_scheduled",
            "cancelled": "task.cancelled",
            "stale_discarded": "task.stale_discarded",
        }.get(status, "task.stage.failed")

        with self.db.session() as sess:
            state = sess.get(TaskStateRow, task_id)
            if state is None:
                raise ContractError("UNKNOWN_TASK", f"未知任务: {task_id}")

            if generation is not None and generation < state.generation and not diagnostics_only:
                diagnostics_only = True
                status = "stale_discarded"
                error_code = "STALE_RESULT_DISCARDED"
                event_name = "task.stale_discarded"

            event = self._append(
                sess,
                state,
                event_name=event_name,
                stage=stage,
                status=status,
                error_code=error_code,
                template_id=template_id,
                template_params=dict(template_params or {}),
                duration_ms=duration_ms,
                progress_current=progress_current,
                progress_total=progress_total,
                generation=generation if generation is not None else state.generation,
                attempt=attempt if attempt is not None else state.attempt,
            )
            if not diagnostics_only:
                state.stage = stage
                state.status = status
                state.error_code = error_code
                state.revision += 1
                if progress_current is not None:
                    state.progress_current = progress_current
                if progress_total is not None:
                    state.progress_total = progress_total
                if attempt is not None:
                    state.attempt = attempt
                if generation is not None:
                    state.generation = generation
            sess.commit()
            return event

    def _next_sequence(self, sess: Session, task_id: str) -> int:
        max_seq = sess.execute(
            select(func.coalesce(func.max(TaskEventRow.sequence), 0)).where(
                TaskEventRow.task_id == task_id
            )
        ).scalar_one()
        return int(max_seq) + 1

    def _append(
        self,
        sess: Session,
        state: TaskStateRow,
        *,
        event_name: str,
        stage: str,
        status: str,
        error_code: Optional[str] = None,
        template_id: Optional[str] = None,
        template_params: Optional[Mapping[str, Any]] = None,
        duration_ms: Optional[int] = None,
        progress_current: Optional[int] = None,
        progress_total: Optional[int] = None,
        generation: Optional[int] = None,
        attempt: Optional[int] = None,
    ) -> TaskEvent:
        if event_name not in _TASK_EVENT_NAMES:
            raise ContractError("UNKNOWN_EVENT", f"未注册任务事件: {event_name}")
        validate_event_name(event_name)

        seq = self._next_sequence(sess, state.task_id)
        params = sanitize_template_params(template_params or {})
        created_at = _utc_now()
        event_id = f"evt_{uuid.uuid4().hex[:16]}"
        row = TaskEventRow(
            event_id=event_id,
            task_id=state.task_id,
            owner_user_id=state.owner_user_id,
            subject_id=state.subject_id,
            sequence=seq,
            stage=stage,
            status=status,
            error_code=error_code,
            progress_current=progress_current if progress_current is not None else state.progress_current,
            progress_total=progress_total if progress_total is not None else state.progress_total,
            template_id=template_id,
            template_params=json.dumps(params, ensure_ascii=False),
            created_at=created_at,
            generation=generation if generation is not None else state.generation,
            attempt=attempt if attempt is not None else state.attempt,
            duration_ms=duration_ms,
            revision=state.revision,
        )
        sess.add(row)
        sess.flush()
        return TaskEvent(
            event_id=event_id,
            owner_user_id=row.owner_user_id,
            task_id=row.task_id,
            subject_id=row.subject_id,
            sequence=row.sequence,
            stage=row.stage,
            status=row.status,
            error_code=row.error_code,
            progress_current=row.progress_current,
            progress_total=row.progress_total,
            template_id=row.template_id,
            template_params=params,
            created_at=_iso(row.created_at),
            generation=row.generation,
            attempt=row.attempt,
            duration_ms=row.duration_ms,
            revision=row.revision,
        )

    def _row_to_event(self, row: TaskEventRow) -> TaskEvent:
        return TaskEvent(
            event_id=row.event_id,
            owner_user_id=row.owner_user_id,
            task_id=row.task_id,
            subject_id=row.subject_id,
            sequence=row.sequence,
            stage=row.stage,
            status=row.status,
            error_code=row.error_code,
            progress_current=row.progress_current,
            progress_total=row.progress_total,
            template_id=row.template_id,
            template_params=json.loads(row.template_params or "{}"),
            created_at=_iso(row.created_at),
            generation=row.generation,
            attempt=row.attempt,
            duration_ms=row.duration_ms,
            revision=row.revision,
        )

    def _load_state(self, sess: Session, task_id: str, actor_id: Optional[str] = None) -> TaskState:
        state = sess.get(TaskStateRow, task_id)
        if state is None:
            raise ContractError("UNKNOWN_TASK", f"未知任务: {task_id}")
        if actor_id is not None and actor_id != state.owner_user_id:
            raise AccessDeniedError("not_owner")
        return TaskState(
            task_id=state.task_id,
            owner_user_id=state.owner_user_id,
            subject_id=state.subject_id,
            stage=state.stage,
            status=state.status,
            generation=state.generation,
            attempt=state.attempt,
            revision=state.revision,
            progress_current=state.progress_current,
            progress_total=state.progress_total,
            error_code=state.error_code,
        )

    def get_state(self, task_id: str, *, actor_id: Optional[str] = None) -> TaskState:
        with self.db.session() as sess:
            return self._load_state(sess, task_id, actor_id)

    def get_events(
        self,
        task_id: str,
        *,
        actor_id: Optional[str] = None,
        after_sequence: int = 0,
    ) -> List[TaskEvent]:
        with self.db.session() as sess:
            self._load_state(sess, task_id, actor_id)
            rows = (
                sess.execute(
                    select(TaskEventRow)
                    .where(TaskEventRow.task_id == task_id, TaskEventRow.sequence > after_sequence)
                    .order_by(TaskEventRow.sequence)
                )
                .scalars()
                .all()
            )
            return [self._row_to_event(r) for r in rows]

    def replay_sse(
        self,
        task_id: str,
        *,
        actor_id: str,
        last_event_id: Optional[str] = None,
        last_sequence: Optional[int] = None,
    ) -> Tuple[List[TaskEvent], int]:
        with self.db.session() as sess:
            self._load_state(sess, task_id, actor_id)
            rows = (
                sess.execute(
                    select(TaskEventRow)
                    .where(TaskEventRow.task_id == task_id)
                    .order_by(TaskEventRow.sequence)
                )
                .scalars()
                .all()
            )
            all_events = [self._row_to_event(r) for r in rows]
            if not all_events:
                return [], 0
            current_max = all_events[-1].sequence
            cursor = self._resolve_cursor(task_id, last_event_id, last_sequence, all_events)
            if cursor is None:
                return all_events, current_max
            if cursor > current_max:
                raise EventsExpiredError(task_id, current_max, "cursor_ahead")
            if cursor < 0:
                raise EventsExpiredError(task_id, current_max, "invalid_cursor")
            if self._is_expired(all_events[0]):
                raise EventsExpiredError(task_id, current_max, "retention_expired")
            gap = [e for e in all_events if e.sequence > cursor]
            if gap and gap[0].sequence != cursor + 1:
                raise EventsExpiredError(task_id, current_max, "sequence_gap")
            return gap, current_max

    def _resolve_cursor(
        self,
        task_id: str,
        last_event_id: Optional[str],
        last_sequence: Optional[int],
        all_events: Sequence[TaskEvent],
    ) -> Optional[int]:
        if last_sequence is not None:
            return last_sequence
        if last_event_id:
            for e in all_events:
                if e.event_id == last_event_id:
                    return e.sequence
            raise EventsExpiredError(task_id, None, "unknown_event_id")
        return None

    def _is_expired(self, oldest: TaskEvent) -> bool:
        created = datetime.fromisoformat(oldest.created_at.replace("Z", "+00:00"))
        return _utc_now().timestamp() - created.timestamp() > self.retention_seconds

    def full_status(self, task_id: str, *, actor_id: Optional[str] = None) -> dict:
        with self.db.session() as sess:
            state = self._load_state(sess, task_id, actor_id)
            last_seq = sess.execute(
                select(func.coalesce(func.max(TaskEventRow.sequence), 0)).where(
                    TaskEventRow.task_id == task_id
                )
            ).scalar_one()
            return {
                "task_id": state.task_id,
                "owner_user_id": state.owner_user_id,
                "subject_id": state.subject_id,
                "stage": state.stage,
                "status": state.status,
                "generation": state.generation,
                "attempt": state.attempt,
                "revision": state.revision,
                "progress_current": state.progress_current,
                "progress_total": state.progress_total,
                "error_code": state.error_code,
                "last_sequence": int(last_seq),
            }


class PgAuditLog:
    """PostgreSQL 追加式审计。"""

    def __init__(self, db: Database, fail_write: bool = False) -> None:
        self.db = db
        self._fail_write = fail_write
        self.write_failures = 0

    def set_fail_write(self, flag: bool) -> None:
        self._fail_write = flag

    def append(
        self,
        event: str,
        *,
        actor_id: str,
        result: str,
        object_type: Optional[str] = None,
        object_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        reason: Optional[str] = None,
        admin_id: Optional[str] = None,
        role: str = "app",
        target_actor_id: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        **extra: Any,
    ) -> AuditRecord:
        if event not in _AUDIT_EVENTS:
            raise ContractError("UNKNOWN_EVENT", f"未注册审计事件: {event}")
        if self._fail_write:
            self.write_failures += 1
            raise AuditWriteError("injected_or_storage_failure")

        if object_type is None:
            object_type = resource_type
        if object_id is None:
            object_id = resource_id
        if reason is not None:
            reason = sanitize_string(str(reason), "reason")
        if trace_id is not None:
            trace_id = sanitize_string(str(trace_id), "trace_id")

        timestamp = _utc_now()
        payload = "|".join(
            [event, _iso(timestamp), actor_id, object_type or "", object_id or "", result, trace_id or ""]
        )
        content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        audit_id = f"aud_{uuid.uuid4().hex[:16]}"
        record = AuditRecord(
            audit_id=audit_id,
            event=event,
            timestamp=_iso(timestamp),
            actor_id=actor_id,
            object_type=object_type,
            object_id=object_id,
            result=result,
            trace_id=trace_id,
            reason=reason,
            admin_id=admin_id,
            role=role,
            content_hash=content_hash,
            target_actor_id=target_actor_id,
        )
        with self.db.session() as sess:
            try:
                sess.add(
                    AuditLogRow(
                        audit_id=audit_id,
                        event=event,
                        timestamp=timestamp,
                        actor_id=actor_id,
                        object_type=object_type,
                        object_id=object_id,
                        result=result,
                        trace_id=trace_id,
                        reason=reason,
                        admin_id=admin_id,
                        role=role,
                        target_actor_id=target_actor_id,
                        content_hash=content_hash,
                    )
                )
                sess.commit()
            except Exception as exc:  # noqa: BLE001
                self.write_failures += 1
                raise AuditWriteError(str(exc)) from exc
        return record

    def update(self, *args: Any, **kwargs: Any) -> None:
        raise AuditImmutabilityError("update")

    def delete(self, *args: Any, **kwargs: Any) -> None:
        raise AuditImmutabilityError("delete")

    def sql_update_forbidden(self) -> None:
        with self.db.session() as sess:
            try:
                sess.execute(update(AuditLogRow).where(AuditLogRow.audit_id.is_not(None)).values(result="tampered"))
                sess.commit()
            except Exception as exc:  # noqa: BLE001
                raise AuditImmutabilityError("sql_update") from exc
            raise AuditImmutabilityError("sql_update_not_blocked")

    def query(
        self,
        *,
        actor_id: str,
        role: str,
        event: Optional[str] = None,
        object_type: Optional[str] = None,
        object_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[AuditRecord]:
        if role not in ALLOWED_AUDIT_ROLES:
            raise AuditAccessDeniedError("role_not_allowed")
        with self.db.session() as sess:
            stmt = select(AuditLogRow)
            if event:
                stmt = stmt.where(AuditLogRow.event == event)
            if object_type:
                stmt = stmt.where(AuditLogRow.object_type == object_type)
            if object_id:
                stmt = stmt.where(AuditLogRow.object_id == object_id)
            stmt = stmt.order_by(AuditLogRow.timestamp.desc()).limit(limit)
            rows = sess.execute(stmt).scalars().all()
            snapshot = [self._row_to_record(r) for r in rows]

        try:
            self.append(
                "audit.query.executed",
                actor_id=actor_id,
                result="success",
                object_type=object_type,
                object_id=object_id,
                role=role,
                reason=f"query_id={uuid.uuid4().hex[:8]}",
            )
        except AuditWriteError:
            self.write_failures += 1
        return snapshot

    def list_all_for_test(self) -> List[AuditRecord]:
        with self.db.session() as sess:
            rows = sess.execute(select(AuditLogRow).order_by(AuditLogRow.timestamp)).scalars().all()
            return [self._row_to_record(r) for r in rows]

    def _row_to_record(self, row: AuditLogRow) -> AuditRecord:
        return AuditRecord(
            audit_id=row.audit_id,
            event=row.event,
            timestamp=_iso(row.timestamp),
            actor_id=row.actor_id,
            object_type=row.object_type,
            object_id=row.object_id,
            result=row.result,
            trace_id=row.trace_id,
            reason=row.reason,
            admin_id=row.admin_id,
            role=row.role,
            content_hash=row.content_hash,
            target_actor_id=row.target_actor_id,
        )

    def __len__(self) -> int:
        with self.db.session() as sess:
            return int(sess.execute(select(func.count()).select_from(AuditLogRow)).scalar_one())
