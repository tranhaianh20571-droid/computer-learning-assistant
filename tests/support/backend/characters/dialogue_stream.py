"""Private, lease-fenced reply snapshots; never task events or runtime logs."""
import json
from datetime import UTC, datetime
from time import monotonic

from pydantic_core import from_json
from sqlalchemy import select

from ..planning.errors import error
from ..tasks.service import LearningTaskRow
from .distillation_models import CharacterBuildStageRow
from .service import reject


def reply_fields(value):
    return {key: value.get(key, "")[:limit] if isinstance(value.get(key, ""), str) else ""
            for key, limit in (("reply", 6000), ("question", 1000))}


class DialoguePreviewWriter:
    def __init__(self, service, root, lease):
        self.service, self.root, self.lease = service, root, lease
        self.raw = ""
        self.last_payload = None
        self.last_write = 0.0

    def __call__(self, delta, reset=False):
        if reset:
            self.raw = ""
        else:
            self.raw += str(delta)
        if len(self.raw.encode("utf-8")) > 512_000:
            raise error("MODEL_RESPONSE_TOO_LARGE")
        try:
            value = from_json(self.raw, allow_partial="trailing-strings") if self.raw else {}
        except ValueError:
            return
        if not isinstance(value, dict):
            return
        payload = reply_fields(value)
        if payload == self.last_payload or (
            not reset and self.last_payload is not None and any(self.last_payload.values())
            and monotonic() - self.last_write < .08
        ):
            return
        from .distillation import build_domain

        with self.service.db.session() as sess:
            self.service.tasks.validate_lease(self.root, session=sess)
            self.service.tasks.validate_lease(self.lease, session=sess)
            build_domain(sess, sess.get(LearningTaskRow, self.lease.task_id))
            stage = sess.get(CharacterBuildStageRow, self.lease.task_id, with_for_update=True)
            state = json.loads(stage.state_json or "{}")
            state["dialogue_preview"] = {
                **payload, "generation": self.root.generation,
                "attempt_id": self.lease.attempt_id,
            }
            stage.state_json = json.dumps(state, ensure_ascii=False)
        self.last_payload, self.last_write = payload, monotonic()


def dialogue_snapshot(service, owner, ident):
    from .distillation import build_domain, utc_deadline

    with service.db.session() as sess:
        row = service._owned(sess, owner, ident)
        if not json.loads(row.request_json).get("dialogue"):
            reject("state_invalid", 409)
        task = sess.get(LearningTaskRow, row.task_id)
        terminal = task.status in ("succeeded", "failed", "cancelled", "expired")
        snapshot = {"build_id": ident, "generation": task.generation,
                    "status": task.status, "event": "done" if terminal else "preview"}
        if terminal:
            return snapshot
        build_domain(sess, task)
        stage = sess.scalar(select(CharacterBuildStageRow).where(
            CharacterBuildStageRow.build_id == ident,
            CharacterBuildStageRow.stage_key == "dialogue_reply"))
        preview = {}
        if stage:
            child = sess.get(LearningTaskRow, stage.task_id)
            if child.status == "succeeded" and stage.result_json != "{}":
                preview = json.loads(stage.result_json)
            else:
                stored = json.loads(stage.state_json or "{}").get("dialogue_preview", {})
                if (stored.get("generation") == task.generation
                    and stored.get("attempt_id") == child.attempt_id
                    and child.status in ("leased", "running") and not child.cancel_requested
                    and child.lease_until
                    and utc_deadline(child.lease_until) > datetime.now(UTC)):
                    preview = stored
        return {**snapshot, **reply_fields(preview)}
