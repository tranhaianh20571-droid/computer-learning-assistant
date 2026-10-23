"""Small durable delivery layer; extraction and consolidation belong to Hindsight."""
import asyncio
import hashlib
import json
import threading
import uuid
from datetime import timedelta

from sqlalchemy import func, or_, select

from ..auth.models import UserRow
from ..planning.errors import error
from .adapter import HindsightAdapter
from .models import MemorySettingsRow, MemorySourceRow, MemorySpaceRow, now

ALLOWED = {
    "chat_user_statement": "profile",
    "planning_user_statement": "profile",
    "unit_assessment_result": "profile",
    "course_assessment_result": "course",
    "course_question": "course",
    "course_feynman": "course",
    "character_delivery_feedback": "character",
    "character_feedback_candidate": "character",
}


def clean_payload(value):
    from ..logging_audit.sanitize import redact_sensitive_text, strip_control_chars
    if isinstance(value, str):
        return redact_sensitive_text(strip_control_chars(value))[:12000]
    if isinstance(value, list):
        return [clean_payload(item) for item in value[:100]]
    if isinstance(value, dict):
        return {key: clean_payload(item) for key, item in value.items()
                if key.lower() not in ("password", "api_key", "token", "cookie", "authorization", "credentials")}
    return value


def enqueue(session, owner, kind, event_id, payload, *, course="", card=""):
    """Call in the publishing business transaction; no network IO here."""
    setting = session.get(MemorySettingsRow, owner, with_for_update=True)
    if not setting or not setting.enabled or owner.startswith("guest_"):
        return
    category = ALLOWED.get(kind)
    if not category or (category == "course" and not course) or (category == "character" and not card):
        raise error("invalid_field", "invalid memory scope")
    if course:
        from ..planning.models import CourseRow
        row = session.get(CourseRow, course, with_for_update=True)
        if not row or row.owner_user_id != owner or row.deleted_at:
            return
    scope = "course:" + course if category == "course" else "general"
    space = session.scalar(select(MemorySpaceRow).where(
        MemorySpaceRow.owner_user_id == owner, MemorySpaceRow.scope_key == scope).with_for_update())
    if not space:
        space = MemorySpaceRow(space_id=uuid.uuid4().hex, owner_user_id=owner,
            scope_key=scope, bank_id="learning-" + uuid.uuid4().hex, epoch=1, status="active")
        session.add(space)
        session.flush()
    if space.status != "active":
        return
    tag = "character:" + card if category == "character" else category
    stable = hashlib.sha256(f"{owner}:{kind}:{event_id}:{scope}:{tag}".encode()).hexdigest()
    previous = session.scalar(select(MemorySourceRow).where(
        MemorySourceRow.owner_user_id == owner, MemorySourceRow.event_key == stable))
    if previous:
        return previous.source_id
    identity = hashlib.sha256(f"{owner}:{kind}:{event_id}:{scope}:{tag}:{space.epoch}".encode()).hexdigest()
    epochs = set(session.scalars(select(MemorySourceRow.epoch).where(
        MemorySourceRow.owner_user_id == owner, MemorySourceRow.scope_key == scope,
        MemorySourceRow.source_kind == kind, MemorySourceRow.tag == tag).distinct()))
    legacy_ids = [hashlib.sha256(f"{owner}:{kind}:{event_id}:{scope}:{tag}:{epoch}".encode()).hexdigest()
                  for epoch in epochs | {space.epoch}]
    existing = session.scalar(select(MemorySourceRow).where(
        MemorySourceRow.owner_user_id == owner, MemorySourceRow.source_id.in_(legacy_ids))
        .order_by(MemorySourceRow.epoch).limit(1))
    if existing:
        existing.event_key = stable
        return existing.source_id
    session.add(MemorySourceRow(source_id=identity, owner_user_id=owner, scope_key=scope,
        tag=tag, source_kind=kind, source_course_id=course, document_id=identity,
        payload_json=json.dumps(clean_payload(payload), ensure_ascii=False), settings_revision=setting.revision,
        epoch=space.epoch, event_key=stable))
    return identity


