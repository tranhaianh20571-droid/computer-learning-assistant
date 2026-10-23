"""Real SDK streaming with synthetic model bytes and isolated private checkpoints."""
import asyncio
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from backend.characters.dialogue_stream import DialoguePreviewWriter, dialogue_snapshot
from backend.characters.distillation_models import (
    BuildRequest,
    CharacterBuildRow,
    CharacterBuildStageRow,
)
from backend.characters.distillation_router import create_distillation_router
from backend.planning.errors import PlanningError
from backend.slice1.errors import Slice1Error
from backend.tasks.service import LearningTaskRow, LeaseError
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from test_persona_workflows import builder

pytest_plugins = ("test_character_cards",)


def create_turn(service, owner):
    return service.create(owner, BuildRequest(
        mode="ai", dialogue=True, topic="Synthetic",
        message="Create a synthetic tutor.", request_key=uuid.uuid4().hex, confirmed=True))


def leased_turn(env, monkeypatch):
    cards, owner, service, _ = builder(env, monkeypatch, pipeline_version=7)
    created = create_turn(service, owner)
    root = service.tasks.claim(created["task_id"], lease_owner="synthetic-root", lease_seconds=120)
    with cards.db.session() as sess:
        task = service.tasks.create_task(
            owner_user_id=owner, kind="character_build_stage", subject_id="synthetic-reply",
            idempotency_key=uuid.uuid4().hex, stage="generate", lane="interactive", session=sess)
        sess.add(CharacterBuildStageRow(
            task_id=task["task_id"], build_id=created["build_id"],
            stage_key="dialogue_reply", input_hash="synthetic"))
    child = service.tasks.claim(task["task_id"], lease_owner="synthetic-reply", lease_seconds=120)
    return cards, owner, service, created, root, child


def test_preview_parses_only_reply_fields_and_resets_attempt(env, monkeypatch):
    cards, owner, service, created, root, child = leased_turn(env, monkeypatch)
    writer = DialoguePreviewWriter(service, root, child)
    writer('{"reply":"Synthetic \\u4f60\\u597d')
    snapshot = dialogue_snapshot(service, owner, created["build_id"])
    assert snapshot["reply"] == "Synthetic 你好"
    assert snapshot["question"] == ""
    assert "skill" not in snapshot and "raw" not in snapshot
    writer("", reset=True)
    assert dialogue_snapshot(service, owner, created["build_id"])["reply"] == ""
    writer('{"reply":"Replacement","question":"One question?","skill":"private draft"}')
    assert dialogue_snapshot(service, owner, created["build_id"])["reply"] == "Replacement"
    with cards.db.session() as sess:
        state = json.loads(sess.get(CharacterBuildStageRow, child.task_id).state_json)
    assert "private draft" not in json.dumps(state)
    with pytest.raises(Slice1Error):
        dialogue_snapshot(service, "synthetic-other", created["build_id"])


@pytest.mark.parametrize("fence", ["cancel", "generation", "attempt", "config", "delete"])
def test_preview_rejects_late_writes_and_stale_or_foreign_reads(env, monkeypatch, fence):
    cards, owner, service, created, root, child = leased_turn(env, monkeypatch)
    writer = DialoguePreviewWriter(service, root, child)
    writer('{"reply":"First')
    with cards.db.session() as sess:
        task = sess.get(LearningTaskRow, root.task_id)
        if fence == "cancel":
            task.cancel_requested = True
            task.status = "cancelled"
        elif fence == "generation":
            task.generation += 1
        elif fence == "attempt":
            sess.get(LearningTaskRow, child.task_id).attempt_id = "synthetic-new-attempt"
        elif fence == "config":
            from backend.slice1.models import ServiceConfigRow
            row = sess.get(CharacterBuildRow, created["build_id"])
            sess.get(ServiceConfigRow, row.config_id).config_version += 1
        else:
            from backend.characters.models import CharacterCardRow, utcnow
            sess.get(CharacterCardRow, created["card_id"]).deleted_at = utcnow()
    with pytest.raises((LeaseError, PlanningError)):
        writer("", reset=True)
    if fence in ("config", "delete"):
        with pytest.raises((PlanningError, Slice1Error)):
            dialogue_snapshot(service, owner, created["build_id"])
    else:
        snapshot = dialogue_snapshot(service, owner, created["build_id"])
        assert not snapshot.get("reply")
        assert snapshot["event"] == ("done" if fence == "cancel" else "preview")


