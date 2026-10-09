"""Retrieval availability is explicit without placing private excerpts in manifests."""
import json

from backend.agents.tools import make_search_materials_tool
from backend.planning.retrieval import PlanningRetrievalService


class SyntheticIndex:
    def search(self, **kwargs):
        return [{"chunk_id": "synthetic_chunk", "material_id": "synthetic_material",
                 "page_no": 1, "region_id": None}]

    def region_is_available(self, region_id):
        return True


def retrieval(resolver=None):
    return PlanningRetrievalService(None, index=SyntheticIndex(), text_resolver=resolver)


def assemble(service, limit=20):
    return service.assemble(owner_user_id="synthetic_owner", material_ids=["synthetic_material"],
                            query="synthetic", budget={"max_retrieval_chars": limit})


def test_missing_resolver_preserves_refs_and_reports_unavailable():
    result = assemble(retrieval())
    assert result.excerpts == [] and result.chars_total == 0
    assert result.refs[0]["chunk_id"] == "synthetic_chunk"
    assert result.truncation_reasons == ["text_resolver_unavailable"]


def test_missing_excerpt_is_distinct_from_unconfigured_resolver():
    result = assemble(retrieval(lambda hit: None))
    assert result.excerpts == [] and result.refs
    assert result.truncation_reasons == ["excerpt_unavailable"]


def test_character_truncation_is_bounded_and_reported():
    result = assemble(retrieval(lambda hit: "synthetic private excerpt"), 9)
    assert result.excerpts == [{"chunk_id": "synthetic_chunk", "text": "synthetic"}]
    assert result.chars_total == 9
    assert result.truncation_reasons == ["max_retrieval_chars"]


def test_zero_budget_never_returns_empty_excerpt_as_readable():
    result = assemble(retrieval(lambda hit: "synthetic private excerpt"), 0)
    assert result.excerpts == [] and result.chars_total == 0 and result.refs
    assert result.truncation_reasons == ["max_retrieval_chars"]


def test_exact_character_budget_is_not_truncated():
    result = assemble(retrieval(lambda hit: "synthetic"), 9)
    assert result.chars_total == 9 and result.truncation_reasons == []


def test_tool_exposes_unavailability_without_claiming_to_read_text():
    tool = make_search_materials_tool(retrieval(), owner_user_id="synthetic_owner",
                                      material_ids=["synthetic_material"])
    result = json.loads(tool("synthetic"))
    assert result["items"] == []
    assert result["unavailable_reasons"] == ["text_resolver_unavailable"]
