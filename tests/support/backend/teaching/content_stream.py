"""Authorized private business snapshots, separate from logs and task events."""
import json
import re
from datetime import timedelta
from time import monotonic
from pydantic_core import from_json
from pydantic import ValidationError
from sqlalchemy import delete
from ..planning.models import ConversationRow, CourseRow, utcnow
from ..logging_audit.models import TaskStateRow
from ..tasks.service import LearningTaskRow, LeaseError
from .learning_models import TeachingPreviewRow
from .outputs import BoardDeclaration, BoardNode, BoardEdge, OriginalRef, declaration_to_scene
from .errors import terr
from .board_blocks import BLOCK_ADAPTER


def completed_blocks(raw):
    # from_json(allow_partial) can return a structurally valid but unfinished
    # graph. raw_decode accepts only closed objects: reserve layout once complete.
    decoder = json.JSONDecoder()
    result = {}
    for match in re.finditer(r'"blocks"\s*:\s*\[', raw):
        pos = match.end()
        while pos < len(raw):
            while pos < len(raw) and raw[pos] in " \r\n\t,":
                pos += 1
            if pos >= len(raw) or raw[pos] == "]":
                break
            try:
                value, end = decoder.raw_decode(raw, pos)
                block = BLOCK_ADAPTER.validate_python(value)
                if block.id not in result:
                    result[block.id] = block
                elif result[block.id] != block:
                    result.pop(block.id, None)
                pos = end
            except (ValueError, ValidationError):
                break
    return result


def aligned_preview(raw, *, followup=False, existing_blocks=None):
    """Do not expose an unfinished quote or unchecked board before its beat closes."""
    from .board_blocks import TeachingBeat
    from .board_alignment import aligned_beats
    decoder, parsed, blocks = json.JSONDecoder(), [], {}
    for match in re.finditer(r'"beats"\s*:\s*\[', raw):
        pos = match.end()
        while pos < len(raw):
            while pos < len(raw) and raw[pos] in " \r\n\t,":
                pos += 1
            try:
                value, pos = decoder.raw_decode(raw, pos)
                beat = TeachingBeat.model_validate(value)
                if any(block.id in (existing_blocks or {}) and block.model_dump()!=existing_blocks[block.id]
                       for block in beat.blocks):
                    break
                proposed = {**blocks, **{block.id: block for block in beat.blocks}}
                for cue in beat.cues:
                    if cue.block_id not in proposed and cue.block_id in (existing_blocks or {}):
                        proposed[cue.block_id] = BLOCK_ADAPTER.validate_python(existing_blocks[cue.block_id])
                if any(block.id in blocks and block != blocks[block.id] for block in beat.blocks):
                    break
                aligned_beats([*parsed, beat], list(proposed.values()))
                blocks, parsed = proposed, [*parsed, beat]
            except (ValueError, ValidationError):
                break
        break
    beats = aligned_beats(parsed, list(blocks.values()))
    board = declaration_to_scene(BoardDeclaration(title="板书", format_version=3,
                                                  blocks=list(blocks.values()))) if blocks else None
    if board:
        board.update(alignment_version=1, beats=beats)
    return {"text": "".join(beat.text for beat in parsed), "title": "", "board": board,
            "beats": beats, "alignment_version": 1}


