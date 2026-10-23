"""Private classroom drafts and version-bound original PDF targets."""
import hashlib
import json
import math
import re

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..materials.models import MaterialRow, DocumentAttachmentRow
from ..materials.storage import MaterialStorage, StorageError
from ..materials.study_sources import verify_sources
from ..planning.models import ConversationRow, CourseRow, OutlineVersionRow, load_json_field
from .errors import terr
from .learning_models import LessonBlueprintRow
from .models import BoardSceneRow, TeachingStepRow

POLICY = (
    "课堂代码交互：Python/JavaScript/Java/C/C++/PyTorch 的完整可运行示例设置 interaction=true，"
    "run_instruction 指引点击运行观察，change_instruction 明确指出一个可修改的数据及观察目标。"
    "代码用教学所需的真实运算，可用 numpy/matplotlib/CPU torch，不能伪造输出，不用文件、网络、input或安装包。"
    "Java/C/C++/PyTorch 每个代码块需独立完整可运行，不依赖其它块的变量；Java 使用含 main 的类，C/C++ 含 main。"
    "PyTorch 示例仅 CPU、小张量及小型模型，无 GPU、torchvision、下载模型或任意 pip；torch 静态 import 可用 python 标签。"
    "本短段结尾说完运行要求并提醒：观察好了点击右下角的继续按钮；程序随后暂停。"
    "修改操作与输出属于同一讲，代码块不能因为后续板书而被覆盖。"
    "原文精读：若 context.teaching_mode=source_reading，使用 pdf_targets 中的 material_id/target_id"
    "填写 board.source_focus，并用每个 beat.source_focus 关联正在解释的段落/公式/原图；"
    "针对当前区域原文讲解，必要时配合代码或推导；"
    "不能编造 PDF、引用 ID、屏幕位置或原文内容。"
)


