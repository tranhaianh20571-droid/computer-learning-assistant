"""身份 API 路由（T02）：注册/验证/审批/登录/登出/密码重置。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field

from ..logging_audit.audit import AuditWriteError
from .service import AuthError, AuthService


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=15, max_length=128)
    display_name: str = Field(default="", max_length=100)


class VerifyIn(BaseModel):
    token: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class PasswordResetRequestIn(BaseModel):
    email: EmailStr


class PasswordResetIn(BaseModel):
    token: str
    new_password: str = Field(min_length=15, max_length=128)


class ApproveIn(BaseModel):
    user_id: str
    reason: str = ""


def create_auth_router(service: AuthService) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    def actor_from_header(x_actor_id: str = Header(default="")) -> str:
        return x_actor_id or "anonymous"

    def session_user(request: Request):
        token = None
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
        if not token:
            raw = request.headers.get("Cookie", "")
            for part in raw.split(";"):
                k, _, v = part.strip().partition("=")
                if k == "session_token":
                    token = v
        if not token:
            raise HTTPException(status_code=401, detail={"error_code": "SESSION_INVALID", "message": "missing session"})
        try:
            return service.resolve_session(token)
        except AuthError as exc:
            raise HTTPException(status_code=exc.status, detail={"error_code": exc.code, "message": exc.message})

    @router.post("/register", status_code=201)
    def register(body: RegisterIn, request: Request) -> dict:
        try:
            return service.register(
                body.email, body.password, body.display_name,
                trace_id=getattr(request.state, "trace_id", None),
            )
        except AuthError as exc:
            raise HTTPException(status_code=exc.status, detail={"error_code": exc.code, "message": exc.message})

    @router.post("/verify-email")
    def verify_email(body: VerifyIn, request: Request) -> dict:
        try:
            return service.verify_email(body.token, trace_id=getattr(request.state, "trace_id", None))
        except AuthError as exc:
            raise HTTPException(status_code=exc.status, detail={"error_code": exc.code, "message": exc.message})

    @router.post("/login")
    def login(body: LoginIn, request: Request, response: Response) -> dict:
        try:
            info, raw_token = service.login(body.email, body.password, trace_id=getattr(request.state, "trace_id", None))
        except AuthError as exc:
            raise HTTPException(status_code=exc.status, detail={"error_code": exc.code, "message": exc.message})
        resp = {
            "user_id": info.user_id,
            "session_id": info.session_id,
            "csrf_token": info.csrf_token,
            "expires_at": info.expires_at.isoformat(),
        }
        # 通过 header 返回会话 token（前端存 httpOnly cookie 由浏览器管理；测试用 header）
        # 浏览器使用 HttpOnly cookie；保留响应字段供合成测试和本地 API 客户端使用。
        response.set_cookie(
            "session_token",
            raw_token,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="lax",
            max_age=14 * 24 * 60 * 60,
            path="/",
        )
        return resp | {"session_token": raw_token}

    @router.post("/logout")
    def logout(request: Request, response: Response) -> dict:
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not token:
            for part in request.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "session_token":
                    token = v
        try:
            result = service.logout(token, trace_id=getattr(request.state, "trace_id", None))
            response.delete_cookie("session_token", path="/")
            return result
        except AuthError as exc:
            raise HTTPException(status_code=exc.status, detail={"error_code": exc.code, "message": exc.message})

    @router.post("/admin/approve")
    def approve(body: ApproveIn, request: Request, user=Depends(session_user)) -> dict:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail={"error_code": "ACCESS_DENIED", "message": "admin only"})
        try:
            return service.approve(user.user_id, body.user_id, trace_id=getattr(request.state, "trace_id", None))
        except AuthError as exc:
            raise HTTPException(status_code=exc.status, detail={"error_code": exc.code, "message": exc.message})

    @router.post("/admin/reject")
    def reject(body: ApproveIn, request: Request, user=Depends(session_user)) -> dict:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail={"error_code": "ACCESS_DENIED", "message": "admin only"})
        try:
            return service.reject(user.user_id, body.user_id, body.reason, trace_id=getattr(request.state, "trace_id", None))
        except AuthError as exc:
            raise HTTPException(status_code=exc.status, detail={"error_code": exc.code, "message": exc.message})

    @router.post("/admin/disable")
    def disable(body: ApproveIn, request: Request, user=Depends(session_user)) -> dict:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail={"error_code": "ACCESS_DENIED", "message": "admin only"})
        try:
            return service.disable(user.user_id, body.user_id, trace_id=getattr(request.state, "trace_id", None))
        except AuthError as exc:
            raise HTTPException(status_code=exc.status, detail={"error_code": exc.code, "message": exc.message})

    @router.get("/me")
    def me(user=Depends(session_user)) -> dict:
        return {
            "user_id": user.user_id,
            "email": user.email,
            "role": user.role,
            "is_admin": user.is_admin,
            "status": user.status,
            "display_name": user.display_name,
        }

    @router.post("/password-reset/request")
    def reset_request(body: PasswordResetRequestIn, request: Request) -> dict:
        return service.request_password_reset(body.email, trace_id=getattr(request.state, "trace_id", None))

    @router.post("/password-reset/confirm")
    def reset_confirm(body: PasswordResetIn, request: Request) -> dict:
        try:
            return service.reset_password(body.token, body.new_password, trace_id=getattr(request.state, "trace_id", None))
        except AuthError as exc:
            raise HTTPException(status_code=exc.status, detail={"error_code": exc.code, "message": exc.message})

    return router
