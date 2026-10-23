"""Owner-isolated planning process content and native thinking streaming contracts."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from pydantic import BaseModel
from pydantic_ai.models.function import FunctionModel, DeltaThinkingPart, DeltaToolCall

from backend.agents.contracts import AgentDeps, GenerationInput
from backend.agents.runner import execute_agent
from backend.logging_audit.models import TaskEventRow
from backend.planning.errors import PlanningError
from backend.planning.models import PlanningTaskStateRow, CourseRow, utcnow
from backend.planning.process import ProcessWriter, read_process, MAX_THINKING
from backend.tasks.service import LearningTaskRow, LeaseError
from test_anysearch_planning import env, make_task, run_pipeline, BoundSearch, evidence


def test_old_repair_search_counts_recovered_read_only_and_config_scoped(env):
    from backend.search.repository import SearchCallRow
    from backend.slice1.models import ServiceConfigRow
    ServiceConfigRow.__table__.create(env.db.engine, checkfirst=True)
    ident, lease, _ = make_task(env)
    writer = ProcessWriter(env.planning, lease)
    writer.stage("generate")
    writer.finish("succeeded")
    with env.db.session() as session:
        session.add(ServiceConfigRow(config_id="search-synthetic", owner_user_id="owner",
            owner_scope="user", kind="search", protocol="anysearch", endpoint="https://example.test"))
        session.add(SearchCallRow(query_id="synthetic-search-result", task_id=ident,
            owner_user_id="owner", attempt_id=lease.attempt_id, config_id="search-synthetic",
            config_version=1, logical_slot=1, state="succeeded", wire_count=1,
            retry_count=0, result_count=10, continuation_mode="with_evidence"))
        original = session.get(PlanningTaskStateRow, ident).process_json
    result = read_process(env.planning, ident, "owner")["process"]
    assert result["search"]["status"] == "succeeded" and result["search"]["result_count"] == 10
    assert result["search"]["sources"] == []
    with env.db.session() as session:
        assert session.get(PlanningTaskStateRow, ident).process_json == original
        session.get(ServiceConfigRow, "search-synthetic").is_active = False
    assert read_process(env.planning, ident, "owner")["process"]["search"]["status"] == "pending"


def test_real_pipeline_persists_search_and_stages_without_event_content(env):
    ident, lease, state = make_task(env)
    env.planning.search_adapter_factory = lambda *args: BoundSearch(evidence())
    assert run_pipeline(env, ident, lease, state)["draft"]["applied"]
    data = read_process(env.planning, ident, "owner")["process"]
    assert data["task_status"] == "succeeded" and data["stage"] == "save"
    assert data["search"]["query"] == "数据库基础"
    assert data["search"]["sources"][0]["used"] is True
    assert {s["key"] for s in data["steps"] if s["status"] == "succeeded"} == {
        "intake", "coverage", "retrieve", "search", "generate", "validate", "evaluate", "save"}
    assert data["sequence"] >= 6 and data["attempt"] == lease.attempt
    with env.db.session() as sess:
        encoded = str([r.__dict__ for r in sess.scalars(select(TaskEventRow))])
        assert "Synthetic database source" not in encoded
        assert "https://example.com/database" not in encoded
        assert "Synthetic SQL evidence" not in encoded


def test_thinking_is_bounded_hidden_when_disabled_and_sequence_monotonic(env):
    ident, lease, _ = make_task(env)
    writer = ProcessWriter(env.planning, lease)
    writer.stage("generate")
    before = read_process(env.planning, ident, "owner")["process"]["sequence"]
    writer.thinking("合成思考内容" + "x" * MAX_THINKING)
    writer.finish("succeeded")
    data = read_process(env.planning, ident, "owner")["process"]
    assert data["sequence"] > before and len(data["thinking"]["text"]) == MAX_THINKING
    assert data["thinking"]["truncated"]
    writer.bind_source(SimpleNamespace(reasoning_settings={"show_summary": False}))
    writer.thinking("should never be stored", reset=True)
    writer.finish("succeeded")
    hidden = read_process(env.planning, ident, "owner")["process"]["thinking"]
    assert hidden["text"] == "" and not hidden["available"]
    with env.db.session() as sess:
        assert "should never be stored" not in sess.get(PlanningTaskStateRow, ident).process_json


@pytest.mark.parametrize("change", ["cancel", "generation", "attempt", "abandon", "deleted", "owner"])
def test_stale_cancelled_deleted_and_foreign_processes_are_inaccessible(env, change):
    ident, lease, state = make_task(env)
    writer = ProcessWriter(env.planning, lease)
    writer.stage("generate")
    writer.thinking("synthetic private thinking")
    writer.flush(force=True)
    with env.db.session() as sess:
        task = sess.get(LearningTaskRow, ident)
        row = sess.get(PlanningTaskStateRow, ident)
        if change == "cancel": task.cancel_requested = True
        if change == "generation": task.generation += 1
        if change == "attempt": task.attempt += 1
        if change == "abandon": row.abandoned_at = utcnow()
        if change == "deleted": sess.get(CourseRow, state.target_id).deleted_at = utcnow()
    if change in ("owner", "deleted"):
        with pytest.raises(PlanningError):
            read_process(env.planning, ident, "other" if change == "owner" else "owner")
    else:
        assert read_process(env.planning, ident, "owner") == {"process": None}
    if change != "owner":
        with pytest.raises((PlanningError, LeaseError)):
            writer.thinking("must not publish", reset=True)


@pytest.mark.parametrize("change", ["version", "inactive", "owner", "scope"])
def test_config_change_hides_process_and_blocks_late_publication(env, change):
    from backend.slice1.models import ServiceConfigRow, SearchConfigOwnerRow
    SearchConfigOwnerRow.__table__.create(env.db.engine, checkfirst=True)
    ServiceConfigRow.__table__.create(env.db.engine, checkfirst=True)
    ident, lease, _ = make_task(env)
    with env.db.session() as sess:
        sess.add(ServiceConfigRow(config_id="synthetic-config", owner_user_id="owner", owner_scope="user",
            kind="content_model", protocol="openai", endpoint="https://example.invalid", model_name="gpt-5"))
    writer = ProcessWriter(env.planning, lease)
    writer.bind_source(SimpleNamespace(config_id="synthetic-config", config_version=1))
    writer.stage("generate")
    assert read_process(env.planning, ident, "owner")["process"]
    with env.db.session() as sess:
        config = sess.get(ServiceConfigRow, "synthetic-config")
        if change == "version": config.config_version += 1
        if change == "inactive": config.is_active = False
        if change == "owner": config.owner_user_id = "other"
        if change == "scope": config.owner_scope = "admin"
    assert read_process(env.planning, ident, "owner") == {"process": None}
    with pytest.raises(PlanningError):
        writer.thinking("must not be published", reset=True)


@pytest.mark.parametrize("change", ["revision", "deleted", "owner", "scope"])
def test_material_change_hides_process_and_blocks_late_publication(env, change):
    from backend.materials.models import MaterialRow
    MaterialRow.__table__.create(env.db.engine, checkfirst=True)
    ident, lease, _ = make_task(env)
    with env.db.session() as sess:
        sess.add(MaterialRow(material_id="synthetic-material", owner_user_id="owner", kind="txt",
            filename_hash="0" * 64, source_bytes_sha256="1" * 64))
        sess.get(PlanningTaskStateRow, ident).material_ids_json = '["synthetic-material"]'
    writer = ProcessWriter(env.planning, lease)
    writer.stage("generate")
    with env.db.session() as sess:
        material = sess.get(MaterialRow, "synthetic-material")
        if change == "revision": material.source_bytes_sha256 = "2" * 64
        if change == "deleted": material.deleted_at = utcnow()
        if change == "owner": material.owner_user_id = "other"
        if change == "scope": sess.get(PlanningTaskStateRow, ident).material_ids_json = "[]"
    assert read_process(env.planning, ident, "owner") == {"process": None}
    with pytest.raises(PlanningError):
        writer.thinking("must not be published", reset=True)


def test_model_guard_is_scoped_to_stream_and_not_reused_after_draft_transition(env):
    ident, lease, _ = make_task(env)
    writer = ProcessWriter(env.planning, lease)
    active = [True]
    def check():
        if not active[0]:
            raise PlanningError("TASK_CANCELLED", "synthetic changed model domain", 409)
    bound = writer.bind_deps(AgentDeps(check_state=check))
    bound.on_process("synthetic thinking")
    active[0] = False
    with pytest.raises(PlanningError):
        bound.on_process("must not publish", reset=True)
    # A normal pipeline save changes draft_id after the model run has ended.
    with env.db.session() as sess:
        sess.get(PlanningTaskStateRow, ident).draft_id = "synthetic-draft"
    writer.stage("save")
    writer.finish("succeeded")
    assert read_process(env.planning, ident, "owner")["process"]["task_status"] == "succeeded"


def test_streaming_native_thinking_keeps_tool_output_and_excludes_signature():
    class Candidate(BaseModel):
        value: int
    chunks, resets = [], []
    async def stream(messages, info):
        yield {0: DeltaThinkingPart(content="合成分析", signature="opaque-secret")}
        yield {0: DeltaThinkingPart(content="与比较")}
        yield {1: DeltaToolCall(name=info.output_tools[0].name, json_args='{"value":7}')}
    def on_process(text, *, reset=False):
        resets.append(reset)
        chunks.append(text)
    model = FunctionModel(stream_function=stream, profile={"supports_json_schema_output": False,
        "supports_tools": True, "supports_text_output": True})
    model.capability_status = {"streaming": "available"}
    run = asyncio.run(execute_agent(model, Candidate,
        GenerationInput.from_messages([{"role": "user", "content": "synthetic"}]),
        AgentDeps(on_process=on_process)))
    assert run.mode == "tool" and run.payload.value == 7
    assert "".join(chunks) == "合成分析与比较" and resets[0]


def test_nonstreaming_model_returns_thinking_without_forcing_stream():
    from pydantic_ai.messages import ModelResponse, ThinkingPart, TextPart
    chunks = []
    def respond(messages, info):
        return ModelResponse(parts=[ThinkingPart(content="synthetic thought", signature="opaque-secret"),
            TextPart(content="synthetic answer")])
    model = FunctionModel(function=respond)
    model.capability_status = {"streaming": "unavailable"}
    result = asyncio.run(execute_agent(model, str,
        GenerationInput.from_messages([{"role": "user", "content": "synthetic"}]),
        AgentDeps(on_process=lambda text, **kwargs: chunks.append(text))))
    assert result.payload == "synthetic answer" and "".join(chunks) == "synthetic thought"
