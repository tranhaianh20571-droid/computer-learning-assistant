"""外发确认与 worker 核验（切片 1 T05）。

规则（架构 §6、交接文档 §3.1）：
- 每次外发前绑定用户、任务、目标配置版本、内容类别与范围快照。
- 更换服务地址、扩大内容范围、删除/停用配置或撤销同意后须重新确认。
- worker 出站前必须核验当前任务仍有有效授权且覆盖拟发送内容。
- 授权记录由普通用户不可修改；核验失败写审计。
"""

from __future__ import annotations

import json
import math
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..logging_audit.db import Database
from ..tasks.service import LearningTaskRow
from ..logging_audit.audit import AuditWriteError
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

    def _audit(self, event: str, *, actor_id: str, object_id: str, result: str,
               reason: Optional[str] = None, session: Optional[Session] = None) -> None:
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
                **({"session": session} if session is not None else {}),
            )
        except AuditWriteError:
            raise
        except Exception as exc:
            raise AuditWriteError("storage_failure") from exc

    @staticmethod
    def _authorize(sess, task_id: str, config: ServiceConfigRow, actor_id: Optional[str]) -> str:
        task = sess.execute(
            select(LearningTaskRow).where(LearningTaskRow.task_id == task_id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if task is None:
            raise error("disclosure_required", "task authorization missing")
        owner = task.owner_user_id
        if actor_id is not None and owner != actor_id:
            raise error("access_denied", "task owner mismatch")
        if config.owner_scope == "user":
            if config.owner_user_id != owner:
                raise error("access_denied", "config owner mismatch")
        elif config.owner_scope != "admin" or config.owner_user_id is not None:
            raise error("access_denied", "invalid shared config")
        return owner

    @staticmethod
    def _scope_covers(granted, actual) -> bool:
        """Only approved mapping keys and list members; scalar types stay exact."""
        if isinstance(actual, dict):
            return isinstance(granted, dict) and all(
                isinstance(key, str) and key in granted and DisclosureService._scope_covers(granted[key], value)
                for key, value in actual.items()
            )
        if isinstance(actual, list):
            return isinstance(granted, list) and all(
                any(DisclosureService._scope_covers(item, value) for item in granted)
                for value in actual
            )
        if type(actual) not in (str, int, float, bool, type(None)):
            return False
        if isinstance(actual, float) and not math.isfinite(actual):
            return False
        return type(granted) is type(actual) and granted == actual

    def create(
        self, *, actor_id: str, task_id: str, config_id: str, content_category: str,
        scope_snapshot: Optional[dict] = None, ttl_seconds: int = DEFAULT_GRANT_TTL_SECONDS,
        config_version: Optional[int] = None, session: Optional[Session] = None,
    ) -> dict:
        if content_category not in CONTENT_CATEGORIES:
            raise error("invalid_field", "unknown content category")
        approved_scope = {} if scope_snapshot is None else scope_snapshot
        if not isinstance(approved_scope, dict) or not self._scope_covers(approved_scope, approved_scope):
            raise error("invalid_field", "scope must be a JSON mapping")
        if type(ttl_seconds) is not int or ttl_seconds <= 0 or ttl_seconds > DEFAULT_GRANT_TTL_SECONDS:
            raise error("invalid_field", "invalid grant lifetime")
        with (self.db.session() if session is None else nullcontext(session)) as sess:
            config = sess.scalar(select(ServiceConfigRow).where(ServiceConfigRow.config_id == config_id)
                                 .with_for_update().execution_options(populate_existing=True))
            if config is None or not config.is_active:
                raise error("config_not_found")
            if config_version is not None and config.config_version != config_version:
                raise error("disclosure_revoked", "config version changed")
            try:
                self._authorize(sess, task_id, config, actor_id)
            except Slice1Error as exc:
                self._audit("access.denied", actor_id=actor_id, object_id=task_id,
                            result="denied", reason=exc.code, session=session)
                raise
            now = utcnow()
            row = DisclosureGrantRow(
                grant_id=f"dg_{uuid.uuid4().hex[:12]}", owner_user_id=actor_id, task_id=task_id,
                config_id=config_id, config_version=config.config_version, service_kind=config.kind,
                service_endpoint=config.endpoint, content_category=content_category,
                scope_snapshot=json.dumps(approved_scope, ensure_ascii=False), granted_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            sess.add(row)
            sess.flush()
            self._audit("external.disclosure.confirmed", actor_id=actor_id, object_id=row.grant_id,
                        result="granted", session=sess)
            view = self._view(row)
            if session is None:
                sess.commit()
            return view

    def _check_chain(self, sess, grant, config, owner, actual_scope, now, *, expected_root_grant_id=None):
        seen = set()
        while grant is not None:
            if grant.grant_id in seen or len(seen) >= 16:
                raise error("disclosure_required", "invalid authorization chain")
            seen.add(grant.grant_id)
            if grant.owner_user_id != owner or grant.config_id != config.config_id:
                raise error("access_denied", "grant owner or config mismatch")
            self._authorize(sess, grant.task_id, config, owner)
            if grant.revoked_at is not None:
                raise error("disclosure_revoked")
            expires = grant.expires_at
            if expires is None:
                raise error("disclosure_expired")
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= now:
                raise error("disclosure_expired")
            if (grant.config_version != config.config_version or grant.service_kind != config.kind
                    or grant.service_endpoint != config.endpoint):
                raise error("disclosure_revoked", "config changed")
            try:
                scope = json.loads(grant.scope_snapshot)
            except (TypeError, ValueError):
                raise error("disclosure_required", "approved scope invalid") from None
            if (not isinstance(scope, dict) or not self._scope_covers(scope, scope)
                    or not self._scope_covers(scope, actual_scope)):
                raise error("disclosure_required", "actual scope exceeds approval")
            parent_id = grant.parent_grant_id
            if parent_id is None:
                if expected_root_grant_id is not None and grant.grant_id != expected_root_grant_id:
                    raise error("disclosure_required", "authorization root mismatch")
                return
            parent = sess.scalar(select(DisclosureGrantRow).where(DisclosureGrantRow.grant_id == parent_id)
                                 .with_for_update().execution_options(populate_existing=True))
            if parent is None or parent.content_category != grant.content_category:
                raise error("disclosure_required", "parent authorization missing")
            # The full child scope, not only this request, must remain a subset.
            actual_scope = scope
            grant = parent

    def derive(
        self, *, parent_grant_id: str, actor_id: str, task_id: str, config_id: str,
        content_category: str, scope_snapshot: dict, config_version: Optional[int] = None,
        session: Optional[Session] = None,
    ) -> dict:
        if content_category not in CONTENT_CATEGORIES or not isinstance(scope_snapshot, dict):
            raise error("invalid_field", "invalid derived authorization")
        if not self._scope_covers(scope_snapshot, scope_snapshot):
            raise error("invalid_field", "scope must be a JSON mapping")
        with (self.db.session() if session is None else nullcontext(session)) as sess:
            config = sess.scalar(select(ServiceConfigRow).where(ServiceConfigRow.config_id == config_id)
                                 .with_for_update().execution_options(populate_existing=True))
            if config is None or not config.is_active:
                raise error("config_not_found")
            if config_version is not None and config.config_version != config_version:
                raise error("disclosure_revoked", "config version changed")
            self._authorize(sess, task_id, config, actor_id)
            parent = sess.scalar(select(DisclosureGrantRow).where(DisclosureGrantRow.grant_id == parent_grant_id)
                                 .with_for_update().execution_options(populate_existing=True))
            if parent is None or parent.content_category != content_category:
                raise error("disclosure_required", "parent authorization missing")
            now = utcnow()
            self._check_chain(sess, parent, config, actor_id, scope_snapshot, now)
            row = DisclosureGrantRow(
                grant_id=f"dg_{uuid.uuid4().hex[:12]}", parent_grant_id=parent.grant_id,
                owner_user_id=actor_id, task_id=task_id, config_id=config_id,
                config_version=config.config_version, service_kind=config.kind,
                service_endpoint=config.endpoint, content_category=content_category,
                scope_snapshot=json.dumps(scope_snapshot, ensure_ascii=False), granted_at=now,
                expires_at=parent.expires_at,
            )
            sess.add(row)
            sess.flush()
            self._audit("external.disclosure.confirmed", actor_id=actor_id, object_id=row.grant_id,
                        result="granted", session=sess)
            view = self._view(row)
            if session is None:
                sess.commit()
            return view

    @staticmethod
    def _view(row: DisclosureGrantRow) -> dict:
        return {
            "grant_id": row.grant_id,
            "parent_grant_id": row.parent_grant_id,
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

    def list_for_task(self, task_id: str, *, actor_id: str, is_admin: bool = False,
                      session: Optional[Session] = None) -> list[dict]:
        with (self.db.session() if session is None else nullcontext(session)) as sess:
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
            self._audit(
                "external.disclosure.confirmed", actor_id=actor_id,
                object_id=grant_id, result="revoked", session=sess,
            )
            view = self._view(row)
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
        scope_snapshot: Optional[dict] = None,
        session: Optional[Session] = None,
        expected_grant_id: Optional[str] = None,
        expected_root_grant_id: Optional[str] = None,
    ) -> dict:
        """worker 出站前核验：授权有效、归属与实际范围、配置版本未变。

        失败抛受控错误码；调用方不得发起外发。
        """
        if content_category not in CONTENT_CATEGORIES:
            raise error("invalid_field", "unknown content category")
        if not isinstance(scope_snapshot, dict) or not self._scope_covers(scope_snapshot, scope_snapshot):
            raise error("disclosure_required", "actual scope missing or invalid")
        now = utcnow()
        with (self.db.session() if session is None else nullcontext(session)) as sess:
            config = sess.scalar(select(ServiceConfigRow).where(ServiceConfigRow.config_id == config_id)
                                 .with_for_update().execution_options(populate_existing=True))
            if config is None or not config.is_active:
                raise error("config_not_found", "config missing or inactive")
            owner = self._authorize(sess, task_id, config, actor_id)
            query = select(DisclosureGrantRow).where(
                DisclosureGrantRow.task_id == task_id,
                DisclosureGrantRow.config_id == config_id,
                DisclosureGrantRow.content_category == content_category,
                DisclosureGrantRow.owner_user_id == owner)
            if expected_grant_id is not None:
                query = query.where(DisclosureGrantRow.grant_id == expected_grant_id)
            rows = sess.scalars(query.order_by(DisclosureGrantRow.granted_at.desc())
                .with_for_update().execution_options(populate_existing=True)).all()
            if not rows:
                raise error("disclosure_required", "no grant covering this disclosure")
            grant = rows[0]
            self._check_chain(sess, grant, config, owner, scope_snapshot, now,
                expected_root_grant_id=expected_root_grant_id)
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
                session=kwargs.get("session"),
            )
            raise
