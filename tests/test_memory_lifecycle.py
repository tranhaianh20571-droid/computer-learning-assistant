"""Synthetic evidence, clock, withdrawal and transactional maintenance contracts."""
from datetime import timedelta
import json
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select

from backend.auth.models import UserRow
from backend.db import Database
from backend.memory import learning, maintenance
from backend.memory.models import (LearningEvidenceRow, MemoryMaintenanceJob,
    MemoryMaintenancePolicy, MemorySettingsRow, MemorySourceRow, MemorySpaceRow, now)
from backend.memory.native_lifecycle import Coverage, valid_coverage
from backend.memory.personal import MemoryMigration
from backend.memory.service import MemoryService, enqueue
from backend.planning.models import CourseRow


class Native:
    def __init__(self):
        self.calls = []
        self.allowed = True
        self.protocol = maintenance.PROTOCOL
        self.before_prepare_return = None
        self.fail = False
        self.lost_receipt = False
        self.committed = False

    async def lifecycle(self, bank, operation, body):
        self.calls.append(operation)
        if self.fail:
            raise RuntimeError("synthetic failure")
        if operation == "prepare":
            if self.before_prepare_return:
                self.before_prepare_return()
            return {"protocol": self.protocol, "allowed": self.allowed, "fingerprint": "synthetic"}
        if operation == "commit":
            self.committed = True
            if self.lost_receipt:
                raise RuntimeError("synthetic lost receipt")
        return {"protocol": self.protocol, "committed": self.committed}

    async def models(self, *args):
        raise RuntimeError("synthetic unavailable")

    async def delete_document(self, *args):
        self.calls.append("ordinary_delete")


@pytest.fixture
def setup(tmp_path):
    db = Database("sqlite+pysqlite:///" + (tmp_path / "lifecycle.db").as_posix())
    for model in (UserRow, CourseRow, MemorySettingsRow, MemorySpaceRow, MemorySourceRow,
                  LearningEvidenceRow, MemoryMaintenancePolicy, MemoryMaintenanceJob, MemoryMigration):
        model.__table__.create(db.engine)
    with db.session() as session:
        for owner in ("alice", "bob"):
            session.add(UserRow(user_id=owner, email=owner + "@example.invalid",
                password_hash="synthetic-unused", status="approved", role="member"))
            session.add(MemorySettingsRow(owner_user_id=owner, enabled=True))
            session.add(CourseRow(course_id=owner + "-course", owner_user_id=owner))
    native = Native()
    service = MemoryService(db, None, native)
    yield db, service, native
    db.dispose()


def add_review(db, event="event", *, owner="alice", days=30, result="correct",
               independence="independent", source="source", concept="concept", version="v1"):
    with db.session() as session:
        learning.record(session, owner, owner + "-course", event,
            [{"concept_id": concept, "content_version": version, "lesson_id": "lesson",
              "independence": independence, "result": result}],
            occurred_at=now() - timedelta(days=days), sources=[source])


def state(db, *, owner="alice", at=None):
    with db.session() as session:
        return learning.context(session, owner, owner + "-course", at=at)


def old_source(db):
    with db.session() as session:
        ident = enqueue(session, "alice", "course_question", "event",
                        {"content": "synthetic"}, course="alice-course")
        session.flush()
        row = session.get(MemorySourceRow, ident)
        row.status, row.created_at = "completed", now() - timedelta(days=91)
    return ident


def test_clock_decay_requires_no_new_write(setup):
    db, _, _ = setup
    add_review(db)
    with db.session() as session:
        row = session.scalar(select(LearningEvidenceRow))
        at = learning.aware(row.occurred_at)
    immediate = state(db, at=at)["items"][0]
    later = state(db, at=at + timedelta(days=30))["items"][0]
    assert immediate["retrievability"] > later["retrievability"]
    assert immediate["action"] == "advance" and later["action"] == "diagnose"


@pytest.mark.parametrize("independence,result", [
    ("assisted", "correct"), ("unknown", "correct"), ("independent", "unknown"),
    ("assisted", "incorrect")])
def test_unverified_events_neither_strengthen_nor_fail(setup, independence, result):
    db, _, _ = setup
    add_review(db, independence=independence, result=result)
    value = state(db)["items"][0]
    assert value["retrievability"] is None and value["independent_count"] == 0
    assert value["status"] == "unknown"


