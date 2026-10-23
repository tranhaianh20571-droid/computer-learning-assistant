from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..planning.errors import error
from .models import MemorySourceRow
from .service import enqueue
from .feedback import StyleAspect, StylePreference


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class SettingsIn(StrictInput):
    enabled: bool
    expected_revision: int = Field(ge=0)


class FeedbackIn(StrictInput):
    card_revision: int = Field(ge=1)
    aspect: StyleAspect
    preference: StylePreference
    idempotency_key: str = Field(min_length=1, max_length=100)


class ReviewIn(StrictInput):
    query: str = Field(min_length=1, max_length=500)


class CorrectionIn(StrictInput):
    content: str = Field(min_length=1, max_length=1000)
    expected_revision: int = Field(ge=1)


class MaintenanceIn(StrictInput):
    mode: Literal["off", "preview", "automatic"]
    expected_revision: int = Field(ge=0)


def create_memory_router(service, resolve_user, characters):
    router = APIRouter(prefix="/api/memory", tags=["memory"])

    def owner(request: Request):
        return resolve_user(request).user_id

    def admin(request: Request):
        user = resolve_user(request)
        if not user.is_admin:
            from ..slice1.errors import error as config_error
            service.audit.append("access.denied", actor_id=user.user_id,
                object_type="memory_engine", object_id="deployment", result="denied")
            raise config_error("admin_only")
        return user.user_id

    from .engine_config import EngineIn, EngineModelsIn, public_settings, save_settings, models
    from .personal import (PersonalModels, PersonalIn, PersonalProbeIn, PersonalCatalogIn,
                           PersonalReasoningIn)
    personal = PersonalModels(service.db, service.audit)

    @router.get("/models")
    def personal_models(user=Depends(owner)):
        return personal.view(user)

    @router.put("/models")
    def save_personal_models(body: PersonalIn, user=Depends(owner)):
        return personal.save(user, body)

    @router.post("/models/catalog")
    def personal_catalog(body: PersonalCatalogIn, user=Depends(owner)):
        return personal.catalog(user, body)

    @router.post("/models/reasoning")
    async def discover_personal_reasoning(body: PersonalReasoningIn, user=Depends(owner)):
        return await personal.discover_reasoning(user, body)

    @router.post("/models/test")
    def test_personal_model(body: PersonalProbeIn, user=Depends(owner)):
        return personal.probe(user, body)

    @router.post("/models/rebuild")
    def rebuild_personal_memory(user=Depends(owner)):
        return personal.rebuild_memory(user)

    @router.delete("/models")
    def disable_personal_models(user=Depends(owner)):
        return personal.disable(user)

    @router.get("/engine")
    def engine(user=Depends(admin)):
        return public_settings()

    @router.put("/engine")
    def save_engine(body: EngineIn, user=Depends(admin)):
        return save_settings(body, service.db, service.audit, user)

    @router.post("/engine/models")
    def engine_models(body: EngineModelsIn, user=Depends(admin)):
        try:
            result = models(body)
        except Exception:
            service.audit.append("config.changed", actor_id=user, admin_id=user,
                object_type="memory_engine", object_id="deployment", result="probe_failed")
            raise
        service.audit.append("config.changed", actor_id=user, admin_id=user,
            object_type="memory_engine", object_id="deployment", result="probe_completed")
        return result

    @router.get("/settings")
    def settings(user=Depends(owner)):
        return service.settings(user)

    @router.get("/maintenance")
    def maintenance(response: Response, user=Depends(owner)):
        from .maintenance import policy_view
        response.headers["Cache-Control"] = "no-store"
        return {**policy_view(service.db, user), "worker_error": service.maintenance_error}

    @router.put("/maintenance")
    def set_maintenance(body: MaintenanceIn, user=Depends(owner)):
        from .maintenance import configure
        return configure(service, user, body.mode, body.expected_revision)

    @router.get("/courses/{course_id}/learning-state")
    def learning_state(course_id: str, response: Response, user=Depends(owner)):
        from .learning import context
        response.headers["Cache-Control"] = "no-store"
        with service.db.session() as session:
            service._space(session, user, "course:" + course_id)
            return context(session, user, course_id)

    @router.patch("/settings")
    def configure(body: SettingsIn, user=Depends(owner)):
        return service.configure(user, body.enabled, body.expected_revision)

    @router.get("/profile")
    async def profile(user=Depends(owner)):
        return await service.read(user)

    @router.get("/sources")
    def sources(user=Depends(owner)):
        with service.db.session() as session:
            rows = session.scalars(select(MemorySourceRow).where(
                MemorySourceRow.owner_user_id == user).order_by(MemorySourceRow.created_at.desc()).limit(100))
            return {"items": [{"id": r.source_id, "kind": r.source_kind, "scope": r.tag,
                "status": r.status, "revision": r.revision, "created_at": r.created_at.isoformat()} for r in rows]}

    @router.get("/characters/{card_id}")
    async def character(card_id: str, user=Depends(owner)):
        characters.get(user, card_id)
        return await service.read(user, tag="character:" + card_id)

    @router.post("/characters/{card_id}/feedback")
    def feedback(card_id: str, body: FeedbackIn, user=Depends(owner)):
        view = characters.get(user, card_id)
        if view["revision"] != body.card_revision:
            raise error("state_invalid", status=409)
        if not service.settings(user)["enabled"]:
            raise error("state_invalid", "请先启用个人记忆。", status=409)
        with service.db.session() as session:
            enqueue(session, user, "character_delivery_feedback", body.idempotency_key,
                {"card_revision": body.card_revision, "aspect": body.aspect,
                 "user_preference": body.preference,
                 "boundary": "只改变表达方式，不改变例子的准确性、生活化和评分。"}, card=card_id)
        return {"status": "pending"}

    @router.get("/courses/{course_id}")
    async def course(course_id: str, user=Depends(owner)):
        return await service.read(user, "course:" + course_id, "course")

    @router.post("/review")
    async def review(body: ReviewIn, user=Depends(owner)):
        return await service.read(user, query=body.query, reflect=True)

    @router.delete("/facts/{source_id}")
    def forget(source_id: str, user=Depends(owner)):
        return service.forget(user, source_id)

    @router.patch("/facts/{source_id}")
    def correct(source_id: str, body: CorrectionIn, user=Depends(owner)):
        return service.correct(user, source_id, body.content, body.expected_revision)

    @router.post("/reset")
    def reset(user=Depends(owner)):
        return service.reset(user)

    return router
