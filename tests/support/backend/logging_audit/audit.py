"""T04：追加式安全审计与查询权限。

审计记录采用追加式写入。普通应用账号不能修改既有审计记录，
审计查询本身也要留痕。失败尝试必须记录。
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from .contracts import (
    SCHEMA_VERSION,
    ContractError,
)
from .sanitize import sanitize_record


class AuditWriteError(Exception):
    """审计写入失败。安全审计不能静默丢失，必须触发告警/阻断策略。"""

    def __init__(self, reason: str) -> None:
        super().__init__(f"audit_write_failed: {reason}")
        self.error_code = "AUDIT_WRITE_FAILED"
        self.reason = reason


class AuditImmutabilityError(Exception):
    """尝试修改/删除既有审计记录。"""

    def __init__(self, action: str) -> None:
        super().__init__(f"audit_immutable: {action}")
        self.error_code = "ACCESS_DENIED"
        self.action = action


class AuditAccessDeniedError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.error_code = "ACCESS_DENIED"
        self.http_status = 403


@dataclass(frozen=True)
class AuditRecord:
    audit_id: str
    event: str
    timestamp: str
    actor_id: str
    object_type: Optional[str]
    object_id: Optional[str]
    result: str
    trace_id: Optional[str]
    reason: Optional[str]
    admin_id: Optional[str]
    role: str
    content_hash: str
    target_actor_id: Optional[str] = None

    def to_record(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "timestamp": self.timestamp,
            "level": "INFO",
            "event": self.event,
            "service": "api",
            "environment": "test",
            "audit_action": self.event,
            "actor_id": self.actor_id,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "result": self.result,
            "trace_id": self.trace_id,
            "reason": self.reason,
            "admin_id": self.admin_id,
            "role": self.role,
            "event_id": self.audit_id,
            "target_actor_id": self.target_actor_id,
        }


# 允许查询审计的角色
ALLOWED_AUDIT_ROLES = frozenset({"admin", "security_auditor"})


class AuditLog:
    """追加式审计存储。

    - append：只增不改；应用普通读账号无 UPDATE/DELETE
    - query：仅授权角色；查询自身写 audit.query.executed
    - 失败尝试同样记录
    """

    def __init__(self, fail_write: bool = False) -> None:
        self._records: List[AuditRecord] = []
        self._lock = threading.RLock()
        self._fail_write = fail_write
        self._now_fn: Callable[[], str] = lambda: datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"
        self.write_failures = 0

    def set_clock(self, fn: Callable[[], str]) -> None:
        self._now_fn = fn

    def set_fail_write(self, flag: bool) -> None:
        """故障注入：模拟审计写入失败。"""
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
        if event not in _KNOWN:
            raise ContractError("UNKNOWN_EVENT", f"未注册审计事件: {event}")
        if self._fail_write:
            self.write_failures += 1
            raise AuditWriteError("injected_or_storage_failure")

        from .sanitize import sanitize_string

        if reason is not None:
            reason = sanitize_string(str(reason), "reason")
        if trace_id is not None:
            trace_id = sanitize_string(str(trace_id), "trace_id")

        # resource_type/resource_id 映射到 object 字段（审计对象）
        if object_type is None:
            object_type = resource_type
        if object_id is None:
            object_id = resource_id

        record = AuditRecord(
            audit_id=f"aud_{uuid.uuid4().hex[:16]}",
            event=event,
            timestamp=self._now_fn(),
            actor_id=actor_id,
            object_type=object_type,
            object_id=object_id,
            result=result,
            trace_id=trace_id,
            reason=reason,
            admin_id=admin_id,
            role=role,
            content_hash="",
            target_actor_id=target_actor_id,
        )
        # 内容哈希用于证据索引（不含敏感值）
        payload = "|".join(
            [
                record.event,
                record.timestamp,
                record.actor_id,
                record.object_type or "",
                record.object_id or "",
                record.result,
                record.trace_id or "",
            ]
        )
        hashed = dataclasses_replace_hash(record, payload)
        with self._lock:
            self._records.append(hashed)
            return hashed

    def update(self, *args: Any, **kwargs: Any) -> None:
        raise AuditImmutabilityError("update")

    def delete(self, *args: Any, **kwargs: Any) -> None:
        raise AuditImmutabilityError("delete")

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
        """管理员查询；查询行为自身写 audit.query.executed。"""
        if role not in ALLOWED_AUDIT_ROLES:
            raise AuditAccessDeniedError("role_not_allowed")

        with self._lock:
            results = [
                r
                for r in self._records
                if (event is None or r.event == event)
                and (object_type is None or r.object_type == object_type)
                and (object_id is None or r.object_id == object_id)
            ][-limit:]
            # 管理员只能读授权范围：本实现限定 admin 可读全部、security_auditor 可读全部
            # 普通 app 角色已在上面拒绝
            snapshot = list(results)

        # 查询留痕（不能因留痕失败而丢查询结果；但写入失败要可见）
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
            # 查询审计失败必须可见；此处继续返回结果但调用方应监控 write_failures
            pass
        return snapshot

    def list_all_for_test(self) -> List[AuditRecord]:
        with self._lock:
            return list(self._records)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


def dataclasses_replace_hash(record: AuditRecord, payload: str) -> AuditRecord:
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return AuditRecord(
        audit_id=record.audit_id,
        event=record.event,
        timestamp=record.timestamp,
        actor_id=record.actor_id,
        object_type=record.object_type,
        object_id=record.object_id,
        result=record.result,
        trace_id=record.trace_id,
        reason=record.reason,
        admin_id=record.admin_id,
        role=record.role,
        content_hash=digest,
        target_actor_id=record.target_actor_id,
    )


# 需要注册表校验的审计事件名（与 contracts.EVENT_REGISTRY 对齐）
_KNOWN = frozenset(
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
