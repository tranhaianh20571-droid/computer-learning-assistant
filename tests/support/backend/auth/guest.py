"""Guest principals for public local use.

Guest state is identified by an opaque HttpOnly cookie and mapped to the same
owner string used by the authenticated resource services.  No user row is
created, so the existing ownership schema remains unchanged.
"""

from __future__ import annotations

import hashlib
import secrets
from types import SimpleNamespace

from fastapi import HTTPException, Request

from .service import AuthError, AuthService

GUEST_COOKIE = "guest_id"
GUEST_TOKEN_STATE = "guest_token_to_set"


def _token_from_cookie(request: Request, name: str) -> str:
    return request.cookies.get(name, "").strip()


def _guest_principal(request: Request):
    raw = _token_from_cookie(request, GUEST_COOKIE)
    if not raw:
        # 同一请求内多次解析必须复用同一个新令牌，否则 owner 会在请求内漂移
        raw = getattr(request.state, GUEST_TOKEN_STATE, None) or secrets.token_urlsafe(32)
        setattr(request.state, GUEST_TOKEN_STATE, raw)
    owner_id = "guest_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return SimpleNamespace(
        user_id=owner_id,
        email="",
        role="guest",
        status="approved",
        display_name="访客",
        is_admin=False,
        is_guest=True,
    )


def resolve_user_or_guest(request: Request, auth: AuthService):
    """Resolve a real session first, then continue as the request's guest.

    Invalid bearer credentials remain errors.  An expired browser cookie falls
    back to a new guest principal so a stale login cannot block public use.
    """
    authorization = request.headers.get("Authorization", "")
    bearer = authorization[7:] if authorization.startswith("Bearer ") else ""
    if bearer:
        try:
            return auth.resolve_session(bearer)
        except AuthError as exc:
            raise HTTPException(
                status_code=exc.status,
                detail={"error_code": exc.code, "message": exc.message},
            ) from exc

    session_token = _token_from_cookie(request, "session_token")
    if session_token:
        try:
            return auth.resolve_session(session_token)
        except AuthError:
            pass
    return _guest_principal(request)


__all__ = ["GUEST_COOKIE", "GUEST_TOKEN_STATE", "resolve_user_or_guest"]