def test_latest_failure_is_not_instant_mastery(setup):
    db, _, _ = setup
    add_review(db)
    add_review(db, "failure", days=1, result="incorrect")
    value = state(db)["items"][0]
    assert value["retrievability"] is None and value["action"] == "review"
    add_review(db, "recovery", days=0)
    assert state(db)["items"][0]["last_correct"] is True


def test_spaced_reviews_and_content_versions_are_distinct(setup):
    db, _, _ = setup
    add_review(db, "one", days=60)
    add_review(db, "two", days=45)
    add_review(db, "three", days=30)
    add_review(db, "new-version", days=30, version="v2")
    items = {r["content_version"]: r for r in state(db)["items"]}
    assert items["v1"]["independent_count"] == 3
    assert items["v1"]["retrievability"] > items["v2"]["retrievability"]
    with db.session() as session:
        current = learning.context(session, "alice", "alice-course", lesson="lesson",
            content_version="v1")
    assert {item["content_version"] for item in current["items"]} == {"v1"}


def test_review_targets_are_exact_and_prioritize_failure_then_low_retrievability(setup):
    db, _, _ = setup
    add_review(db, "old-due", days=75, concept="due")
    add_review(db, "failure", days=1, result="incorrect", concept="failed")
    add_review(db, "unverified", days=1, result="correct", independence="unknown",
        concept="unknown")
    current = state(db, at=now() + timedelta(days=30))
    guidance = learning.select_review_targets(current, {
        "due": "Due concept", "failed": "Failed concept", "unknown": "Unknown concept",
        "unmatched": "Not in this checkpoint"})
    assert guidance and guidance["uncalibrated"]
    assert [item["concept_id"] for item in guidance["items"]] == ["failed", "due"]
    assert guidance["items"][0]["action"] == "review"
    assert guidance["items"][1]["action"] == "recall_check"


def test_review_targets_skip_unknown_advanced_and_mismatched_concepts(setup):
    state_value = {"status": "ready", "algorithm": learning.ALGORITHM, "as_of": "2026-10-22",
        "items": [
            {"concept_id": "unknown", "action": "diagnose", "independent_count": 0,
             "last_correct": None, "retrievability": None},
            {"concept_id": "advanced", "action": "advance", "independent_count": 2,
             "last_correct": True, "retrievability": 0.95},
            {"concept_id": "due", "action": "diagnose", "independent_count": 1,
             "last_correct": True, "retrievability": 0.89},
        ]}
    guidance = learning.select_review_targets(state_value, {"due": "Due concept"})
    assert guidance and [item["concept_id"] for item in guidance["items"]] == ["due"]
    assert learning.select_review_targets({"status": "unavailable"}, {"due": "Due concept"}) is None


def test_dual_delivery_and_same_concept_questions_count_once(setup):
    db, _, _ = setup
    add_review(db)
    add_review(db, source="second-copy")
    assert state(db)["items"][0]["independent_count"] == 1
    with db.session() as session:
        learning.record(session, "alice", "alice-course", "multi-question", [
            {"concept_id": "other", "content_version": "v1", "independence": "independent", "result": "correct"},
            {"concept_id": "other", "content_version": "v1", "independence": "assisted", "result": "incorrect"}],
            occurred_at=now(), sources=["profile", "course"])
    item = next(r for r in state(db)["items"] if r["concept_id"] == "other")
    assert item["independent_count"] == 0


def test_future_evidence_rejected_and_future_read_does_not_leak(setup):
    db, _, _ = setup
    add_review(db, days=0)
    assert state(db, at=now() - timedelta(days=1))["items"] == []
    with pytest.raises(ValueError, match="future"):
        add_review(db, "future", days=-1)


def test_grade_correction_replaces_instead_of_adds_review(setup):
    db, _, _ = setup
    add_review(db)
    with db.session() as session:
        ident = session.scalar(select(LearningEvidenceRow.evidence_id))
        learning.correct_grade(session, "alice", ident, result="incorrect", expected_revision=1)
    value = state(db)["items"][0]
    assert value["independent_count"] == 1 and value["last_correct"] is False
    with pytest.raises(Exception):
        with db.session() as session:
            learning.correct_grade(session, "alice", ident, result="correct", expected_revision=1)


