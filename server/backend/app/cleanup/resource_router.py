from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .manager import LocalResourceManager
from .models import LocalResourceJobRow
from .policy import ResourcePolicy
from ..materials.storage import StorageError


class PolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    policy: ResourcePolicy


class PinInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pinned: bool


class MigrationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination: str = Field(min_length=1, max_length=1024)


class CleanupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_ids: list[str] = Field(max_length=200)


def create_resource_router(manager: LocalResourceManager, resolve_user):
    router = APIRouter(prefix="/api/local-resources", tags=["local-resources"])

    def actor(request, admin=False):
        user = resolve_user(request)
        manager.require_ready()
        if admin and not user.is_admin:
            with manager.db.session() as session:
                manager._audit(session, user.user_id, "local_policy", "denied", "access.denied")
            raise StorageError("local_resources_admin_only")
        return user

    @router.get("")
    def status(request: Request, response: Response):
        user = actor(request)
        response.headers["Cache-Control"] = "no-store"
        return manager.snapshot(user.user_id, user.is_admin)

    @router.put("/policy")
    def policy(body: PolicyInput, request: Request):
        user = actor(request, True)
        return {"version": manager.save_policy(user.user_id, body.policy.model_dump(), body.version)}

    @router.put("/objects/{ident}/pin")
    def pin(ident: str, body: PinInput, request: Request):
        user = actor(request)
        manager.pin(user.user_id, ident, body.pinned, user.is_admin)
        return {"pinned": body.pinned}

    @router.post("/preview")
    def preview(request: Request, response: Response):
        user = actor(request)
        response.headers["Cache-Control"] = "no-store"
        return manager.preview(user.user_id, user.is_admin)

    @router.post("/cleanup")
    def cleanup(body: CleanupInput, request: Request):
        user = actor(request)
        # User-supplied ids are rechecked individually during execution.
        with manager.db.session() as session:
            from .models import LocalResourceRow
            rows = list(session.scalars(select(LocalResourceRow).where(LocalResourceRow.resource_id.in_(body.resource_ids))))
            if len(rows) != len(set(body.resource_ids)) or any(
                    row.owner_user_id != user.user_id and not user.is_admin for row in rows):
                raise StorageError("local_resource_not_found")
        allowed = set(manager.candidates(None if user.is_admin else user.user_id))
        if not set(body.resource_ids).issubset(allowed):
            raise StorageError("local_resource_conflict")
        return {"job_id": manager.enqueue(user.user_id, "cleanup",
            {"resource_ids": body.resource_ids, "global": user.is_admin})}

    @router.post("/scan")
    def scan(request: Request):
        user = actor(request, True)
        return {"job_id": manager.enqueue(user.user_id, "scan")}

    @router.post("/backup")
    def backup(request: Request):
        user = actor(request, True)
        return {"job_id": manager.enqueue(user.user_id, "backup")}

    @router.post("/migration")
    def migrate(body: MigrationInput, request: Request):
        user = actor(request, True)
        destination = manager.validate_destination(body.destination)
        return {"job_id": manager.enqueue(user.user_id, "migrate", {"destination": destination})}

    @router.get("/jobs/{ident}")
    def job(ident: str, request: Request, response: Response):
        user = actor(request)
        response.headers["Cache-Control"] = "no-store"
        with manager.db.session() as session:
            row = session.get(LocalResourceJobRow, ident)
            if not row or not user.is_admin and row.owner_user_id != user.user_id:
                raise StorageError("local_resource_not_found")
            return manager.job_view(row)

    @router.post("/jobs/{ident}/retry")
    def retry(ident: str, request: Request):
        user = actor(request)
        manager.cleanup.require_not_locked()
        with manager.db.session() as session:
            row = session.get(LocalResourceJobRow, ident, with_for_update=True)
            if not row or not user.is_admin and row.owner_user_id != user.user_id:
                raise StorageError("local_resource_not_found")
            if row.status not in ("failed","retry"):
                raise StorageError("local_resource_conflict")
            if row.kind in ("backup","migrate","scan") and not user.is_admin:
                raise StorageError("local_resources_admin_only")
            row.status, row.next_retry_at, row.error_code = "queued", None, None
            row.attempts = 0
            manager._audit(session,user.user_id,ident,"retry_requested")
        return {"job_id":ident}

    return router
