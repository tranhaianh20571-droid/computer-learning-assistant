"""Disclosure grants bind real task owners and immutable outbound scope."""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from backend.logging_audit.db import Database
from backend.logging_audit.models import install_audit_triggers
from backend.logging_audit.repositories import PgAuditLog, PgTaskEventStore
from backend.slice1.config_service import ConfigService
from backend.slice1.disclosure_service import DisclosureService
from backend.slice1.errors import Slice1Error
from backend.slice1.models import DisclosureGrantRow, utcnow
from backend.tasks.service import TaskService

FAKE_KEY = "sk-test-fake-key-000000000000"
MATERIAL_SCOPE = {"material_ids": ["synthetic-material-1"]}
QUERY_SCOPE = {"query_id": "synthetic-query-1"}


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    yield db
    db.dispose()


@pytest.fixture()
def services(database: Database):
    audit = PgAuditLog(database)
    return {
        "configs": ConfigService(database, encryption_key="k"),
        "disclosures": DisclosureService(database, audit),
        "tasks": TaskService(database, PgTaskEventStore(database)),
        "audit": audit,
    }


def _setup_config(configs: ConfigService, owner: str) -> dict:
    return configs.create(
        actor_id=owner, is_admin=False, kind="content_model", protocol="openai",
        endpoint="https://api.example.test/v1", model_name="m",
        credentials={"api_key": FAKE_KEY},
    )


def _setup(services):
    owner = f"user_{uuid.uuid4().hex[:8]}"
    cfg = _setup_config(services["configs"], owner)
    task = services["tasks"].create_task(
        owner_user_id=owner, kind="generic", subject_id="synthetic-material-1",
        idempotency_key=f"synthetic-disclosure-{uuid.uuid4().hex}",
        model_config_version=f"{cfg['config_id']}:{cfg['config_version']}",
    )["task_id"]
    return owner, cfg, task


def _grant(services, owner, cfg, task, *, category="material", scope=None, **kwargs):
    return services["disclosures"].create(
        actor_id=owner, task_id=task, config_id=cfg["config_id"],
        content_category=category,
        scope_snapshot=MATERIAL_SCOPE if scope is None else scope, **kwargs,
    )


def _verify(services, cfg, task, *, category="material", scope=None, **kwargs):
    return services["disclosures"].verify(
        task_id=task, config_id=cfg["config_id"], content_category=category,
        scope_snapshot=MATERIAL_SCOPE if scope is None else scope, **kwargs,
    )