def test_withdrawal_tombstone_and_owner_boundaries(setup):
    db, _, _ = setup
    add_review(db)
    add_review(db, owner="bob")
    with db.session() as session:
        learning.revoke(session, "alice", source_id="source")
    add_review(db)
    assert state(db)["items"] == []
    assert state(db, owner="bob")["items"][0]["independent_count"] == 1
    with db.session() as session:
        row = session.scalar(select(LearningEvidenceRow).where(
            LearningEvidenceRow.owner_user_id == "alice"))
        assert row.status == "revoked" and row.concept_id == "" and row.source_ids_json == "[]"


def test_native_unavailable_preserves_numeric_context_and_disable_blocks(setup):
    db, service, _ = setup
    add_review(db)
    value = service.context_for_role("alice", "teaching", course="alice-course")
    assert value["learning_state"]["items"][0]["independent_count"] == 1
    service.configure("alice", False, 1)
    assert "learning_state" not in service.context_for_role("alice", "teaching", course="alice-course")
    assert service.context_for_role("alice", "research", course="alice-course")["status"] == "excluded"


def test_cache_revision_changes_without_learning_write(setup, monkeypatch):
    db, service, _ = setup
    from backend.memory import service as service_module
    fixed = now()
    monkeypatch.setattr(service_module, "now", lambda: fixed)
    first = service.version("alice")
    monkeypatch.setattr(service_module, "now", lambda: fixed + timedelta(days=1))
    assert service.version("alice") != first


def test_current_lesson_does_not_inherit_other_lesson_mastery(setup):
    db, _, _ = setup
    add_review(db)
    with db.session() as session:
        assert learning.context(session, "alice", "alice-course", lesson="other")["items"] == []
        assert len(learning.context(session, "alice", "alice-course", lesson="lesson")["items"]) == 1


def test_assessment_without_independence_does_not_claim_mastery(setup):
    db, _, _ = setup
    assessment = SimpleNamespace(course_id="alice-course", outline_version_id="v1",
        questions_json=json.dumps({"questions": [
            {"id": "q", "knowledge_point_ids": ["concept"], "primary_lesson_id": "lesson"}]}))
    attempt = SimpleNamespace(attempt_id="attempt", created_at=now())
    with db.session() as session:
        learning.record_assessment(session, "alice", assessment, attempt,
            {"grades": [{"question_id": "q", "earned": 1, "maximum": 1}]}, ["profile", "course"])
    assert state(db)["items"][0]["independent_count"] == 0


@pytest.mark.parametrize("phase,assisted,result,count", [
    ("explain", False, "understood", 0), ("transfer", True, "understood", 0),
    ("transfer", False, "understood", 1), ("transfer", False, "probe", 0)])
def test_feynman_only_independent_transfer_is_evidence(setup, phase, assisted, result, count):
    db, _, _ = setup
    with db.session() as session:
        learning.record_feynman(session, "alice", "alice-course", "turn", now(),
            {"phase": phase, "assisted": assisted, "learning_result": result,
             "knowledge_point_ids": ["concept"], "content_version": "v1"}, ["source"])
    assert sum(v["independent_count"] for v in state(db)["items"]) == count


@pytest.mark.parametrize("mode,allowed,expected", [
    ("preview", True, "preview"), ("automatic", True, "completed"),
    ("automatic", False, "kept")])
def test_maintenance_policy_preserves_ledger(setup, mode, allowed, expected):
    db, service, native = setup
    ident = old_source(db)
    add_review(db, source=ident)
    maintenance.configure(service, "alice", mode, 0)
    native.allowed = allowed
    maintenance.maintenance_tick(service)
    with db.session() as session:
        source = session.get(MemorySourceRow, ident)
        job = session.scalar(select(MemoryMaintenanceJob))
        assert job.status == expected
        assert job.native_committed is (expected == "completed")
        assert (source.payload_json == "{}") is (expected == "completed")
    assert ("commit" in native.calls) is (expected == "completed")
    assert "ordinary_delete" not in native.calls
    assert state(db)["items"][0]["independent_count"] == 1


