"""Worker lifecycle composition without supplier calls or background threads."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.lifecycle import worker_lifespan
from test_course_library import database, env  # noqa: F401


def worker(name, events, *, fail_start=False, fail_close=False):
    def start():
        events.append("start:" + name)
        if fail_start:
            raise RuntimeError("synthetic startup failure")
    def close():
        events.append("close:" + name)
        if fail_close:
            raise RuntimeError("synthetic shutdown failure")
    return SimpleNamespace(start=start, close=close)


@pytest.mark.parametrize("failure", ["none", "start", "close", "body"])
def test_workers_unwind_in_reverse_order_on_all_exit_paths(failure):
    events = []
    @asynccontextmanager
    async def parent(app):
        events.append("parent:start")
        try:
            yield
        finally:
            events.append("parent:close")
    app = FastAPI(lifespan=worker_lifespan([
        worker("first", events, fail_close=failure == "close"),
        worker("second", events, fail_start=failure == "start")], parent=parent))
    def run():
        with TestClient(app):
            events.append("body")
            if failure == "body":
                raise RuntimeError("synthetic request failure")
    if failure == "none":
        run()
    else:
        with pytest.raises(RuntimeError):
            run()
    assert events == ["parent:start", "start:first", "start:second"] + (
        [] if failure == "start" else ["body"]) + ["close:second", "close:first", "parent:close"]


def test_slice_workers_start_and_close_once_without_deprecated_events(env, monkeypatch):
    from backend.slice3_app import create_slice3_app
    app = create_slice3_app(env[1])
    state, events = app.state.slice3, []
    workers = [state.features.cover_background, state.attachments,
        state.source_imports, state.distillation, state.resources]
    for index, item in enumerate(workers):
        stub = worker(str(index), events)
        monkeypatch.setattr(item, "start", stub.start)
        monkeypatch.setattr(item, "close", stub.close)
    assert app.router.on_startup == app.router.on_shutdown == []
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert events == [f"start:{index}" for index in range(5)]
    assert events[5:] == [f"close:{index}" for index in reversed(range(5))]


def test_production_mount_enters_child_lifespan_and_starts_every_worker_once(monkeypatch):
    from app.assembler import create_app
    from app.logging_audit import otel_setup
    from starlette.routing import Mount
    events = []
    monkeypatch.setattr(otel_setup, "shutdown_otel", lambda **kwargs: events.append("telemetry:close"))
    app = create_app()
    child = next(route.app for route in app.routes if isinstance(route, Mount))
    state, base = child.state.slice3, child.state.slice1
    workers = [state.teaching.background, state.features.cover_background,
        state.attachments, state.source_imports, state.distillation, state.resources,
        state.memory, base.character_media, state.planning.background, base.market_receiver]
    for index, item in enumerate(workers):
        stub = worker(str(index), events)
        monkeypatch.setattr(item, "start", stub.start)
        monkeypatch.setattr(item, "close", stub.close)
    assert app.router.on_startup == app.router.on_shutdown == []
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert events == [f"start:{index}" for index in range(10)]
    assert events[10:] == [f"close:{index}" for index in reversed(range(10))] + ["telemetry:close"]
