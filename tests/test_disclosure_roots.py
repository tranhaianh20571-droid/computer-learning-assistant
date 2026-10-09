"""Offline root consent, narrow derivation, and atomic audit contracts."""
from datetime import timedelta

import pytest
from sqlalchemy import event, func, select

from backend.agents.task_binding import AgentBudgetRow
from backend.logging_audit.audit import AuditWriteError
from backend.logging_audit.db import Database
from backend.logging_audit.models import AuditLogRow, TaskEventRow, TaskStateRow
from backend.logging_audit.repositories import PgAuditLog, PgTaskEventStore
from backend.slice1.disclosure_service import DisclosureService
from backend.slice1.errors import Slice1Error
from backend.slice1.models import DisclosureGrantRow, ServiceConfigRow
from backend.tasks.service import LearningTaskRow, TaskAttemptRow, TaskService, utcnow


@pytest.fixture
def consent():
    db = Database("sqlite+pysqlite:///:memory:")
    event.listen(db.engine, "connect", lambda connection, _: connection.create_function("char_length", 1, len))
    for model in (LearningTaskRow, AgentBudgetRow, TaskAttemptRow, TaskStateRow,
                  TaskEventRow, AuditLogRow, ServiceConfigRow, DisclosureGrantRow):
        model.__table__.create(db.engine)
    tasks = TaskService(db, PgTaskEventStore(db))
    with db.session() as sess:
        sess.add(ServiceConfigRow(config_id="synthetic_cfg", owner_user_id="owner",
            owner_scope="user", kind="content_model", protocol="openai", config_version=1,
            endpoint="https://provider.example.test/v1"))
    disclosures = DisclosureService(db, PgAuditLog(db))
    root_task = tasks.create_task(owner_user_id="owner", kind="generic", subject_id="lesson",
                                 idempotency_key="root")["task_id"]
    root = disclosures.create(actor_id="owner", task_id=root_task, config_id="synthetic_cfg",
        config_version=1, content_category="material", scope_snapshot={"material_ids": ["one", "two"]})
    try:
        yield db, tasks, disclosures, root
    finally:
        db.dispose()


def child(consent, **overrides):
    db, tasks, disclosures, root = consent
    task = tasks.create_task(owner_user_id="owner", kind="generic", subject_id="lesson",
                            idempotency_key="child")["task_id"]
    args = dict(actor_id="owner", task_id=task, parent_grant_id=root["grant_id"],
                config_id="synthetic_cfg", config_version=1, content_category="material",
                scope_snapshot={"material_ids": ["one"]})
    args.update(overrides)
    return disclosures.derive(**args)


def verify(consent, grant):
    return consent[2].verify(task_id=grant["task_id"], config_id="synthetic_cfg",
        content_category="material", actor_id="owner", scope_snapshot={"material_ids": ["one"]})


def test_derived_grant_is_narrow_and_inherits_expiry(consent):
    grant = child(consent)
    assert grant["parent_grant_id"] == consent[3]["grant_id"]
    assert grant["expires_at"] == consent[3]["expires_at"]
    assert verify(consent, grant)["grant_id"] == grant["grant_id"]


@pytest.mark.parametrize("override,code", [
    ({"scope_snapshot": {"material_ids": ["three"]}}, "disclosure_required"),
    ({"scope_snapshot": {"unapproved": True}}, "disclosure_required"),
    ({"content_category": "query"}, "disclosure_required"),
    ({"config_version": 2}, "disclosure_revoked"),
    ({"actor_id": "intruder"}, "access_denied"),
    ({"parent_grant_id": "missing"}, "disclosure_required"),
])
def test_derivation_fails_closed(consent, override, code):
    with pytest.raises(Slice1Error) as exc:
        child(consent, **override)
    assert exc.value.code == code
    with consent[0].session() as sess:
        assert sess.scalar(select(func.count()).select_from(DisclosureGrantRow)) == 1


@pytest.mark.parametrize("field,value,code", [
    ("revoked_at", utcnow(), "disclosure_revoked"),
    ("expires_at", utcnow() - timedelta(seconds=1), "disclosure_expired"),
    ("config_version", 2, "disclosure_revoked"),
    ("service_endpoint", "https://other.example.test", "disclosure_revoked"),
    ("scope_snapshot", '{"material_ids":["two"]}', "disclosure_required"),
])
def test_every_verify_rechecks_root(consent, field, value, code):
    grant = child(consent)
    with consent[0].session() as sess:
        root = sess.get(DisclosureGrantRow, consent[3]["grant_id"])
        setattr(root, field, value)
    with pytest.raises(Slice1Error) as exc:
        verify(consent, grant)
    assert exc.value.code == code


