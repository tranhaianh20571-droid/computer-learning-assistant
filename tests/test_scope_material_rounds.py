"""Synthetic bounded evidence rounds, restart recovery, dedup and ownership."""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from backend.agents.contracts import AgentDeps
from backend.teaching.scope_materials import ScopeEvidenceDecision, ScopeMaterialSearch


def setup_search(monkeypatch, *, checkpoint=None, fail=False):
    import backend.teaching.scope_materials as module
    monkeypatch.setattr(module, "verify_sources", lambda *args: [])
    monkeypatch.setattr(module, "chapter_catalog", lambda *args: [
        {"id": "doc/section", "reference_only": False},
        {"id": "doc/more", "reference_only": False}])

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    calls, saved = [], []

    def assemble(**kwargs):
        calls.append(kwargs)
        if fail:
            raise RuntimeError("synthetic retrieval failure")
        n = len(calls) + len((checkpoint or {}).get("rounds", []))
        return SimpleNamespace(excerpts=[
            {"section_id": "doc/more", "material_id": "doc", "page_no": n,
             "text": f"Synthetic evidence round {n} item {i}."} for i in range(8)],
            truncation_reasons=[])

    teaching = SimpleNamespace(db=SimpleNamespace(session=Session),
        retrieval=SimpleNamespace(assemble=assemble, save_manifest=lambda **kwargs: None))
    context = {"source_snapshot": {"material_ids": ["doc"]},
               "source_sections": [{"id": "doc/section", "text": "Synthetic initial evidence."}]}
    search = ScopeMaterialSearch(teaching, "owner", "task", context, AgentDeps(),
        checkpoint=checkpoint, save=lambda value: saved.append(deepcopy(value)))
    return search, calls, saved


def decision(sufficient=False, query="missing method"):
    return ScopeEvidenceDecision(sufficient=sufficient, missing=[] if sufficient else ["Missing method."],
        query="" if sufficient else query, evidence_section_ids=["doc/section"],
        reason="Synthetic analysis of available evidence.")


def test_sufficient_initial_text_never_searches(monkeypatch):
    search, calls, saved = setup_search(monkeypatch)
    assert search.prepare(lambda context: decision(True))["status"] == "sufficient"
    assert not calls and saved[-1]["status"] == "sufficient"


def test_rounds_get_five_and_keep_unresolved_gaps_at_three(monkeypatch):
    search, calls, _ = setup_search(monkeypatch)
    summary = search.prepare(lambda context: decision(query=f"gap {context['retrieval_round']}"))
    assert len(calls) == 3 and summary["status"] == "round_limit" and summary["missing"]
    assert all(c["budget"]["max_retrieval_chunks"] == 5 for c in calls)
    assert len(search.state["sources"]) == 15
    assert json.loads(search.search_materials("fourth"))["status"] == "budget_exhausted"
    resumed, new_calls, _ = setup_search(monkeypatch, checkpoint=search.state)
    resumed.prepare(lambda _: pytest.fail("Completed decision must be reused."))
    assert not new_calls and len(resumed.receipts) == 3


def test_failure_reserves_round_before_io_and_resume_never_resets(monkeypatch):
    search, _, saved = setup_search(monkeypatch, fail=True)
    with pytest.raises(RuntimeError):
        search.prepare(lambda _: decision())
    assert saved[-1]["rounds"][0]["status"] == "reserved"
    resumed, calls, _ = setup_search(monkeypatch, checkpoint=saved[-1])
    resumed.prepare(lambda context: decision(query=f"new gap {context['retrieval_round']}"))
    assert len(calls) == 2 and len(resumed.receipts) == 3


def test_no_new_evidence_stops_without_claiming_sufficiency(monkeypatch):
    search, calls, _ = setup_search(monkeypatch)
    search.context["source_sections"].append({"id": "doc/more", "text": " ".join(
        f"Synthetic evidence round 1 item {i}." for i in range(8))})
    summary = search.prepare(lambda _: decision())
    assert len(calls) == 1 and summary["status"] == "no_new_evidence" and summary["missing"]


def test_changed_owner_or_expired_binding_rejects_even_restored_result(monkeypatch):
    search, _, _ = setup_search(monkeypatch)
    search.prepare(lambda _: decision(True))
    search.deps = AgentDeps(check_state=lambda: (_ for _ in ()).throw(RuntimeError("revoked")))
    with pytest.raises(RuntimeError, match="revoked"):
        search.prepare(lambda _: decision(True))
