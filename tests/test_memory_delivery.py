"""Synthetic scoped memory, revocation and durable delivery checks."""
import asyncio

import pytest
from sqlalchemy import select

from backend.auth.models import UserRow
from backend.db import Database
from backend.memory.models import (MemorySettingsRow, MemorySourceRow, MemorySpaceRow,
    LearningEvidenceRow, MemoryMaintenancePolicy, MemoryMaintenanceJob)
from backend.memory.service import MemoryService, enqueue


def test_manual_and_extracted_feedback_share_expanded_closed_vocabulary():
    from pydantic import ValidationError
    from backend.memory.router import FeedbackIn
    from backend.memory.style import StyleSignal
    for aspect in ("persona", "warmth", "structure", "detail", "terminology", "notation", "transitions", "recap", "questioning"):
        for preference in ("slower", "faster", "simpler", "deeper", "warmer", "more_direct"):
            assert FeedbackIn(card_revision=1, aspect=aspect, preference=preference,
                idempotency_key="synthetic").aspect == aspect
            assert StyleSignal(explicit=True, aspect=aspect, preference=preference).preference == preference
    with pytest.raises(ValidationError):
        FeedbackIn(card_revision=1, aspect="unrestricted_course_content", preference="keep",
            idempotency_key="synthetic")
from backend.planning.errors import PlanningError


class Engine:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def retain(self, *args):
        self.calls.append(("retain", args))
        if self.fail:
            raise RuntimeError("synthetic provider failure")

    async def refresh(self, *args):
        self.calls.append(("refresh", args))

    async def models(self, bank, tag):
        self.calls.append(("models", (bank, tag)))
        return {"items": [{"content": "synthetic summary"}]}

    async def delete_document(self, *args):
        self.calls.append(("delete", args))

    async def delete_bank(self, *args):
        self.calls.append(("delete_bank", args))


@pytest.fixture
def memory(tmp_path):
    db = Database("sqlite+pysqlite:///" + (tmp_path / "memory.db").as_posix())
    for model in (UserRow, MemorySettingsRow, MemorySpaceRow, MemorySourceRow,
                  LearningEvidenceRow, MemoryMaintenancePolicy, MemoryMaintenanceJob):
        model.__table__.create(db.engine)
    with db.session() as session:
        for name in ("alice", "bob"):
            session.add(UserRow(user_id=name, email=name + "@example.invalid",
                password_hash="synthetic-unused", status="approved", role="member"))
    engine = Engine()
    service = MemoryService(db, None, engine)
    yield db, service, engine
    db.dispose()


def record(db, owner="alice", kind="chat_user_statement", event="one", **kwargs):
    with db.session() as session:
        enqueue(session, owner, kind, event, {"content": "synthetic preference"}, **kwargs)


def test_opt_in_and_no_guest_or_unknown_routing(memory):
    db, service, _ = memory
    record(db)
    assert service.settings("alice")["enabled"] is False
    with db.session() as session:
        assert session.scalar(select(MemorySourceRow)) is None
    service.configure("alice", True, 0)
    with pytest.raises(PlanningError):
        record(db, kind="arbitrary_source")
    with pytest.raises(PlanningError):
        record(db, kind="course_feynman")


def test_duplicate_event_and_character_scope(memory):
    db, service, _ = memory
    service.configure("alice", True, 0)
    record(db)
    record(db)
    record(db, kind="character_delivery_feedback", card="card-A")
    record(db, kind="character_delivery_feedback", card="card-B")
    with db.session() as session:
        rows = list(session.scalars(select(MemorySourceRow)))
        assert len(rows) == 3
        assert {r.tag for r in rows} == {"profile", "character:card-A", "character:card-B"}
        assert len({r.scope_key for r in rows}) == 1


def test_native_delivery_and_account_isolation(memory):
    db, service, engine = memory
    for name in ("alice", "bob"):
        service.configure(name, True, 0)
        record(db, owner=name)
    service.tick()
    service.tick()
    with db.session() as session:
        rows = list(session.scalars(select(MemorySourceRow)))
        alice = next(r for r in rows if r.owner_user_id == "alice")
        source_id = alice.source_id
    assert any(c[0] == "retain" for c in engine.calls)
    assert any(c[0] == "refresh" for c in engine.calls)
    with pytest.raises(PlanningError) as denied:
        service.forget("bob", source_id)
    assert denied.value.status == 404
    a = asyncio.run(service.read("alice"))
    b = asyncio.run(service.read("bob"))
    assert a["status"] == b["status"] == "ready"
    banks = [c[1][0] for c in engine.calls if c[0] == "models"]
    assert banks[0] != banks[1]


def test_disable_prevents_outbound_and_revision_conflict(memory):
    db, service, engine = memory
    service.configure("alice", True, 0)
    record(db)
    with pytest.raises(PlanningError):
        service.configure("alice", False, 0)
    service.configure("alice", False, 1)
    service.tick()
    assert not engine.calls
    assert asyncio.run(service.read("alice"))["status"] == "disabled"


