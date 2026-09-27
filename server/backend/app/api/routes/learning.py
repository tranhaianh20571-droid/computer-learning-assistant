"""切片 0：任务/提示词/SSE/账户状态 API（基于 full-stack-fastapi-template）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.api.deps import CurrentUser
from app.core.db import engine
from app.models import User

from app.logging_audit.repositories import PgAuditLog, PgTaskEventStore
from app.logging_audit.db import Database
from app.prompts.service import PromptService, PromptUnavailable
from app.tasks.service import Lease, LeaseError, TaskService

router = APIRouter(prefix="/learning", tags=["learning"])

# 共享仓储（复用模板 DATABASE_URL）
_db = Database(str(__import__("app.core.config", fromlist=["settings"]).settings.DATABASE_URL))
_events = PgTaskEventStore(_db)
_audit = PgAuditLog(_db)
_tasks = TaskService(_db, _events)
_prompts = PromptService(_db)


class TaskCreateIn(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=128)
    kind: str = "generic"
    subject_id: str = "subject_default"
    stage: str = "upload"
    prompt_name: str = "lesson_step_v1"
    allow_fallback: bool = True


class TaskResultIn(BaseModel):
    attempt_id: str
    lease_token: str
    lease_owner: str
    generation: int = 1
    attempt: int = 1
    stage: str = "save"
    status: str = "succeeded"
    error_code: str | None = None
    duration_ms: int | None = None
    progress_current: int | None = None
    diagnostics_only: bool = False


class ClaimIn(BaseModel):
    lease_owner: str = "worker-1"
    lease_seconds: int = 60


@router.post("/tasks", status_code=201)
def create_task(body: TaskCreateIn, current_user: CurrentUser) -> dict:
    if not current_user.is_active or current_user.account_status != "approved":
        raise HTTPException(status_code=403, detail={"error_code": "ACCESS_DENIED", "message": "not approved"})
    created = _tasks.create_task(
        owner_user_id=str(current_user.id),
        kind=body.kind,
        subject_id=body.subject_id,
        idempotency_key=body.idempotency_key,
        stage=body.stage,
    )
    try:
        binding = _prompts.bind_for_task(
            created["task_id"],
            body.prompt_name,
            allow_fallback=body.allow_fallback,
            source="local_fallback",
        )
    except PromptUnavailable as exc:
        return created | {"prompt_unavailable": exc.reason}
    return created | {
        "prompt_binding_id": binding["binding_id"],
        "template_sha256": binding["template_sha256"],
    }


@router.get("/tasks/{task_id}")
def get_task(task_id: str, current_user: CurrentUser) -> dict:
    return _tasks.get_task(task_id, actor_id=str(current_user.id))


@router.get("/tasks/{task_id}/status")
def task_status(task_id: str, current_user: CurrentUser) -> dict:
    return _events.full_status(task_id, actor_id=str(current_user.id))


@router.get("/tasks/{task_id}/events")
def task_sse(
    task_id: str,
    request: Request,
    current_user: CurrentUser,
):
    last_event_id = request.headers.get("Last-Event-ID")
    last_seq_hdr = request.headers.get("X-Last-Sequence")
    last_sequence = int(last_seq_hdr) if last_seq_hdr and last_seq_hdr.isdigit() else None
    events, next_seq = _events.replay_sse(
        task_id,
        actor_id=str(current_user.id),
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

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/tasks/{task_id}/claim")
def claim_task(task_id: str, body: ClaimIn) -> dict:
    lease = _tasks.claim(task_id, lease_owner=body.lease_owner, lease_seconds=body.lease_seconds)
    return {
        "task_id": lease.task_id,
        "attempt_id": lease.attempt_id,
        "lease_token": lease.lease_token,
        "lease_owner": lease.lease_owner,
        "generation": lease.generation,
        "attempt": lease.attempt,
        "lease_until": lease.lease_until.isoformat(),
    }


@router.post("/tasks/{task_id}/result")
def submit_result(task_id: str, body: TaskResultIn) -> dict:
    lease = Lease(
        task_id=task_id,
        attempt_id=body.attempt_id,
        lease_token=body.lease_token,
        lease_owner=body.lease_owner,
        generation=body.generation,
        attempt=body.attempt,
        lease_until=datetime.now(UTC),
        deadline_at=None,
    )
    return _tasks.submit_result(
        lease,
        stage=body.stage,
        status=body.status,
        error_code=body.error_code,
        duration_ms=body.duration_ms,
        progress_current=body.progress_current,
        diagnostics_only=body.diagnostics_only,
    )


@router.get("/tasks/{task_id}/prompt")
def get_prompt(task_id: str, current_user: CurrentUser) -> dict:
    _tasks.get_task(task_id, actor_id=str(current_user.id))
    return _prompts.resolve_for_task(task_id)