def partial_step(raw, *, followup=False, allowed_material_ids=None,existing_blocks=None,require_alignment=False):
    """Expose unfinished text but only complete, schema-valid board fields."""
    try:
        if require_alignment:
            return aligned_preview(raw, followup=followup,existing_blocks=existing_blocks)
        loose = from_json(raw, allow_partial="trailing-strings")
        closed = from_json(raw, allow_partial=True)
        item = loose if followup else (loose.get("steps") or [{}])[0]
        complete = closed if followup else (closed.get("steps") or [{}])[0]
        ready = completed_blocks(raw)
        ready = {key:block for key,block in ready.items() if key not in (existing_blocks or {})
                 or block.model_dump()==existing_blocks[key]}
        beats = []
        available = set()
        for beat in item.get("beats",[])[:6]:
            if not isinstance(beat,dict) or not isinstance(beat.get("id"),str):
                continue
            ids = [b.get("id") for b in beat.get("blocks",[]) if isinstance(b,dict)]
            # Wait for required board elements before exposing the corresponding text.
            if any(ident not in ready for ident in ids):
                break
            available.update(ids)
            for ident in ids:
                available.update(n.id for n in getattr(ready[ident],"nodes",[]))
            highlights = [ident for ident in beat.get("highlight_ids",[]) if ident in available]
            beats.append({"id":beat["id"],"unit_id":beat["id"],"text":str(beat.get("text","")),"block_ids":ids,"highlight_ids":highlights})
        text = "".join(b["text"] for b in beats) if item.get("beats") else item.get("answer" if followup else "text", "")
        if not isinstance(text, str):
            text = ""
        text = "".join(c for c in text if c >= " " or c in "\n\t")[:2000 if followup else 480]
        result = {"text": text, "title": str(complete.get("title", ""))[:80], "board": None,"beats":beats}
        source = complete.get("board")
        if not isinstance(source,dict):
            source = {}
        if not source.get("title") and not ready:
            return result
        board = BoardDeclaration(title=source.get("title") or result["title"] or "板书",
            blocks=list(ready.values())[:12],format_version=2 if ready else 1)
        board.points = [p for p in source.get("points", [])[:6] if isinstance(p,str) and 0 < len(p) <= 600]
        for name, limit in (("code",600),("formula",200)):
            value = source.get(name)
            if isinstance(value,str) and len(value)<=limit:
                setattr(board,name,value)
        for field, cls, limit in (("nodes",BoardNode,12),("original_refs",OriginalRef,3)):
            valid = []
            for value in source.get(field, [])[:limit]:
                try:
                    parsed = cls.model_validate(value)
                    if field == "original_refs" and allowed_material_ids is not None and parsed.material_id not in allowed_material_ids:
                        continue
                    valid.append(parsed)
                except ValidationError:
                    pass
            setattr(board,field,valid)
        ids = {n.id for n in board.nodes}
        for value in source.get("edges",[])[:12]:
            try:
                edge = BoardEdge.model_validate(value)
                if edge.from_id in ids and edge.to_id in ids:
                    board.edges.append(edge)
            except ValidationError:
                pass
        result["board"] = declaration_to_scene(board)
        return result
    except (ValueError, TypeError, IndexError, AttributeError, ValidationError):
        return None


