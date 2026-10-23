"""SDK log isolation: synthetic credentials and offline transports only."""
from __future__ import annotations

import importlib
import io
import json
import logging
from pathlib import Path
import socket
import subprocess
import sys
import uuid

import pytest

from backend.logging_audit.logger import StructuredLogger
from backend.logging_audit.sdk_logging import (
    SDK_LOGGER_NAMESPACES, install_sdk_logging_safety,
)

ROOT = Path(__file__).resolve().parents[1]
SECRET = "SYNTHETIC_SDK_SECRET_c185"
BODY = "SYNTHETIC_PRIVATE_BODY_d24c"
URL = "https://sdk.example.test/v1/generate?key=" + SECRET


@pytest.fixture(autouse=True)
def isolated_logging(monkeypatch):
    # Restore the entrypoint's previous state after each test, including when
    # another scoped module already installed the production guard.
    monkeypatch.setattr(logging.Logger, "handle", logging.Logger.handle)
    def forbidden(*args, **kwargs):
        raise AssertionError("SDK log tests must not open network connections")
    monkeypatch.setattr(socket, "create_connection", forbidden)


def emit_unsafe_records(logger):
    logger.debug("headers=%s body=%s", {"Authorization": "Bearer " + SECRET}, BODY)
    logger.info('HTTP Request: POST %s "HTTP/1.1 200 OK"', URL)
    logger.warning("provider response %s", BODY)
    try:
        raise RuntimeError(URL + " " + BODY)
    except RuntimeError:
        logger.exception("provider failed %s", URL, stack_info=True)
    logger.critical("provider exception %s", SECRET)


@pytest.mark.parametrize("namespace", SDK_LOGGER_NAMESPACES)
@pytest.mark.parametrize("child", [False, True], ids=["root", "late-child"])
def test_raw_sdk_records_never_reach_any_handler(namespace, child, caplog, monkeypatch):
    install_sdk_logging_safety()
    # Create the child AFTER installation; it has its own handler and DEBUG
    # level, so neither a parent filter nor raising the parent level suffices.
    name = namespace + (".late_" + uuid.uuid4().hex if child else "")
    logger = logging.getLogger(name)
    direct = io.StringIO()
    handler = logging.StreamHandler(direct)
    monkeypatch.setattr(logger, "handlers", [handler])
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "propagate", True)
    monkeypatch.setattr(logger, "level", logging.DEBUG)
    caplog.set_level(logging.DEBUG)
    emit_unsafe_records(logger)
    assert direct.getvalue() == ""
    assert not [record for record in caplog.records if record.name == name]
    assert SECRET not in caplog.text
    assert BODY not in caplog.text


def test_preexisting_child_with_nonpropagating_handler_is_blocked(monkeypatch):
    logger = logging.getLogger("google_genai.models")
    output = io.StringIO()
    monkeypatch.setattr(logger, "handlers", [logging.StreamHandler(output)])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "level", logging.DEBUG)
    monkeypatch.setattr(logger, "disabled", False)
    install_sdk_logging_safety()
    emit_unsafe_records(logger)
    assert output.getvalue() == ""


def test_discard_occurs_before_formatting_or_exception_handler(monkeypatch):
    class NeverFormat:
        def __str__(self):
            raise AssertionError("SDK payload must never be formatted")
    class NeverHandle(logging.Handler):
        def emit(self, record):
            raise AssertionError("SDK record must never reach a handler")
    logger = logging.getLogger("httpcore.connection")
    monkeypatch.setattr(logger, "handlers", [NeverHandle()])
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "level", logging.DEBUG)
    install_sdk_logging_safety()
    logger.error("%s", NeverFormat(), exc_info=True)


def test_idempotent_install_preserves_original_application_dispatch(monkeypatch, caplog):
    install_sdk_logging_safety()
    first = logging.Logger.handle
    install_sdk_logging_safety()
    assert logging.Logger.handle is first
    # Exact namespace matching: app-owned and similarly named loggers survive.
    caplog.set_level(logging.DEBUG)
    for name in ("app.learning", "app.httpx", "httpx_application", "google_internal"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "disabled", False)
        logger.warning("controlled application marker")
        assert any(record.name == name for record in caplog.records)
    output = io.StringIO()
    StructuredLogger("worker", "test", stream=output).log(
        "task.stage.failed", level="ERROR", task_id="task_synthetic",
        stage="generate", status="failed", error_code="MODEL_CONNECTION_ERROR",
        duration_ms=1,
    )
    record = json.loads(output.getvalue())
    assert record["event"] == "task.stage.failed"
    assert record["error_code"] == "MODEL_CONNECTION_ERROR"
    assert record["schema_version"] == 1
    assert record["service"] == "worker"


