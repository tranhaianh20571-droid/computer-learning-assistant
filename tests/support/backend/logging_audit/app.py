"""FastAPI 应用：任务事件 SSE/REST + 审计 + trace 中间件。

测试支持入口；生产应用位于 server/backend/app/。
契约与 server.py / 架构 §2、§3.2 对齐。
"""

from __future__ import annotations

from typing import Any, Generator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .audit import AuditAccessDeniedError, AuditImmutabilityError, AuditWriteError
from .contracts import ContractError
from .db import Database, get_db, set_db
from .logger import MemoryLogStream, StructuredLogger
from .repositories import PgAuditLog, PgTaskEventStore
from .task_events import AccessDeniedError, EventsExpiredError
from .trace import ensure_trace, trace_context


class AppState:
    def __init__(self, db: Database, logger: Optional[StructuredLogger] = None) -> None:
        self.db = db
        self.tasks = PgTaskEventStore(db)
        self.audit = PgAuditLog(db)
        self.logger = logger or StructuredLogger("api", "test", stream=MemoryLogStream())


class TaskCreate(BaseModel):
    task_id: str
    owner_user_id: str
    subject_id: str
    stage: str = "upload"
    generation: int = 1
    attempt: int = 1
    progress_total: int = 0
    template_id: Optional[str] = None
    template_params: dict[str, Any] = Field(default_factory=dict)


class TaskEventIn(BaseModel):
    task_id: str
    stage: str = "save"
    status: str = "succeeded"
    error_code: Optional[str] = None
    progress_current: Optional[int] = None
    progress_total: Optional[int] = None
    duration_ms: Optional[int] = None
    generation: Optional[int] = None
    attempt: Optional[int] = None
    actor_id: Optional[str] = None
    diagnostics_only: bool = False
    template_id: Optional[str] = None
    template_params: dict[str, Any] = Field(default_factory=dict)


class AuditIn(BaseModel):
    event: str
    actor_id: str
    result: str = "success"
    object_type: Optional[str] = None
    object_id: Optional[str] = None
    trace_id: Optional[str] = None
    reason: Optional[str] = None
    admin_id: Optional[str] = None
    role: str = "app"
    target_actor_id: Optional[str] = None