@pytest.mark.parametrize("streaming", [True, False])
def test_native_dialogue_outputs_preview_before_completion_and_saves_validated_reply(
    env, monkeypatch, streaming,
):
    cards, owner, service, _ = builder(env, monkeypatch, pipeline_version=7)
    base_factory = service.model_factory
    entered, release = threading.Event(), threading.Event()
    reply = {
        "reply": "Synthetic incremental response.", "question": "Which habit?",
        "profile": {"name": "Synthetic tutor"},
        "skill": "# Synthetic\n" + "Check conditions and boundaries.\n" * 14,
        "thinking_mechanism": "Check conditions.", "decision_method": "Ask for reasons.",
        "expression_style": "Brief.", "knowledge_boundary": "Unknown stays unknown.",
        "ready": False,
    }

    def factory(current, task_id):
        bound = base_factory(current, task_id)
        with cards.db.session() as sess:
            key = sess.get(CharacterBuildStageRow, task_id).stage_key
        value = ({"topic": "Synthetic", "subject_kind": "original", "queries": []}
                 if key == "dialogue_plan" else reply)
        encoded = json.dumps(value)

        def generate(messages, info):
            return ModelResponse(parts=[TextPart(encoded)])

        async def streamed(messages, info):
            if key == "dialogue_reply":
                yield '{"reply":"Synthetic incremental'
                entered.set()
                assert await asyncio.to_thread(release.wait, 10)
                yield encoded[len('{"reply":"Synthetic incremental'):]
            else:
                yield encoded

        # Compact encoding gives the held prefix exactly the same byte boundary.
        if key == "dialogue_reply":
            encoded = json.dumps(value, separators=(",", ":"))
        model = FunctionModel(generate, stream_function=streamed)
        model.supports_text_stream = streaming
        model.agent_deps = bound.agent_deps
        return model

    monkeypatch.setattr(service, "model_factory", factory)
    created = create_turn(service, owner)
    with ThreadPoolExecutor(max_workers=1) as pool:
        job = pool.submit(service.run, created["build_id"])
        try:
            if streaming:
                assert entered.wait(10)
                snapshot = dialogue_snapshot(service, owner, created["build_id"])
                assert snapshot["reply"] == "Synthetic incremental"
                assert snapshot["event"] == "preview"
                assert service.get(owner, created["build_id"])["result"] == {}
        finally:
            release.set()
        receipt = job.result(timeout=20)
    assert receipt["status"] == "succeeded", receipt
    final = service.get(owner, created["build_id"])
    assert final["result"]["dialogue"]["reply"] == reply["reply"]
    assert dialogue_snapshot(service, owner, created["build_id"])["event"] == "done"


def stream_app(service, owner):
    app = FastAPI()
    for kind in (Slice1Error, PlanningError):
        app.add_exception_handler(kind, lambda request, exc: JSONResponse(
            {"error_code": exc.code}, status_code=exc.status))
    app.include_router(create_distillation_router(
        service, lambda request: SimpleNamespace(user_id=request.headers.get("x-owner", owner))))
    return app


def test_content_endpoint_rechecks_owner_and_exposes_only_snapshot_until_terminal(env, monkeypatch):
    _, owner, service, created, root, child = leased_turn(env, monkeypatch)
    DialoguePreviewWriter(service, root, child)('{"reply":"Synthetic first chunk')
    real_snapshot = dialogue_snapshot
    reads = []

    def completing_snapshot(*args):
        snapshot = real_snapshot(*args)
        reads.append(snapshot)
        if len(reads) == 2:
            service.action(owner, created["build_id"], "cancel")
            return real_snapshot(*args)
        return snapshot

    monkeypatch.setattr("backend.characters.dialogue_stream.dialogue_snapshot", completing_snapshot)
    with TestClient(stream_app(service, owner)) as client:
        url = f"/api/character-builds/{created['build_id']}/content-events"
        assert client.get(url, headers={"x-owner": "synthetic-other"}).status_code == 404
        response = client.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "event: preview" in response.text and "Synthetic first chunk" in response.text
    assert "event: done" in response.text
    assert "source_text" not in response.text and "skill" not in response.text