def test_forgetting_blocks_reingestion_and_stale_models(memory):
    db, service, engine = memory
    service.configure("alice", True, 0)
    record(db)
    service.tick()
    with db.session() as session:
        ident = session.scalar(select(MemorySourceRow.source_id))
    service.forget("alice", ident)
    assert asyncio.run(service.read("alice"))["status"] == "cleaning"
    service.tick()
    record(db)
    with db.session() as session:
        row = session.get(MemorySourceRow, ident)
        assert row.status == "forgotten" and row.payload_json == "{}"
        assert len(list(session.scalars(select(MemorySourceRow)))) == 1
    assert any(c[0] == "delete" for c in engine.calls)


def test_provider_failure_has_persistent_attempt_budget(memory):
    db, service, engine = memory
    service.configure("alice", True, 0)
    record(db)
    engine.fail = True
    for _ in range(5):
        service.tick()
    assert len(engine.calls) == 3
    with db.session() as session:
        assert session.scalar(select(MemorySourceRow)).attempts == 3


def test_correction_revision_and_reset_preserve_other_owner(memory):
    db, service, _ = memory
    for name in ("alice", "bob"):
        service.configure(name, True, 0)
        record(db, owner=name)
    with db.session() as session:
        ident = session.scalar(select(MemorySourceRow.source_id).where(MemorySourceRow.owner_user_id == "alice"))
    service.correct("alice", ident, "synthetic corrected preference", 1)
    with pytest.raises(PlanningError) as conflict:
        service.correct("alice", ident, "duplicate", 1)
    assert conflict.value.status == 409
    service.reset("alice")
    with db.session() as session:
        rows = list(session.scalars(select(MemorySourceRow)))
        assert all(r.status == "forgetting" for r in rows if r.owner_user_id == "alice")
        assert next(r for r in rows if r.owner_user_id == "bob").status == "pending"


def test_deleted_course_preserves_published_general_and_cancels_candidates(memory):
    from backend.planning.models import CourseRow
    from backend.memory.models import now
    from backend.memory.service import delete_course_memory
    db, service, _ = memory
    CourseRow.__table__.create(db.engine)
    service.configure("alice", True, 0)
    with db.session() as session:
        session.add(CourseRow(course_id="course-A", owner_user_id="alice", title_ref="Synthetic course"))
    record(db, kind="unit_assessment_result", event="graded", course="course-A")
    service.tick()
    record(db, kind="course_feynman", event="feynman", course="course-A")
    record(db, kind="character_feedback_candidate", event="style", course="course-A", card="card-A")
    with db.session() as session:
        session.get(CourseRow, "course-A").deleted_at = now()
        delete_course_memory(session, "alice", "course-A")
    with db.session() as session:
        sources = list(session.scalars(select(MemorySourceRow)))
        general = next(r for r in sources if r.source_kind == "unit_assessment_result")
        assert general.status == "retained" and general.source_course_id == ""
        assert all(r.status == "suppressed" and r.payload_json == "{}" for r in sources
                   if r.source_kind != "unit_assessment_result")
    with pytest.raises(PlanningError) as denied:
        asyncio.run(service.read("alice", "course:course-A", "course"))
    assert denied.value.status == 404


def test_sensitive_values_are_not_sent_to_memory():
    from backend.memory.service import clean_payload
    value = clean_payload({"content": "sk-synthetic-secret-123456 user@example.com C:\\private\\test.txt",
                           "password": "synthetic-secret"})
    assert "password" not in value
    assert "sk-synthetic" not in value["content"]
    assert "user@example.com" not in value["content"]


def test_forget_during_read_fences_late_summary(memory):
    db, service, engine = memory
    service.configure("alice", True, 0)
    record(db)
    with db.session() as session:
        ident = session.scalar(select(MemorySourceRow.source_id))
    async def delayed(bank, tag):
        service.forget("alice", ident)
        return {"items": [{"content": "synthetic stale summary"}]}
    engine.models = delayed
    result = asyncio.run(service.read("alice"))
    assert result["status"] == "stale" and "data" not in result


def test_process_crash_preserves_debit_and_lease(memory):
    db, service, engine = memory
    service.configure("alice", True, 0)
    record(db)
    class SyntheticCrash(BaseException):
        pass
    async def crash(*args):
        raise SyntheticCrash()
    engine.retain = crash
    with pytest.raises(SyntheticCrash):
        service.tick()
    with db.session() as session:
        source = session.scalar(select(MemorySourceRow))
        assert source.attempts == 1 and source.lease_until is not None


def test_style_filter_has_closed_output_and_native_deps():
    from pydantic_ai.models.test import TestModel
    from backend.memory.style import explicit_style_feedback
    output = asyncio.run(explicit_style_feedback("synthetic feedback", "alice",
        TestModel(custom_output_args={"explicit": True, "aspect": "examples", "preference": "clearer"})))
    assert output.explicit and output.aspect == "examples"
    assert set(output.model_dump()) == {"explicit", "aspect", "preference"}


def test_official_pydantic_ai_tools_have_no_free_write():
    from hindsight_client import Hindsight
    from hindsight_pydantic_ai import create_hindsight_tools
    tools = create_hindsight_tools(bank_id="synthetic-bank",
        client=Hindsight("http://127.0.0.1:1"), recall_tags=["profile"],
        recall_tags_match="exact", include_retain=False, include_reflect=False)
    assert len(tools) == 1
    assert "recall" in tools[0].name
