"""T03：持久任务事件与 SSE 回放契约。

task_events 至少包含 event_id、owner_user_id、task_id、subject_id、sequence、
stage、status、error_code、进度、模板参数和 created_at。
状态更新与事件写入同一事务；task_id + sequence 唯一。
SSE 按 Last-Event-ID 补取缺口；过期返回 410 events_expired。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .contracts import (
    SCHEMA_VERSION,
    ContractError,
    EventSpec,
    LogEventType,
    TaskStatus,
    validate_error_code,
    validate_event_name,
    validate_stage,
    validate_status,
)
from .sanitize import sanitize_record, sanitize_template_params

# SSE 过期：事件保留天数（架构要求至少 7 天；此处为契约默认）
DEFAULT_EVENT_RETENTION_SECONDS = 7 * 24 * 3600

HTTP_EVENTS_EXPIRED = 410


class EventsExpiredError(Exception):
    """SSE 游标过期或序号不连续，客户端必须改用 REST 全量状态。"""

    def __init__(self, task_id: str, last_sequence: Optional[int], reason: str) -> None:
        super().__init__(f"events_expired: {reason}")
        self.task_id = task_id
        self.last_sequence = last_sequence
        self.reason = reason
        self.http_status = HTTP_EVENTS_EXPIRED
        self.error_code = "EVENTS_EXPIRED"


class AccessDeniedError(Exception):
    def __init__(self, reason: str = "ACCESS_DENIED") -> None:
        super().__init__(reason)
        self.error_code = "ACCESS_DENIED"
        self.http_status = 403


@dataclass
class TaskEvent:
    event_id: str
    owner_user_id: str
    task_id: str
    subject_id: str
    sequence: int
    stage: str
    status: str
    error_code: Optional[str]
    progress_current: int
    progress_total: int
    template_id: Optional[str]
    template_params: dict
    created_at: str
    generation: int = 1
    attempt: int = 1
    duration_ms: Optional[int] = None
    revision: int = 1

    def to_record(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "timestamp": self.created_at,
            "level": "INFO",
            "event": f"task.{self.stage}.{self.status}" if self.stage else f"task.{self.status}",
            "service": "worker",
            "environment": "test",
            "event_id": self.event_id,
            "owner_user_id": self.owner_user_id,
            "task_id": self.task_id,
            "subject_id": self.subject_id,
            "sequence": self.sequence,
            "stage": self.stage,
            "status": self.status,
            "error_code": self.error_code,
            "progress_current": self.progress_current,
            "progress_total": self.progress_total,
            "template_id": self.template_id,
            "template_params": self.template_params,
            "generation": self.generation,
            "attempt": self.attempt,
            "duration_ms": self.duration_ms,
            "revision": self.revision,
        }


@dataclass
class TaskState:
    task_id: str
    owner_user_id: str
    subject_id: str
    stage: str
    status: str
    generation: int = 1
    attempt: int = 1
    revision: int = 1
    progress_current: int = 0
    progress_total: int = 0
    error_code: Optional[str] = None


class TaskEventStore:
    """内存实现的持久任务事件仓储契约。

    生产实现替换为 PostgreSQL 表 + 事务；此处保证：
    - task_id + sequence 唯一且单调递增
    - 状态更新与事件写入同一事务（本实现用锁模拟）
    - 所有权校验
    - 旧 generation/迟到 worker 只能写诊断，不能覆盖当前结果
    - SSE Last-Event-ID 补取与 410 events_expired
    """

    def __init__(self, retention_seconds: int = DEFAULT_EVENT_RETENTION_SECONDS) -> None:
        self.retention_seconds = retention_seconds
        self._events: Dict[str, List[TaskEvent]] = {}
        self._states: Dict[str, TaskState] = {}
        self._sequences: Dict[str, int] = {}
        self._lock = threading.RLock()
        self._now_fn: Callable[[], float] = lambda: datetime.now(timezone.utc).timestamp()

    def set_clock(self, fn: Callable[[], float]) -> None:
        self._now_fn = fn

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
            if task_id in self._states:
                raise ContractError("DUPLICATE_TASK", f"任务已存在: {task_id}")
            state = TaskState(
                task_id=task_id,
                owner_user_id=owner_user_id,
                subject_id=subject_id,
                stage=stage,
                status="started",
                generation=generation,
                attempt=attempt,
                progress_total=progress_total,
            )
            self._states[task_id] = state
            self._sequences[task_id] = 0
            self._events[task_id] = []
            return self._append(
                state,
                event_name="task.accepted",
                stage=stage,
                status="started",
                template_id=template_id,
                template_params=dict(template_params or {}),
                progress_total=progress_total,
            )

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
        """写入任务事件并（非诊断时）更新任务状态。

        diagnostics_only=True 时仅写诊断事件，不覆盖当前业务状态
        （用于旧 generation/租约过期的迟到 worker）。
        """
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
            state = self._states.get(task_id)
            if state is None:
                raise ContractError("UNKNOWN_TASK", f"未知任务: {task_id}")

            # 租约/代际围栏：旧 generation 只能写诊断
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
            return event

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
        # 确认事件类型在注册表中（task.* 命名）
        # 允许 task.started / task.stage.* / task.stale_discarded 等
        self._validate_event_name(event_name)

        seq = self._sequences[state.task_id] + 1
        self._sequences[state.task_id] = seq
        now = datetime.fromtimestamp(self._now_fn(), tz=timezone.utc)
        event = TaskEvent(
            event_id=f"evt_{uuid.uuid4().hex[:16]}",
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
            template_params=sanitize_template_params(template_params or {}),
            created_at=now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            generation=generation if generation is not None else state.generation,
            attempt=attempt if attempt is not None else state.attempt,
            duration_ms=duration_ms,
            revision=state.revision,
        )
        # 幂等：task_id + sequence 唯一（内存实现由自增保证；生产用唯一约束）
        self._events[state.task_id].append(event)
        return event

    def _validate_event_name(self, name: str) -> None:
        # 任务事件使用运行+任务双类型注册名
        if name in (
            "task.accepted",
            "task.started",
            "task.stage.succeeded",
            "task.stage.partial",
            "task.stage.failed",
            "task.retry_scheduled",
            "task.cancelled",
            "task.stale_discarded",
        ):
            validate_event_name(name)
            return
        raise ContractError("UNKNOWN_EVENT", f"未注册任务事件: {name}")

    def get_state(self, task_id: str, *, actor_id: Optional[str] = None) -> TaskState:
        with self._lock:
            state = self._states.get(task_id)
            if state is None:
                raise ContractError("UNKNOWN_TASK", f"未知任务: {task_id}")
            if actor_id is not None and actor_id != state.owner_user_id:
                raise AccessDeniedError("not_owner")
            return TaskState(**vars(state))

    def get_events(
        self,
        task_id: str,
        *,
        actor_id: Optional[str] = None,
        after_sequence: int = 0,
    ) -> List[TaskEvent]:
        with self._lock:
            state = self._states.get(task_id)
            if state is None:
                raise ContractError("UNKNOWN_TASK", f"未知任务: {task_id}")
            if actor_id is not None and actor_id != state.owner_user_id:
                raise AccessDeniedError("not_owner")
            return [e for e in self._events[task_id] if e.sequence > after_sequence]

    def replay_sse(
        self,
        task_id: str,
        *,
        actor_id: str,
        last_event_id: Optional[str] = None,
        last_sequence: Optional[int] = None,
    ) -> Tuple[List[TaskEvent], int]:
        """SSE 补取：校验所有权，按 Last-Event-ID 补发缺口。

        返回 (events, next_expected_sequence)。
        游标过期或序号不连续时抛出 EventsExpiredError（HTTP 410）。
        """
        with self._lock:
            state = self._states.get(task_id)
            if state is None:
                raise ContractError("UNKNOWN_TASK", f"未知任务: {task_id}")
            if actor_id != state.owner_user_id:
                raise AccessDeniedError("not_owner")

            all_events = self._events[task_id]
            if not all_events:
                return [], 0

            current_max = all_events[-1].sequence
            cursor = self._resolve_cursor(last_event_id, last_sequence, all_events)
            if cursor is None:
                # 无游标：全量（首次连接）
                return list(all_events), current_max

            if cursor > current_max:
                raise EventsExpiredError(task_id, current_max, "cursor_ahead")
            if cursor < 0:
                raise EventsExpiredError(task_id, current_max, "invalid_cursor")

            # 过期策略：事件保留窗口
            if self._is_expired(all_events[0]):
                raise EventsExpiredError(task_id, current_max, "retention_expired")

            # 序号连续性：cursor 之后应能补出 cursor+1 ...
            gap = [e for e in all_events if e.sequence > cursor]
            if gap and gap[0].sequence != cursor + 1:
                raise EventsExpiredError(task_id, current_max, "sequence_gap")
            return gap, current_max

    def _resolve_cursor(
        self,
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
            raise EventsExpiredError(
                all_events[0].task_id if all_events else "?", None, "unknown_event_id"
            )
        return None

    def _is_expired(self, oldest: TaskEvent) -> bool:
        created = datetime.fromisoformat(oldest.created_at.replace("Z", "+00:00"))
        age = self._now_fn() - created.timestamp()
        return age > self.retention_seconds

    def full_status(self, task_id: str, *, actor_id: Optional[str] = None) -> dict:
        """REST 全量状态：客户端 410 后按当前 revision 重建。"""
        state = self.get_state(task_id, actor_id=actor_id)
        events = self.get_events(task_id, actor_id=actor_id)
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
            "last_sequence": events[-1].sequence if events else 0,
        }
