"""Checkpointed shadow-bank rebuild; only publish a complete current source set."""
import asyncio
import json
from datetime import timedelta
from sqlalchemy import select, or_

from .models import MemorySettingsRow, MemorySourceRow, MemorySpaceRow
from .personal import MemoryMigration, PersonalModels
from .models import now
from ..slice1.errors import error


def migration_tick(service):
    db, adapter = service.db, service.adapter
    if not hasattr(adapter, "client_for_bank"):
        return False
    with db.session() as session:
        orphan = session.scalar(select(MemoryMigration).where(
            MemoryMigration.status.in_(("stale", "failed")), MemoryMigration.cleanup_done.is_(False),
            MemoryMigration.cleanup_attempts < 3).with_for_update(skip_locked=True).limit(1))
        if orphan:
            current = session.get(MemorySpaceRow, orphan.space_id)
            cleanup_bank = (orphan.source_bank if current and current.bank_id == orphan.target_bank
                            else orphan.target_bank)
            orphan.cleanup_attempts += 1
            identity = orphan.migration_id
            session.commit()
            orphan = session.get(MemoryMigration, identity, with_for_update=True, populate_existing=True)
            try:
                asyncio.run(adapter.delete_bank(cleanup_bank))
                orphan.cleanup_done = True
            except Exception:
                pass
            return True
        job = session.scalar(select(MemoryMigration).where(
            MemoryMigration.status.in_(("pending", "replaying", "refreshing", "cleanup")),
            MemoryMigration.attempts < 3,
            or_(MemoryMigration.lease_until.is_(None), MemoryMigration.lease_until < now())
            ).with_for_update(skip_locked=True).limit(1))
        if not job:
            return False
        try:
            space = session.get(MemorySpaceRow, job.space_id, with_for_update=True)
            setting = session.get(MemorySettingsRow, job.owner_user_id, with_for_update=True)
            if (not space or space.owner_user_id != job.owner_user_id or space.status != "active"
                    or not setting or not setting.enabled or space.epoch != job.epoch):
                raise error("disclosure_revoked")
            PersonalModels(db).check(job.owner_user_id, job.revision)
            if job.status == "cleanup":
                # New evidence after publication must not strand the retired bank.
                if space.bank_id != job.target_bank:
                    raise error("state_invalid")
                asyncio.run(adapter.delete_bank(job.source_bank))
                job.status, job.attempts = "completed", 0
                return True
            refs = json.loads(job.source_refs_json)
            sources = list(session.scalars(select(MemorySourceRow).where(
                MemorySourceRow.owner_user_id == job.owner_user_id,
                MemorySourceRow.scope_key == space.scope_key,
                    MemorySourceRow.status.in_(("completed", "compacted"))).order_by(MemorySourceRow.source_id)))
            if [[s.source_id, s.revision, s.tag] for s in sources] != refs:
                raise error("state_invalid", "重建期间来源发生变化，请重新重建。")
            if session.scalar(select(MemorySourceRow.source_id).where(
                    MemorySourceRow.owner_user_id == job.owner_user_id,
                    MemorySourceRow.scope_key == space.scope_key,
                    MemorySourceRow.status.in_(("pending", "retained", "forgetting", "forget_failed")))):
                return True
            job.attempts += 1
            job.lease_until = now() + timedelta(seconds=240)
            identity = job.migration_id
            session.commit()
            job = session.get(MemoryMigration, identity, with_for_update=True, populate_existing=True)
            space = session.get(MemorySpaceRow, job.space_id, with_for_update=True, populate_existing=True)
            setting = session.get(MemorySettingsRow, job.owner_user_id, with_for_update=True, populate_existing=True)
            if (not space or space.status != "active" or space.epoch != job.epoch
                    or not setting or not setting.enabled):
                raise error("disclosure_revoked")
            sources = list(session.scalars(select(MemorySourceRow).where(
                MemorySourceRow.owner_user_id == job.owner_user_id,
                    MemorySourceRow.scope_key == space.scope_key, MemorySourceRow.status.in_(("completed", "compacted")))
                .order_by(MemorySourceRow.source_id).with_for_update()))
            if [[s.source_id, s.revision, s.tag] for s in sources] != refs:
                raise error("state_invalid")
            if job.completed < len(sources):
                source = sources[job.completed]
                if source.status == "compacted":
                    if not callable(getattr(adapter, "transfer_compacted", None)):
                        raise error("capability_test_failed")
                    # One bundle carries the live native observations and their withdrawal graph.
                    # Repeated import is idempotent; no source text is reconstructed.
                    if not any(s.status == "compacted" for s in sources[:job.completed]):
                        asyncio.run(adapter.transfer_compacted(job.source_bank, job.target_bank))
                else:
                    asyncio.run(adapter.retain(job.target_bank, source.document_id,
                        source.payload_json, source.tag, source.created_at))
                job.completed += 1
                job.status = "replaying"
            else:
                tags = sorted({s.tag for s in sources})
                if job.tag_index < len(tags):
                    if job.operation_id:
                        result = asyncio.run(adapter.operation(job.target_bank, job.operation_id))
                        data = result if isinstance(result, dict) else result.model_dump(mode="json") if result else {}
                        if data.get("status") == "failed":
                            raise error("capability_test_failed")
                        if data.get("status") == "completed":
                            job.operation_id = None
                            job.tag_index += 1
                    else:
                        result = asyncio.run(adapter.refresh(job.target_bank, tags[job.tag_index]))
                        data = result if isinstance(result, dict) else result.model_dump(mode="json") if result else {}
                        job.operation_id = data.get("operation_id")
                        if not job.operation_id:
                            job.tag_index += 1
                    job.status = "refreshing"
                else:
                    if space.bank_id != job.source_bank:
                        raise error("state_invalid")
                    space.bank_id = job.target_bank
                    space.epoch += 1
                    job.epoch = space.epoch
                    for source in sources:
                        source.epoch = space.epoch
                    job.status = "cleanup"
                    if service.audit:
                        service.audit.append("config.changed", actor_id=job.owner_user_id,
                            object_type="memory", object_id=space.space_id, result="rebuilt", session=session)
            job.attempts = 0
        except Exception as exc:
            if getattr(exc, "code", "") in ("disclosure_revoked", "state_invalid"):
                job.status = "stale"
            elif job.attempts >= 3:
                job.status = "failed"
        finally:
            job.lease_until = None
        return True
