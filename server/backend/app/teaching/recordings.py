"""Private, bounded classroom video storage; no provider calls or media in logs."""
import hashlib
import json
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..materials.storage import StorageError
from ..planning.models import (
    ConversationRow,
    CourseRow,
    LessonContentVersionRow,
    LessonProgressRow,
    LessonRow,
)
from .errors import terr
from .learning_models import ClassroomRecordingRow
from .models import utcnow

MAX_CHUNK = 8 * 1024 * 1024
MAX_RECORDING = 4 * 1024 ** 3


class StartRecording(BaseModel):
    mime_type: str = Field(default="video/webm", max_length=80)


class FinishRecording(BaseModel):
    duration_ms: int = Field(ge=0, le=24 * 3600 * 1000)
    complete: bool = True
    events: list[dict] = Field(default_factory=list, max_length=4000)


def prefix(row):
    return f"recordings/{row.conversation_id}/{row.recording_id}/"


def view(row):
    return {"recording_id": row.recording_id, "status": row.status, "bytes": row.size_bytes,
        "chunks": len(json.loads(row.chunks_json)), "duration_ms": row.duration_ms,
        "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat()}


def finalize_video(storage, chunks, key):
    """Stream-copy into a seekable WebM, including its duration and cue index."""
    import imageio_ffmpeg
    size = sum(item["bytes"] for item in chunks)
    if storage.location.write_guard:
        storage.location.write_guard(size * 3)
    with tempfile.TemporaryDirectory(prefix=".recording-", dir=storage.base_dir) as directory:
        source, target = Path(directory) / "source.webm", Path(directory) / "video.webm"
        with source.open("wb") as output:
            for item in chunks:
                with storage.local_path(item["key"]).open("rb") as incoming:
                    shutil.copyfileobj(incoming, output, length=1024*1024)
        result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner",
            "-loglevel", "error", "-protocol_whitelist", "file,pipe", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-f", "webm", str(target)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode or not target.is_file() or target.stat().st_size < 100:
            raise StorageError("recording_container_invalid")
        storage.put_file(key, target)


def create_recording_router(service, resolve_user):
    router = APIRouter(prefix="/api/teaching/{conversation_id}/recordings")

    def owned(sess, conversation, owner, ident=None):
        conv, _ = service.knowledge.owned(sess, conversation, owner, lock=True)
        if ident is None:
            return conv
        row = sess.get(ClassroomRecordingRow, ident, with_for_update=True)
        if not row or row.owner_user_id != owner or row.conversation_id != conversation or row.deleted_at:
            raise terr("conversation_not_found", status=404)
        return row

    @router.get("")
    def listing(conversation_id: str, request: Request):
        owner = resolve_user(request).user_id
        with service.db.session() as sess:
            owned(sess, conversation_id, owner)
            rows = sess.scalars(select(ClassroomRecordingRow).where(
                ClassroomRecordingRow.conversation_id == conversation_id,
                ClassroomRecordingRow.owner_user_id == owner, ClassroomRecordingRow.deleted_at.is_(None))
                .order_by(ClassroomRecordingRow.created_at.desc()).limit(30))
            return {"data": [view(row) for row in rows]}

    @router.post("")
    def start(conversation_id: str, body: StartRecording, request: Request):
        if body.mime_type not in ("video/webm", "video/webm;codecs=vp8,opus", "video/webm;codecs=vp9,opus"):
            raise terr("invalid_field", status=400)
        owner = resolve_user(request).user_id
        with service.db.session() as sess:
            conv = owned(sess, conversation_id, owner)
            active = sess.scalar(select(ClassroomRecordingRow.recording_id).where(
                ClassroomRecordingRow.conversation_id == conversation_id,
                ClassroomRecordingRow.status == "recording", ClassroomRecordingRow.deleted_at.is_(None)))
            if active:
                raise terr("state_invalid", "请先保存上一次录制。", status=409)
            row = ClassroomRecordingRow(recording_id="rec_" + uuid.uuid4().hex,
                owner_user_id=owner, conversation_id=conversation_id,
                content_version_id=conv.lesson_content_version_id or "", mime_type=body.mime_type)
            sess.add(row)
            sess.flush()
            return view(row)

    @router.put("/{recording_id}/chunks/{sequence}")
    async def chunk(conversation_id: str, recording_id: str, sequence: int, request: Request):
        owner = resolve_user(request).user_id
        if not 0 <= sequence < 20000:
            raise terr("invalid_field", status=400)
        payload = bytearray()
        async for part in request.stream():
            if len(payload) + len(part) > MAX_CHUNK:
                raise terr("invalid_field", "录制片段过大。", status=413)
            payload.extend(part)
        if not payload:
            raise terr("invalid_field", status=400)
        digest = hashlib.sha256(payload).hexdigest()
        with service.storage.location.lock, service.db.session() as sess:
            row = owned(sess, conversation_id, owner, recording_id)
            chunks = json.loads(row.chunks_json)
            if sequence < len(chunks):
                if chunks[sequence]["sha256"] != digest:
                    raise terr("CHECKPOINT_STALE", status=409)
                return {"saved": True, "sequence": sequence, "deduped": True}
            if row.status != "recording" or sequence != len(chunks) or row.size_bytes + len(payload) > MAX_RECORDING:
                raise terr("state_invalid", status=409)
            if sequence == 0 and payload[:4] != b"\x1a\x45\xdf\xa3":
                raise terr("invalid_field", "录制文件格式无效。", status=400)
            key = prefix(row) + f"{sequence:06d}.webm"
            try:
                service.storage.put(key, bytes(payload))
            except (OSError, StorageError):
                raise terr("SAVE_FAILED", "录制保存失败，请检查可用空间。", status=503) from None
            chunks.append({"key": key, "sha256": digest, "bytes": len(payload)})
            row.chunks_json = json.dumps(chunks)
            row.size_bytes += len(payload)
            row.updated_at = utcnow()
        return {"saved": True, "sequence": sequence}

    @router.post("/{recording_id}/finish")
    def finish(conversation_id: str, recording_id: str, body: FinishRecording, request: Request):
        owner = resolve_user(request).user_id
        with service.storage.location.lock, service.db.session() as sess:
            row = owned(sess, conversation_id, owner, recording_id)
            if row.storage_key:
                return view(row)
            chunks = json.loads(row.chunks_json)
            if not chunks:
                raise terr("state_invalid", "尚无可保存的录像。", status=409)
            events = []
            for event in body.events:
                if (type(event.get("at_ms")) is not int or not 0 <= event["at_ms"] <= body.duration_ms
                        or type(event.get("step_no")) is not int or not 1 <= event["step_no"] <= 240):
                    raise terr("invalid_field", status=400)
                events.append({"at_ms": event["at_ms"], "step_no": event["step_no"]})
            key = prefix(row) + "video.webm"
            try:
                finalize_video(service.storage, chunks, key)
            except (OSError, StorageError, subprocess.SubprocessError):
                raise terr("SAVE_FAILED", "录像封装失败，已保存片段仍可恢复。", status=503) from None
            row.storage_key = key
            row.status = "ready" if body.complete else "interrupted"
            row.duration_ms = body.duration_ms
            row.events_json = json.dumps(events)
            row.updated_at = utcnow()
            result = view(row)
        # The complete video is self contained. Retain the manifest, not duplicate bytes.
        for item in chunks:
            try:
                service.storage.delete(item["key"])
            except (OSError, StorageError):
                pass
        return result

    @router.get("/{recording_id}/video")
    def video(conversation_id: str, recording_id: str, request: Request):
        with service.db.session() as sess:
            row = owned(sess, conversation_id, resolve_user(request).user_id, recording_id)
            if not row.storage_key:
                raise terr("state_invalid", status=409)
            try:
                path = service.storage.local_path(row.storage_key)
            except StorageError:
                raise terr("conversation_not_found", status=404) from None
            return FileResponse(path, media_type="video/webm", filename="lecture.webm", content_disposition_type="inline",
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @router.delete("/{recording_id}")
    def remove(conversation_id: str, recording_id: str, request: Request):
        with service.storage.location.lock, service.db.session() as sess:
            row = owned(sess, conversation_id, resolve_user(request).user_id, recording_id)
            from ..cleanup.models import LocalResourceRow
            if sess.scalar(select(LocalResourceRow.resource_id).where(
                    LocalResourceRow.object_key.like(prefix(row) + "%"),
                    LocalResourceRow.volume_id == service.storage.location.volume_id,
                    LocalResourceRow.pinned.is_(True), LocalResourceRow.state != "missing").limit(1)):
                raise terr("state_invalid", "请先取消录像文件的固定保留。", status=409)
            audit = getattr(service.disclosures, "audit", None)
            if audit:
                audit.append("resource.delete.requested", actor_id=row.owner_user_id,
                    object_type="recording", object_id=row.recording_id, result="deleted", session=sess)
            row.deleted_at = utcnow()
            row.status = "deleted"
        # Keep metadata if a disk operation fails; the resource collector can retry.
        for key, _ in service.storage.iter_files(prefix(row), reject_links=True):
            service.storage.delete(key)
        return {"deleted": True}

    root = APIRouter()
    root.include_router(router)

    @root.get("/api/courses/{course_id}/lessons/{lesson_id}/recordings")
    def lesson_recordings(course_id: str, lesson_id: str, request: Request):
        owner = resolve_user(request).user_id
        with service.db.session() as sess:
            course, lesson = sess.get(CourseRow, course_id), sess.get(LessonRow, lesson_id)
            if (not course or course.owner_user_id != owner or course.deleted_at or not lesson
                    or lesson.outline_version_id != course.current_outline_version_id):
                raise terr("conversation_not_found", status=404)
            content = sess.scalar(select(LessonContentVersionRow).where(
                LessonContentVersionRow.lesson_id == lesson_id).order_by(LessonContentVersionRow.content_version_no.desc()))
            if not content:
                raise terr("conversation_not_found", status=404)
            progress = sess.scalar(select(LessonProgressRow).where(
                LessonProgressRow.owner_user_id == owner, LessonProgressRow.lesson_id == lesson_id))
            conversations = list(sess.scalars(select(ConversationRow).where(
                ConversationRow.owner_user_id == owner, ConversationRow.lesson_id == lesson_id,
                ConversationRow.course_id == course_id, ConversationRow.deleted_at.is_(None),
                ConversationRow.outline_version_id == course.current_outline_version_id,
                ConversationRow.lesson_content_version_id == content.content_version_id,
                ConversationRow.kind == "teaching").order_by(ConversationRow.created_at.desc())))
            rows = list(sess.scalars(select(ClassroomRecordingRow).where(
                ClassroomRecordingRow.conversation_id.in_([c.conversation_id for c in conversations]),
                ClassroomRecordingRow.owner_user_id == owner, ClassroomRecordingRow.deleted_at.is_(None),
                ClassroomRecordingRow.content_version_id == content.content_version_id)
                .order_by(ClassroomRecordingRow.created_at).limit(240)))
            return {"course_id": course_id, "lesson_id": lesson_id, "title": lesson.title_ref or lesson.goal_ref,
                "completed": bool(progress and progress.learning_state == "user_completed"
                    and progress.lesson_content_version_id == content.content_version_id),
                "conversation_id": conversations[0].conversation_id if conversations else None,
                "data": [{**view(row), "conversation_id": row.conversation_id} for row in rows]}

    return root
