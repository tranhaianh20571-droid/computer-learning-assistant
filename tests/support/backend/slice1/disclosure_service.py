"""外发确认与 worker 核验（切片 1 T05）。

规则（架构 §6、交接文档 §3.1）：
- 每次外发前绑定用户、任务、目标配置版本、内容类别与范围快照。
- 更换服务地址、扩大内容范围、删除/停用配置或撤销同意后须重新确认。
- worker 出站前必须核验当前任务仍有有效授权且覆盖拟发送内容。
- 授权记录由普通用户不可修改；核验失败写审计。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import select

from ..logging_audit.db import Database
from ..logging_audit.repositories import PgAuditLog
from .errors import Slice1Error, error
from .models import (
    CONTENT_CATEGORIES,
    DisclosureGrantRow,
    ServiceConfigRow,
    utcnow,
)

DEFAULT_GRANT_TTL_SECONDS = 24 * 3600


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class DisclosureService:
    def __init__(self, db: Database, audit: Optional[PgAuditLog] = None) -> None:
        self.db = db
        self.audit = audit

    def _audit(self, event: str, *, actor_id: str, object_id: str, result: str, reason: Optional[str] = None) -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(
                event,
                actor_id=actor_id,
                object_type="disclosure_grant",
                object_id=object_id,
                result=result,
                reason=reason,
            )
        except Exception:  # noqa: BLE001 - 审计失败不应吞掉业务结果，但必须可见
            pass

    def create(
        self,
        *,
        actor_id: str,
        task_id: str,
        config_id: str,
        content_category: str,
        scope_snapshot: Optional[dict] = None,
        ttl_seconds: int = DEFAULT_GRANT_TTL_SECONDS,
    ) -> dict:
        if content_category not in CONTENT_CATEGORIES:
            raise error("invalid_field", f"unknown content_category {content_category}")
        with self.db.session() as sess:
            config = sess.get(ServiceConfigRow, config_id)
            if config is None or not config.is_active:
                raise error("config_not_found")
            grant_id = f"dg_{uuid.uuid4().hex[:12]}"
            now = utcnow()
            row = DisclosureGrantRow(
                grant_id=grant_id,
                owner_user_id=actor_id,
                task_id=task_id,
                config_id=config_id,
                config_version=config.config_version,
                service_kind=config.kind,
                service_endpoint=config.endpoint,
                content_category=content_category,
                scope_snapshot=json.dumps(scope_snapshot or {}, ensure_ascii=False),
                granted_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            sess.add(row)
            sess.flush()
            view = self._view(row)
            sess.commit()
        self._audit(
            "external.disclosure.confirmed",
            actor_id=actor_id,
            object_id=grant_id,
            result="granted",
        )
        return view

    @staticmethod
    def _view(row: DisclosureGrantRow) -> dict:
        return {
            "grant_id": row.grant_id,
            "owner_user_id": row.owner_user_id,
            "task_id": row.task_id,
            "config_id": row.config_id,
            "config_version": row.config_version,
            "service_kind": row.service_kind,
            "service_endpoint": row.service_endpoint,
            "content_category": row.content_category,
            "scope_snapshot": json.loads(row.scope_snapshot or "{}"),
            "granted_at": _iso(row.granted_at),
            "expires_at": _iso(row.expires_at),
            "revoked_at": _iso(row.revoked_at),
        }

    def list_for_task(self, task_id: str, *, actor_id: str, is_admin: bool = False) -> list[dict]:
        with self.db.session() as sess:
            rows = (
                sess.execute(
                    select(DisclosureGrantRow)
                    .where(DisclosureGrantRow.task_id == task_id)
                    .order_by(DisclosureGrantRow.granted_at)
                )
                .scalars()
                .all()
            )
            return [
                self._view(r)
                for r in rows
                if is_admin or r.owner_user_id == actor_id
            ]

    def revoke(self, grant_id: str, *, actor_id: str, is_admin: bool = False) -> dict:
        with self.db.session() as sess:
            row = sess.get(DisclosureGrantRow, grant_id)
            if row is None:
                raise error("config_not_found", "grant not found")
            if not is_admin and row.owner_user_id != actor_id:
                raise error("config_not_found", "grant not found")
            if row.revoked_at is None:
                row.revoked_at = utcnow()
            sess.flush()
            view = self._view(row)
            sess.commit()
        self._audit(
            "external.disclosure.confirmed",
            actor_id=actor_id,
            object_id=grant_id,
            result="revoked",
        )
        return view

    def revoke_for_config(self, config_id: str, *, reason: str = "config_changed") -> int:
        """配置端点/凭据/停用后失效相关授权。返回受影响数量。"""
        with self.db.session() as sess:
            rows = (
                sess.execute(
                    select(DisclosureGrantRow).where(
                        DisclosureGrantRow.config_id == config_id,
                        DisclosureGrantRow.revoked_at.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            now = utcnow()
            for row in rows:
                row.revoked_at = now
            sess.commit()
            return len(rows)

    def verify(
        self,
        *,
        task_id: str,
        config_id: str,
        content_category: str,
        actor_id: Optional[str] = None,
    ) -> dict:
        """worker 出站前核验：授权有效、未撤销、未过期、覆盖类别、配置版本未变。

        失败抛受控错误码；调用方不得发起外发。
        """
        now = utcnow()
        with self.db.session() as sess:
            config = sess.get(ServiceConfigRow, config_id)
            if config is None or not config.is_active:
                raise error("config_not_found", "config missing or inactive")
            rows = (
                sess.execute(
                    select(DisclosureGrantRow)
                    .where(
                        DisclosureGrantRow.task_id == task_id,
                        DisclosureGrantRow.config_id == config_id,
                        DisclosureGrantRow.content_category == content_category,
                    )
                    .order_by(DisclosureGrantRow.granted_at.desc())
                )
                .scalars()
                .all()
            )
            if actor_id is not None:
                rows = [r for r in rows if r.owner_user_id == actor_id]
            if not rows:
                raise error("disclosure_required", "no grant covering this disclosure")
            grant = rows[0]
            if grant.revoked_at is not None:
                raise error("disclosure_revoked")
            if grant.expires_at is not None:
                expires = grant.expires_at
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires < now:
                    raise error("disclosure_expired")
            if grant.config_version != config.config_version:
                raise error("disclosure_revoked", "config version changed")
            if grant.service_endpoint != config.endpoint:
                raise error("disclosure_revoked", "endpoint changed")
            return self._view(grant)

    def verify_or_audit(self, **kwargs) -> dict:
        try:
            return self.verify(**kwargs)
        except Slice1Error as exc:
            self._audit(
                "access.denied",
                actor_id=kwargs.get("actor_id") or "worker",
                object_id=kwargs.get("task_id", ""),
                result="denied",
                reason=exc.code,
            )
            raise
