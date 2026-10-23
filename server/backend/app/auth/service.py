"""身份服务：注册、邮箱验证、审批、登录会话、密码重置（T02）。

约束（架构 §3 / PRD 1.6）：
- 账户状态机 pending_email → pending_approval → approved | rejected | disabled
- 密码 Argon2id，最少 8 字符
- 会话持久随机 token，只存哈希；停用/登出/重置立即撤销
- 登录失败 15 分钟内 5 次 → 锁定 15 分钟
- 重置令牌只存哈希、30 分钟一次性
- 成功/失败身份操作写审计，不记密码/令牌/邮箱
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..logging_audit.audit import AuditRecord
from ..logging_audit.db import Database
from ..logging_audit.repositories import PgAuditLog
from .models import (
    EmailTokenRow,
    LoginAttemptRow,
    LoginSessionRow,
    PasswordResetTokenRow,
    UserRow,
    utcnow,
)

_ph = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2)

MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
LOCK_THRESHOLD = 5
LOCK_WINDOW_MIN = 15
LOCK_DURATION_MIN = 15
RESET_TOKEN_TTL_MIN = 30
EMAIL_TOKEN_TTL_H = 24
SESSION_TTL_DAYS = 14


class AuthError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class SessionInfo:
    session_id: str
    user_id: str
    csrf_token: str
    expires_at: datetime


class AuthService:
    def __init__(self, db: Database, audit: PgAuditLog) -> None:
        self.db = db
        self.audit = audit

    # ---- helpers ----
    def _get_user_by_email(self, sess: Session, email: str) -> Optional[UserRow]:
        return sess.execute(select(UserRow).where(UserRow.email == email)).scalar_one_or_none()

    def _revoke_user_sessions(self, sess: Session, user_id: str) -> int:
        rows = (
            sess.execute(
                select(LoginSessionRow).where(
                    LoginSessionRow.user_id == user_id, LoginSessionRow.revoked_at.is_(None)
                )
            )
            .scalars()
            .all()
        )
        now = utcnow()
        for r in rows:
            r.revoked_at = now
        return len(rows)

    def _count_recent_failures(self, sess: Session, email: str) -> int:
        since = utcnow() - timedelta(minutes=LOCK_WINDOW_MIN)
        return len(
            sess.execute(
                select(LoginAttemptRow).where(
                    LoginAttemptRow.email == email,
                    LoginAttemptRow.success.is_(False),
                    LoginAttemptRow.attempted_at >= since,
                )
            )
            .scalars()
            .all()
        )

    def _hash_password(self, password: str) -> str:
        if len(password) < MIN_PASSWORD_LEN:
            raise AuthError("PASSWORD_TOO_SHORT", f"password must be at least {MIN_PASSWORD_LEN} chars")
        if len(password) > MAX_PASSWORD_LEN:
            raise AuthError("PASSWORD_TOO_LONG", "password too long")
        return _ph.hash(password)

    def _verify_password(self, password_hash: str, password: str) -> bool:
        try:
            return _ph.verify(password_hash, password)
        except (VerifyMismatchError, Exception):
            return False

    # ---- register ----
    def register(self, email: str, password: str, display_name: str = "", trace_id: Optional[str] = None,
                 *, open_registration: bool = False) -> dict:
        email = (email or "").strip().lower()
        if not EMAIL_RE.match(email):
            raise AuthError("EMAIL_INVALID", "invalid email")
        display_name = (display_name or "")[:100]

        with self.db.session() as sess:
            if self._get_user_by_email(sess, email):
                # 不泄露存在性：对外统一提示
                raise AuthError("AUTH_REJECTED", "registration rejected", status=400)
            user_id = f"user_{uuid.uuid4().hex[:12]}"
            pwd_hash = self._hash_password(password)
            user = UserRow(
                user_id=user_id,
                email=email,
                password_hash=pwd_hash,
                display_name=display_name,
                role="user",
                status="approved" if open_registration else "pending_email",
                is_admin=False,
            )
            sess.add(user)
            sess.flush()
            raw_token = None if open_registration else _new_token()
            if raw_token:
                sess.add(
                    EmailTokenRow(
                        token_id=f"etok_{uuid.uuid4().hex[:12]}",
                        user_id=user_id,
                        token_hash=_hash_token(raw_token),
                        purpose="verify_email",
                        expires_at=utcnow() + timedelta(hours=EMAIL_TOKEN_TTL_H),
                    )
                )
            sess.commit()

        self.audit.append(
            "auth.register.submitted",
            actor_id=user_id,
            result="success",
            object_type="user",
            object_id=user_id,
            trace_id=trace_id,
        )
        # 返回原始令牌仅用于测试/开发邮件夹具；生产由邮件发送
        if open_registration:
            return {"user_id": user_id, "status": "approved"}
        return {"user_id": user_id, "verify_token": raw_token, "status": "pending_email"}

    def verify_email(self, token: str, trace_id: Optional[str] = None) -> dict:
        token_hash = _hash_token(token)
        with self.db.session() as sess:
            row = sess.execute(
                select(EmailTokenRow).where(
                    EmailTokenRow.token_hash == token_hash, EmailTokenRow.used_at.is_(None)
                )
            ).scalar_one_or_none()
            if row is None or row.expires_at < utcnow():
                raise AuthError("TOKEN_INVALID", "invalid or expired token")
            user = sess.get(UserRow, row.user_id)
            if user is None:
                raise AuthError("TOKEN_INVALID", "invalid token")
            row.used_at = utcnow()
            if user.status == "pending_email":
                user.status = "pending_approval"
            user.updated_at = utcnow()
            sess.commit()
            uid = user.user_id
            status = user.status

        self.audit.append(
            "auth.email.verified",
            actor_id=uid,
            result="success",
            object_type="user",
            object_id=uid,
            trace_id=trace_id,
        )
        return {"user_id": uid, "status": status}

    # ---- admin approval ----
    def _require_admin(self, sess: Session, admin_id: str) -> UserRow:
        admin = sess.get(UserRow, admin_id)
        if admin is None or not admin.is_admin or admin.status != "approved":
            raise AuthError("ACCESS_DENIED", "admin only", status=403)
        return admin

    def list_pending(self, admin_id: str) -> list[dict]:
        """待审批用户列表（管理员入口）；只返回 pending_approval 状态。"""
        with self.db.session() as sess:
            self._require_admin(sess, admin_id)
            rows = (
                sess.execute(
                    select(UserRow)
                    .where(UserRow.status == "pending_approval")
                    .order_by(UserRow.created_at)
                )
                .scalars()
                .all()
            )
            return [
                {
                    "user_id": u.user_id,
                    "email": u.email,
                    "display_name": u.display_name,
                    "created_at": u.created_at.isoformat() if u.created_at else None,
                }
                for u in rows
            ]

    def approve(self, admin_id: str, target_user_id: str, trace_id: Optional[str] = None) -> dict:
        with self.db.session() as sess:
            self._require_admin(sess, admin_id)
            target = sess.get(UserRow, target_user_id)
            if target is None:
                raise AuthError("USER_NOT_FOUND", "user not found", status=404)
            if target.status != "pending_approval":
                raise AuthError("STATE_INVALID", f"cannot approve from {target.status}")
            if target.is_admin:
                raise AuthError("ACCESS_DENIED", "cannot approve admin", status=403)
            target.status = "approved"
            target.updated_at = utcnow()
            sess.commit()
            tid = target.user_id

        self.audit.append(
            "admin.approval.changed",
            actor_id=admin_id,
            admin_id=admin_id,
            target_actor_id=tid,
            object_type="user",
            object_id=tid,
            result="approved",
            trace_id=trace_id,
        )
        return {"user_id": tid, "status": "approved"}

    def reject(self, admin_id: str, target_user_id: str, reason: str = "", trace_id: Optional[str] = None) -> dict:
        with self.db.session() as sess:
            self._require_admin(sess, admin_id)
            target = sess.get(UserRow, target_user_id)
            if target is None:
                raise AuthError("USER_NOT_FOUND", "user not found", status=404)
            if target.status not in ("pending_approval", "pending_email", "approved"):
                raise AuthError("STATE_INVALID", f"cannot reject from {target.status}")
            target.status = "rejected"
            target.updated_at = utcnow()
            self._revoke_user_sessions(sess, target.user_id)
            sess.commit()
            tid = target.user_id

        self.audit.append(
            "admin.approval.changed",
            actor_id=admin_id,
            admin_id=admin_id,
            target_actor_id=tid,
            object_type="user",
            object_id=tid,
            result="rejected",
            reason=reason[:200] or None,
            trace_id=trace_id,
        )
        return {"user_id": tid, "status": "rejected"}

    def disable(self, admin_id: str, target_user_id: str, trace_id: Optional[str] = None) -> dict:
        with self.db.session() as sess:
            self._require_admin(sess, admin_id)
            target = sess.get(UserRow, target_user_id)
            if target is None:
                raise AuthError("USER_NOT_FOUND", "user not found", status=404)
            if target.is_admin and target.user_id == admin_id:
                raise AuthError("ACCESS_DENIED", "cannot disable self", status=403)
            target.status = "disabled"
            target.updated_at = utcnow()
            revoked = self._revoke_user_sessions(sess, target.user_id)
            sess.commit()
            tid = target.user_id

        self.audit.append(
            "admin.approval.changed",
            actor_id=admin_id,
            admin_id=admin_id,
            target_actor_id=tid,
            object_type="user",
            object_id=tid,
            result="disabled",
            trace_id=trace_id,
        )
        return {"user_id": tid, "status": "disabled", "sessions_revoked": revoked}

    def ensure_bootstrap_admin(self, email: str, password: str) -> dict:
        """一次性初始化管理员（仅当无任何 admin 时）。"""
        email = email.strip().lower()
        with self.db.session() as sess:
            existing = sess.execute(select(UserRow).where(UserRow.is_admin.is_(True))).scalars().first()
            if existing:
                return {"user_id": existing.user_id, "status": existing.status, "created": False}
            user_id = f"user_{uuid.uuid4().hex[:12]}"
            sess.add(
                UserRow(
                    user_id=user_id,
                    email=email,
                    password_hash=self._hash_password(password),
                    display_name="admin",
                    role="admin",
                    status="approved",
                    is_admin=True,
                )
            )
            sess.commit()
        self.audit.append(
            "admin.approval.changed",
            actor_id=user_id,
            admin_id=user_id,
            target_actor_id=user_id,
            object_type="user",
            object_id=user_id,
            result="bootstrap_admin",
        )
        return {"user_id": user_id, "status": "approved", "created": True}

    # ---- login / session ----
    def login(self, email: str, password: str, trace_id: Optional[str] = None,
              *, open_registration: bool = False) -> tuple[SessionInfo, str]:
        """返回 (SessionInfo, raw_session_token)。"""
        email = (email or "").strip().lower()
        generic = AuthError("AUTH_FAILED", "invalid credentials", status=401)

        with self.db.session() as sess:
            now = utcnow()
            # 锁定检查
            failures = self._count_recent_failures(sess, email)
            if failures >= LOCK_THRESHOLD:
                self.audit.append(
                    "auth.login.failed",
                    actor_id="unknown",
                    result="locked",
                    reason="lockout",
                    trace_id=trace_id,
                )
                raise AuthError("AUTH_LOCKED", "temporarily locked", status=429)

            user = self._get_user_by_email(sess, email)
            if user is None or not self._verify_password(user.password_hash, password):
                sess.add(LoginAttemptRow(email=email, success=False))
                self.audit.append(
                    "auth.login.failed",
                    actor_id=user.user_id if user else "unknown",
                    result="failed",
                    trace_id=trace_id,
                )
                sess.commit()
                raise generic

            if open_registration and user.status in ("pending_email", "pending_approval"):
                user.status, user.updated_at = "approved", now
                self.audit.append("admin.approval.changed", actor_id=user.user_id,
                    target_actor_id=user.user_id, object_type="user", object_id=user.user_id,
                    result="open_registration", trace_id=trace_id, session=sess)
            if user.status != "approved":
                sess.add(LoginAttemptRow(email=email, success=False))
                self.audit.append(
                    "auth.login.failed",
                    actor_id=user.user_id,
                    result="denied",
                    reason=f"status={user.status}",
                    trace_id=trace_id,
                )
                sess.commit()
                raise generic

            sess.add(LoginAttemptRow(email=email, success=True))
            raw_session = _new_token()
            raw_csrf = _new_token()
            sid = f"sess_{uuid.uuid4().hex[:12]}"
            sess.add(
                LoginSessionRow(
                    session_id=sid,
                    user_id=user.user_id,
                    session_token_hash=_hash_token(raw_session),
                    csrf_token_hash=_hash_token(raw_csrf),
                    expires_at=now + timedelta(days=SESSION_TTL_DAYS),
                )
            )
            user.failed_login_count = 0
            sess.commit()
            return SessionInfo(
                session_id=sid,
                user_id=user.user_id,
                csrf_token=raw_csrf,
                expires_at=now + timedelta(days=SESSION_TTL_DAYS),
            ), raw_session  # type: ignore[return-value]

    def ensure_market_identity(self, user_id: str, email: str, display_name: str = "") -> str:
        """Create an account space after market authorization, without opening a session."""
        email = (email or "").strip().lower()
        if not user_id or not email:
            raise AuthError("SESSION_INVALID", "market identity invalid", status=401)
        with self.db.session() as sess:
            user = sess.get(UserRow, user_id)
            if user is not None and user.email != email:
                raise AuthError("SESSION_INVALID", "market identity mismatch", status=409)
            if user is None:
                user = sess.execute(select(UserRow).where(UserRow.email == email)).scalar_one_or_none()
            if user is None:
                user = UserRow(user_id=user_id, email=email, password_hash="", display_name=display_name,
                               role="user", status="approved", is_admin=False)
                sess.add(user)
            elif user.status != "approved":
                raise AuthError("SESSION_INVALID", "account unavailable", status=403)
            return user.user_id

    def issue_market_session(self, user_id: str, email: str, display_name: str = "") -> tuple[str, str]:
        """Mirror a market account locally and open a local session.

        Returns (raw_session_token, local_user_id). An existing local account with
        the same email is reused so migrated installs keep their data and owner ids;
        otherwise the market id becomes the local id.

        The password hash stays empty: this account can only be used through the
        market login flow, never through the local password endpoint.
        """
        email = (email or "").strip().lower()
        with self.db.session() as sess:
            user = sess.get(UserRow, user_id) if user_id else None
            if user is None and email:
                user = sess.execute(select(UserRow).where(UserRow.email == email)).scalar_one_or_none()
            if user is None:
                user = UserRow(user_id=user_id, email=email, password_hash="", display_name=display_name,
                               role="user", status="approved", is_admin=False)
                sess.add(user)
            else:
                if user.status in ("disabled", "rejected") or (user.user_id == user_id and user.email != email):
                    raise AuthError("SESSION_INVALID", "account unavailable", status=403)
                user.status = "approved"
                # The market owns the credential: drop any local password so the
                # local password endpoint can never authenticate this account.
                user.password_hash = ""
                user.display_name = display_name
            local_user_id = user.user_id
            raw_session = _new_token()
            raw_csrf = _new_token()
            sess.add(LoginSessionRow(
                session_id=f"sess_{uuid.uuid4().hex[:12]}",
                user_id=local_user_id,
                session_token_hash=_hash_token(raw_session),
                csrf_token_hash=_hash_token(raw_csrf),
                expires_at=utcnow() + timedelta(days=SESSION_TTL_DAYS),
            ))
            sess.commit()
        return raw_session, local_user_id

    def sync_market_profile(self, owner: str, email: str, display_name: str) -> None:
        with self.db.session() as sess:
            user = sess.get(UserRow, owner)
            if user is None or user.status != "approved" or user.email != email.strip().lower():
                raise AuthError("SESSION_INVALID", "account unavailable", status=403)
            user.display_name = display_name[:100]

    def logout(self, session_token: str, trace_id: Optional[str] = None) -> dict:
        th = _hash_token(session_token)
        with self.db.session() as sess:
            row = sess.execute(
                select(LoginSessionRow).where(
                    LoginSessionRow.session_token_hash == th, LoginSessionRow.revoked_at.is_(None)
                )
            ).scalar_one_or_none()
            if row is None:
                raise AuthError("SESSION_INVALID", "not logged in", status=401)
            row.revoked_at = utcnow()
            uid = row.user_id
            sess.commit()
        self.audit.append(
            "auth.session.revoked",
            actor_id=uid,
            result="logout",
            object_type="session",
            object_id=row.session_id,
            trace_id=trace_id,
        )
        return {"ok": True}

    def resolve_session(self, session_token: str) -> UserRow:
        th = _hash_token(session_token)
        with self.db.session() as sess:
            row = sess.execute(
                select(LoginSessionRow).where(
                    LoginSessionRow.session_token_hash == th, LoginSessionRow.revoked_at.is_(None)
                )
            ).scalar_one_or_none()
            if row is None or row.expires_at < utcnow():
                raise AuthError("SESSION_INVALID", "session invalid", status=401)
            user = sess.get(UserRow, row.user_id)
            if user is None or user.status not in ("approved",):
                raise AuthError("SESSION_INVALID", "session invalid", status=401)
            sess.expunge(user)
            return user

    # ---- password reset ----
    def request_password_reset(self, email: str, trace_id: Optional[str] = None) -> dict:
        email = (email or "").strip().lower()
        with self.db.session() as sess:
            user = self._get_user_by_email(sess, email)
            if user is None:
                # 不泄露存在性
                return {"ok": True, "reset_token": None}
            raw = _new_token()
            sess.add(
                PasswordResetTokenRow(
                    token_id=f"rtok_{uuid.uuid4().hex[:12]}",
                    user_id=user.user_id,
                    token_hash=_hash_token(raw),
                    expires_at=utcnow() + timedelta(minutes=RESET_TOKEN_TTL_MIN),
                )
            )
            sess.commit()
            uid = user.user_id
        self.audit.append(
            "auth.password.reset_requested",
            actor_id=uid,
            result="success",
            object_type="user",
            object_id=uid,
            trace_id=trace_id,
        )
        return {"ok": True, "reset_token": raw}

    def reset_password(self, token: str, new_password: str, trace_id: Optional[str] = None) -> dict:
        th = _hash_token(token)
        with self.db.session() as sess:
            row = sess.execute(
                select(PasswordResetTokenRow).where(
                    PasswordResetTokenRow.token_hash == th, PasswordResetTokenRow.used_at.is_(None)
                )
            ).scalar_one_or_none()
            if row is None or row.expires_at < utcnow():
                raise AuthError("TOKEN_INVALID", "invalid or expired token")
            user = sess.get(UserRow, row.user_id)
            if user is None:
                raise AuthError("TOKEN_INVALID", "invalid token")
            row.used_at = utcnow()
            user.password_hash = self._hash_password(new_password)
            user.updated_at = utcnow()
            revoked = self._revoke_user_sessions(sess, user.user_id)
            sess.commit()
            uid = user.user_id
        self.audit.append(
            "auth.password.reset_completed",
            actor_id=uid,
            result="success",
            object_type="user",
            object_id=uid,
            trace_id=trace_id,
        )
        return {"ok": True, "sessions_revoked": revoked}


