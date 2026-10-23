"""Explicit lesson consent, immutable snapshots, and revocable child grants."""
from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from ..logging_audit.models import Base
from ..materials.models import MaterialRow
from ..planning.models import (ConversationRow, CourseRow, LessonRow, OutlineVersionRow,
    LessonContentVersionRow, utcnow)
from ..slice1.errors import error
from ..slice1.models import DisclosureGrantRow, ServiceConfigRow
from ..tasks.service import LearningTaskRow
from .learning_models import LessonBlueprintRow, PreparedFirstSegmentRow
from .pedagogy import lesson_context


class LessonAuthorizationRow(Base):
    __tablename__ = "lesson_authorizations"
    __table_args__ = (Index("idx_lesson_authorizations_owner", "owner_user_id"),
        Index("idx_lesson_authorizations_lesson", "lesson_id"),)

    authorization_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    course_id: Mapped[str] = mapped_column(String(64), ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False)
    lesson_id: Mapped[str] = mapped_column(String(64), ForeignKey("lessons.lesson_id", ondelete="CASCADE"), nullable=False)
    outline_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("conversations.conversation_id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    context_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_config_id: Mapped[str | None] = mapped_column(String(64))
    content_config_version: Mapped[int | None] = mapped_column(Integer)
    tts_config_id: Mapped[str | None] = mapped_column(String(64))
    tts_config_version: Mapped[int | None] = mapped_column(Integer)
    content_root_grant_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("disclosure_grants.grant_id"))
    tts_root_grant_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("disclosure_grants.grant_id"))
    prewarm_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def audio_scope(conv):
    return {"course_id": conv.course_id, "lesson_id": conv.lesson_id,
        "outline_version_id": conv.outline_version_id}


