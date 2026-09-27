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


def create_teaching_router(
    *,
    teaching: TeachingService,
    resolve_user: Callable[[Request], Any],
) -> APIRouter:
    router = APIRouter(tags=["slice4"])

    def _user(request: Request):
        return resolve_user(request)

    @router.post("/api/courses/{course_id}/lessons/{lesson_id}/start", status_code=201)
    def start_lesson(course_id: str, lesson_id: str, request: Request) -> dict:
        """教学门槛在服务端校验；返回课程会话与首段任务。"""
        user = _user(request)
        return teaching.start_lesson(course_id, lesson_id, actor_id=user.user_id)

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
            if conv is None or conv.owner_user_id != user.user_id:
                from .errors import terr

                raise terr("audio_not_found", status=404)
            key = audio.storage_key
            audio_format = audio.audio_format
        try:
            payload = teaching.storage.get(key)
        except Exception:  # noqa: BLE001
            from .errors import terr

            raise terr("audio_not_found", status=404)
        return Response(content=payload, media_type=f"audio/{audio_format or 'mpeg'}")

    def ConversationRowFor(audio):
        from ..planning.models import ConversationRow

        return ConversationRow

    return router


__all__ = ["create_teaching_router", "TeachingError"]
