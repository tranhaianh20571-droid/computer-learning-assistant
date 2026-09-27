"""本机连接器：配对、请求围栏、nonce 防重放、撤销（切片 1 T06）。

安全边界：
- 配对码一次性、短时（10 分钟）有效，绑定账户与设备。
- 连接器主动建立出站 WSS/TLS 通道；服务端校验绑定关系。
- 每个请求带账户、设备、任务、过期时间和 nonce；拒绝过期或重复 nonce。
- 连接器只接受绑定账户的任务，限制请求大小与调用类型。
- 连接器只允许本机回环目标；撤销后拒绝新请求并使在途请求失效。
"""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..logging_audit.db import Database
from ..logging_audit.repositories import PgAuditLog
from .errors import error
from .models import ConnectorBindingRow, ConnectorRequestRow, utcnow

PAIRING_TTL_SECONDS = 10 * 60
DEFAULT_REQUEST_TTL_SECONDS = 120
MAX_REQUEST_BYTES = 64 * 1024
ALLOWED_CALL_TYPES = frozenset({"model.generate", "model.stream", "tts.synthesize", "health"})


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def is_loopback_target(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname or ""
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            if ipaddress.ip_address(info[4][0]).is_loopback:
                return True
        except ValueError:
            continue
    return False


def validate_loopback_target(url: str) -> None:
    if not is_loopback_target(url):
        raise error("invalid_target", "connector target must be loopback")


class ConnectorService:
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
                object_type="connector_binding",
                object_id=object_id,
                result=result,
                reason=reason,
            )
        except Exception:  # noqa: BLE001
            pass

    # ---- 配对 ----
    def create_pairing(self, *, actor_id: str, device_name: str = "") -> dict:
        raw_code = secrets.token_urlsafe(18)
        binding_id = f"cb_{uuid.uuid4().hex[:12]}"
        now = utcnow()
        with self.db.session() as sess:
            sess.add(
                ConnectorBindingRow(
                    binding_id=binding_id,
                    owner_user_id=actor_id,
                    device_name=(device_name or "")[:120],
                    binding_token_hash=None,  # 绑定后写入
                    pairing_code_hash=_hash(raw_code),
                    pairing_expires_at=now + timedelta(seconds=PAIRING_TTL_SECONDS),
                    status="pending",
                )
            )
            sess.commit()
        self._audit("connector.bound", actor_id=actor_id, object_id=binding_id, result="pairing_issued")
        return {
            "binding_id": binding_id,
            "pairing_code": raw_code,
            "expires_at": _iso(now + timedelta(seconds=PAIRING_TTL_SECONDS)),
            "status": "pending",
        }

    def bind(self, *, pairing_code: str, device_name: str = "") -> dict:
        """连接器用配对码兑换一次性绑定令牌；配对码只能使用一次。"""
        code_hash = _hash(pairing_code)
        with self.db.session() as sess:
            row = sess.execute(
                select(ConnectorBindingRow).where(ConnectorBindingRow.pairing_code_hash == code_hash)
            ).scalar_one_or_none()
            if row is None:
                raise error("pairing_code_invalid")
            now = utcnow()
            expires = row.pairing_expires_at
            if expires is not None and expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if row.status != "pending" or expires is None or expires < now:
                raise error("pairing_code_invalid", "pairing code used or expired")
            raw_token = secrets.token_urlsafe(32)
            row.binding_token_hash = _hash(raw_token)
            row.pairing_code_hash = None  # 一次性
            row.status = "bound"
            row.bound_at = now
            # last_seen_at 仅在连接器建立通道/心跳后更新，单纯绑定不视为在线
            if device_name:
                row.device_name = device_name[:120]
            sess.flush()
            result = {
                "binding_id": row.binding_id,
                "binding_token": raw_token,
                "status": "bound",
                "device_name": row.device_name,
            }
            owner = row.owner_user_id
            sess.commit()
        self._audit("connector.bound", actor_id=owner, object_id=result["binding_id"], result="bound")
        return result

    def resolve_binding(self, binding_token: str) -> dict:
        token_hash = _hash(binding_token)
        with self.db.session() as sess:
            row = sess.execute(
                select(ConnectorBindingRow).where(ConnectorBindingRow.binding_token_hash == token_hash)
            ).scalar_one_or_none()
            if row is None:
                raise error("binding_not_found")
            if row.status == "revoked":
                raise error("connector_revoked")
            if row.status != "bound":
                raise error("connector_offline", "binding not active")
            return {
                "binding_id": row.binding_id,
                "owner_user_id": row.owner_user_id,
                "device_name": row.device_name,
                "status": row.status,
            }

    def mark_seen(self, binding_id: str) -> None:
        with self.db.session() as sess:
            row = sess.get(ConnectorBindingRow, binding_id)
            if row is not None and row.status == "bound":
                row.last_seen_at = utcnow()
                sess.commit()

    def list_bindings(self, *, actor_id: str, is_admin: bool = False) -> list[dict]:
        with self.db.session() as sess:
            rows = (
                sess.execute(
                    select(ConnectorBindingRow).order_by(ConnectorBindingRow.created_at.desc())
                )
                .scalars()
                .all()
            )
            return [
                self._binding_view(r)
                for r in rows
                if is_admin or r.owner_user_id == actor_id
            ]

    @staticmethod
    def _binding_view(row: ConnectorBindingRow) -> dict:
        online = False
        if row.status == "bound" and row.last_seen_at is not None:
            seen = row.last_seen_at
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            online = (utcnow() - seen).total_seconds() < 120
        status = row.status
        if status == "bound" and not online:
            status = "offline"
        return {
            "binding_id": row.binding_id,
            "device_name": row.device_name,
            "status": status,
            "bound_at": _iso(row.bound_at),
            "revoked_at": _iso(row.revoked_at),
            "last_seen_at": _iso(row.last_seen_at),
        }

    def revoke(self, binding_id: str, *, actor_id: str, is_admin: bool = False) -> dict:
        with self.db.session() as sess:
            row = sess.get(ConnectorBindingRow, binding_id)
            if row is None:
                raise error("binding_not_found")
            if not is_admin and row.owner_user_id != actor_id:
                raise error("binding_not_found")
            row.status = "revoked"
            row.revoked_at = utcnow()
            # 在途请求失效
            pending = (
                sess.execute(
                    select(ConnectorRequestRow).where(
                        ConnectorRequestRow.binding_id == binding_id,
                        ConnectorRequestRow.status.in_(("pending", "delivered")),
                    )
                )
                .scalars()
                .all()
            )
            for req in pending:
                req.status = "rejected"
                req.updated_at = utcnow()
            sess.flush()
            view = self._binding_view(row)
            owner = row.owner_user_id
            sess.commit()
        self._audit("connector.revoked", actor_id=actor_id, object_id=binding_id, result="revoked")
        return view

    # ---- 请求围栏 ----
    def enqueue_request(
        self,
        *,
        binding_id: str,
        task_id: str,
        call_type: str = "model.generate",
        payload: Optional[dict] = None,
        ttl_seconds: int = DEFAULT_REQUEST_TTL_SECONDS,
    ) -> dict:
        if call_type not in ALLOWED_CALL_TYPES:
            raise error("invalid_field", f"call_type not allowed: {call_type}")
        if payload is not None:
            import json

            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if len(encoded) > MAX_REQUEST_BYTES:
                raise error("invalid_field", "request too large")
        now = utcnow()
        request_id = f"cr_{uuid.uuid4().hex[:12]}"
        nonce = secrets.token_urlsafe(24)
        with self.db.session() as sess:
            binding = sess.get(ConnectorBindingRow, binding_id)
            if binding is None:
                raise error("binding_not_found")
            if binding.status == "revoked":
                raise error("connector_revoked")
            if binding.status != "bound":
                raise error("connector_offline")
            row = ConnectorRequestRow(
                request_id=request_id,
                binding_id=binding_id,
                task_id=task_id,
                nonce=nonce,
                status="pending",
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            sess.add(row)
            sess.flush()
            view = self._request_view(row, payload=payload, call_type=call_type)
            sess.commit()
        return view

    @staticmethod
    def _request_view(row: ConnectorRequestRow, *, payload: Optional[dict] = None, call_type: str = "") -> dict:
        return {
            "request_id": row.request_id,
            "binding_id": row.binding_id,
            "task_id": row.task_id,
            "nonce": row.nonce,
            "call_type": call_type,
            "payload": payload,
            "status": row.status,
            "expires_at": _iso(row.expires_at),
        }

    def accept_request(
        self,
        *,
        binding_id: str,
        task_id: str,
        nonce: str,
        owner_user_id: str,
        task_owner_user_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> dict:
        """服务端/连接器共同核验：绑定有效、账户匹配、任务归属、未过期、nonce 未用过。"""
        now = now or utcnow()
        with self.db.session() as sess:
            binding = sess.get(ConnectorBindingRow, binding_id)
            if binding is None:
                raise error("binding_not_found")
            if binding.status == "revoked":
                raise error("connector_revoked")
            if binding.status != "bound":
                raise error("connector_offline")
            if binding.owner_user_id != owner_user_id:
                raise error("access_denied", "binding owner mismatch")
            if task_owner_user_id is not None and task_owner_user_id != owner_user_id:
                raise error("access_denied", "task owner mismatch")

            existing = sess.execute(
                select(ConnectorRequestRow).where(ConnectorRequestRow.nonce == nonce)
            ).scalar_one_or_none()
            if existing is not None and existing.status in ("delivered", "completed", "rejected"):
                raise error("nonce_replay")

            request_row = existing
            if request_row is None:
                request_row = sess.execute(
                    select(ConnectorRequestRow).where(
                        ConnectorRequestRow.binding_id == binding_id,
                        ConnectorRequestRow.task_id == task_id,
                        ConnectorRequestRow.nonce == nonce,
                    )
                ).scalar_one_or_none()
            expires = request_row.expires_at if request_row is not None else None
            if expires is not None:
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires < now:
                    raise error("request_expired")

            # 记录 nonce（唯一约束兜底防并发重放）
            row = request_row or ConnectorRequestRow(
                request_id=f"cr_{uuid.uuid4().hex[:12]}",
                binding_id=binding_id,
                task_id=task_id,
                nonce=nonce,
                status="delivered",
                expires_at=now + timedelta(seconds=DEFAULT_REQUEST_TTL_SECONDS),
            )
            row.status = "delivered"
            row.updated_at = now
            if request_row is None:
                sess.add(row)
            try:
                sess.flush()
            except IntegrityError as exc:
                sess.rollback()
                raise error("nonce_replay") from exc
            view = self._request_view(row)
            sess.commit()
            return view

    def complete_request(self, request_id: str, *, result_ok: bool) -> dict:
        with self.db.session() as sess:
            row = sess.get(ConnectorRequestRow, request_id)
            if row is None:
                raise error("config_not_found", "request not found")
            row.status = "completed" if result_ok else "rejected"
            row.updated_at = utcnow()
            sess.flush()
            view = self._request_view(row)
            sess.commit()
            return view

    def is_online(self, binding_id: str) -> bool:
        with self.db.session() as sess:
            row = sess.get(ConnectorBindingRow, binding_id)
            if row is None or row.status != "bound" or row.last_seen_at is None:
                return False
            seen = row.last_seen_at
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            return (utcnow() - seen).total_seconds() < 120