class TeachingAuthorization:
    def __init__(self, service):
        self.service = service
        # Production enables configured teaching; injected legacy callers keep explicit consent.
        self.automatic_enabled = False

    def automatic_request(self, course_id, lesson_id, *, actor_id, purpose="start", conversation_id=None, session=None):
        """Use enabled configurations while preserving snapshots and explicit revocations."""
        if not self.automatic_enabled:
            raise error("disclosure_required", "automatic teaching disabled")
        with (self.service.db.session() if session is None else nullcontext(session)) as sess:
            snapshot = self.snapshot(sess, course_id, lesson_id, actor_id,
                purpose=purpose, conversation_id=conversation_id)
            if not snapshot["content_model"] or not self.service.disclosures:
                raise error("config_not_found", "content configuration unavailable")
            rows = list(sess.scalars(select(LessonAuthorizationRow).where(
                LessonAuthorizationRow.owner_user_id == actor_id,
                LessonAuthorizationRow.course_id == course_id,
                LessonAuthorizationRow.lesson_id == lesson_id)))
            for row in rows:
                for kind, root_id in (("content_model", row.content_root_grant_id), ("tts", row.tts_root_grant_id)):
                    config = snapshot[kind]
                    root = sess.get(DisclosureGrantRow, root_id, populate_existing=True) if root_id else None
                    if (config and root and root.config_id == config["config_id"]
                            and root.config_version == config["config_version"] and root.revoked_at):
                        if kind == "content_model":
                            raise error("disclosure_revoked", "configured teaching authorization revoked")
                        snapshot["tts"] = None
            if conversation_id:
                revoked = sess.scalars(select(DisclosureGrantRow).join(LearningTaskRow,
                    DisclosureGrantRow.task_id == LearningTaskRow.task_id).where(
                    LearningTaskRow.owner_user_id == actor_id,
                    LearningTaskRow.subject_id.like(conversation_id + ":%"),
                    DisclosureGrantRow.revoked_at.is_not(None))).all()
                for grant in revoked:
                    config = snapshot["tts" if grant.content_category == "audio_text" else "content_model"]
                    if config and grant.config_id == config["config_id"] and grant.config_version == config["config_version"]:
                        if grant.content_category != "audio_text":
                            raise error("disclosure_revoked", "task authorization revoked")
                        snapshot["tts"] = None
            # Recompute using the unchanged live snapshot: voice revocation only disables audio confirmation.
            voice_enabled = snapshot["tts"] is not None
            live = self.snapshot(sess, course_id, lesson_id, actor_id,
                purpose=purpose, conversation_id=conversation_id)
            return {"fingerprint": live["fingerprint"], "content_confirmed": True,
                "tts_confirmed": voice_enabled, "prewarm_opt_in": bool(self.service.preparation.enabled),
                "purpose": purpose, "conversation_id": conversation_id}

    @property
    def enforced(self):
        return self.service.disclosures is not None or self.service.configs is not None

    def _config(self, sess, owner, kind, *, role="teaching"):
        configs = self.service.configs
        if configs and kind == "content_model":
            from ..agents.model_routing import role_config
            view = role_config(configs, owner, role)
        else:
            view = configs.latest_active_for_owner(owner, kind) if configs else None
        if not view:
            factory = self.service.model_adapter_factory if kind == "content_model" else self.service.tts_adapter_factory
            getter = getattr(factory, "config_for_owner", None)
            view = getter(owner) if getter else None
        row = sess.get(ServiceConfigRow, view["config_id"], populate_existing=True) if view else None
        if not row or not row.is_active or row.kind != kind:
            return None
        if not ((row.owner_scope == "user" and row.owner_user_id == owner) or
                (row.owner_scope == "admin" and row.owner_user_id is None)):
            raise error("access_denied")
        if kind == "tts" and row.protocol != "minimax":
            return None
        return {"config_id": row.config_id, "config_version": row.config_version,
            "service_kind": row.kind, "protocol": row.protocol, "model_name": row.model_name,
            "service_endpoint": row.endpoint}

    def snapshot(self, sess, course_id, lesson_id, owner, *, purpose="start", conversation_id=None):
        if purpose not in ("start", "prewarm", "recover"):
            raise error("invalid_field")
        course = sess.get(CourseRow, course_id, with_for_update=True, populate_existing=True)
        lesson = sess.get(LessonRow, lesson_id, populate_existing=True)
        outline = (sess.get(OutlineVersionRow, course.current_outline_version_id)
            if course and course.current_outline_version_id else None)
        if not course or course.owner_user_id != owner or course.deleted_at:
            raise error("course_not_found", status=404)
        if not outline or outline.course_id != course_id or not lesson or lesson.outline_version_id != course.current_outline_version_id:
            raise error("LESSON_UNLOCK_REQUIRED", status=409)
        conv = sess.get(ConversationRow, conversation_id, populate_existing=True) if conversation_id else None
        if conversation_id and (not conv or conv.owner_user_id != owner or conv.deleted_at or
                conv.course_id != course_id or conv.lesson_id != lesson_id or conv.outline_version_id != course.current_outline_version_id):
            raise error("conversation_not_found", status=404)
        target = conv or SimpleNamespace(owner_user_id=owner, course_id=course_id, lesson_id=lesson_id,
            outline_version_id=course.current_outline_version_id, conversation_id="preflight")
        context = lesson_context(self.service, target)
        context.pop("scope_capabilities", None)
        context.pop("unit_scope_hash", None)
        materials = []
        for material_id in sorted(set(context.get("material_ids", []))):
            material = sess.get(MaterialRow, material_id, populate_existing=True)
            if not material or material.owner_user_id != owner or material.deleted_at:
                raise error("material_not_found", status=404)
            materials.append({"material_id": material_id, "source_bytes_sha256": material.source_bytes_sha256,
                "kind": material.kind})
        version = sess.scalars(select(LessonContentVersionRow).where(LessonContentVersionRow.lesson_id == lesson_id)
            .order_by(LessonContentVersionRow.content_version_no.desc())).first()
        data = {"course_id": course_id, "lesson_id": lesson_id, "outline_version_id": course.current_outline_version_id,
            "context": context, "materials": materials, "content_version": version.content_version_no if version else 1,
            "templates": {name: self.service.prompts.load_local_template(name)["sha256"]
                for name in ("teaching_step_v1", "lesson_blueprint_v1", "followup_v1")}}
        if conv:
            blueprint = sess.get(LessonBlueprintRow, conv.conversation_id)
            frozen_context = ({k:v for k,v in json.loads(blueprint.context_json).items()
                if not k.startswith("_") and k not in ("scope_capabilities", "unit_scope_hash")}
                if blueprint else None)
            if blueprint and frozen_context != context:
                raise error("disclosure_required", "lesson context changed; start a new session")
            if blueprint:
                # A template deployment cannot invalidate a running lesson's
                # authorization while its tasks still use frozen prompt snapshots.
                from ..prompts.service import PromptBindingRow
                for name in data["templates"]:
                    binding=sess.scalar(select(PromptBindingRow).where(
                        PromptBindingRow.task_id==blueprint.source_task_id,
                        PromptBindingRow.prompt_name==name).order_by(PromptBindingRow.created_at))
                    if binding:
                        if hashlib.sha256(binding.template_snapshot.encode()).hexdigest()!=binding.template_sha256:
                            raise error("prompt_unavailable")
                        data["templates"][name]=binding.template_sha256
        content_config = self._config(sess, owner, "content_model")
        planning_config = self._config(sess, owner, "content_model", role="planning")
        if planning_config and content_config and planning_config != content_config:
            data["planning_model"] = planning_config
        tts_config = self._config(sess, owner, "tts")
        scope = {"course_id": course_id, "lesson_id": lesson_id, "outline_version_id": course.current_outline_version_id,
            "material_ids": context.get("material_ids", [])}
        snapshot = {"purpose": purpose, "conversation_id": conversation_id, "scope": scope, "data": data,
            "content_model": content_config, "planning_model": planning_config, "tts": tts_config}
        snapshot["fingerprint"] = digest(snapshot)
        return snapshot

    def preflight(self, course_id, lesson_id, *, actor_id, purpose="start", conversation_id=None):
        with self.service.db.session() as sess:
            snapshot = self.snapshot(sess, course_id, lesson_id, actor_id, purpose=purpose, conversation_id=conversation_id)
        return {key: value for key,value in snapshot.items() if key != "data"} | {
            "course_title": snapshot["data"]["context"]["course_title"],
            "lesson_title": snapshot["data"]["context"]["lesson_title"],
            "readiness": "ready" if snapshot["content_model"] else "content_config_required",
            "confirmation_required": {"content": True, "tts": bool(snapshot["tts"]), "prewarm": purpose == "prewarm"}}

    def prepare_confirmation(self, sess, course_id, lesson_id, owner, *, fingerprint,
                             content_confirmed, tts_confirmed=False, prewarm_opt_in=False,
                             purpose="start", conversation_id=None):
        snapshot = self.snapshot(sess, course_id, lesson_id, owner, purpose=purpose, conversation_id=conversation_id)
        if snapshot["fingerprint"] != fingerprint:
            raise error("disclosure_required", "preflight changed; review again")
        if not content_confirmed or not snapshot["content_model"] or not self.service.disclosures:
            raise error("disclosure_required", "content confirmation required")
        if tts_confirmed and not snapshot["tts"]:
            raise error("config_not_found", "TTS configuration unavailable")
        if purpose == "prewarm" and not prewarm_opt_in:
            raise error("disclosure_required", "prewarm opt-in required")
        if self.automatic_enabled:
            # Check revocations again inside the task/authorization transaction.
            automatic = self.automatic_request(course_id, lesson_id, actor_id=owner,
                purpose=purpose, conversation_id=conversation_id, session=sess)
            if tts_confirmed and not automatic["tts_confirmed"]:
                raise error("disclosure_revoked", "audio authorization revoked")
        snapshot.update(content_confirmed=True, tts_confirmed=bool(tts_confirmed), prewarm_opt_in=bool(prewarm_opt_in))
        return snapshot

    def record(self, sess, snapshot, owner, task_id, conversation_id=None):
        if self.automatic_enabled:
            from ..slice1.errors import Slice1Error
            target = SimpleNamespace(owner_user_id=owner, conversation_id=conversation_id,
                **{k: snapshot["scope"][k] for k in ("course_id", "lesson_id", "outline_version_id")})
            try:
                existing = self.require(sess, target, prewarm=snapshot["purpose"] == "prewarm")
                if snapshot["tts_confirmed"]:
                    self.require(sess, target, audio=True, prewarm=snapshot["purpose"] == "prewarm")
                if snapshot["prewarm_opt_in"] and not existing.prewarm_opt_in:
                    existing = None
            except Slice1Error as exc:
                if exc.code in ("disclosure_revoked", "access_denied"):
                    raise
                existing = None
            if existing:
                for audio, root_id in ((False, existing.content_root_grant_id), (True, existing.tts_root_grant_id)):
                    if audio and not snapshot["tts_confirmed"]:
                        continue
                    root = sess.get(DisclosureGrantRow, root_id)
                    if root and root.task_id != task_id:
                        self.bind_task(sess, target, task_id, audio=audio, prewarm=snapshot["purpose"] == "prewarm")
                return existing
        disclosures = self.service.disclosures
        content = snapshot["content_model"]
        root = disclosures.create(actor_id=owner, task_id=task_id, config_id=content["config_id"],
            content_category="material" if snapshot["scope"]["material_ids"] else "query",
            scope_snapshot=snapshot["scope"], config_version=content["config_version"], session=sess)
        from ..agents.model_routing import pin_role
        pin_role(sess, task_id, "teaching", content, root["grant_id"],rebind=True)
        planner = snapshot.get("planning_model") or content
        planner_root = disclosures.create(actor_id=owner, task_id=task_id, config_id=planner["config_id"],
            content_category="material" if snapshot["scope"]["material_ids"] else "query",
            scope_snapshot=snapshot["scope"], config_version=planner["config_version"], session=sess
        ) if planner["config_id"] != content["config_id"] else root
        pin_role(sess, task_id, "planning", planner, planner_root["grant_id"],rebind=True)
        voice = snapshot["tts"] if snapshot["tts_confirmed"] else None
        audio = disclosures.create(actor_id=owner, task_id=task_id, config_id=voice["config_id"],
            content_category="audio_text", scope_snapshot={k:v for k,v in snapshot["scope"].items() if k != "material_ids"},
            config_version=voice["config_version"], session=sess) if voice else None
        row = LessonAuthorizationRow(authorization_id="la_" + uuid.uuid4().hex[:12], owner_user_id=owner,
            course_id=snapshot["scope"]["course_id"], lesson_id=snapshot["scope"]["lesson_id"],
            outline_version_id=snapshot["scope"]["outline_version_id"], conversation_id=conversation_id,
            purpose=snapshot["purpose"], fingerprint=snapshot["fingerprint"], context_json=encoded(snapshot["data"]),
            content_config_id=content["config_id"], content_config_version=content["config_version"],
            tts_config_id=voice["config_id"] if voice else None, tts_config_version=voice["config_version"] if voice else None,
            content_root_grant_id=root["grant_id"], tts_root_grant_id=audio["grant_id"] if audio else None,
            prewarm_opt_in=snapshot["prewarm_opt_in"])
        sess.add(row)
        sess.flush()
        return row

    def require(self, sess, conv, *, audio=False, prewarm=False):
        if not self.enforced:
            return None
        conversation_id = getattr(conv, "conversation_id", None)
        live_conversation = sess.get(ConversationRow, conversation_id) if conversation_id else None
        snapshot = self.snapshot(sess, conv.course_id, conv.lesson_id, conv.owner_user_id,
            purpose="prewarm" if prewarm else "start", conversation_id=conversation_id if live_conversation else None)
        rows = sess.scalars(select(LessonAuthorizationRow).where(
            LessonAuthorizationRow.owner_user_id == conv.owner_user_id,
            LessonAuthorizationRow.course_id == conv.course_id, LessonAuthorizationRow.lesson_id == conv.lesson_id,
            LessonAuthorizationRow.outline_version_id == conv.outline_version_id)
            .order_by(LessonAuthorizationRow.created_at.desc())).all()
        row = next((item for item in rows if (item.prewarm_opt_in if prewarm else
            item.purpose != "prewarm" or self.automatic_enabled) and item.context_json == encoded(snapshot["data"])), None)
        if not row:
            raise error("disclosure_required", "lesson approval required")
        config = snapshot["tts" if audio else "content_model"]
        config_id = row.tts_config_id if audio else row.content_config_id
        version = row.tts_config_version if audio else row.content_config_version
        root_id = row.tts_root_grant_id if audio else row.content_root_grant_id
        if not root_id or not config or config["config_id"] != config_id or config["config_version"] != version:
            raise error("disclosure_required", "configuration approval required")
        root = sess.get(DisclosureGrantRow, root_id, populate_existing=True)
        if not root or root.owner_user_id != conv.owner_user_id:
            raise error("disclosure_required")
        self.service.disclosures.verify_or_audit(task_id=root.task_id, config_id=config_id,
            content_category="audio_text" if audio else "material" if snapshot["scope"]["material_ids"] else "query",
            actor_id=conv.owner_user_id, session=sess, expected_grant_id=root_id,
            scope_snapshot=audio_scope(conv) if audio else {"material_ids": snapshot["scope"]["material_ids"]})
        return row

    def bind_task(self, sess, conv, task_id, *, audio=False, prewarm=False):
        row = self.require(sess, conv, audio=audio, prewarm=prewarm)
        if not row:
            return
        root_id = row.tts_root_grant_id if audio else row.content_root_grant_id
        from ..agents.model_routing import TaskModelRoleRow, role_for_task, pin_role
        task = sess.get(LearningTaskRow, task_id)
        role = role_for_task(task)
        root = sess.get(DisclosureGrantRow, row.content_root_grant_id)
        planning = sess.get(TaskModelRoleRow, (root.task_id, "planning")) if root else None
        if not audio and role == "planning" and planning:
            root_id = planning.grant_id
        model_root = sess.get(DisclosureGrantRow, root_id)
        scope = audio_scope(conv) if audio else {"material_ids": json.loads(row.context_json)["context"]["material_ids"]}
        material_ids = json.loads(row.context_json)["context"]["material_ids"]
        derived = self.service.disclosures.derive(actor_id=conv.owner_user_id, task_id=task_id,
            parent_grant_id=root_id, scope_snapshot=scope,
            config_id=model_root.config_id,
            config_version=model_root.config_version,
            content_category="audio_text" if audio else "material" if material_ids else "query", session=sess)
        if not audio:
            pin_role(sess, task_id, role, {"config_id":model_root.config_id,
                "config_version":model_root.config_version}, derived["grant_id"],rebind=True)

    def check_task(self, sess, task, *, audio=False):
        if not self.enforced:
            return
        if task.kind == "lesson_first_prepare":
            row = sess.scalars(select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.task_id == task.task_id)).first()
            if not row:
                raise error("disclosure_required")
            context = json.loads(row.context_json)
            conv = SimpleNamespace(owner_user_id=row.owner_user_id, course_id=row.course_id, lesson_id=row.lesson_id,
                outline_version_id=context["outline_version_id"], conversation_id="prewarm")
            row = self.require(sess, conv, audio=audio, prewarm=True)
        else:
            conv = sess.get(ConversationRow, task.subject_id.split(":")[0], populate_existing=True)
            if not conv or conv.deleted_at or conv.owner_user_id != task.owner_user_id:
                raise error("disclosure_required")
            row = self.require(sess, conv, audio=audio)
        consent = row
        material_ids = json.loads(consent.context_json)["context"]["material_ids"]
        from ..agents.model_routing import TaskModelRoleRow, role_for_task
        pinned = sess.get(TaskModelRoleRow, (task.task_id, role_for_task(task))) if not audio else None
        config = self._config(sess, task.owner_user_id, "tts" if audio else "content_model",
            role=role_for_task(task) if not audio else "teaching")
        if not config:
            raise error("config_not_found")
        self.service.disclosures.verify_or_audit(task_id=task.task_id, config_id=config["config_id"],
            content_category="audio_text" if audio else "material" if material_ids else "query",
            actor_id=task.owner_user_id, session=sess,
            expected_root_grant_id=consent.tts_root_grant_id if audio else
                self._planning_root(sess,consent) if pinned and role_for_task(task)=="planning"
                else consent.content_root_grant_id,
            scope_snapshot=audio_scope(conv) if audio else {"material_ids": material_ids})
        if pinned and role_for_task(task) == "planning":
            return SimpleNamespace(**{column.name:getattr(consent,column.name)
                for column in LessonAuthorizationRow.__table__.columns
                if column.name not in ("content_config_id","content_config_version")},
                content_config_id=pinned.config_id,content_config_version=pinned.config_version)
        return consent

    @staticmethod
    def _planning_root(sess,consent):
        from ..agents.model_routing import TaskModelRoleRow
        root=sess.get(DisclosureGrantRow,consent.content_root_grant_id)
        planning=sess.get(TaskModelRoleRow,(root.task_id,"planning")) if root else None
        return planning.grant_id if planning else consent.content_root_grant_id