def test_explicit_root_revoke_blocks_existing_child(consent):
    grant = child(consent)
    consent[2].revoke(consent[3]["grant_id"], actor_id="owner")
    with pytest.raises(Slice1Error) as exc:
        verify(consent, grant)
    assert exc.value.code == "disclosure_revoked"


def test_revoke_audit_failure_rolls_back(consent):
    db, _, _, root = consent
    service = DisclosureService(db, PgAuditLog(db, fail_write=True))
    with pytest.raises(AuditWriteError):
        service.revoke(root["grant_id"], actor_id="owner")
    with db.session() as sess:
        assert sess.get(DisclosureGrantRow, root["grant_id"]).revoked_at is None


def test_cycle_is_rejected(consent):
    grant = child(consent)
    with consent[0].session() as sess:
        root = sess.get(DisclosureGrantRow, consent[3]["grant_id"])
        root.parent_grant_id = grant["grant_id"]
    with pytest.raises(Slice1Error):
        verify(consent, grant)


@pytest.mark.parametrize("fail_audit", [False, True])
def test_task_grant_and_audit_are_atomic(consent, fail_audit):
    db, tasks, disclosures, root = consent
    audit = PgAuditLog(db, fail_write=fail_audit)
    service = DisclosureService(db, audit)
    with db.session() as sess:
        before = {model: sess.scalar(select(func.count()).select_from(model)) for model in
                  (LearningTaskRow, AgentBudgetRow, TaskStateRow, DisclosureGrantRow, AuditLogRow)}
    with pytest.raises(AuditWriteError if fail_audit else RuntimeError):
        with db.session() as sess:
            task = tasks.create_task(owner_user_id="owner", kind="generic", subject_id="lesson",
                                     idempotency_key="atomic_abort", session=sess)
            derived = service.derive(actor_id="owner", task_id=task["task_id"],
                parent_grant_id=root["grant_id"], config_id="synthetic_cfg", config_version=1,
                content_category="material", scope_snapshot={"material_ids": ["one"]}, session=sess)
            assert service.verify(task_id=task["task_id"], config_id="synthetic_cfg", actor_id="owner",
                content_category="material", scope_snapshot={"material_ids": ["one"]},
                session=sess)["grant_id"] == derived["grant_id"]
            raise RuntimeError("synthetic rollback")
    with db.session() as sess:
        for model, count in before.items():
            assert sess.scalar(select(func.count()).select_from(model)) == count


def test_exact_root_cannot_be_replaced_by_newer_grant(consent):
    _, _, service, root = consent
    service.revoke(root['grant_id'], actor_id='owner')
    replacement = service.create(actor_id='owner', task_id=root['task_id'],
        config_id='synthetic_cfg', content_category='material', scope_snapshot={'material_ids':['one']})
    assert replacement['grant_id'] != root['grant_id']
    with pytest.raises(Slice1Error) as caught:
        service.verify(task_id=root['task_id'], config_id='synthetic_cfg', content_category='material',
            actor_id='owner', scope_snapshot={'material_ids':['one']}, expected_grant_id=root['grant_id'])
    assert caught.value.code == 'disclosure_revoked'


def test_chain_must_terminate_at_recorded_root(consent):
    _, _, service, root = consent
    grant = child(consent)
    replacement = service.create(actor_id='owner', task_id=grant['task_id'],
        config_id='synthetic_cfg', content_category='material', scope_snapshot={'material_ids':['one']})
    with pytest.raises(Slice1Error) as caught:
        service.verify(task_id=grant['task_id'], config_id='synthetic_cfg', content_category='material',
            actor_id='owner', scope_snapshot={'material_ids':['one']}, expected_root_grant_id=root['grant_id'])
    assert caught.value.code == 'disclosure_required'
    assert service.verify(task_id=grant['task_id'], config_id='synthetic_cfg', content_category='material',
        actor_id='owner', scope_snapshot={'material_ids':['one']}, expected_grant_id=grant['grant_id'],
        expected_root_grant_id=root['grant_id'])['grant_id'] == grant['grant_id']
    assert replacement['grant_id'] != grant['grant_id']


def test_create_pins_confirmation_version(consent):
    db, tasks, service, root = consent
    with pytest.raises(Slice1Error) as exc:
        service.create(actor_id="owner", task_id=root["task_id"], config_id="synthetic_cfg",
            config_version=2, content_category="query")
    assert exc.value.code == "disclosure_revoked"
