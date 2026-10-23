"""Authenticated principals; legacy import compatibility never creates guests."""
from fastapi import HTTPException, Request

from .service import AuthError, AuthService


def require_user(request: Request, auth: AuthService):
    authorization = request.headers.get("Authorization", "")
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    if authorization and not token:
        raise HTTPException(401, detail={"error_code": "SESSION_INVALID", "message": "请登录后继续。"})
    token = token or request.cookies.get("session_token", "")
    if not token:
        raise HTTPException(401, detail={"error_code": "SESSION_INVALID", "message": "请登录后继续。"})
    try:
        return auth.resolve_session(token)
    except AuthError as exc:
        raise HTTPException(exc.status, detail={"error_code": exc.code, "message": "登录已失效，请重新登录。"}) from None


resolve_user_or_guest = require_user
