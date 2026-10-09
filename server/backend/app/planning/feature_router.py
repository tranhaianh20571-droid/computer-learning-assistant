from typing import Literal
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from .feature_models import CoverAssetRow
from .errors import error


class DialogueIn(BaseModel):
    revision: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=4000)
    idempotency_key: str = Field(min_length=1, max_length=100)
    selected_node: str = Field(default="", max_length=80)


class DecisionIn(BaseModel):
    revision: int = Field(ge=1)


class CoverIn(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=100)
    method: Literal["procedural", "image"] = "procedural"
    regenerate: bool = False


class AutomaticCoverIn(BaseModel):
    external_authorized: bool = False
    regenerate: bool = False
    config_id: str | None = None
    config_version: int | None = None
    title_hash: str | None = Field(default=None, min_length=64, max_length=64)
    authorization_scope: Literal["exact_target", "route_and_confirmed_courses"] = "exact_target"
    course_topic_hashes: list[str] = Field(default_factory=list, max_length=100)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)


def create_feature_router(service, resolve_user):
    router = APIRouter(tags=["planning-adjustments"])

    @router.get("/api/planning/drafts/{draft_id}/dialogue")
    def history(draft_id: str, request: Request):
        return service.dialogue(draft_id, resolve_user(request).user_id)

    @router.post("/api/planning/drafts/{draft_id}/dialogue", status_code=202)
    def message(draft_id: str, body: DialogueIn, request: Request):
        if not body.content.strip(): raise error("invalid_field")
        return service.enqueue_dialogue(draft_id, owner=resolve_user(request).user_id, revision=body.revision,
            content=body.content.strip(), key=body.idempotency_key, selected_node=body.selected_node)

    @router.post("/api/planning/dialogue/{task_id}/adopt")
    def adopt(task_id: str, body: DecisionIn, request: Request):
        return service.decide(task_id, resolve_user(request).user_id, body.revision, True)

    @router.post("/api/planning/dialogue/{task_id}/discard")
    def discard(task_id: str, body: DecisionIn, request: Request):
        return service.decide(task_id, resolve_user(request).user_id, body.revision, False)

    @router.post("/api/planning/dialogue/{task_id}/retry", status_code=202)
    def retry(task_id: str, request: Request):
        from .feature_models import PlanningDialogueRow
        owner = resolve_user(request).user_id
        with service.db.session() as sess:
            turn = sess.get(PlanningDialogueRow, task_id)
            if turn is None or turn.owner_user_id != owner: raise error("task_not_found", status=404)
            draft = service.draft(sess, turn.draft_id, owner)
            if draft.revision != turn.base_revision: raise error("DRAFT_REVISION_MISMATCH", status=409)
        service.tasks.requeue_for_retry(task_id, actor_id=owner); service.kick(task_id)
        return {"task_id": task_id}

    @router.get("/api/planning/{kind}/{target_id}/cover")
    def cover(kind: str, target_id: str, request: Request):
        return service.cover(kind, target_id, resolve_user(request).user_id)

    @router.post("/api/planning/{kind}/{target_id}/cover", status_code=202)
    def generate(kind: str, target_id: str, body: CoverIn, request: Request):
        return service.enqueue_cover(kind, target_id, resolve_user(request).user_id, body.idempotency_key, body.method, regenerate=body.regenerate)

    @router.get("/api/planning/{kind}/{target_id}/cover/preflight")
    def preflight(kind: str, target_id: str, request: Request):
        return service.cover_preflight(kind, target_id, resolve_user(request).user_id)

    @router.post("/api/planning/{kind}/{target_id}/cover/automatic", status_code=202)
    def automatic_cover(kind: str, target_id: str, body: AutomaticCoverIn, request: Request):
        return service.enqueue_automatic_cover(kind, target_id, resolve_user(request).user_id,
            authorized=body.external_authorized, config_id=body.config_id, config_version=body.config_version,
            title_hash=body.title_hash, authorization_scope=body.authorization_scope,
            course_topic_hashes=body.course_topic_hashes, idempotency_key=body.idempotency_key, regenerate=body.regenerate)

    @router.post("/api/planning/covers/{task_id}/run", status_code=202)
    def run_cover(task_id: str, request: Request):
        owner = resolve_user(request).user_id
        with service.db.session() as sess:
            asset = sess.execute(select(CoverAssetRow).where(CoverAssetRow.task_id == task_id)).scalars().first()
            if asset is None or asset.owner_user_id != owner: raise error("task_not_found", status=404)
            service.target(sess, asset.target_kind, asset.target_id, owner)
            from .cover_policy import require_eligible
            require_eligible(sess, asset.target_kind, asset.target_id, owner)
            from .models import load_json_field
            from .automatic_cover import topic_hash, _config, _membership
            metadata = load_json_field(asset.recipe_json, {})
            if asset.method == "image" or metadata.get("requested_method") == "image":
                target = service.target(sess, asset.target_kind, asset.target_id, owner)
                if not metadata.get("config_id") or topic_hash(target.title_ref) != metadata.get("title_hash"):
                    raise error("disclosure_required", "请重新确认封面主题与模型", status=409)
                _config(sess, owner, metadata["config_id"], metadata["config_version"])
                if metadata.get("parent_grant_id"):
                    _membership(service, sess, metadata["scope_snapshot"]["root_id"], asset.target_id, owner)
                from ..slice1.errors import Slice1Error
                from ..logging_audit.contracts import normalize_error_code
                try:
                    service.cover_disclosures.verify_or_audit(task_id=task_id, config_id=metadata["config_id"],
                        content_category="generated_text", actor_id=owner, scope_snapshot=metadata["scope_snapshot"],
                        expected_grant_id=metadata["grant_id"],
                        expected_root_grant_id=metadata.get("parent_grant_id") or metadata["grant_id"], session=sess)
                except Slice1Error as exc:
                    raise error(normalize_error_code(exc.code), status=exc.status) from None
        service.tasks.requeue_for_retry(task_id, actor_id=owner); service.kick(task_id)
        return {"task_id": task_id}

    @router.get("/api/planning/covers/{asset_id}/image")
    def image(asset_id: str, request: Request):
        return Response(service.image(asset_id, resolve_user(request).user_id), media_type="image/png",
                        headers={"Cache-Control": "private, max-age=0, must-revalidate", "X-Content-Type-Options": "nosniff"})

    return router
