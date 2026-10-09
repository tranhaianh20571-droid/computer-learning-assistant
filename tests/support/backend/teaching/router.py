"""切片 4 API 路由：讲次启动、工作区、检查点、追问、恢复、音频受权读取。"""

from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from .errors import TeachingError
from .service import TeachingService


class CheckpointIn(BaseModel):
    client_seq: int = Field(ge=1)
    step_no: int = Field(ge=0)
    position_ms: int = Field(ge=0)
    board_state: dict = Field(default_factory=dict)


class FollowupIn(BaseModel):
    content: str = Field(min_length=1, max_length=1000)
    idempotency_key: str = Field(min_length=1, max_length=128)


class AudioIn(BaseModel):
    step_no: int = Field(ge=1,le=24)


class LessonConfirmIn(BaseModel):
    fingerprint: str = Field(min_length=64, max_length=64)
    content_confirmed: bool = False
    tts_confirmed: bool = False
    prewarm_opt_in: bool = False
    purpose: str = Field(default="start", pattern="^(start|prewarm|recover)$")
    conversation_id: str | None = None


def create_teaching_router(
    *,
    teaching: TeachingService,
    resolve_user: Callable[[Request], Any],
) -> APIRouter:
    router = APIRouter(tags=["slice4"])

    def _user(request: Request):
        return resolve_user(request)

    @router.post("/api/teaching/prewarm-library")
    def prewarm_library(request: Request):
        return teaching.preparation.prepare_owner(_user(request).user_id)

    @router.get("/api/courses/{course_id}/first-preparation")
    def first_preparation(course_id: str,request: Request):
        return teaching.preparation.status(course_id,_user(request).user_id)

    @router.get("/api/courses/{course_id}/lessons/{lesson_id}/authorization-preflight")
    def authorization_preflight(course_id: str, lesson_id: str, request: Request, purpose: str = "start", conversation_id: str | None = None):
        user = _user(request)
        return teaching.preflight_lesson(course_id, lesson_id, actor_id=user.user_id,
            purpose=purpose, conversation_id=conversation_id)

    @router.post("/api/courses/{course_id}/lessons/{lesson_id}/authorization-confirm", status_code=201)
    def authorization_confirm(course_id: str, lesson_id: str, body: LessonConfirmIn, request: Request):
        user = _user(request)
        return teaching.confirm_lesson(course_id, lesson_id, actor_id=user.user_id, **body.model_dump())

    @router.post("/api/courses/{course_id}/lessons/{lesson_id}/start", status_code=201)
    def start_lesson(course_id: str, lesson_id: str, request: Request) -> dict:
        """Teaching ownership and authorization are checked on the server."""
        user = _user(request)
        if teaching.authorization.automatic_enabled:
            return teaching.confirm_lesson(course_id, lesson_id, actor_id=user.user_id,
                **teaching.authorization.automatic_request(course_id, lesson_id, actor_id=user.user_id))
        return teaching.start_lesson(course_id, lesson_id, actor_id=user.user_id)

    @router.post("/api/courses/{course_id}/lessons/{lesson_id}/prewarm", status_code=201)
    def prewarm_lesson(course_id: str, lesson_id: str, request: Request):
        user = _user(request)
        approval = teaching.authorization.automatic_request(course_id, lesson_id,
            actor_id=user.user_id, purpose="prewarm")
        return teaching.confirm_lesson(course_id, lesson_id, actor_id=user.user_id, **approval)

    @router.post("/api/teaching/{conversation_id}/recover")
    def recover_lesson(conversation_id: str, request: Request):
        from ..planning.models import ConversationRow
        from .errors import terr
        user = _user(request)
        with teaching.db.session() as sess:
            conv = sess.get(ConversationRow, conversation_id)
            if not conv or conv.owner_user_id != user.user_id or conv.deleted_at:
                raise terr("conversation_not_found", status=404)
            course_id, lesson_id = conv.course_id, conv.lesson_id
        approval = teaching.authorization.automatic_request(course_id, lesson_id,
            actor_id=user.user_id, purpose="recover", conversation_id=conversation_id)
        return teaching.confirm_lesson(course_id, lesson_id, actor_id=user.user_id, **approval)

    @router.post("/api/teaching/{conversation_id}/complete")
    def complete(conversation_id: str, request: Request):
        return teaching.complete_lesson(conversation_id, actor_id=_user(request).user_id)

    @router.post("/api/teaching/{conversation_id}/audio")
    def ensure_audio(conversation_id: str, body: AudioIn, request: Request):
        return teaching.ensure_audio(conversation_id,actor_id=_user(request).user_id,step_no=body.step_no)

    @router.post("/api/teaching/{conversation_id}/upgrade")
    def upgrade(conversation_id: str, request: Request):
        return teaching.upgrade_lesson(conversation_id, actor_id=_user(request).user_id)

    @router.get("/api/teaching/{conversation_id}")
    def workspace(conversation_id: str, request: Request) -> dict:
        user = _user(request)
        return teaching.get_workspace(conversation_id, actor_id=user.user_id)

    @router.post("/api/teaching/{conversation_id}/checkpoint")
    def checkpoint(conversation_id: str, body: CheckpointIn, request: Request) -> dict:
        user = _user(request)
        return teaching.save_checkpoint(
            conversation_id, actor_id=user.user_id,
            client_seq=body.client_seq, step_no=body.step_no,
            position_ms=body.position_ms, board_state=body.board_state,
        )

    @router.post("/api/teaching/{conversation_id}/followup")
    def followup(conversation_id: str, body: FollowupIn, request: Request) -> dict:
        user = _user(request)
        return teaching.followup(
            conversation_id, actor_id=user.user_id,
            content=body.content, idempotency_key=body.idempotency_key,
        )

    @router.post("/api/teaching/{conversation_id}/resume")
    def resume(conversation_id: str, request: Request) -> dict:
        user = _user(request)
        return teaching.resume_lesson(conversation_id, actor_id=user.user_id)

    @router.post("/api/teaching/tasks/{task_id}/run")
    def run_task(task_id: str, request: Request) -> dict:
        """执行步骤/TTS/追问 worker（本地开发与测试入口；生产由 worker 池领取）。"""
        user = _user(request)
        task = teaching.tasks.get_task(task_id, actor_id=user.user_id)
        kind = task["kind"]
        if task["status"]=="succeeded":return {"status":"succeeded"}
        teaching._check_authorization(task_id, audio=kind == "tts")
        teaching.tasks.requeue_for_retry(task_id, actor_id=user.user_id)
        if teaching.server_managed and kind in ('tts','lesson_step_generate') and ':followup:' not in task['subject_id']:
            return {'status':'queued','task_id':task_id}
        if kind == "lesson_step_generate":
            if ":followup:" in task["subject_id"]:
                return teaching.run_followup(task_id)
            return teaching.run_step_generation(task_id)
        if kind == "tts":
            return teaching.run_tts(task_id)
        from ..planning.errors import error

        raise error("invalid_field", f"unknown teaching task kind {kind}")

    @router.get("/api/teaching/tasks/{task_id}")
    def get_task(task_id: str, request: Request) -> dict:
        user = _user(request)
        return teaching.tasks.get_task(task_id, actor_id=user.user_id)

    @router.get("/api/teaching/tasks/{task_id}/content-events")
    def content_events(task_id: str, request: Request):
        """Private replaceable teaching snapshots; never task_events."""
        import json
        import time
        from fastapi.responses import StreamingResponse
        from .content_stream import read_snapshot
        owner = _user(request).user_id
        initial = read_snapshot(teaching, task_id, owner)  # authorize before headers
        last_id = request.headers.get("Last-Event-ID", "")[:80]
        def stream():
            previous = last_id
            snapshot = initial
            try:
                while True:
                    if snapshot["id"] != previous or snapshot["event"] in ("done","error"):
                        previous = snapshot["id"]
                        yield f'id: {previous}\nevent: {snapshot["event"]}\ndata: {json.dumps(snapshot,ensure_ascii=False)}\n\n'
                    if snapshot["event"] in ("done","error"):
                        return
                    yield ": keepalive\n\n"
                    time.sleep(.15)
                    snapshot = read_snapshot(teaching, task_id, owner)
            except TeachingError:
                yield 'event: error\ndata: {"status":"unavailable"}\n\n'
        return StreamingResponse(stream(), media_type="text/event-stream",
            headers={"Cache-Control":"no-store", "X-Accel-Buffering":"no"})

    @router.get("/api/teaching/audio/{audio_id}/content")
    def audio_content(audio_id: str, request: Request):
        """音频受权读取：会话所有权校验后回传音频字节。"""
        user = _user(request)
        from fastapi import Response

        from sqlalchemy import select

        from .models import AudioAssetRow

        with teaching.db.session() as sess:
            audio = sess.get(AudioAssetRow, audio_id)
            if audio is None:
                from .errors import terr

                raise terr("audio_not_found", status=404)
            conv = sess.get(ConversationRowFor(audio), audio.conversation_id)
            if conv is None or conv.deleted_at or conv.owner_user_id != user.user_id or audio.status!="ready":
                from .errors import terr

                raise terr("audio_not_found", status=404)
            key = audio.storage_key
            audio_format = audio.audio_format
        try:
            payload = teaching.storage.get(key)
        except Exception:  # noqa: BLE001
            from .errors import terr

            raise terr("audio_not_found", status=404)
        media_type=f"audio/{audio_format or 'mpeg'}"
        headers={"Accept-Ranges":"bytes","Cache-Control":"private, no-store"}
        requested=request.headers.get("Range")
        if requested:
            import re
            match=re.fullmatch(r"bytes=(\d*)-(\d*)",requested) if len(requested)<=120 else None
            size=len(payload)
            if not match or not size or not any(match.groups()):
                return Response(status_code=416,headers={**headers,"Content-Range":f"bytes */{size}"})
            first,last=match.groups()
            start=int(first) if first else max(0,size-int(last))
            end=min(size-1,int(last)) if first and last else size-1
            if start> end or start>=size or (not first and int(last)==0):
                return Response(status_code=416,headers={**headers,"Content-Range":f"bytes */{size}"})
            return Response(content=payload[start:end+1],status_code=206,media_type=media_type,
                headers={**headers,"Content-Range":f"bytes {start}-{end}/{size}"})
        return Response(content=payload, media_type=media_type,headers=headers)

    def ConversationRowFor(audio):
        from ..planning.models import ConversationRow

        return ConversationRow

    return router


__all__ = ["create_teaching_router", "TeachingError"]