@pytest.mark.parametrize("module_name", ["httpx", "httpx2"])
def test_actual_http_clients_mock_transport_does_not_log_url(module_name, caplog, monkeypatch):
    sdk = importlib.import_module(module_name)
    logger = logging.getLogger(module_name)
    direct = io.StringIO()
    monkeypatch.setattr(logger, "handlers", [logging.StreamHandler(direct)])
    monkeypatch.setattr(logger, "level", logging.DEBUG)
    monkeypatch.setattr(logger, "disabled", False)
    caplog.set_level(logging.DEBUG)
    install_sdk_logging_safety()
    seen = []
    def transport(request):
        seen.append(request)
        return sdk.Response(200, json={"ok": True})
    with sdk.Client(transport=sdk.MockTransport(transport)) as client:
        response = client.post(URL, headers={"Authorization": "Bearer " + SECRET}, content=BODY)
    assert response.json() == {"ok": True}
    assert len(seen) == 1
    assert SECRET in str(seen[0].url)
    assert direct.getvalue() == ""
    assert not [record for record in caplog.records if record.name == module_name]


def test_production_entrypoint_installs_guard_before_routes_and_on_reassembly():
    # A new interpreter proves actual app.main assembly, not a mocked installer
    # or a preinstalled test-process guard. Database environment is inherited
    # from run_native_regression's owned random schema; no worker is started.
    script = r'''
import builtins
import io
import json
import logging
import os
import socket
os.environ["FASTAPI_ENV"] = "development"
os.environ["OTEL_ENABLED"] = "0"
def no_network(*args, **kwargs):
    raise AssertionError("No SDK network allowed in production logging test")
socket.create_connection = no_network
captured = io.StringIO()
logging.basicConfig(level=logging.DEBUG, stream=captured, force=True)
from fastapi import FastAPI
original_import = builtins.__import__
route_probe = []
def probing_import(name, *args, **kwargs):
    if name == "app.api.main":
        route_probe.append(name)
        logging.getLogger("google_genai.models").error("SYNTHETIC_EARLY_SECRET")
    return original_import(name, *args, **kwargs)
builtins.__import__ = probing_import
from app.main import app
builtins.__import__ = original_import
assert isinstance(app, FastAPI)
assert route_probe
assert "SYNTHETIC_EARLY_SECRET" not in captured.getvalue()
from app.logging_audit.sdk_logging import install_sdk_logging_safety
from app.logging_audit.logger import StructuredLogger
from app.logging_audit.trace import TraceContext, trace_context
from app.assembler import create_app
first = logging.Logger.handle
install_sdk_logging_safety()
assert logging.Logger.handle is first
# Reassembly reinstalls safety if a logging integration restored stdlib handle.
logging.Logger.handle = first.__wrapped__
assert isinstance(create_app(), FastAPI)
for namespace in ("httpx", "httpx2", "httpcore", "google_genai", "google", "pydantic_ai", "pydanticai", "openai", "anthropic"):
    logger = logging.getLogger(namespace + ".after_assembly")
    logger.disabled = False
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(logging.StreamHandler(captured))
    try:
        raise RuntimeError("https://sdk.example.test/?key=SYNTHETIC_PRODUCTION_SECRET")
    except RuntimeError:
        logger.exception("SYNTHETIC_PRODUCTION_BODY", stack_info=True)
    logger.debug("Authorization: Bearer SYNTHETIC_PRODUCTION_SECRET")
assert "SYNTHETIC_PRODUCTION_SECRET" not in captured.getvalue()
assert "SYNTHETIC_PRODUCTION_BODY" not in captured.getvalue()
# Controlled events stay writable: they need a live trace context, like production entrypoints.
with trace_context(TraceContext(trace_id="c" * 32, span_id="d" * 16)):
    StructuredLogger("api", "test").log("task.accepted", task_id="task_synthetic", attempt=1, stage="generate")
print("SDK_LOGGING_PRODUCTION_OK")
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=ROOT / "server" / "backend",
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SDK_LOGGING_PRODUCTION_OK" in result.stdout
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert any(event["event"] == "task.accepted" and event["service"] == "api" for event in events)
    for secret in ("SYNTHETIC_EARLY_SECRET", "SYNTHETIC_PRODUCTION_SECRET", "SYNTHETIC_PRODUCTION_BODY"):
        assert secret not in result.stdout + result.stderr