class Cell(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_no: int = Field(ge=0, le=24)
    generation: int = Field(default=1, ge=1)
    block_id: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    original_code: str = Field(max_length=1200)
    code: str = Field(max_length=12000)
    output: str = Field(default="", max_length=32768)
    status: str = Field(default="idle", pattern=r"^(idle|success|error|stopped)$")
    runs: int = Field(default=0, ge=0, le=100000)
    phase: str = Field(default="run", pattern=r"^(run|change|done)$")
    baseline_code: str = Field(default="", max_length=12000)
    baseline_output: str = Field(default="", max_length=32768)


class StateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    playback_version: int = Field(ge=0)
    cells: list[Cell] = Field(default_factory=list, max_length=96)


class FeedbackIn(BaseModel):
    revision: int = Field(ge=1)
    step_no: int = Field(ge=1, le=24)
    block_id: str = Field(min_length=1, max_length=40)
    action: str = Field(pattern=r"^(observe|compare)$")


def guard(service, sess, ident, owner):
    conv = sess.get(ConversationRow, ident, with_for_update=True)
    if not conv or conv.owner_user_id != owner or conv.deleted_at or conv.kind != "teaching":
        raise terr("conversation_not_found", status=404)
    course = sess.get(CourseRow, conv.course_id)
    if not course or course.deleted_at or course.owner_user_id != owner:
        raise terr("conversation_not_found", status=404)
    outline = sess.get(OutlineVersionRow, conv.outline_version_id)
    snapshot = load_json_field(outline.source_snapshot_json, {}) if outline else {}
    verify_sources(sess, owner, snapshot)
    return conv, snapshot


def classroom_state(service, ident, owner, value=None):
    from ..characters.classroom import playback_version
    with service.db.session() as sess:
        conv, _snapshot = guard(service, sess, ident, owner)
        version = playback_version(sess, ident)
        row = sess.get(LessonBlueprintRow, ident, with_for_update=True)
        if not row:
            if value:
                raise terr("state_invalid", status=409)
            return {"revision": 0, "playback_version": version, "cells": []}
        context = load_json_field(row.context_json, {})
        state = context.get("_classroom_state", {})
        if state.get("playback_version") != version:
            state = {"revision": 0, "playback_version": version, "cells": []}
        if value is None:
            return state
        if value.playback_version != version or value.revision != state.get("revision", 0):
            raise terr("CHECKPOINT_STALE", status=409)
        keys = set()
        for cell in value.cells:
            if cell.generation > conv.generation:
                raise terr("CHECKPOINT_STALE", status=409)
            key = (cell.step_no, cell.generation, cell.block_id)
            if key in keys:
                raise terr("invalid_field", status=400)
            keys.add(key)
            if cell.step_no == 0:
                scene = sess.scalar(select(BoardSceneRow).where(
                    BoardSceneRow.conversation_id == ident, BoardSceneRow.step_no == 0,
                    BoardSceneRow.generation == cell.generation)
                    .order_by(BoardSceneRow.scene_version_no.desc()))
            else:
                step = sess.scalar(select(TeachingStepRow).where(
                    TeachingStepRow.conversation_id == ident, TeachingStepRow.step_no == cell.step_no,
                    TeachingStepRow.generation == cell.generation))
                scene = sess.get(BoardSceneRow, step.board_scene_id) if step else None
            blocks = load_json_field(scene.scene_json, {}).get("presentation", {}).get("blocks", []) if scene else []
            block = next((b for b in blocks if b.get("id") == cell.block_id and b.get("type") == "code"), None)
            if not block or cell.original_code != block.get("code"):
                raise terr("CHECKPOINT_STALE", status=409)
        state = {"revision": value.revision + 1, "playback_version": version,
                 "cells": [cell.model_dump() for cell in value.cells]}
        if len(json.dumps(state).encode()) > 2 * 1024 * 1024:
            raise terr("invalid_field", status=400)
        context["_classroom_state"] = state
        row.context_json = json.dumps(context, ensure_ascii=False)
        return state


def source_file(service, ident, owner, material_id, sha256):
    with service.db.session() as sess:
        _, snapshot = guard(service, sess, ident, owner)
    return source_path(service, owner, snapshot, material_id, sha256)


def source_path(service, owner, snapshot, material_id, sha256):
    with service.db.session() as sess:
        verify_sources(sess, owner, snapshot)
        doc = next((d for d in snapshot.get("documents", [])
                    if d["material_id"] == material_id and d["sha256"] == sha256), None)
        row = sess.get(MaterialRow, material_id)
        if not doc or not row or row.deleted_at or row.owner_user_id != owner:
            raise terr("conversation_not_found", status=404)
        from ..materials.figure_crops import owned_pdf_state
        try:
            _, _, state = owned_pdf_state(sess, owner, material_id)
        except StorageError:
            raise terr("conversation_not_found", status=404) from None
        expected_sha = (state.get("document_input", {}).get("sha256", sha256)
                        if row.kind == "file" else sha256)
    storage = service.storage
    try:
        path = storage.local_path(MaterialStorage.original_key(material_id))
    except StorageError:
        raise terr("conversation_not_found", status=404) from None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha:
        raise terr("CHECKPOINT_STALE", status=409)
    return path


def pdf_page_targets(path, material_id, sha256, page_no, source_blocks=()):
    import pypdfium2 as pdfium
    from ..materials.upload import PDFIUM_LOCK
    with PDFIUM_LOCK, pdfium.PdfDocument(path) as pdf:
        if not 1 <= page_no <= len(pdf):
            raise terr("invalid_field", status=400)
        page = pdf[page_no - 1]
        width, height = page.get_size()
        rotation = page.get_rotation()
        raw_width, raw_height = (height, width) if rotation in (90, 270) else (width, height)
        text = page.get_textpage()
        full = text.get_text_range()
        result = []
        # Geometry is computed from the exact original page, never model coordinates.
        for index, match in enumerate(re.finditer(r"[^\r\n]+", full)):
            if len(result) >= 90:
                break
            quote = match.group().strip()
            if not quote:
                continue
            count = text.count_rects(match.start(), len(match.group()))
            boxes = [text.get_rect(i) for i in range(count)]
            boxes = [b for b in boxes if all(math.isfinite(v) for v in b)]
            if not boxes:
                continue
            left, bottom = min(b[0] for b in boxes), min(b[1] for b in boxes)
            right, top = max(b[2] for b in boxes), max(b[3] for b in boxes)
            if rotation == 90:
                box = [bottom/raw_height, left/raw_width, top/raw_height, right/raw_width]
            elif rotation == 180:
                box = [1-right/raw_width, bottom/raw_height, 1-left/raw_width, top/raw_height]
            elif rotation == 270:
                box = [1-top/raw_height, 1-right/raw_width, 1-bottom/raw_height, 1-left/raw_width]
            else:
                box = [left/width, 1-top/height, right/width, 1-bottom/height]
            box = [max(0, min(1, value)) for value in box]
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            result.append({"material_id": material_id, "sha256": sha256,
                "target_id": f"p{page_no}_r{index}", "page_no": page_no,
                "page_count": len(pdf), "text": quote[:400], "bbox": box, "precision": "text"})
        for index, block in enumerate(source_blocks):
            if block.get("crop") and block.get("type") == "figure":
                crop = block["crop"]
                result.append({"material_id": material_id, "sha256": sha256,
                    "target_id": f"p{page_no}_b{index}", "page_no": page_no, "page_count": len(pdf),
                    "text": (block.get("content") or "资料原图")[:400], "bbox": None,
                    "precision": "provider_crop", "crop_sha256": crop["sha256"],
                    "block_no": index, "crop_width": crop["width"], "crop_height": crop["height"]})
                continue
            box = block.get("source_bbox")
            if (not isinstance(box, list) or len(box) != 4
                    or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in box)
                    or box[0] >= box[2] or box[1] >= box[3]):
                continue
            result.append({"material_id": material_id, "sha256": sha256,
                "target_id": f"p{page_no}_b{index}", "page_no": page_no, "page_count": len(pdf),
                "text": (block.get("content") or "原图区域")[:400], "bbox": box, "precision": "ocr_region"})
        result.insert(0, {"material_id": material_id, "sha256": sha256,
            "target_id": f"p{page_no}", "page_no": page_no, "page_count": len(pdf),
            "text": "整页原文；无可靠区域时使用此项", "bbox": None, "precision": "page"})
        text.close()
        page.close()
        return result