def delete_course_memory(session, owner, course):
    from .learning import revoke
    revoke(session, owner, course=course)
    from .maintenance import revoke_jobs
    revoke_jobs(session, owner, course=course)
    space = session.scalar(select(MemorySpaceRow).where(MemorySpaceRow.owner_user_id == owner,
        MemorySpaceRow.scope_key == "course:" + course).with_for_update())
    if space:
        space.status = "deleting"
        space.epoch += 1
    # Published general sources have an independent lifecycle.
    for source in session.scalars(select(MemorySourceRow).where(
            MemorySourceRow.owner_user_id == owner, MemorySourceRow.source_course_id == course)):
        if source.scope_key.startswith("course:"):
            source.status, source.payload_json = "suppressed", "{}"
        elif source.status in ("retained", "completed", "compacted", "forgetting", "forget_failed"):
            source.source_course_id = ""
        else:
            source.status, source.payload_json = "suppressed", "{}"


class MemoryService:
    def __init__(self, db, audit, adapter=None):
        self.db, self.audit = db, audit
        self.adapter = adapter or HindsightAdapter(db)
        self.stop_event = threading.Event()
        self.thread = None
        self.maintenance_thread = None
        self.maintenance_error = None

    def settings(self, owner):
        with self.db.session() as session:
            row = session.get(MemorySettingsRow, owner)
            return {"enabled": bool(row and row.enabled), "revision": row.revision if row else 0,
                    "processor": "self_hosted_hindsight",
                    "scope": "profile, course memory and private character delivery feedback"}

    def version(self, owner):
        from .learning import ALGORITHM
        from .models import LearningEvidenceRow
        with self.db.session() as session:
            setting = session.get(MemorySettingsRow, owner)
            if not setting or not setting.enabled:
                return "disabled"
            latest, count = session.execute(select(func.max(MemorySourceRow.updated_at),
                func.count(MemorySourceRow.source_id)).where(MemorySourceRow.owner_user_id == owner)).one()
            evidence_version = session.execute(select(func.max(LearningEvidenceRow.updated_at),
                func.count(LearningEvidenceRow.evidence_id)).where(
                    LearningEvidenceRow.owner_user_id == owner)).one()
            return hashlib.sha256(
                f"{setting.revision}:{latest}:{count}:{evidence_version}:{now().date()}:{ALGORITHM}".encode()).hexdigest()

    def configure(self, owner, enabled, expected):
        with self.db.session() as session:
            user = session.get(UserRow, owner, with_for_update=True)
            if not user or user.status != "approved":
                raise error("state_invalid", status=409)
            row = session.get(MemorySettingsRow, owner, with_for_update=True)
            if (row.revision if row else 0) != expected:
                raise error("state_invalid", status=409)
            if not row:
                row = MemorySettingsRow(owner_user_id=owner, revision=0)
                session.add(row)
            row.enabled, row.revision = enabled, row.revision + 1
            if self.audit:
                self.audit.append("config.changed", actor_id=owner, object_type="memory_settings",
                    object_id=owner, result="enabled" if enabled else "disabled", session=session)
        return self.settings(owner)

    def _space(self, session, owner, scope):
        if scope.startswith("course:"):
            from ..planning.models import CourseRow
            course = session.get(CourseRow, scope[7:])
            if not course or course.owner_user_id != owner or course.deleted_at:
                raise error("course_not_found", status=404)
        return session.scalar(select(MemorySpaceRow).where(MemorySpaceRow.owner_user_id == owner,
            MemorySpaceRow.scope_key == scope, MemorySpaceRow.status == "active"))

    async def read(self, owner, scope="general", tag="profile", *, query=None, reflect=False):
        with self.db.session() as session:
            space = self._space(session, owner, scope)
            if not space:
                return {"status": "empty", "items": []}
            bank, epoch = space.bank_id, space.epoch
            enabled = session.get(MemorySettingsRow, owner)
            if not enabled or not enabled.enabled:
                return {"status": "disabled", "items": []}
            blocked = session.scalar(select(MemorySourceRow.source_id).where(
                MemorySourceRow.owner_user_id == owner, MemorySourceRow.scope_key == scope,
                MemorySourceRow.tag == tag, MemorySourceRow.status.in_(("forgetting", "forget_failed", "suppressed_pending"))))
            if blocked:
                return {"status": "cleaning", "items": []}
        try:
            if reflect:
                result = await self.adapter.reflect(bank, tag, query)
            elif query:
                result = await self.adapter.recall(bank, tag, query)
            else:
                result = await self.adapter.models(bank, tag)
            result = result if isinstance(result, dict) else result.model_dump(mode="json")
        except Exception:
            return {"status": "unavailable", "items": []}
        with self.db.session() as session:
            current = self._space(session, owner, scope)
            setting = session.get(MemorySettingsRow, owner)
            user = session.get(UserRow, owner)
            blocked = session.scalar(select(MemorySourceRow.source_id).where(
                MemorySourceRow.owner_user_id == owner, MemorySourceRow.scope_key == scope,
                MemorySourceRow.tag == tag, MemorySourceRow.status.in_(("forgetting", "forget_failed", "suppressed_pending"))))
            if (not current or current.epoch != epoch or current.bank_id != bank or not setting or not setting.enabled
                    or not user or user.status != "approved" or blocked):
                return {"status": "stale", "items": []}
        return {"status": "ready", "data": result}

    def context_for_role(self, owner, role, *, course="", card="", lesson=""):
        if role in ("research","research_search"):
            return {"status":"excluded","scopes":[]}
        if role not in ("planning","teaching","evaluation"):
            return {"status":"unavailable","scopes":[]}
        """Only already materialized native models; no foreground reflect."""
        async def read_all():
            scopes = [("general", "profile")]
            if course:
                scopes.append(("course:" + course, "course"))
            if card and role == "teaching":
                scopes.append(("general", "character:" + card))
            values = await asyncio.wait_for(asyncio.gather(
                *(self.read(owner, scope, tag) for scope, tag in scopes)), timeout=.8)
            remaining = [3200]
            def bounded(value):
                if isinstance(value,str):
                    part=value[:min(1600,max(0,remaining[0]))];remaining[0]-=len(part);return part
                if isinstance(value,list):
                    return [bounded(item) for item in value[:8] if remaining[0]>0]
                if isinstance(value,dict):
                    return {key:bounded(item) for key,item in list(value.items())[:12] if remaining[0]>0}
                return value
            return {"status":"ready","scopes":[{"scope":scope,"tag":tag,"result":bounded(value)}
                for (scope,tag),value in zip(scopes,values)]}
        try:
            result = asyncio.run(read_all())
        except Exception:
            result = {"status":"unavailable","scopes":[]}
        if course:
            from .learning import context
            with self.db.session() as session:
                numeric = context(session, owner, course, lesson=lesson)
            if numeric["status"] == "ready":
                result["learning_state"] = numeric
        return result

    def context(self, owner, *, course="", card="", lesson=""):
        return json.dumps(self.context_for_role(owner,"teaching" if course or card else "planning",
            course=course,card=card,lesson=lesson),ensure_ascii=False)

    def forget(self, owner, source_id):
        with self.db.session() as session:
            source = session.get(MemorySourceRow, source_id, with_for_update=True)
            if not source or source.owner_user_id != owner:
                raise error("conversation_not_found", status=404)
            source.status, source.payload_json, source.attempts = "forgetting", "{}", 0
            from .learning import revoke
            revoke(session, owner, source_id=source_id)
            from .maintenance import revoke_jobs
            revoke_jobs(session, owner, source_id=source_id)
            source.updated_at = now()
            source.revision += 1
            if self.audit:
                self.audit.append("resource.delete.requested", actor_id=owner,
                    object_type="memory", object_id=source_id, result="accepted", session=session)
        return {"status": "cleaning"}

    def correct(self, owner, source_id, text, expected_revision):
        with self.db.session() as session:
            source = session.get(MemorySourceRow, source_id, with_for_update=True)
            if not source or source.owner_user_id != owner or source.scope_key != "general":
                raise error("conversation_not_found", status=404)
            if source.revision != expected_revision:
                raise error("state_invalid", status=409)
            setting = session.get(MemorySettingsRow, owner, with_for_update=True)
            if not setting or not setting.enabled:
                raise error("state_invalid", status=409)
            source.status, source.payload_json, source.attempts = "forgetting", "{}", 0
            from .learning import revoke
            revoke(session, owner, source_id=source_id)
            from .maintenance import revoke_jobs
            revoke_jobs(session, owner, source_id=source_id)
            source.revision += 1
            source.updated_at = now()
            ident = uuid.uuid4().hex
            session.add(MemorySourceRow(source_id=ident, owner_user_id=owner, scope_key="general",
                tag=source.tag, source_kind=source.source_kind, source_course_id="",
                document_id=ident, payload_json=json.dumps({"explicit_user_correction": clean_payload(text),
                    "supersedes": source.source_id}, ensure_ascii=False),
                settings_revision=setting.revision, epoch=source.epoch))
            if self.audit:
                self.audit.append("config.changed", actor_id=owner, object_type="memory",
                    object_id=source_id, result="corrected", session=session)
        return {"status": "pending"}

    def reset(self, owner):
        with self.db.session() as session:
            from .learning import revoke
            from .maintenance import revoke_jobs
            revoke(session, owner)
            revoke_jobs(session, owner)
            rows = list(session.scalars(select(MemorySourceRow).where(
                MemorySourceRow.owner_user_id == owner).with_for_update()))
            for source in rows:
                if source.status != "forgotten":
                    source.status, source.payload_json, source.attempts = "forgetting", "{}", 0
                    source.updated_at = now()
                    source.revision += 1
            if self.audit:
                self.audit.append("resource.delete.requested", actor_id=owner,
                    object_type="memory", object_id=owner, result="reset", session=session)
        return {"status": "cleaning"}

    def start(self):
        if self.thread:
            return
        self.thread = threading.Thread(target=self._run, name="product-memory", daemon=True)
        self.thread.start()
        self.maintenance_thread = threading.Thread(
            target=self._maintain, name="memory-maintenance", daemon=True)
        self.maintenance_thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        if self.maintenance_thread:
            self.maintenance_thread.join(timeout=2)

    def _maintain(self):
        from .maintenance import maintenance_tick
        while not self.stop_event.wait(30):
            try:
                maintenance_tick(self)
                self.maintenance_error = None
            except Exception:
                self.maintenance_error = "maintenance_unconfirmed"
                # Jobs retain controlled failure state; never emit provider content.
                if self.audit:
                    try:
                        self.audit.append("resource.delete.requested", actor_id="system", object_type="memory",
                            object_id="maintenance", result="maintenance_unconfirmed")
                    except Exception:
                        self.maintenance_error = "AUDIT_WRITE_FAILED"

    def _run(self):
        while not self.stop_event.is_set():
            try:
                from ..materials.corpus import CorpusWorker
                CorpusWorker(self.db, self.audit).tick()
                self.tick()
            except Exception:
                pass  # No third-party error/body is allowed into application logs.
            self.stop_event.wait(2)

    def tick(self):
        from .migration import migration_tick
        migration_tick(self)
        with self.db.session() as session:
            deleting = session.scalar(select(MemorySpaceRow).where(MemorySpaceRow.status == "deleting")
                                      .with_for_update(skip_locked=True))
            if deleting:
                bank = deleting.bank_id
                try:
                    asyncio.run(self.adapter.delete_bank(bank))
                    deleting.status = "deleted"
                except Exception:
                    pass
                return
            source = session.scalar(select(MemorySourceRow).where(
                MemorySourceRow.status.in_(("pending", "retained", "forgetting")),
                or_(MemorySourceRow.lease_until.is_(None), MemorySourceRow.lease_until < now()),
                MemorySourceRow.attempts < 3).order_by(MemorySourceRow.created_at)
                .with_for_update(skip_locked=True))
            if not source:
                return
            user = session.get(UserRow, source.owner_user_id)
            setting = session.get(MemorySettingsRow, source.owner_user_id)
            space = self._space(session, source.owner_user_id, source.scope_key)
            if source.source_course_id and source.status == "pending":
                from ..planning.models import CourseRow
                course = session.get(CourseRow, source.source_course_id)
                if not course or course.deleted_at:
                    source.status, source.payload_json = "suppressed", "{}"
                    return
            if (not user or user.status != "approved" or not space or space.epoch != source.epoch):
                source.status = "suppressed"
                return
            if source.status != "forgetting" and (
                    not setting or not setting.enabled or setting.revision != source.settings_revision):
                source.status = "suppressed"
                return
            source.attempts += 1
            source.updated_at = now()
            source.lease_until = now() + timedelta(seconds=240)
            # Persist the debit before an external call; a crashed process cannot
            # reset the budget or make the source immediately available to peers.
            identity, source_owner, source_course = source.source_id, source.owner_user_id, source.source_course_id
            session.commit()
            user = session.get(UserRow, source_owner, with_for_update=True, populate_existing=True)
            setting = session.get(MemorySettingsRow, source_owner, with_for_update=True, populate_existing=True)
            current_course = None
            if source_course:
                from ..planning.models import CourseRow
                current_course = session.get(CourseRow, source_course, with_for_update=True, populate_existing=True)
            source = session.get(MemorySourceRow, identity, with_for_update=True, populate_existing=True)
            if source.status == "pending" and source.source_course_id and (
                    not current_course or current_course.deleted_at):
                source.status, source.payload_json, source.lease_until = "suppressed", "{}", None
                return
            space = self._space(session, source.owner_user_id, source.scope_key)
            if (not user or user.status != "approved" or not space or space.epoch != source.epoch
                    or source.status not in ("pending", "retained", "forgetting")
                    or source.status != "forgetting" and (
                        not setting or not setting.enabled or setting.revision != source.settings_revision)):
                source.status, source.lease_until = "suppressed", None
                return
            # Row locks fence deletion/disable across this bounded engine call.
            try:
                if source.status == "forgetting":
                    from .maintenance import retract_compacted
                    retract_compacted(self, session, source, space)
                    asyncio.run(self.adapter.delete_document(space.bank_id, source.document_id, source.tag))
                    source.status = "forgotten"
                elif source.status == "retained":
                    if source.operation_id:
                        result = asyncio.run(self.adapter.operation(space.bank_id, source.operation_id))
                        data = result if isinstance(result, dict) else result.model_dump(mode="json")
                        if data.get("status") == "completed":
                            source.status = "completed"
                        elif data.get("status") == "failed":
                            source.status = "failed"
                        else:
                            source.attempts -= 1
                            if now() - source.created_at.replace(tzinfo=now().tzinfo) > timedelta(minutes=20):
                                source.status = "failed"
                    else:
                        result = asyncio.run(self.adapter.refresh(space.bank_id, source.tag))
                        source.operation_id = getattr(result, "operation_id", None)
                        if isinstance(result, dict):
                            source.operation_id = result.get("operation_id")
                        if not source.operation_id:
                            source.status = "completed"
                else:
                    if hasattr(self.adapter, "bind"):
                        self.adapter.bind(space.bank_id, source.owner_user_id)
                    if source.source_kind == "character_feedback_candidate":
                        from .style import explicit_style_feedback
                        reference = session.get(MemorySourceRow, json.loads(source.payload_json)["source_id"])
                        if (not reference or reference.owner_user_id != source.owner_user_id
                                or reference.source_course_id != source.source_course_id
                                or reference.status in ("suppressed", "forgetting", "forgotten", "forget_failed")):
                            source.status, source.payload_json = "suppressed", "{}"
                            return
                        original = json.loads(reference.payload_json)
                        utterance = original.get("answer" if reference.source_kind == "course_feynman" else "question", "")
                        signal = asyncio.run(explicit_style_feedback(utterance, source.owner_user_id, db=self.db))
                        if not signal.explicit:
                            source.status, source.payload_json = "suppressed", "{}"
                            return
                        source.payload_json = json.dumps(signal.model_dump(), ensure_ascii=False)
                        source.source_kind = "character_delivery_feedback"
                    asyncio.run(self.adapter.retain(space.bank_id, source.document_id,
                        source.payload_json, source.tag, source.created_at))
                    source.status, source.attempts = "retained", 0
            except Exception as exc:
                # Persist attempt count without retaining the provider exception.
                if getattr(exc, "code", "") == "config_not_found":
                    # A missing personal configuration is waiting for user action,
                    # not an attempted billable provider request.
                    source.attempts = max(0, source.attempts - 1)
                elif source.attempts >= 3:
                    source.status = "forget_failed" if source.status == "forgetting" else "failed"
            finally:
                source.lease_until = None
