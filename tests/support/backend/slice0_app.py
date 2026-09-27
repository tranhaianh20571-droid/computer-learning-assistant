"""切片 0 完整 FastAPI 应用：身份 + 任务围栏 + SSE + 提示词绑定 + 观测。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from .auth import AuthError, AuthService
from .auth.router import create_auth_router
from .db import Database, get_db
from .logging_audit.app import AppState, create_app as create_logging_app
from .logging_audit.repositories import PgAuditLog, PgTaskEventStore
from .prompts import PromptService, PromptUnavailable
from .tasks import LeaseError, TaskService


class Slice0State:
    def __init__(self, db: Database, audit: PgAuditLog) -> None:
        self.db = db
        self.audit = audit
        self.events = PgTaskEventStore(db)
        self.auth = AuthService(db, audit)
        self.tasks = TaskService(db, self.events)
        self.prompts = PromptService(db)


def create_slice0_app(state: Optional[Slice0State] = None) -> FastAPI:
    if state is None:
        db = get_db()
        audit = PgAuditLog(db)
        state = Slice0State(db, audit)

    # 复用日志专项 app（trace 中间件 + task/audit 路由）
    from .logging_audit.app import AppState as LogState, create_app as create_log_app

    log_state = LogState(db=state.db, logger=None)
    log_state.tasks = state.events
    log_state.audit = state.audit
    app = create_log_app(log_state)
    app.title = "learning-assistant-slice-0"
    app.state.slice0 = state

    # CORS 供本地前端
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(create_auth_router(state.auth))

    @app.exception_handler(LeaseError)
    async def lease_err(_: Request, exc: LeaseError):
        from fastapi.responses import JSONResponse

        status = 404 if exc.code == "TASK_NOT_FOUND" else 409
        return JSONResponse(status_code=status, content={"error_code": exc.code, "message": exc.message})

    @app.exception_handler(PromptUnavailable)
    async def prompt_err(_: Request, exc: PromptUnavailable):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=409, content={"error_code": "PROMPT_UNAVAILABLE", "message": exc.reason}
        )

    def _session_actor(request: Request) -> str:
        raw = request.headers.get("Authorization", "")
        token = raw[7:] if raw.startswith("Bearer ") else ""
        if not token:
            for part in request.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "session_token":
                    token = v
        if not token:
            raise HTTPException(status_code=401, detail={"error_code": "SESSION_INVALID", "message": "auth required"})
        try:
            user = state.auth.resolve_session(token)
            return user.user_id
        except AuthError as exc:
            raise HTTPException(status_code=exc.status, detail={"error_code": exc.code, "message": exc.message})

    @app.get("/api/tasks/{task_id}/sse")
    def sse_for_session(task_id: str, request: Request):
        actor = _session_actor(request)
        from fastapi.responses import StreamingResponse

        last_event_id = request.headers.get("Last-Event-ID")
        last_seq_hdr = request.headers.get("X-Last-Sequence")
        last_sequence = int(last_seq_hdr) if last_seq_hdr and last_seq_hdr.isdigit() else None
        events, next_seq = state.events.replay_sse(
            task_id,
            actor_id=actor,
            last_event_id=last_event_id,
            last_sequence=last_sequence,
        )

        def gen():
            import json as _json

            for e in events:
                payload = {
                    "event_id": e.event_id,
                    "sequence": e.sequence,
                    "stage": e.stage,
                    "status": e.status,
                    "error_code": e.error_code,
                    "progress_current": e.progress_current,
                    "progress_total": e.progress_total,
                    "created_at": e.created_at,
                }
                yield f"id: {e.event_id}\nevent: task\ndata: {_json.dumps(payload, ensure_ascii=False)}\n\n"
            yield f": cursor {next_seq}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @app.get("/api/tasks/{task_id}/status")
    def status_for_session(task_id: str, request: Request) -> dict:
        actor = _session_actor(request)
        return state.events.full_status(task_id, actor_id=actor)

    @app.post("/api/tasks", status_code=201)
    def create_task(body: dict, request: Request) -> dict:
        actor = _session_actor(request)
        idem = body.get("idempotency_key") or ""
        if not idem:
            raise HTTPException(status_code=400, detail={"error_code": "INVALID_FIELD", "message": "idempotency_key required"})
        kind = body.get("kind") or "generic"
        subject_id = body.get("subject_id") or subject_default(actor)
        prompt_name = body.get("prompt_name") or "lesson_step_v1"

        # 先建任务（含首事件），再绑提示词（无绑定禁止调用模型）
        created = state.tasks.create_task(
            owner_user_id=actor,
            kind=kind,
            subject_id=subject_id,
            idempotency_key=idem,
            stage=body.get("stage") or "upload",
            model_config_version=body.get("model_config_version"),
        )
        try:
            binding = state.prompts.bind_for_task(
                created["task_id"],
                prompt_name,
                allow_fallback=bool(body.get("allow_fallback", True)),
                source=body.get("prompt_source") or "local_fallback",
            )
        except PromptUnavailable as exc:
            # 绑定失败：任务保留但标记不可调用模型
            created["prompt_unavailable"] = exc.reason
            return created
        return created | {"prompt_binding_id": binding["binding_id"], "template_sha256": binding["template_sha256"]}

    def subject_default(actor: str) -> str:
        return f"subject_{actor[-6:]}"

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str, request: Request) -> dict:
        actor = _session_actor(request)
        return state.tasks.get_task(task_id, actor_id=actor)

    @app.post("/api/tasks/{task_id}/claim")
    def claim_task(task_id: str, body: dict, request: Request) -> dict:
        _session_actor(request)  # 需要有效会话
        lease = state.tasks.claim(
            task_id,
            lease_owner=body.get("lease_owner") or "worker-1",
            lease_seconds=int(body.get("lease_seconds") or 60),
        )
        return {
            "task_id": lease.task_id,
            "attempt_id": lease.attempt_id,
            "lease_token": lease.lease_token,
            "lease_owner": lease.lease_owner,
            "generation": lease.generation,
            "attempt": lease.attempt,
            "lease_until": lease.lease_until.isoformat(),
        }

    @app.post("/api/tasks/{task_id}/result")
    def submit_result(task_id: str, body: dict, request: Request) -> dict:
        _session_actor(request)
        from datetime import datetime, timezone

        from .tasks.service import Lease

        now = datetime.now(timezone.utc)
        lease = Lease(
            task_id=task_id,
            attempt_id=body.get("attempt_id") or "",
            lease_token=body.get("lease_token") or "",
            lease_owner=body.get("lease_owner") or "",
            generation=int(body.get("generation") or 1),
            attempt=int(body.get("attempt") or 1),
            lease_until=now,
            deadline_at=None,
        )
        return state.tasks.submit_result(
            lease,
            stage=body.get("stage") or "save",
            status=body.get("status") or "succeeded",
            error_code=body.get("error_code"),
            duration_ms=body.get("duration_ms"),
            progress_current=body.get("progress_current"),
            diagnostics_only=bool(body.get("diagnostics_only")),
        )

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str, request: Request) -> dict:
        actor = _session_actor(request)
        return state.tasks.request_cancel(task_id, actor_id=actor)

    @app.get("/api/tasks/{task_id}/prompt")
    def get_prompt(task_id: str, request: Request) -> dict:
        actor = _session_actor(request)
        state.tasks.get_task(task_id, actor_id=actor)  # 所有权
        return state.prompts.resolve_for_task(task_id)

    return app


app = create_slice0_app()