def source_targets(service, conv, context, step_no=1, total=1):
    sections = context.get("source_sections", [])
    documents = context.get("source_snapshot", {}).get("documents", [])
    targets = []
    for doc in documents[:3]:
        ranges = [(s["page_no"], s.get("end_page", s["page_no"]))
                  for s in sections if s["id"].startswith(doc["material_id"] + "/")]
        if not ranges:
            continue
        first, last = min(p[0] for p in ranges), max(p[1] for p in ranges)
        page = first + round((last-first) * max(0, step_no-1)/max(1, total-1))
        with service.db.session() as sess:
            material = sess.get(MaterialRow, doc["material_id"])
            if material and material.kind != "pdf":
                continue
        path = source_path(service, conv.owner_user_id, context["source_snapshot"],
                           doc["material_id"], doc["sha256"])
        with service.db.session() as sess:
            attachment = sess.get(DocumentAttachmentRow, doc["material_id"])
            pages = load_json_field(attachment.result_json, {}).get("page_results", []) if attachment else []
            blocks = next((item.get("blocks", []) for item in pages if item.get("page_no") == page), [])
        targets.extend(pdf_page_targets(path, doc["material_id"], doc["sha256"], page, blocks))
    return targets


def bind_source(board, targets):
    if not board.source_focus:
        return
    focus = board.source_focus
    found = next((item for item in targets if item["material_id"] == focus.material_id
                  and item["target_id"] == focus.target_id), None)
    if not found:
        from pydantic_ai.exceptions import ModelRetry
        raise ModelRetry("原文精读只能引用本次 pdf_targets 中的来源与区域。")
    board._resolved_source = found


def question_targets(service, conv, context, candidates=(), focuses=()):
    """Resolve reviewed input figures through the existing frozen PDF viewer."""
    requested = {ref for q in candidates if q.get("original_allowed")
                 for ref in q.get("required_figure_refs", [])}
    requested.update(f"{f.material_id}/" + f.target_id.replace("_b", "/b", 1)
                     for f in focuses if f is not None)
    if not requested:
        return []
    result = []
    with service.db.session() as session:
        docs = verify_sources(session, conv.owner_user_id, context["source_snapshot"])
        refs = set(context["source_snapshot"].get("lessons", {}).get(conv.lesson_id, []))
        for doc in docs:
            material = session.get(MaterialRow, doc["material_id"])
            if not material:
                continue
            from ..materials.figure_crops import owned_pdf_state, load_crop
            try:
                _, _, state = owned_pdf_state(session, conv.owner_user_id, doc["material_id"])
            except StorageError:
                continue
            bank = state.get("question_bank", {})
            if bank.get("status") != "completed":
                continue
            sections = [s for s in doc["index"]["sections"]
                        if doc["material_id"] + "/" + s["id"] in refs]
            allowed = {doc["material_id"] + "/" + figure["block_id"]
                for q in bank.get("questions", []) if q.get("assessment_eligible")
                and any(s["offset"] <= q["start"] < s["end_offset"] for s in sections)
                for figure in q.get("figure_roles", []) if figure["role"] == "input"}
            wanted = requested & allowed
            if not wanted:
                continue
            path = source_path(service, conv.owner_user_id, context["source_snapshot"],
                               doc["material_id"], doc["sha256"])
            for page in state["page_results"]:
                page_wanted = {ref.rsplit("/", 1)[-1] for ref in wanted
                    if ref.startswith(f'{doc["material_id"]}/p{page["page_no"]}/')}
                if not page_wanted:
                    continue
                choices = pdf_page_targets(path, doc["material_id"], doc["sha256"],
                                           page["page_no"], page["blocks"])
                choices = [target for target in choices if target["precision"] != "provider_crop"]
                for number, block in enumerate(page["blocks"]):
                    crop = block.get("crop")
                    ref = f'{doc["material_id"]}/p{page["page_no"]}/b{number}'
                    verified = any(q.get("assessment_eligible") and q.get("figures_complete")
                        and q.get("figure_crops", {}).get(block.get("block_id")) == (crop or {}).get("sha256")
                        for q in bank.get("questions", []))
                    if ref not in wanted or not crop or not verified:
                        continue
                    try:
                        load_crop(service.storage, doc["material_id"], page["page_no"], number, crop)
                    except StorageError:
                        continue
                    target_id = f'p{page["page_no"]}_b{number}'
                    choices = [choice for choice in choices if choice["target_id"] != target_id]
                    choices.append({"material_id": doc["material_id"], "sha256": doc["sha256"],
                        "target_id": target_id, "page_no": page["page_no"], "page_count": state["pages"],
                        "text": "已复核题设原图", "bbox": None, "precision": "provider_crop",
                        "crop_sha256": crop["sha256"], "block_no": number,
                        "crop_width": crop["width"], "crop_height": crop["height"]})
                result.extend(target for target in choices
                    if target["target_id"].rsplit("_", 1)[-1] in page_wanted)
    return result


