"""Bounded, checkpointed maintenance; explicit withdrawal always takes priority."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json

from sqlalchemy import select, or_, exists, func

from .models import (MemoryMaintenanceJob, MemoryMaintenancePolicy, MemorySettingsRow,
                     MemorySourceRow, MemorySpaceRow, now)

PROTOCOL = "learning-lifecycle-1"


def policy_view(db, owner):
    with db.session() as session:
        row = session.get(MemoryMaintenancePolicy, owner)
        jobs = list(session.execute(select(MemoryMaintenanceJob.status, func.count()).where(
            MemoryMaintenanceJob.owner_user_id == owner).group_by(MemoryMaintenanceJob.status)))
        last_error = session.scalar(select(MemoryMaintenanceJob.error_code).where(
            MemoryMaintenanceJob.owner_user_id == owner, MemoryMaintenanceJob.error_code != "")
            .order_by(MemoryMaintenanceJob.checked_at.desc()).limit(1))
        return {"mode": row.mode if row else "preview", "revision": row.revision if row else 0,
                "minimum_age_days": row.minimum_age_days if row else 90,
                "jobs": dict(jobs), "last_error": last_error}


def configure(service, owner, mode, revision):
    from ..auth.models import UserRow
    from ..planning.errors import error
    if mode not in ("off", "preview", "automatic"):
        raise error("state_invalid", status=409)
    with service.db.session() as session:
        user = session.get(UserRow, owner, with_for_update=True)
        row = session.get(MemoryMaintenancePolicy, owner, with_for_update=True)
        if not user or user.status != "approved" or (row.revision if row else 0) != revision:
            raise error("state_invalid", status=409)
        if row is None:
            row = MemoryMaintenancePolicy(owner_user_id=owner, revision=0)
            session.add(row)
        row.mode, row.revision = mode, row.revision + 1
        if service.audit:
            service.audit.append("config.changed", actor_id=owner, object_type="memory",
                                 object_id=owner, result="maintenance_" + mode, session=session)
    return policy_view(service.db, owner)


def revoke_jobs(session, owner, *, source_id=None, course=None):
    jobs = select(MemoryMaintenanceJob).where(MemoryMaintenanceJob.owner_user_id == owner)
    if source_id:
        jobs = jobs.where(MemoryMaintenanceJob.source_id == source_id)
    if course:
        jobs = jobs.join(MemorySpaceRow, MemoryMaintenanceJob.space_id == MemorySpaceRow.space_id).where(
            MemorySpaceRow.scope_key == "course:" + course)
    for job in session.scalars(jobs.with_for_update()):
        job.native_committed = (job.native_committed or job.status == "committing"
                                or job.bundle_json != "{}")
        job.status, job.bundle_json = "revoked", "{}"


def retract_compacted(service, session, source, space):
    dependent = session.scalar(select(MemoryMaintenanceJob.job_id).where(
        MemoryMaintenanceJob.space_id == space.space_id,
        or_(MemoryMaintenanceJob.native_committed.is_(True),
            MemoryMaintenanceJob.status == "committing")).limit(1))
    if dependent:
        if not callable(getattr(service.adapter, "lifecycle", None)):
            raise RuntimeError("lifecycle_required")
        asyncio.run(service.adapter.lifecycle(space.bank_id, "retract", {"document_id": source.document_id}))


def schedule(service):
    from ..auth.models import UserRow
    from .personal import MemoryMigration
    with service.db.session() as session:
        midnight = now().replace(hour=0, minute=0, second=0, microsecond=0)
        revision = func.coalesce(select(MemoryMaintenancePolicy.revision).where(
            MemoryMaintenancePolicy.owner_user_id == MemorySourceRow.owner_user_id)
            .correlate(MemorySourceRow).scalar_subquery(), 0)
        blocked = exists(select(MemoryMaintenanceJob.job_id).where(
            MemoryMaintenanceJob.source_id == MemorySourceRow.source_id,
            or_(MemoryMaintenanceJob.status.in_(("pending", "retry", "committing", "completed")),
                (MemoryMaintenanceJob.checked_at >= midnight)
                & (MemoryMaintenanceJob.policy_revision == revision))))
        failures = select(func.coalesce(func.sum(MemoryMaintenanceJob.attempts), 0)).where(
            MemoryMaintenanceJob.source_id == MemorySourceRow.source_id,
            MemoryMaintenanceJob.policy_revision == revision,
            MemoryMaintenanceJob.source_revision == MemorySourceRow.revision,
            MemoryMaintenanceJob.error_code != "").scalar_subquery()
        disabled = exists(select(MemoryMaintenancePolicy.owner_user_id).where(
            MemoryMaintenancePolicy.owner_user_id == MemorySourceRow.owner_user_id,
            MemoryMaintenancePolicy.mode == "off"))
        migrating = exists(select(MemoryMigration.migration_id).where(
            MemoryMigration.space_id == MemorySpaceRow.space_id,
            MemoryMigration.status.in_(("pending", "replaying", "refreshing", "cleanup"))))
        daily_jobs = select(func.count()).select_from(MemoryMaintenanceJob).where(
            MemoryMaintenanceJob.space_id == MemorySpaceRow.space_id,
            or_(MemoryMaintenanceJob.checked_at >= midnight,
                MemoryMaintenanceJob.status.in_(("pending", "retry", "committing"))))
        daily_jobs = daily_jobs.correlate(MemorySpaceRow).scalar_subquery()
        candidates = session.execute(select(MemorySourceRow, MemorySpaceRow).join(
            MemorySpaceRow, (MemorySpaceRow.owner_user_id == MemorySourceRow.owner_user_id)
            & (MemorySpaceRow.scope_key == MemorySourceRow.scope_key)).join(
            MemorySettingsRow, MemorySettingsRow.owner_user_id == MemorySourceRow.owner_user_id).join(
            UserRow, UserRow.user_id == MemorySourceRow.owner_user_id).where(
                MemorySourceRow.status == "completed", MemorySpaceRow.status == "active",
                MemorySettingsRow.enabled.is_(True), UserRow.status == "approved",
                MemorySourceRow.created_at < now() - timedelta(days=90),
                MemorySourceRow.settings_revision == MemorySettingsRow.revision,
                MemorySourceRow.epoch == MemorySpaceRow.epoch,
                ~blocked, failures < 3, ~disabled, ~migrating, daily_jobs < 10)
            .order_by(MemorySourceRow.created_at).limit(100)).all()
        per_space = dict(session.execute(select(MemoryMaintenanceJob.space_id, func.count()).where(
            or_(MemoryMaintenanceJob.checked_at >= midnight,
                MemoryMaintenanceJob.status.in_(("pending", "retry", "committing"))))
            .group_by(MemoryMaintenanceJob.space_id)).all())
        for source, space in candidates:
            policy = session.get(MemoryMaintenancePolicy, source.owner_user_id)
            if policy and policy.mode == "off":
                continue
            if source.settings_revision != session.get(MemorySettingsRow, source.owner_user_id).revision:
                continue
            if source.epoch != space.epoch or per_space.get(space.space_id, 0) >= 10:
                continue
            prior = list(session.scalars(select(MemoryMaintenanceJob).where(
                MemoryMaintenanceJob.source_id == source.source_id,
                MemoryMaintenanceJob.policy_revision == (policy.revision if policy else 0),
                MemoryMaintenanceJob.source_revision == source.revision)))
            if any(j.status in ("pending", "retry", "committing", "completed") for j in prior):
                continue
            if sum(j.attempts for j in prior if j.error_code) >= 3 or any(
                    j.checked_at.date() == now().date() for j in prior):
                continue
            migration = session.scalar(select(MemoryMigration.migration_id).where(
                MemoryMigration.space_id == space.space_id,
                MemoryMigration.status.in_(("pending", "replaying", "refreshing", "cleanup"))))
            if migration:
                continue
            ident = hashlib.sha256(
                f"{source.source_id}:{source.revision}:{policy.revision if policy else 0}:{now().date()}".encode()).hexdigest()
            session.add(MemoryMaintenanceJob(job_id=ident, owner_user_id=source.owner_user_id,
                space_id=space.space_id, source_id=source.source_id, source_revision=source.revision,
                epoch=space.epoch, policy_revision=policy.revision if policy else 0))
            per_space[space.space_id] = per_space.get(space.space_id, 0) + 1


def maintenance_tick(service):
    if not callable(getattr(service.adapter, "lifecycle", None)):
        return
    schedule(service)
    with service.db.session() as session:
        job = session.scalar(select(MemoryMaintenanceJob).where(
            MemoryMaintenanceJob.status.in_(("pending", "retry", "committing")),
            or_(MemoryMaintenanceJob.lease_until.is_(None), MemoryMaintenanceJob.lease_until < now()),
            or_(MemoryMaintenanceJob.attempts < 3,
                MemoryMaintenanceJob.status == "committing")).order_by(MemoryMaintenanceJob.checked_at)
            .with_for_update(skip_locked=True).limit(1))
        if not job:
            return
        source = session.get(MemorySourceRow, job.source_id)
        space = session.get(MemorySpaceRow, job.space_id)
        setting = session.get(MemorySettingsRow, job.owner_user_id)
        policy = session.get(MemoryMaintenancePolicy, job.owner_user_id)
        mode = policy.mode if policy else "preview"
        if (not source or not space or space.status != "active" or mode == "off"
                or not setting or not setting.enabled or source.status != "completed"
                or source.revision != job.source_revision or space.epoch != job.epoch
                or (policy.revision if policy else 0) != job.policy_revision):
            job.status, job.bundle_json = "stale", "{}"
            return
        job.attempts += 1
        job.lease_until = now() + timedelta(minutes=4)
        ident, bank, document, tag = job.job_id, space.bank_id, source.document_id, source.tag
        prepared = json.loads(job.bundle_json)
    try:
        if not prepared:
            prepared = asyncio.run(service.adapter.lifecycle(bank, "prepare",
                {"plan_id": ident, "document_id": document, "tag": tag}))
        if prepared.get("protocol") != PROTOCOL:
            raise RuntimeError("lifecycle_required")
        with service.db.session() as session:
            from ..auth.models import UserRow
            from .personal import MemoryMigration
            user = session.get(UserRow, job.owner_user_id, with_for_update=True)
            setting = session.get(MemorySettingsRow, job.owner_user_id, with_for_update=True)
            space = session.get(MemorySpaceRow, job.space_id, with_for_update=True)
            source = session.get(MemorySourceRow, job.source_id, with_for_update=True)
            policy = session.get(MemoryMaintenancePolicy, job.owner_user_id, with_for_update=True)
            current = session.get(MemoryMaintenanceJob, ident, with_for_update=True)
            migration = session.scalar(select(MemoryMigration.migration_id).where(
                MemoryMigration.space_id == job.space_id,
                MemoryMigration.status.in_(("pending", "replaying", "refreshing", "cleanup"))))
            if (not user or user.status != "approved" or not setting or not setting.enabled
                    or current.status not in ("pending", "retry", "committing")
                    or not space or space.status != "active" or space.bank_id != bank
                    or space.epoch != job.epoch or not source or source.status != "completed"
                    or source.revision != job.source_revision or migration
                    or (policy.revision if policy else 0) != job.policy_revision):
                current.status, current.bundle_json = "stale", "{}"
                return
            if not prepared.get("allowed"):
                current.status = "kept"
            elif mode != "automatic":
                current.status = "preview"
            else:
                current.bundle_json = json.dumps(prepared)
                current.status = "committing"
                if service.audit:
                    service.audit.append("resource.delete.requested", actor_id=job.owner_user_id,
                        object_type="memory", object_id=source.source_id,
                        result="maintenance_commit_authorized", session=session)
                session.commit()
                current = session.get(MemoryMaintenanceJob, ident, with_for_update=True, populate_existing=True)
                space = session.get(MemorySpaceRow, job.space_id, with_for_update=True, populate_existing=True)
                setting = session.get(MemorySettingsRow, job.owner_user_id, with_for_update=True, populate_existing=True)
                policy = session.get(MemoryMaintenancePolicy, job.owner_user_id, with_for_update=True, populate_existing=True)
                source = session.get(MemorySourceRow, job.source_id, with_for_update=True, populate_existing=True)
                user = session.get(UserRow, job.owner_user_id, with_for_update=True, populate_existing=True)
                migration = session.scalar(select(MemoryMigration.migration_id).where(
                    MemoryMigration.space_id == job.space_id,
                    MemoryMigration.status.in_(("pending", "replaying", "refreshing", "cleanup"))))
                if (not current or not source or not space or not setting
                        or current.status != "committing" or source.status != "completed"
                        or source.revision != job.source_revision or space.status != "active"
                        or space.epoch != job.epoch or space.bank_id != bank or not setting.enabled
                        or not user or user.status != "approved" or migration
                        or not policy or policy.mode != "automatic" or policy.revision != job.policy_revision):
                    current.status, current.bundle_json = "stale", "{}"
                    return
                response = asyncio.run(service.adapter.lifecycle(bank,
                    "status" if current.attempts > 3 else "commit",
                    {"plan_id": ident, "fingerprint": prepared["fingerprint"]}))
                if response.get("committed") is not True or response.get("protocol") != PROTOCOL:
                    raise RuntimeError("commit_unconfirmed")
                source.status, source.payload_json = "compacted", "{}"
                source.revision += 1
                source.updated_at = now()
                current.status, current.bundle_json = "completed", "{}"
                current.native_committed = True
                if service.audit:
                    service.audit.append("resource.delete.requested", actor_id=job.owner_user_id,
                        object_type="memory", object_id=source.source_id,
                        result="maintenance_compacted", session=session)
            current.lease_until = None
            current.checked_at = now()
    except Exception as exc:
        from ..logging_audit.audit import AuditWriteError
        with service.db.session() as session:
            row = session.get(MemoryMaintenanceJob, ident, with_for_update=True)
            if row.status not in ("revoked", "stale", "completed"):
                # A saved receipt may represent a committed native transaction.
                # Keep it reconcilable across the final attempt and process restarts.
                row.status = ("committing" if row.bundle_json != "{}" and row.attempts <= 3 else
                              "retry" if row.attempts < 3 else "failed")
                row.error_code = "AUDIT_WRITE_FAILED" if isinstance(exc, AuditWriteError) else "maintenance_unconfirmed"
                row.lease_until = now() + timedelta(hours=1)
