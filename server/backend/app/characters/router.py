"""Authenticated cards; request validation never echoes private profile or credentials."""
from fastapi import APIRouter, Request, Response
from pydantic import ValidationError
from .schemas import Profile, EditCard, MediaRequest, UseMentor, ExpressionPreview, ExpressionResult, VoiceChoice
from .service import reject
from .package import MAX_PACKAGE_BYTES, export_package, import_package


def create_character_router(service, jobs, resolve_user):
    router = APIRouter(prefix="/api", tags=["characters"])

    def owner(request):
        return resolve_user(request).user_id

    async def body(request, model):
        raw = await request.body()
        if len(raw) > 48000:
            reject("invalid_field", 413)
        try:
            return model.model_validate_json(raw)
        except ValidationError as exc:
            messages = {
                "name": "人物名字须为 1 到 80 字。",
                "identity": "人物身份最多 240 字。",
                "background": "人物背景最多 3000 字。",
                "personality": "人物性格最多 1200 字。",
                "appearance": "人物形象描述最多 1200 字。",
                "speaking_style": "说话风格最多 1600 字。",
                "tags": "人物标签最多 12 个，请删除多余标签。",
                "count": "图片候选数量须为 1 到 4 的整数。",
                "duration": "视频时长须为 4 到 15 秒的整数。",
                "expected_revision": "人物版本无效，请重新加载人物。",
            }
            # Only fixed messages may leave validation; never echo input or unknown fields.
            message = next((messages[field] for error in exc.errors(include_input=False)
                for field in error["loc"] if field in messages), "提交的字段不符合要求，请重新加载人物后重试。")
            reject("invalid_field", 422, message=message)
        except ValueError:
            reject("invalid_field", 422, message="提交的数据格式无效，请重新加载人物后重试。")

    @router.get("/card-market")
    def market(request: Request):
        return {"data": service.list(owner(request), market=True)}

    @router.get("/cards")
    def library(request: Request):
        return {"data": service.list(owner(request))}

    @router.get("/cards/{card_id}/export")
    def export_card(card_id: str, request: Request):
        payload = export_package(service, owner(request), card_id)
        return Response(payload, media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{card_id}.studycard.zip"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        })

    @router.post("/cards/import", status_code=201)
    async def import_card(request: Request):
        user = owner(request)
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_PACKAGE_BYTES:
                reject("invalid_field", 413, message="卡片包大小超出限制。")
            chunks.append(chunk)
        return import_package(service, user, b"".join(chunks))

    @router.get("/mentor-preference")
    def preference(request: Request, conversation_id: str | None = None):
        return service.preference(owner(request), conversation_id)

    @router.put("/mentor-preference")
    async def use(request: Request):
        from starlette.concurrency import run_in_threadpool
        user = owner(request)
        parsed = await body(request, UseMentor)
        return await run_in_threadpool(service.use, user, parsed)

    @router.get("/character-voices")
    def voice_catalog(request: Request, config_id: str):
        return jobs.voices(owner(request), None, config_id)

    @router.post("/character-expression", response_model=ExpressionResult)
    async def expression(request: Request):
        from starlette.concurrency import run_in_threadpool
        user = owner(request)
        parsed = await body(request, ExpressionPreview)
        return await run_in_threadpool(jobs.expression, user, parsed.profile.model_dump(), parsed.config_id)

    @router.post("/cards", status_code=201)
    async def create(request: Request):
        return service.create(owner(request), await body(request, Profile))

    @router.post("/character-voice-preview")
    async def preview(request: Request):
        from starlette.concurrency import run_in_threadpool
        user = owner(request)
        parsed = await body(request, ExpressionPreview)
        payload = await run_in_threadpool(jobs.preview_voice, user, parsed)
        return Response(payload, media_type="audio/mpeg", headers={"Cache-Control":"private, no-store"})

    @router.put("/cards/{card_id}/voice-choice")
    async def choose_voice(card_id: str, request: Request):
        from starlette.concurrency import run_in_threadpool
        user = owner(request)
        parsed = await body(request, VoiceChoice)
        return await run_in_threadpool(service.choose_voice, user, card_id, parsed)

    @router.get("/cards/{card_id}")
    def get(card_id: str, request: Request):
        return service.get(owner(request), card_id)

    @router.patch("/cards/{card_id}")
    async def edit(card_id: str, request: Request):
        parsed = await body(request, EditCard)
        return service.edit(owner(request), card_id, parsed.expected_revision, parsed.profile)

    @router.put("/cards/{card_id}/persona")
    async def update_persona(card_id: str, request: Request):
        from .persona import PersonaEdit
        raw = await request.body()
        if len(raw)>2200000:
            reject("invalid_field",413)
        try:
            parsed=PersonaEdit.model_validate_json(raw)
        except ValueError:
            reject("invalid_field",422)
        return service.edit_persona(owner(request),card_id,parsed.expected_revision,parsed.persona)

    @router.delete("/cards/{card_id}")
    def delete(card_id: str, request: Request):
        return service.delete(owner(request), card_id)

    @router.post("/cards/{card_id}/publish")
    def publish(card_id: str, request: Request, revision: int):
        return service.publish(owner(request), card_id, revision)

    @router.post("/cards/{card_id}/collect")
    def collect(card_id: str, request: Request):
        return service.collect(owner(request), card_id)

    @router.post("/cards/{card_id}/copy", status_code=201)
    def copy(card_id: str, request: Request):
        return service.copy(owner(request), card_id)

    @router.delete("/cards/{card_id}/collect")
    def remove(card_id: str, request: Request):
        return service.collect(owner(request), card_id, False)

    @router.get("/cards/{card_id}/assets/{asset_id}")
    def asset(card_id: str, asset_id: str, request: Request):
        payload, mime = service.asset(owner(request), card_id, asset_id)
        return Response(payload, media_type=mime, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @router.post("/cards/{card_id}/assets", status_code=201)
    async def upload(card_id: str, request: Request, revision: int, kind: str = "image", scene: str = "neutral"):
        user = owner(request)
        service.get(user, card_id)
        if kind not in ("image", "video") or scene not in ("neutral", "explaining", "thinking", "encouraging", "celebrating"):
            reject("invalid_field", 422)
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > (8 * 1024 * 1024 if kind == "image" else 32 * 1024 * 1024):
                reject("invalid_field", 413)
            chunks.append(chunk)
        from .media import normalize_media
        payload, mime = normalize_media(b"".join(chunks), kind)
        asset_id = service.add_asset(user, card_id, revision, payload, kind, mime, scene, confirmed=True)
        return {"asset_id": asset_id}

    @router.post("/cards/{card_id}/assets/{asset_id}/confirm")
    def confirm(card_id: str, asset_id: str, request: Request):
        return service.confirm_asset(owner(request), card_id, asset_id)

    @router.post("/cards/{card_id}/media-jobs", status_code=202)
    async def generate(card_id: str, request: Request):
        parsed = await body(request, MediaRequest)
        return jobs.create(owner(request), card_id, parsed, request.headers.get("Idempotency-Key", ""))

    @router.get("/cards/{card_id}/media-jobs")
    def job_list(card_id: str, request: Request):
        return {"data": jobs.list(owner(request), card_id)}

    @router.delete("/cards/{card_id}/media-jobs/{task_id}")
    def cancel(card_id: str, task_id: str, request: Request):
        return jobs.cancel(owner(request), card_id, task_id)

    @router.get("/cards/{card_id}/voices")
    def voices(card_id: str, request: Request, config_id: str):
        return jobs.voices(owner(request), card_id, config_id)

    @router.post("/teaching/{conversation_id}/card-binding")
    def bind(conversation_id: str, request: Request, card_id: str, revision: int, effective_step_no: int):
        return service.bind(owner(request), conversation_id, card_id, revision, effective_step_no)

    @router.get("/teaching/{conversation_id}/character-assets/{step_no}")
    def segment_assets(conversation_id: str, step_no: int, request: Request):
        return {"data": service.segment_assets(owner(request), conversation_id, step_no)}

    @router.get("/teaching/{conversation_id}/character-assets/{step_no}/{asset_id}")
    def segment_asset(conversation_id: str, step_no: int, asset_id: str, request: Request):
        payload, mime = service.segment_assets(owner(request), conversation_id, step_no, asset_id)
        return Response(payload, media_type=mime, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    return router
