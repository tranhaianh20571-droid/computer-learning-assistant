"""T03/T04：SQLite 持久化仓储（PostgreSQL 契约的开发环境等价实现）。

约束语义与架构 §3.2 对齐：
- task_events：task_id + sequence 唯一，单调递增
- 状态更新与事件写入同一事务
- audit_logs 追加式；应用层禁止 UPDATE/DELETE，并用触发器双保险
- 所有权索引、旧 generation 围栏、保留期

生产替换为 PostgreSQL 时保持同一接口（TaskEventStore / AuditLog）。
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .audit import (
    AuditAccessDeniedError,
    AuditImmutabilityError,
    AuditRecord,
    AuditWriteError,
    ALLOWED_AUDIT_ROLES,
)
from .contracts import ContractError, validate_error_code, validate_event_name, validate_stage, validate_status
from .sanitize import sanitize_template_params
from .task_events import (
    DEFAULT_EVENT_RETENTION_SECONDS,
    AccessDeniedError,
    EventsExpiredError,
    HTTP_EVENTS_EXPIRED,
    TaskEvent,
    TaskState,
)

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS task_states (
    task_id        TEXT PRIMARY KEY,
    owner_user_id  TEXT NOT NULL,
    subject_id     TEXT NOT NULL,
    stage          TEXT NOT NULL,
    status         TEXT NOT NULL,
    generation     INTEGER NOT NULL DEFAULT 1,
    attempt        INTEGER NOT NULL DEFAULT 1,
    revision       INTEGER NOT NULL DEFAULT 1,
    progress_current INTEGER NOT NULL DEFAULT 0,
    progress_total   INTEGER NOT NULL DEFAULT 0,
    error_code     TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_states_owner ON task_states(owner_user_id);

CREATE TABLE IF NOT EXISTS task_events (
    event_id       TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL,
    owner_user_id  TEXT NOT NULL,
    subject_id     TEXT NOT NULL,
    sequence       INTEGER NOT NULL,
    stage          TEXT NOT NULL,
    status         TEXT NOT NULL,
    error_code     TEXT,
    progress_current INTEGER NOT NULL DEFAULT 0,
    progress_total   INTEGER NOT NULL DEFAULT 0,
    template_id    TEXT,
    template_params TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL,
    generation     INTEGER NOT NULL DEFAULT 1,
    attempt        INTEGER NOT NULL DEFAULT 1,
    duration_ms    INTEGER,
    revision       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (task_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_task_events_task_seq ON task_events(task_id, sequence);
CREATE INDEX IF NOT EXISTS idx_task_events_owner ON task_events(owner_user_id);

CREATE TABLE IF NOT EXISTS audit_logs (
    audit_id       TEXT PRIMARY KEY,
    event          TEXT NOT NULL,
    timestamp      TEXT NOT NULL,
    actor_id       TEXT NOT NULL,
    object_type    TEXT,
    object_id      TEXT,
    result         TEXT NOT NULL,
    trace_id       TEXT,
    reason         TEXT,
    admin_id       TEXT,
    role           TEXT NOT NULL DEFAULT 'app',
    target_actor_id TEXT,
    content_hash   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_logs(event);
CREATE INDEX IF NOT EXISTS idx_audit_object ON audit_logs(object_type, object_id);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_logs(actor_id);

-- 追加式双保险：禁止 UPDATE/DELETE
CREATE TRIGGER IF NOT EXISTS audit_logs_no_update
BEFORE UPDATE ON audit_logs
BEGIN
    SELECT RAISE(ABORT, 'audit_immutable');
END;

CREATE TRIGGER IF NOT EXISTS audit_logs_no_delete
BEFORE DELETE ON audit_logs
BEGIN
    SELECT RAISE(ABORT, 'audit_immutable');
END;
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _utc_now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


class SqliteTaskEventStore:
    """SQLite 版任务事件仓储；接口与 TaskEventStore 对齐。"""

    def __init__(self, db_path: str | Path = ":memory:", retention_seconds: int = DEFAULT_EVENT_RETENTION_SECONDS) -> None:
        self.db_path = str(db_path)
        self.retention_seconds = retention_seconds
        self._lock = threading.RLock()
        self._now_fn: Callable[[], float] = _utc_now_ts
        # 每线程独立连接（check_same_thread=False + 外部锁）
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def set_clock(self, fn: Callable[[], float]) -> None:
        self._now_fn = fn

    def close(self) -> None:
        with self._lock:
            self._conn.close()

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
        with self._lock:
            cur = self._conn.execute("SELECT 1 FROM task_states WHERE task_id=?", (task_id,))
            if cur.fetchone():
                raise ContractError("DUPLICATE_TASK", f"任务已存在: {task_id}")
            now = _utc_now()
            self._conn.execute(
                """INSERT INTO task_states
                   (task_id, owner_user_id, subject_id, stage, status, generation, attempt,
                    revision, progress_current, progress_total, error_code, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, owner_user_id, subject_id, stage, "started", generation, attempt,
                 1, 0, progress_total, None, now),
            )
            state = self._load_state(task_id)
            event = self._append(
                state,
                event_name="task.accepted",
                stage=stage,
                status="started",
                template_id=template_id,
                template_params=dict(template_params or {}),
                progress_total=progress_total,
            )
            self._conn.commit()
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

        with self._lock:
            state = self._load_state(task_id)
            if generation is not None and generation < state.generation and not diagnostics_only:
                diagnostics_only = True
                status = "stale_discarded"
                error_code = "STALE_RESULT_DISCARDED"
                event_name = "task.stale_discarded"

            event = self._append(
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
                self._conn.execute(
                    """UPDATE task_states SET stage=?, status=?, error_code=?, revision=revision+1,
                       progress_current=COALESCE(?, progress_current),
                       progress_total=COALESCE(?, progress_total),
                       attempt=COALESCE(?, attempt),
                       generation=COALESCE(?, generation)
                       WHERE task_id=?""",
                    (stage, status, error_code, progress_current, progress_total, attempt, generation, task_id),
                )
            self._conn.commit()
            return event

    def _load_state(self, task_id: str) -> TaskState:
        cur = self._conn.execute("SELECT * FROM task_states WHERE task_id=?", (task_id,))
        row = cur.fetchone()
        if row is None:
            raise ContractError("UNKNOWN_TASK", f"未知任务: {task_id}")
        return TaskState(
            task_id=row["task_id"],
            owner_user_id=row["owner_user_id"],
            subject_id=row["subject_id"],
            stage=row["stage"],
            status=row["status"],
            generation=row["generation"],
            attempt=row["attempt"],
            revision=row["revision"],
            progress_current=row["progress_current"],
            progress_total=row["progress_total"],
            error_code=row["error_code"],
        )

    def _next_sequence(self, task_id: str) -> int:
        cur = self._conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS n FROM task_events WHERE task_id=?", (task_id,)
        )
        return int(cur.fetchone()["n"])

    def _append(
        self,
        state: TaskState,
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
        if event_name not in (
            "task.accepted",
            "task.started",
            "task.stage.succeeded",
            "task.stage.partial",
            "task.stage.failed",
            "task.retry_scheduled",
            "task.cancelled",
            "task.stale_discarded",
        ):
            raise ContractError("UNKNOWN_EVENT", f"未注册任务事件: {event_name}")
        validate_event_name(event_name)

        seq = self._next_sequence(state.task_id)
        now = datetime.fromtimestamp(self._now_fn(), tz=timezone.utc)
        created_at = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        event_id = f"evt_{uuid.uuid4().hex[:16]}"
        params = sanitize_template_params(template_params or {})
        import json as _json

        self._conn.execute(
            """INSERT INTO task_events
               (event_id, task_id, owner_user_id, subject_id, sequence, stage, status, error_code,
                progress_current, progress_total, template_id, template_params, created_at,
                generation, attempt, duration_ms, revision)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                state.task_id,
                state.owner_user_id,
                state.subject_id,
                seq,
                stage,
                status,
                error_code,
                progress_current if progress_current is not None else state.progress_current,
                progress_total if progress_total is not None else state.progress_total,
                template_id,
                _json.dumps(params, ensure_ascii=False),
                created_at,
                generation if generation is not None else state.generation,
                attempt if attempt is not None else state.attempt,
                duration_ms,
                state.revision,
            ),
        )
        return TaskEvent(
            event_id=event_id,
            owner_user_id=state.owner_user_id,
            task_id=state.task_id,
            subject_id=state.subject_id,
            sequence=seq,
            stage=stage,
            status=status,
            error_code=error_code,
            progress_current=progress_current if progress_current is not None else state.progress_current,
            progress_total=progress_total if progress_total is not None else state.progress_total,
            template_id=template_id,
            template_params=params,
            created_at=created_at,
            generation=generation if generation is not None else state.generation,
            attempt=attempt if attempt is not None else state.attempt,
            duration_ms=duration_ms,
            revision=state.revision,
        )

    def _assert_owner(self, task_id: str, actor_id: Optional[str]) -> TaskState:
        state = self._load_state(task_id)
        if actor_id is not None and actor_id != state.owner_user_id:
            raise AccessDeniedError("not_owner")
        return state

    def _rows_to_events(self, rows: Sequence[sqlite3.Row]) -> List[TaskEvent]:
        import json as _json

        out: List[TaskEvent] = []
        for row in rows:
            out.append(
                TaskEvent(
                    event_id=row["event_id"],
                    owner_user_id=row["owner_user_id"],
                    task_id=row["task_id"],
                    subject_id=row["subject_id"],
                    sequence=row["sequence"],
                    stage=row["stage"],
                    status=row["status"],
                    error_code=row["error_code"],
                    progress_current=row["progress_current"],
                    progress_total=row["progress_total"],
                    template_id=row["template_id"],
                    template_params=_json.loads(row["template_params"] or "{}"),
                    created_at=row["created_at"],
                    generation=row["generation"],
                    attempt=row["attempt"],
                    duration_ms=row["duration_ms"],
                    revision=row["revision"],
                )
            )
        return out

    def get_state(self, task_id: str, *, actor_id: Optional[str] = None) -> TaskState:
        with self._lock:
            return self._assert_owner(task_id, actor_id)

    def get_events(
        self,
        task_id: str,
        *,
        actor_id: Optional[str] = None,
        after_sequence: int = 0,
    ) -> List[TaskEvent]:
        with self._lock:
            self._assert_owner(task_id, actor_id)
            cur = self._conn.execute(
                "SELECT * FROM task_events WHERE task_id=? AND sequence>? ORDER BY sequence",
                (task_id, after_sequence),
            )
            return self._rows_to_events(cur.fetchall())

    def replay_sse(
        self,
        task_id: str,
        *,
        actor_id: str,
        last_event_id: Optional[str] = None,
        last_sequence: Optional[int] = None,
    ) -> Tuple[List[TaskEvent], int]:
        with self._lock:
            state = self._assert_owner(task_id, actor_id)
            cur = self._conn.execute(
                "SELECT * FROM task_events WHERE task_id=? ORDER BY sequence", (task_id,)
            )
            all_events = self._rows_to_events(cur.fetchall())
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
        return self._now_fn() - created.timestamp() > self.retention_seconds

    def full_status(self, task_id: str, *, actor_id: Optional[str] = None) -> dict:
        with self._lock:
            state = self._assert_owner(task_id, actor_id)
            cur = self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS n FROM task_events WHERE task_id=?", (task_id,)
            )
            last_seq = int(cur.fetchone()["n"])
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
                "last_sequence": last_seq,
            }


class SqliteAuditLog:
    """SQLite 版追加式审计；UPDATE/DELETE 由触发器拒绝。"""

    def __init__(self, db_path: str | Path = ":memory:", fail_write: bool = False) -> None:
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._fail_write = fail_write
        self._now_fn: Callable[[], str] = _utc_now
        self.write_failures = 0
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def set_clock(self, fn: Callable[[], str]) -> None:
        self._now_fn = fn

    def set_fail_write(self, flag: bool) -> None:
        self._fail_write = flag

    def close(self) -> None:
        with self._lock:
            self._conn.close()

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

        # 自由文本脱敏（与运行日志同一规则）
        if reason is not None:
            from .sanitize import sanitize_string

            reason = sanitize_string(str(reason), "reason")
        if trace_id is not None:
            from .sanitize import sanitize_string

            trace_id = sanitize_string(str(trace_id), "trace_id")

        audit_id = f"aud_{uuid.uuid4().hex[:16]}"
        timestamp = self._now_fn()
        payload = "|".join(
            [event, timestamp, actor_id, object_type or "", object_id or "", result, trace_id or ""]
        )
        import hashlib

        content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        record = AuditRecord(
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
            content_hash=content_hash,
            target_actor_id=target_actor_id,
        )
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO audit_logs
                       (audit_id, event, timestamp, actor_id, object_type, object_id, result,
                        trace_id, reason, admin_id, role, target_actor_id, content_hash)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        audit_id, event, timestamp, actor_id, object_type, object_id, result,
                        trace_id, reason, admin_id, role, target_actor_id, content_hash,
                    ),
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                self.write_failures += 1
                raise AuditWriteError(str(exc)) from exc
        return record

    def update(self, *args: Any, **kwargs: Any) -> None:
        raise AuditImmutabilityError("update")

    def delete(self, *args: Any, **kwargs: Any) -> None:
        raise AuditImmutabilityError("delete")

    def sql_update_forbidden(self) -> None:
        """直接 SQL UPDATE 也必须被触发器拒绝（集成验证用）。"""
        with self._lock:
            try:
                self._conn.execute("UPDATE audit_logs SET result='tampered'")
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
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
        with self._lock:
            sql = "SELECT * FROM audit_logs WHERE 1=1"
            args: List[Any] = []
            if event:
                sql += " AND event=?"
                args.append(event)
            if object_type:
                sql += " AND object_type=?"
                args.append(object_type)
            if object_id:
                sql += " AND object_id=?"
                args.append(object_id)
            sql += " ORDER BY timestamp DESC LIMIT ?"
            args.append(limit)
            rows = self._conn.execute(sql, args).fetchall()
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
        with self._lock:
            rows = self._conn.execute("SELECT * FROM audit_logs ORDER BY timestamp").fetchall()
            return [self._row_to_record(r) for r in rows]

    def _row_to_record(self, row: sqlite3.Row) -> AuditRecord:
        return AuditRecord(
            audit_id=row["audit_id"],
            event=row["event"],
            timestamp=row["timestamp"],
            actor_id=row["actor_id"],
            object_type=row["object_type"],
            object_id=row["object_id"],
            result=row["result"],
            trace_id=row["trace_id"],
            reason=row["reason"],
            admin_id=row["admin_id"],
            role=row["role"],
            content_hash=row["content_hash"],
            target_actor_id=row["target_actor_id"],
        )

    def __len__(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) AS n FROM audit_logs").fetchone()["n"])


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

# 便于测试内存库
__all__ = ["SqliteTaskEventStore", "SqliteAuditLog"]