def create_app(state: Optional[AppState] = None) -> FastAPI:
    if state is None:
        db = get_db()
        state = AppState(db)
    app = FastAPI(title="learning-assistant-logging-audit", version="0.1.0")
    app.state.core = state

    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):  # noqa: ANN001
        ctx = ensure_trace(request.headers.get("traceparent"))
        with trace_context(ctx):
            request.state.trace_id = ctx.trace_id
            request.state.span_id = ctx.span_id
            response = await call_next(request)
            response.headers["X-Trace-Id"] = ctx.trace_id
            return response

    @app.exception_handler(ContractError)
    async def contract_err(_: Request, exc: ContractError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error_code": exc.code, "message": exc.message})

    @app.exception_handler(AccessDeniedError)
    async def denied(_: Request, exc: AccessDeniedError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"error_code": "ACCESS_DENIED", "message": str(exc)})

    @app.exception_handler(AuditAccessDeniedError)
    async def audit_denied(_: Request, exc: AuditAccessDeniedError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"error_code": "ACCESS_DENIED", "message": str(exc)})

    @app.exception_handler(EventsExpiredError)
    async def expired(_: Request, exc: EventsExpiredError) -> JSONResponse:
        return JSONResponse(
            status_code=410,
            content={
                "error_code": "EVENTS_EXPIRED",
                "message": exc.reason,
                "last_sequence": exc.last_sequence,
            },
        )

    @app.exception_handler(AuditWriteError)
    async def audit_write(_: Request, exc: AuditWriteError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"error_code": "AUDIT_WRITE_FAILED", "message": str(exc)})

    @app.exception_handler(AuditImmutabilityError)
    async def audit_immutable(_: Request, exc: AuditImmutabilityError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"error_code": "ACCESS_DENIED", "message": str(exc)})

    def core() -> AppState:
        return app.state.core

    def actor_id(x_actor_id: str = Header(default="anonymous")) -> str:
        return x_actor_id

    def role(x_role: str = Header(default="app")) -> str:
        return x_role

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.post("/tasks", status_code=201)
    def create_task(
        body: TaskCreate,
        request: Request,
        actor: str = Depends(actor_id),
    ) -> dict:
        st = core()
        event = st.tasks.create_task(
            body.task_id,
            body.owner_user_id or actor,
            body.subject_id,
            body.stage,
            generation=body.generation,
            attempt=body.attempt,
            progress_total=body.progress_total,
            template_id=body.template_id,
            template_params=body.template_params,
        )
        st.logger.log(
            "task.accepted",
            level="INFO",
            task_id=body.task_id,
            attempt=body.attempt,
            actor_id=body.owner_user_id or actor,
            stage=body.stage,
            status="started",
            trace_id=getattr(request.state, "trace_id", None),
        )
        return {"task_id": body.task_id, "sequence": event.sequence, "event_id": event.event_id}

    @app.post("/tasks/events", status_code=201)
    def post_event(body: TaskEventIn, request: Request) -> dict:
        st = core()
        event = st.tasks.record(
            body.task_id,
            stage=body.stage,
            status=body.status,
            error_code=body.error_code,
            progress_current=body.progress_current,
            progress_total=body.progress_total,
            duration_ms=body.duration_ms,
            generation=body.generation,
            attempt=body.attempt,
            actor_id=body.actor_id,
            diagnostics_only=body.diagnostics_only,
        )
        event_name = {
            "started": "task.started",
            "succeeded": "task.stage.succeeded",
            "partial": "task.stage.partial",
            "failed": "task.stage.failed",
            "retry_scheduled": "task.retry_scheduled",
            "cancelled": "task.cancelled",
            "stale_discarded": "task.stale_discarded",
        }.get(body.status, "task.stage.failed")
        st.logger.log(
            event_name,
            level="INFO",
            task_id=body.task_id,
            stage=body.stage,
            status=body.status,
            error_code=body.error_code,
            duration_ms=body.duration_ms,
            attempt=body.attempt,
        )
        return {"event_id": event.event_id, "sequence": event.sequence, "status": event.status}

    @app.get("/tasks/{task_id}/status")
    def get_status(task_id: str, actor: str = Depends(actor_id)) -> dict:
        return core().tasks.full_status(task_id, actor_id=actor)

    @app.get("/tasks/{task_id}/events.json")
    def get_events_json(
        task_id: str,
        after_sequence: int = Query(default=0),
        actor: str = Depends(actor_id),
    ) -> dict:
        events = core().tasks.get_events(task_id, actor_id=actor, after_sequence=after_sequence)
        return {
            "task_id": task_id,
            "events": [
                {
                    "event_id": e.event_id,
                    "sequence": e.sequence,
                    "stage": e.stage,
                    "status": e.status,
                    "error_code": e.error_code,
                    "created_at": e.created_at,
                    "progress_current": e.progress_current,
                    "progress_total": e.progress_total,
                }
                for e in events
            ],
        }

    @app.get("/tasks/{task_id}/events")
    def sse_events(
        task_id: str,
        actor: str = Depends(actor_id),
        last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
        x_last_sequence: Optional[str] = Header(default=None, alias="X-Last-Sequence"),
    ):
        last_sequence = int(x_last_sequence) if x_last_sequence and x_last_sequence.isdigit() else None
        events, next_seq = core().tasks.replay_sse(
            task_id,
            actor_id=actor,
            last_event_id=last_event_id,
            last_sequence=last_sequence,
        )

        def gen() -> Generator[str, None, None]:
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
                    "generation": e.generation,
                    "attempt": e.attempt,
                }
                import json as _json

                yield f"id: {e.event_id}\nevent: task\ndata: {_json.dumps(payload, ensure_ascii=False)}\n\n"
            yield f": cursor {next_seq}\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/audit", status_code=201)
    def post_audit(body: AuditIn, request: Request) -> dict:
        st = core()
        rec = st.audit.append(
            body.event,
            actor_id=body.actor_id,
            result=body.result,
            object_type=body.object_type,
            object_id=body.object_id,
            trace_id=body.trace_id or getattr(request.state, "trace_id", None),
            reason=body.reason,
            admin_id=body.admin_id,
            role=body.role,
            target_actor_id=body.target_actor_id,
        )
        return {"audit_id": rec.audit_id, "content_hash": rec.content_hash}

    @app.get("/audit")
    def get_audit(
        actor: str = Depends(actor_id),
        role: str = Depends(role),
        event: Optional[str] = None,
        object_type: Optional[str] = None,
        object_id: Optional[str] = None,
        limit: int = Query(default=100, le=500),
    ) -> dict:
        records = core().audit.query(
            actor_id=actor,
            role=role,
            event=event,
            object_type=object_type,
            object_id=object_id,
            limit=limit,
        )
        return {"records": [r.to_record() for r in records]}

    return app


app = create_app()