class TestDisclosureGrants:
    def test_worker_without_grant_rejected(self, services):
        owner, cfg, task = _setup(services)
        with pytest.raises(Slice1Error) as exc:
            _verify(services, cfg, task)
        assert exc.value.code == "disclosure_required"

    def test_grant_allows_covered_category(self, services):
        owner, cfg, task = _setup(services)
        _grant(services, owner, cfg, task)
        assert _verify(services, cfg, task)["config_version"] == 1

    def test_category_must_match(self, services):
        owner, cfg, task = _setup(services)
        _grant(services, owner, cfg, task)
        with pytest.raises(Slice1Error) as exc:
            _verify(services, cfg, task, category="query", scope=QUERY_SCOPE)
        assert exc.value.code == "disclosure_required"

    def test_revoke_stops_worker(self, services):
        owner, cfg, task = _setup(services)
        grant = _grant(services, owner, cfg, task, category="query", scope=QUERY_SCOPE)
        services["disclosures"].revoke(grant["grant_id"], actor_id=owner)
        with pytest.raises(Slice1Error) as exc:
            _verify(services, cfg, task, category="query", scope=QUERY_SCOPE)
        assert exc.value.code == "disclosure_revoked"

    def test_expired_grant_rejected(self, services, database):
        owner, cfg, task = _setup(services)
        grant = _grant(services, owner, cfg, task, ttl_seconds=1)
        with database.session() as sess:
            sess.get(DisclosureGrantRow, grant["grant_id"]).expires_at = utcnow() - timedelta(seconds=1)
        with pytest.raises(Slice1Error) as exc:
            _verify(services, cfg, task)
        assert exc.value.code == "disclosure_expired"

    def test_config_endpoint_change_invalidates_grant(self, services):
        owner, cfg, task = _setup(services)
        _grant(services, owner, cfg, task)
        services["configs"].update(
            cfg["config_id"], actor_id=owner, is_admin=False, endpoint="https://api2.example.test/v1",
        )
        with pytest.raises(Slice1Error) as exc:
            _verify(services, cfg, task)
        assert exc.value.code == "disclosure_revoked"

    def test_deactivate_config_blocks_worker(self, services):
        owner, cfg, task = _setup(services)
        _grant(services, owner, cfg, task)
        services["configs"].deactivate(cfg["config_id"], actor_id=owner, is_admin=False)
        with pytest.raises(Slice1Error) as exc:
            _verify(services, cfg, task)
        assert exc.value.code == "config_not_found"

    def test_cross_account_grant_hidden(self, services):
        owner, cfg, task = _setup(services)
        _grant(services, owner, cfg, task)
        assert services["disclosures"].list_for_task(task, actor_id="synthetic-intruder") == []
        with pytest.raises(Slice1Error) as exc:
            _verify(services, cfg, task, actor_id="synthetic-intruder")
        assert exc.value.code == "access_denied"

    def test_audit_records_confirmation_and_revocation(self, services):
        owner, cfg, task = _setup(services)
        grant = _grant(services, owner, cfg, task)
        services["disclosures"].revoke(grant["grant_id"], actor_id=owner)
        records = [r for r in services["audit"].list_all_for_test() if r.object_id == grant["grant_id"]]
        assert [r.event for r in records] == ["external.disclosure.confirmed"] * 2
        assert {r.result for r in records} == {"granted", "revoked"}

    def test_nonexistent_task_cannot_create_grant(self, services):
        owner, cfg, task = _setup(services)
        with pytest.raises(Slice1Error) as exc:
            _grant(services, owner, cfg, f"task_missing_{uuid.uuid4().hex[:8]}")
        assert exc.value.code == "disclosure_required"

    def test_task_owner_cannot_use_foreign_user_config(self, services):
        owner, cfg, task = _setup(services)
        foreign = _setup_config(services["configs"], f"user_{uuid.uuid4().hex[:8]}")
        with pytest.raises(Slice1Error) as exc:
            _grant(services, owner, foreign, task)
        assert exc.value.code == "access_denied"
        with pytest.raises(Slice1Error) as exc:
            _verify(services, foreign, task)
        assert exc.value.code == "access_denied"

    def test_foreign_actor_cannot_grant_for_known_task(self, services):
        owner, cfg, task = _setup(services)
        with pytest.raises(Slice1Error) as exc:
            _grant(services, "synthetic-intruder", cfg, task)
        assert exc.value.code == "access_denied"

    @pytest.mark.parametrize("actual", [
        {"material_ids": ["synthetic-material-2"]},
        {"material_ids": ["synthetic-material-1", "synthetic-material-2"]},
        {"material_ids": ["synthetic-material-1"], "extra": "synthetic-private-data"},
    ])
    def test_scope_expansion_requires_new_confirmation(self, services, actual):
        owner, cfg, task = _setup(services)
        _grant(services, owner, cfg, task)
        with pytest.raises(Slice1Error) as exc:
            _verify(services, cfg, task, scope=actual)
        assert exc.value.code == "disclosure_required"

    def test_missing_actual_scope_is_not_implicit_authorization(self, services):
        owner, cfg, task = _setup(services)
        _grant(services, owner, cfg, task)
        with pytest.raises(Slice1Error) as exc:
            services["disclosures"].verify(
                task_id=task, config_id=cfg["config_id"], content_category="material",
            )
        assert exc.value.code == "disclosure_required"

    def test_approved_material_subset_is_covered(self, services):
        owner, cfg, task = _setup(services)
        _grant(services, owner, cfg, task, scope={"material_ids": ["synthetic-material-1", "synthetic-material-2"]})
        assert _verify(services, cfg, task)["task_id"] == task

    def test_scalar_scope_types_are_not_coerced(self, services):
        owner, cfg, task = _setup(services)
        _grant(services, owner, cfg, task, scope={"step_no": 1})
        with pytest.raises(Slice1Error) as exc:
            _verify(services, cfg, task, scope={"step_no": True})
        assert exc.value.code == "disclosure_required"
