"""Real ASGI disconnects must cancel native chat workers and release task leases."""
import asyncio
import threading

import anyio
import pytest
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from backend.logging_audit.models import TaskEventRow
from backend.planning.router import _ChatStreamingResponse
from backend.tasks.service import LearningTaskRow
from sqlalchemy import select
from test_native_chat import env, stream, send, answers  # noqa: F401


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
@pytest.mark.parametrize("preview", ["synthetic fragment", "x" * 4001, "   "])
def test_http_disconnect_cancels_blocked_chat_and_allows_next_turn(env, spec_version, preview):
    _, db, _, state, _ = env
    stopped = threading.Event()

    async def model(messages, info):
        try:
            yield preview
            await asyncio.sleep(.05)
            for _ in range(200):
                yield "late fragment"
        finally:
            stopped.set()

    state["model"] = FunctionModel(stream_function=model)
    meta, events = stream(env)
    response = _ChatStreamingResponse(meta, events)
    emitted = []

    async def exercise():
        disconnect = anyio.Event()

        async def send_body(message):
            if message["type"] == "http.response.body":
                body = message.get("body", b"")
                emitted.append(body)
                if body.startswith(b"event: delta"):
                    if spec_version == "2.4":
                        raise OSError("synthetic disconnect")
                    disconnect.set()

        async def receive():
            await disconnect.wait()
            # Let the next() worker block before notifying the response.
            await anyio.sleep(.01)
            return {"type": "http.disconnect"}

        scope = {"type": "http", "asgi": {"spec_version": spec_version}}
        with anyio.fail_after(3):
            if spec_version == "2.4":
                from starlette.requests import ClientDisconnect
                with pytest.raises(ClientDisconnect):
                    await response(scope, receive, send_body)
            else:
                await response(scope, receive, send_body)

    asyncio.run(exercise())
    assert stopped.wait(1)
    assert events.buffer.cancelled.is_set()
    assert not any(thread.name == f"chat-{meta['task_id']}" for thread in threading.enumerate())
    assert not any(b"event: done" in body for body in emitted)
    saved = answers(env)
    if preview.strip() and len(preview) <= 4000:
        assert len(saved) == 1 and saved[0]["status"] == "interrupted"
        assert saved[0]["content"] == preview
    else:
        assert saved == []
    with db.session() as sess:
        task = sess.get(LearningTaskRow, meta["task_id"])
        assert task.status == "failed"
        assert task.lease_owner is None and task.lease_token is None
        assert not sess.scalar(select(TaskEventRow.task_id).where(
            TaskEventRow.task_id == meta["task_id"], TaskEventRow.status == "succeeded"))
    state["model"] = TestModel(custom_output_text="synthetic next answer")
    assert send(env, key="next-turn")["message"]["content"] == "synthetic next answer"


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
def test_complete_asgi_stream_preserves_done_contract(env, spec_version):
    _, db, _, state, _ = env

    async def model(messages, info):
        yield "synthetic complete answer"

    state["model"] = FunctionModel(stream_function=model)
    meta, events = stream(env)
    response = _ChatStreamingResponse(meta, events)
    emitted = []

    async def exercise():
        async def send_body(message):
            if message["type"] == "http.response.body":
                emitted.append(message.get("body", b""))

        async def receive():
            await anyio.sleep_forever()

        with anyio.fail_after(3):
            await response({"type": "http", "asgi": {"spec_version": spec_version}}, receive, send_body)

    asyncio.run(exercise())
    assert emitted[0].startswith(b"event: meta")
    assert any(body.startswith(b"event: done") for body in emitted)
    assert [message["status"] for message in answers(env)] == ["completed"]
    with db.session() as sess:
        assert sess.get(LearningTaskRow, meta["task_id"]).status == "succeeded"