@pytest.mark.parametrize("change", ["disable", "revision", "epoch", "revoke", "migration"])
def test_revalidation_blocks_mutation_during_semantic_audit(setup, change):
    db, service, native = setup
    ident = old_source(db)
    maintenance.configure(service, "alice", "automatic", 0)
    def mutate():
        with db.session() as session:
            source = session.get(MemorySourceRow, ident)
            space = session.scalar(select(MemorySpaceRow))
            if change == "disable":
                session.get(MemorySettingsRow, "alice").enabled = False
            elif change == "revision":
                source.revision += 1
            elif change == "epoch":
                space.epoch += 1
            elif change == "revoke":
                session.get(UserRow, "alice").status = "disabled"
            else:
                session.add(MemoryMigration(migration_id="migration", owner_user_id="alice",
                    space_id=space.space_id, source_bank=space.bank_id, target_bank="synthetic-target",
                    revision=1, epoch=1, source_refs_json="[]"))
    native.before_prepare_return = mutate
    maintenance.maintenance_tick(service)
    assert "commit" not in native.calls
    with db.session() as session:
        assert session.get(MemorySourceRow, ident).payload_json != "{}"


def test_failures_have_durable_budget_and_no_ordinary_delete_fallback(setup):
    db, service, native = setup
    old_source(db)
    native.fail = True
    for _ in range(5):
        maintenance.maintenance_tick(service)
        with db.session() as session:
            for job in session.scalars(select(MemoryMaintenanceJob)):
                job.lease_until = None
    assert native.calls == ["prepare"] * 3
    with db.session() as session:
        assert session.scalar(select(MemoryMaintenanceJob)).status == "failed"


def test_lost_final_receipt_reconciles_read_only(setup):
    db, service, native = setup
    ident = old_source(db)
    maintenance.configure(service, "alice", "automatic", 0)
    native.lost_receipt = True
    for _ in range(4):
        maintenance.maintenance_tick(service)
        with db.session() as session:
            session.scalar(select(MemoryMaintenanceJob)).lease_until = None
    assert native.calls.count("commit") == 3 and native.calls[-1] == "status"
    with db.session() as session:
        assert session.get(MemorySourceRow, ident).status == "compacted"


def test_unsupported_protocol_cannot_delete(setup):
    db, service, native = setup
    old_source(db)
    native.protocol = "old-service"
    maintenance.configure(service, "alice", "automatic", 0)
    maintenance.maintenance_tick(service)
    assert native.calls == ["prepare"]


def test_recent_source_not_scheduled(setup):
    db, service, native = setup
    ident = old_source(db)
    with db.session() as session:
        session.get(MemorySourceRow, ident).created_at = now() - timedelta(days=30)
    maintenance.maintenance_tick(service)
    assert native.calls == []


def test_daily_batch_budget_is_persistent(setup):
    db, service, _ = setup
    with db.session() as session:
        for index in range(12):
            ident = enqueue(session, "alice", "course_question", str(index),
                            {"content": "synthetic"}, course="alice-course")
            session.flush()
            row = session.get(MemorySourceRow, ident)
            row.status, row.created_at = "completed", now() - timedelta(days=91)
    maintenance.schedule(service)
    maintenance.schedule(service)
    with db.session() as session:
        assert len(list(session.scalars(select(MemoryMaintenanceJob)))) == 10


def test_postgresql_maintenance_query_and_evidence_read():
    from backend.db import get_database_url
    from sqlalchemy.engine import make_url
    url = make_url(get_database_url())
    if (url.get_backend_name() != "postgresql"
            or "native_validation_" not in str(url.query.get("options", ""))):
        pytest.skip("requires the owned PostgreSQL schema runner")
    db = Database(url)
    owner = "synthetic-" + uuid.uuid4().hex
    service = MemoryService(db, None, Native())
    try:
        with db.session() as session:
            session.add(UserRow(user_id=owner, email=owner + "@example.invalid",
                password_hash="synthetic-unused", status="approved", role="member"))
            session.add(MemorySettingsRow(owner_user_id=owner, enabled=True))
            session.add(CourseRow(course_id=owner + "-course", owner_user_id=owner))
        with db.session() as session:
            ident = enqueue(session, owner, "course_question", "event",
                            {"content": "synthetic"}, course=owner + "-course")
            session.flush()
            source = session.get(MemorySourceRow, ident)
            source.status, source.created_at = "completed", now() - timedelta(days=91)
        add_review(db, owner=owner, source=ident)
        maintenance.maintenance_tick(service)
        with db.session() as session:
            job = session.scalar(select(MemoryMaintenanceJob).where(
                MemoryMaintenanceJob.owner_user_id == owner))
            assert job.status == "preview"
        assert state(db, owner=owner)["items"][0]["independent_count"] == 1
    finally:
        db.dispose()


