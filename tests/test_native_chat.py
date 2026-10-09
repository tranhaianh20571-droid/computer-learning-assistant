"""Isolated native SDK chat: context, retries, streaming and transaction fences."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from sqlalchemy import select

from backend.agents.task_binding import AgentBudgetRow
from backend.logging_audit.db import Database
from backend.logging_audit.models import TaskEventRow, TaskStateRow
from backend.logging_audit.repositories import PgTaskEventStore
from backend.planning.chat import ChatService, _StreamBuffer
from backend.planning.errors import PlanningError, WaitStateSignal
from backend.planning.models import ConversationRow, ContextManifestRow, MessageRow, PlanningTaskStateRow
from backend.planning.retrieval import PlanningRetrievalService, RetrievalResult
from backend.prompts.service import PromptBindingRow, PromptService
from backend.tasks.service import LearningTaskRow, LeaseError, TaskAttemptRow, TaskService


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = Database(f"sqlite+pysqlite:///{(tmp_path / 'chat.sqlite').as_posix()}")
    for model in (LearningTaskRow, AgentBudgetRow, TaskAttemptRow, TaskStateRow, TaskEventRow,
                  ConversationRow, MessageRow, PlanningTaskStateRow, ContextManifestRow, PromptBindingRow):
        model.__table__.create(db.engine)
    events = PgTaskEventStore(db)
    tasks = TaskService(db, events)
    prompts = PromptService(db)
    monkeypatch.setattr(prompts, "load_local_template", lambda name: {"template": "synthetic bound prompt"})
    retrieval = PlanningRetrievalService(db)
    monkeypatch.setattr(retrieval, "assemble", lambda **kwargs: RetrievalResult(
        refs=[{"chunk_id": "chunk_1", "source": "material"}],
        excerpts=[{"chunk_id": "chunk_1", "text": "synthetic excerpt body"}]))
    state = {"model": TestModel(custom_output_text="synthetic answer"), "search": None, "factory_tasks": []}
    def factory(actor, task_id):
        with db.session() as sess:
            assert sess.get(LearningTaskRow, task_id).status in ("leased", "running")
        state["factory_tasks"].append(task_id)
        return state["model"]
    service = ChatService(db, tasks, events, prompts, retrieval, factory,
        lambda actor, task_id: state["search"])
    conv_id = service.create_conversation(owner_user_id="user_owner")["conversation_id"]
    try:
        yield service, db, tasks, state, conv_id
    finally:
        db.dispose()


def send(env, key="first", **kwargs):
    service, _, _, _, conv_id = env
    return service.send_message(conv_id, actor_id="user_owner", content="synthetic question",
        idempotency_key=key, **kwargs)


def stream(env, key="first", **kwargs):
    service, _, _, _, conv_id = env
    return service.stream_answer(conv_id, actor_id="user_owner", content="synthetic question",
        idempotency_key=key, use_search=kwargs.get("use_search", False))


def answers(env):
    service, _, _, _, conv_id = env
    return [m for m in service.get_conversation(conv_id, actor_id="user_owner")["messages"]
            if m["role"] == "assistant"]


def test_chat_validator_accepts_native_context_and_trims_output():
    from backend.planning.chat import _validate_chat_output
    from pydantic_ai import ModelRetry

    assert _validate_chat_output(object(), "  synthetic answer  ") == "synthetic answer"
    with pytest.raises(ModelRetry):
        _validate_chat_output(object(), "   ")


def test_testmodel_nonstream_and_idempotency(env):
    result = send(env)
    assert result["message"]["content"] == "synthetic answer"
    assert send(env)["deduped"] is True
    assert len(answers(env)) == 1
    service, db, _, state, _ = env
    assert len(state["factory_tasks"]) == 1
    with db.session() as sess:
        task = sess.get(LearningTaskRow, result["task_id"])
        assert task.status == "succeeded"
        assert sess.get(AgentBudgetRow, result["task_id"]).logical_requests == 1
    assert service.events.get_events(result["task_id"])[-1].status == "succeeded"


def test_actual_prompt_history_and_context_reach_native_model(env):
    _, db, _, state, _ = env
    observed = []
    def model(messages, info):
        observed.append((messages, info.instructions))
        return ModelResponse(parts=[TextPart("native answer")])
    state["model"] = FunctionModel(model)
    first = send(env)
    second = send(env, key="second", use_search=True)
    assert "synthetic bound prompt" in observed[1][1]
    body = str(observed[1][0])
    assert "native answer" in body and "synthetic question" in body
    assert "synthetic excerpt body" in body
    with db.session() as sess:
        manifest = sess.get(ContextManifestRow, second["manifest_id"])
        assert json.loads(manifest.history_range_json)["kept"] == 2
        assert "synthetic excerpt body" not in manifest.retrieval_refs_json
        binding = sess.scalar(select(PromptBindingRow).where(PromptBindingRow.task_id == first["task_id"]))
        assert binding.template_snapshot == "synthetic bound prompt"


@pytest.mark.parametrize("invalid", ["   ", "x" * 4001])
def test_native_output_validator_retries_instead_of_truncating(env, invalid):
    _, _, _, state, _ = env
    calls = []
    def model(messages, info):
        calls.append(messages)
        return ModelResponse(parts=[TextPart(invalid if len(calls) == 1 else "valid answer")])
    state["model"] = FunctionModel(model)
    assert send(env)["message"]["content"] == "valid answer"
    assert len(calls) == 2
    assert "Chat answer must contain" in str(calls[1])
    assert len(answers(env)) == 1


def test_invalid_exhaustion_is_failed_without_answer(env):
    _, db, _, state, _ = env
    state["model"] = TestModel(custom_output_text="x" * 4001)
    with pytest.raises(PlanningError) as exc:
        send(env)
    assert exc.value.code == "OUTPUT_INVALID"
    assert answers(env) == []
    with db.session() as sess:
        assert sess.get(LearningTaskRow, state["factory_tasks"][0]).status == "failed"


def test_stream_resets_retried_preview_and_saves_only_complete_result(env):
    _, _, _, state, _ = env
    calls = []
    async def model(messages, info):
        calls.append(messages)
        yield "x" * 4001 if len(calls) == 1 else " valid answer "
    state["model"] = FunctionModel(stream_function=model)
    _, events = stream(env)
    emitted = list(events)
    assert len(calls) == 2
    assert {"type": "delta", "text": "", "reset": True} in emitted
    assert emitted[-1]["type"] == "done"
    assert emitted[-1]["content"] == "valid answer"
    assert [m["content"] for m in answers(env)] == ["valid answer"]


def test_disconnect_preserves_only_interrupted_and_wakes_producer(env):
    _, db, _, state, _ = env
    stopped = threading.Event()
    async def model(messages, info):
        try:
            for _ in range(200):
                yield "fragment "
        finally:
            stopped.set()
    state["model"] = FunctionModel(stream_function=model)
    meta, events = stream(env)
    assert next(events)["type"] == "delta"
    assert answers(env) == []
    events.close()
    assert stopped.wait(2)
    assert len(answers(env)) == 1
    assert answers(env)[0]["status"] == "interrupted"
    with db.session() as sess:
        assert sess.get(LearningTaskRow, meta["task_id"]).status == "failed"


@pytest.mark.parametrize("mutation", ["generation", "deleted_at", "owner_user_id"])
def test_changed_conversation_fences_complete_stream(env, mutation):
    _, db, _, state, conv_id = env
    async def model(messages, info):
        yield "native answer"
    state["model"] = FunctionModel(stream_function=model)
    _, events = stream(env)
    assert next(events)["type"] == "delta"
    with db.session() as sess:
        row = sess.get(ConversationRow, conv_id)
        setattr(row, mutation, {"generation": 9, "deleted_at": datetime.now(timezone.utc),
            "owner_user_id": "other_owner"}[mutation])
    tail = list(events)
    assert tail[-1] == {"type": "error", "error_code": "STALE_RESULT_DISCARDED"}
    with db.session() as sess:
        assert sess.scalars(select(MessageRow).where(MessageRow.role == "assistant")).all() == []


def test_cancelled_task_never_publishes(env):
    _, _, tasks, state, _ = env
    async def model(messages, info):
        yield "native answer"
    state["model"] = FunctionModel(stream_function=model)
    meta, events = stream(env)
    assert next(events)["type"] == "delta"
    tasks.request_cancel(meta["task_id"], actor_id="user_owner")
    assert list(events)[-1]["type"] == "error"
    assert answers(env) == []


def test_message_and_success_event_share_transaction(env, monkeypatch):
    service, db, _, _, _ = env
    original = service.events.record
    def fail_success(*args, **kwargs):
        original(*args, **kwargs)
        if kwargs["status"] == "succeeded":
            raise RuntimeError("synthetic_success_event_failure")
    monkeypatch.setattr(service.events, "record", fail_success)
    with pytest.raises(PlanningError):
        send(env)
    assert answers(env) == []
    with db.session() as sess:
        assert sess.scalars(select(TaskEventRow).where(TaskEventRow.status == "succeeded")).all() == []


def test_search_wait_and_offline_resume_without_generation_increment(env):
    service, db, _, state, conv_id = env
    class FailingSearch:
        def search(self, *args, **kwargs):
            raise TimeoutError("synthetic timeout")
    state["search"] = FailingSearch()
    with pytest.raises(WaitStateSignal):
        send(env, use_search=True)
    assert state["factory_tasks"] == []
    with db.session() as sess:
        assert sess.scalar(select(LearningTaskRow)).status == "partial"
    result = service.resume_message(conv_id, actor_id="user_owner", decision="offline_continue")
    assert result["message"]["content"] == "synthetic answer"
    assert result["generation"] == 1
    assert service.resume_message(conv_id, actor_id="user_owner", decision="offline_continue")["deduped"]


def test_bounded_buffer_close_wakes_blocked_producer():
    buffer = _StreamBuffer(limit=1)
    buffer.put(("delta", "first"))
    stopped = threading.Event()
    def produce():
        try:
            buffer.put(("delta", "second"))
        except LeaseError:
            stopped.set()
    thread = threading.Thread(target=produce, daemon=True)
    thread.start()
    buffer.close()
    thread.join(timeout=2)
    assert stopped.is_set() and not thread.is_alive()
    assert len(buffer.items) == 1
