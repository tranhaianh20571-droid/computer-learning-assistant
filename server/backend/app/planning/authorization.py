"""Planning entry authorization; workers only consume the pinned grant."""
from __future__ import annotations

import json
from sqlalchemy import select

from ..slice1.disclosure_service import DisclosureService
from ..slice1.errors import Slice1Error
from ..slice1.models import DisclosureGrantRow, ServiceConfigRow
from ..tasks.service import LearningTaskRow
from .errors import error
from .models import PlanningTaskStateRow
from ..materials.models import MaterialRow

PLANNING_KINDS = {"route_generate", "outline_generate", "planning_revise", "planning_dialogue"}


def dialogue_state(session, task_id, owner):
    from types import SimpleNamespace
    from .feature_models import PlanningDialogueRow
    from .models import PlanningDraftRow
    turn = session.get(PlanningDialogueRow, task_id)
    draft = session.get(PlanningDraftRow, turn.draft_id) if turn else None
    if not turn or not draft or turn.owner_user_id != owner or draft.owner_user_id != owner:
        raise error("task_not_found", status=404)
    if draft.revision != turn.base_revision:
        raise error("DRAFT_REVISION_MISMATCH", status=409)
    source = session.scalar(select(PlanningTaskStateRow).where(
        PlanningTaskStateRow.draft_id == draft.draft_id,
        PlanningTaskStateRow.owner_user_id == owner).order_by(PlanningTaskStateRow.updated_at.desc()).limit(1))
    return SimpleNamespace(owner_user_id=owner, target_id=draft.target_id,
        target_kind="route" if draft.draft_kind == "route" else "course",
        abandoned_at=source.abandoned_at if source else None,
        material_ids_json=source.material_ids_json if source else "[]")


def task_grants(sess, task_id, owner):
    return list(sess.scalars(select(DisclosureGrantRow).where(
        DisclosureGrantRow.task_id == task_id,
        DisclosureGrantRow.owner_user_id == owner,
        DisclosureGrantRow.service_kind == "content_model",
    ).order_by(DisclosureGrantRow.granted_at.desc(), DisclosureGrantRow.grant_id.desc())))


def check_revoked_chain(sess, grant):
    seen = set()
    while grant is not None:
        sess.refresh(grant, with_for_update=True)
        if grant.grant_id in seen or len(seen) >= 16:
            raise error("disclosure_required", "invalid planning grant chain", status=409)
        seen.add(grant.grant_id)
        if grant.revoked_at:
            raise error("disclosure_revoked", "planning authorization revoked", status=409)
        if not grant.parent_grant_id:
            return
        grant = sess.get(DisclosureGrantRow, grant.parent_grant_id, populate_existing=True)
        if grant is None:
            raise error("disclosure_required", "planning grant ancestor missing", status=409)


def bound_planning_config(configs, owner, task_id):
    """Return (managed, view); never fall back to a newer config or env key."""
    with configs.db.session() as sess:
        task = sess.get(LearningTaskRow, task_id)
        if task is None or task.kind not in PLANNING_KINDS:
            return False, None
        state = dialogue_state(sess, task_id, owner) if task.kind == "planning_dialogue" else sess.get(PlanningTaskStateRow, task_id)
        if not state or task.owner_user_id != owner or state.owner_user_id != owner:
            raise error("task_not_found", status=404)
        grants = task_grants(sess, task_id, owner)
        if not grants:
            raise error("disclosure_required", "planning execution not prepared", status=409)
        grant = grants[0]
        config = sess.get(ServiceConfigRow, grant.config_id)
        if (not config or not config.is_active or config.kind != "content_model"
                or config.owner_scope != "user" or config.owner_user_id != owner
                or config.config_version != grant.config_version
                or config.endpoint != grant.service_endpoint):
            raise error("MODEL_CREDENTIAL_INVALID", "planning configuration changed", status=409)
        scope = {"material_ids": sorted(set(json.loads(state.material_ids_json or "[]")))}
        category = "material" if scope["material_ids"] else "query"
        DisclosureService(configs.db).verify(task_id=task_id, config_id=config.config_id,
            actor_id=owner, content_category=category, scope_snapshot=scope,
            expected_grant_id=grant.grant_id, session=sess)
        config_id, version, endpoint, grant_id = (config.config_id, grant.config_version,
            grant.service_endpoint, grant.grant_id)
    view = configs.get(config_id, actor_id=owner, is_admin=False)
    if view["config_version"] != version or view["endpoint"] != endpoint or not view["is_active"]:
        raise error("MODEL_CREDENTIAL_INVALID", "planning configuration changed", status=409)
    return True, {**view, "_planning_grant_id": grant_id}