def test_enabling_automatic_after_preview_can_run_same_day(setup):
    db, service, native = setup
    ident = old_source(db)
    maintenance.maintenance_tick(service)
    maintenance.configure(service, "alice", "automatic", 0)
    maintenance.maintenance_tick(service)
    with db.session() as session:
        assert session.get(MemorySourceRow, ident).status == "compacted"
    assert native.calls.count("commit") == 1


def test_withdrawal_after_uncertain_commit_keeps_retraction_requirement(setup):
    db, service, native = setup
    ident = old_source(db)
    maintenance.configure(service, "alice", "automatic", 0)
    native.lost_receipt = True
    maintenance.maintenance_tick(service)
    service.forget("alice", ident)
    native.lost_receipt = False
    service.tick()
    assert native.calls[-2:] == ["retract", "ordinary_delete"]


def test_preview_withdrawal_does_not_require_compaction_support(setup):
    db, service, native = setup
    ident = old_source(db)
    maintenance.maintenance_tick(service)
    with db.session() as session:
        maintenance.revoke_jobs(session, "alice", source_id=ident)
        source = session.get(MemorySourceRow, ident)
        maintenance.retract_compacted(service, session, source, session.scalar(select(MemorySpaceRow)))
    assert "retract" not in native.calls


def test_explicit_forget_retracts_compacted_dependencies(setup):
    db, service, native = setup
    ident = old_source(db)
    maintenance.configure(service, "alice", "automatic", 0)
    maintenance.maintenance_tick(service)
    add_review(db, source=ident)
    service.forget("alice", ident)
    assert state(db)["items"] == []
    service.tick()
    assert native.calls[-2:] == ["retract", "ordinary_delete"]


@pytest.mark.parametrize("legacy", [True, False])
def test_source_identity_survives_bank_epoch(setup, legacy):
    db, service, _ = setup
    ident = old_source(db)
    if legacy:
        with db.session() as session:
            session.get(MemorySourceRow, ident).event_key = None
    service.forget("alice", ident)
    with db.session() as session:
        session.scalar(select(MemorySpaceRow)).epoch += 1
        same = enqueue(session, "alice", "course_question", "event",
                       {"content": "synthetic"}, course="alice-course")
        assert same == ident
    with db.session() as session:
        assert len(list(session.scalars(select(MemorySourceRow)))) == 1


def test_multiple_unmapped_capabilities_are_not_all_marked_mastered(setup):
    db, _, _ = setup
    with db.session() as session:
        learning.record_feynman(session, "alice", "alice-course", "turn", now(), {
            "phase": "transfer", "assisted": False, "learning_result": "understood",
            "knowledge_point_ids": ["one", "two"], "content_version": "v1"}, ["source"])
    assert len(state(db)["items"]) == 2
    assert all(v["status"] == "unknown" for v in state(db)["items"])


def test_no_source_means_no_orphan_evidence(setup):
    db, _, _ = setup
    with db.session() as session:
        learning.record(session, "alice", "alice-course", "missing-source",
            [{"concept_id": "concept", "content_version": "v1", "independence": "independent",
              "result": "correct"}], occurred_at=now(), sources=[None])
    assert state(db)["items"] == []


def test_audit_failure_blocks_native_delete(setup):
    from backend.logging_audit.audit import AuditWriteError
    db, service, native = setup
    ident = old_source(db)
    maintenance.configure(service, "alice", "automatic", 0)
    class Audit:
        def append(self, *args, **kwargs):
            raise AuditWriteError("synthetic")
    service.audit = Audit()
    maintenance.maintenance_tick(service)
    assert "commit" not in native.calls
    with db.session() as session:
        assert session.get(MemorySourceRow, ident).status == "completed"
        assert session.scalar(select(MemoryMaintenanceJob)).error_code == "AUDIT_WRITE_FAILED"


@pytest.mark.parametrize("covered,disposable,keep,observations,allowed", [
    (["f"], [], [], ["o"], True), ([], [], [], ["o"], False),
    (["f"], ["f"], [], ["o"], False), ([], [], ["f"], ["o"], False),
    (["f"], [], [], ["invented"], False), (["f"], [], [], [], False)])
def test_coverage_requires_exact_partition(covered, disposable, keep, observations, allowed):
    value = Coverage(decision="allow", covered_fact_ids=covered,
        disposable_fact_ids=disposable, keep_fact_ids=keep, observation_ids=observations)
    assert valid_coverage(value, [{"id": "f"}], [{"id": "o"}]) is allowed