class PreviewWriter:
    def __init__(self, service, lease, conv, step_no, *, followup=False,stage_no=None,stage_segment_no=1,existing_blocks=None,require_alignment=False):
        self.service, self.lease, self.conv, self.step_no = service, lease, conv, step_no
        self.raw = ""
        self.last_write = 0.0
        self.last_payload = None
        self.followup = followup
        self.stage_no = stage_no or step_no
        self.stage_segment_no = stage_segment_no
        self.existing_blocks = existing_blocks or {}
        self.require_alignment = require_alignment
        self.allowed_material_ids = set(json.loads(conv.material_snapshot_json or "[]"))

    def __call__(self, delta, reset=False):
        if reset:
            self.raw = ""
            self.last_payload = None
        else:
            self.raw += str(delta)
        if len(self.raw.encode("utf8")) > 32*1024:
            raise terr("MODEL_RESPONSE_TOO_LARGE")
        payload = partial_step(self.raw,followup=self.followup,allowed_material_ids=self.allowed_material_ids,
            existing_blocks=self.existing_blocks,require_alignment=self.require_alignment) if self.raw else {"text":"", "title":"", "board":None}
        if payload is None:
            return
        payload.update(stage_no=self.stage_no,stage_segment_no=self.stage_segment_no,
                       board_scope=f"{self.conv.conversation_id}:stage:{self.stage_no}",step_no=self.step_no, status="generating", followup=self.followup,
                       conversation_generation=self.conv.generation)
        if payload == self.last_payload or (not reset and self.last_payload is not None and monotonic()-self.last_write < .08):
            return
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded.encode("utf8")) > 24*1024:
            raise terr("MODEL_RESPONSE_TOO_LARGE")
        with self.service.db.session() as sess:
            task = sess.get(LearningTaskRow,self.lease.task_id,with_for_update=True)
            conv = sess.get(ConversationRow,self.conv.conversation_id,with_for_update=True)
            course = sess.get(CourseRow,conv.course_id) if conv else None
            now = utcnow()
            if (not task or task.attempt_id != self.lease.attempt_id or task.lease_token != self.lease.lease_token
                or task.generation != self.lease.generation or task.cancel_requested
                or not task.lease_until or task.lease_until < now
                or not conv or conv.deleted_at or conv.generation != self.conv.generation
                or (not self.followup and conv.session_state != "active") or not course or course.deleted_at):
                raise LeaseError("STALE_RESULT_DISCARDED", "preview fence rejected")
            row = sess.get(TeachingPreviewRow,task.task_id)
            if row is None:
                row = TeachingPreviewRow(task_id=task.task_id,conversation_id=conv.conversation_id,
                    attempt_id=task.attempt_id,task_generation=task.generation,sequence=0,payload_json="{}")
                sess.add(row)
            row.attempt_id = task.attempt_id
            row.task_generation = task.generation
            row.sequence += 1
            row.payload_json = encoded
            row.updated_at = now
            # Expired drafts are cleared opportunistically, including unused streams.
            sess.execute(delete(TeachingPreviewRow).where(TeachingPreviewRow.updated_at < now-timedelta(minutes=10)))
            sess.commit()
        self.last_payload = payload
        self.last_write = monotonic()


def read_snapshot(service, task_id, actor):
    service.tasks.sweep_expired_tasks(task_ids=[task_id])
    with service.db.session() as sess:
        task = sess.get(LearningTaskRow,task_id)
        if not task or task.owner_user_id != actor or task.kind != "lesson_step_generate":
            raise terr("task_not_found",status=404)
        state = sess.get(TaskStateRow, task_id)
        task_error_code = state.error_code if state else None
        conv = sess.get(ConversationRow,task.subject_id.split(":")[0])
        course = sess.get(CourseRow,conv.course_id) if conv else None
        if not conv or conv.owner_user_id != actor or conv.deleted_at or not course or course.deleted_at:
            raise terr("task_not_found",status=404)
        row = sess.get(TeachingPreviewRow,task_id)
        is_followup = ":followup:" in task.subject_id
        obsolete = row and json.loads(row.payload_json).get("conversation_generation") != conv.generation
        terminal = obsolete or task.status in ("succeeded","failed","cancelled","expired") or task.cancel_requested or (not is_followup and conv.session_state != "active")
        if terminal:
            if row:
                sess.delete(row);sess.commit()
            return {"event":"done" if task.status == "succeeded" and not task.cancel_requested and not obsolete else "error",
                "status":task.status if not task.cancel_requested else "cancelled",
                "error_code": task_error_code,
                "required_action": "authorize" if task_error_code in ("DISCLOSURE_REQUIRED","DISCLOSURE_REVOKED","DISCLOSURE_EXPIRED","CONFIG_NOT_FOUND") else "retry" if task.status in ("failed","expired") else None,
                "generation":task.generation,"attempt":task.attempt,
                "id":f"{task.generation}:{task.attempt}:done"}
        if (row and row.attempt_id == task.attempt_id and row.task_generation == task.generation
            and row.updated_at >= utcnow()-timedelta(minutes=10)):
            return {"event":"snapshot","generation":task.generation,"attempt":task.attempt,
                "id":f"{task.generation}:{task.attempt}:{row.sequence}",**json.loads(row.payload_json)}
        if row:
            sess.delete(row);sess.commit()
        return {"event":"status","status":task.status,"generation":task.generation,"attempt":task.attempt,
                "id":f"{task.generation}:{task.attempt}:waiting"}