class PlanningAuthorization:
    def __init__(self, configs, disclosures):
        self.configs, self.disclosures = configs, disclosures

    def prepare(self, sess, task, state, owner, *, allow_refresh):
        """Caller holds target -> task -> planning locks before enabling execution."""
        if (task.kind not in PLANNING_KINDS or task.owner_user_id != owner
                or state.owner_user_id != owner):
            raise error("task_not_found", status=404)
        if state.abandoned_at or task.cancel_requested or task.status == "cancelled":
            raise error("TASK_CANCELLED", status=409)
        materials = sorted(set(json.loads(state.material_ids_json or "[]")))
        if materials:
            owned = set(sess.scalars(select(MaterialRow.material_id).where(
                MaterialRow.owner_user_id == owner, MaterialRow.material_id.in_(materials),
                MaterialRow.deleted_at.is_(None))))
            if owned != set(materials):
                raise error("access_denied", "planning material ownership mismatch", status=403)
        scope = {"material_ids": materials}
        category = "material" if materials else "query"
        existing = task_grants(sess, task.task_id, owner)
        if existing:
            previous = existing[0]
            live = sess.scalar(select(ServiceConfigRow).where(
                ServiceConfigRow.config_id == previous.config_id).with_for_update()
                .execution_options(populate_existing=True))
            if live and live.is_active and live.config_version == previous.config_version:
                check_revoked_chain(sess, previous)
            try:
                view = self.disclosures.verify(task_id=task.task_id, config_id=previous.config_id,
                    actor_id=owner, content_category=category, scope_snapshot=scope,
                    expected_grant_id=previous.grant_id, session=sess)
                # An active grant stays pinned even when a newer config exists.
                return view
            except Slice1Error as exc:
                if not allow_refresh or exc.code not in (
                        "disclosure_expired", "disclosure_required", "disclosure_revoked", "config_not_found"):
                    raise
        elif not allow_refresh:
            raise error("disclosure_required", "explicit planning resume required", status=409)

        from ..agents.model_routing import role_config
        chosen = role_config(self.configs, owner, "planning")
        config = sess.get(ServiceConfigRow, chosen["config_id"], with_for_update=True) if chosen else None
        if config is None:
            raise error("config_not_found", "content model configuration required", status=409)

        # A new repair task must not escape a revoked authorization on its source target.
        related_ids = select(PlanningTaskStateRow.task_id).where(
            PlanningTaskStateRow.owner_user_id == owner,
            PlanningTaskStateRow.target_kind == state.target_kind,
            PlanningTaskStateRow.target_id == state.target_id,
        ) if state.target_id else select(PlanningTaskStateRow.task_id).where(
            PlanningTaskStateRow.task_id == task.task_id)
        related = list(sess.scalars(select(DisclosureGrantRow).where(
            DisclosureGrantRow.task_id.in_(related_ids), DisclosureGrantRow.owner_user_id == owner,
            DisclosureGrantRow.config_id == config.config_id,
            DisclosureGrantRow.config_version == config.config_version,
            DisclosureGrantRow.service_kind == "content_model",
        ).order_by(DisclosureGrantRow.granted_at.desc(), DisclosureGrantRow.grant_id.desc())))
        for grant in related:
            check_revoked_chain(sess, grant)

        # Use a valid source grant for a repair subset; an explicit operation may
        # establish a fresh exact snapshot after expiry or a configuration update.
        for parent in related:
            if parent.task_id == task.task_id or parent.content_category != category:
                continue
            try:
                self.disclosures.verify(task_id=parent.task_id, config_id=config.config_id,
                    actor_id=owner, content_category=category, scope_snapshot=scope,
                    expected_grant_id=parent.grant_id, session=sess)
            except Slice1Error as exc:
                if exc.code not in ("disclosure_expired", "disclosure_required", "config_not_found"):
                    raise
                continue
            return self.disclosures.derive(parent_grant_id=parent.grant_id, actor_id=owner,
                task_id=task.task_id, config_id=config.config_id, config_version=config.config_version,
                content_category=category, scope_snapshot=scope, session=sess)
        return self.disclosures.create(actor_id=owner, task_id=task.task_id,
            config_id=config.config_id, config_version=config.config_version,
            content_category=category, scope_snapshot=scope, session=sess)