def create_interactive_router(service, resolve_user):
    router = APIRouter(prefix="/api/teaching")
    from .code_execution import create_execution_router
    router.include_router(create_execution_router(service, resolve_user))

    @router.get("/{ident}/interactive")
    def get_state(ident: str, request: Request):
        return Response(json.dumps(classroom_state(service, ident, resolve_user(request).user_id)),
                        media_type="application/json", headers={"Cache-Control": "no-store"})

    @router.put("/{ident}/interactive")
    def save_state(ident: str, body: StateIn, request: Request):
        return Response(json.dumps(classroom_state(service, ident, resolve_user(request).user_id, body)),
                        media_type="application/json", headers={"Cache-Control": "no-store"})

    @router.post("/{ident}/interactive/feedback")
    def feedback(ident: str, body: FeedbackIn, request: Request):
        owner = resolve_user(request).user_id
        state = classroom_state(service, ident, owner)
        cell = next((cell for cell in state["cells"] if cell["step_no"] == body.step_no
                     and cell["block_id"] == body.block_id), None)
        if state["revision"] != body.revision or not cell or cell["status"] == "idle":
            raise terr("CHECKPOINT_STALE", status=409)
        marker = ("我运行了这段代码，请解释实际结果，并引导我修改数据再运行。"
                  if body.action == "observe" else "我修改代码后又运行了一次，请比较前后实际结果并解释变化。")
        return service.followup(ident, actor_id=owner, content=marker,
            idempotency_key=f"code:{ident}:{body.revision}:{body.step_no}:{body.block_id}:{body.action}",
            _code_feedback={"revision": body.revision, "action": body.action, "cell": cell})

    @router.get("/{ident}/sources/{material_id}/{sha256}/original")
    def original(ident: str, material_id: str, sha256: str, request: Request):
        from fastapi.responses import FileResponse
        path = source_file(service, ident, resolve_user(request).user_id, material_id, sha256)
        return FileResponse(path, media_type="application/pdf", headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'inline; filename="source.pdf"'})

    @router.get("/{ident}/sources/{material_id}/{sha256}/original/download")
    def download(ident: str, material_id: str, sha256: str, request: Request):
        response = original(ident, material_id, sha256, request)
        response.headers["Content-Disposition"] = 'attachment; filename="source.pdf"'
        return response

    @router.get("/{ident}/sources/{material_id}/{sha256}/figures/{page_no}/{block_no}/{crop_sha256}")
    def figure(ident: str, material_id: str, sha256: str, page_no: int, block_no: int,
               crop_sha256: str, request: Request):
        owner = resolve_user(request).user_id
        with service.db.session() as session:
            conv, snapshot = guard(service, session, ident, owner)
            docs = verify_sources(session, owner, snapshot)
            doc = next((d for d in docs if d["material_id"] == material_id and d["sha256"] == sha256), None)
            refs = set(snapshot.get("lessons", {}).get(conv.lesson_id, []))
            if not doc or not any(material_id + "/" + s["id"] in refs
                    and s["page_no"] <= page_no <= s["end_page"] for s in doc["index"]["sections"]):
                raise terr("conversation_not_found", status=404)
            from ..materials.figure_crops import owned_pdf_state, load_crop
            try:
                _, _, state = owned_pdf_state(session, owner, material_id)
                page = next(p for p in state["page_results"] if p["page_no"] == page_no)
                block = page["blocks"][block_no]
                if block_no < 0 or block.get("type") != "figure" or block["crop"]["sha256"] != crop_sha256:
                    raise KeyError()
                payload = load_crop(service.storage, material_id, page_no, block_no, block["crop"])
            except (StorageError, KeyError, IndexError, StopIteration):
                raise terr("conversation_not_found", status=404) from None
        return Response(payload, media_type="image/png",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    return router
