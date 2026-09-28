"""T08：内容 trace 通道（ADR-022）——事件路由、脱敏边界、开关、OTel 装配与删除联动。

覆盖：
- `model.call.content` 只进内容输出口（content_stream），主口/聚合指标零内容；
- 内容字段 sanitize：危险控制字符清除、换行保留（JSON 单行）、16 KiB 截断；
- 凭据类字段名（prompt/response/api_key 等）在任何通道仍被 FORBIDDEN 拒绝；
- `CONTENT_TRACE_ENABLED` 关：emit 完全 no-op；开：事件携带 model_prompt/model_answer；
- otel_setup：端点补全、未启用 no-op、record_model_span 未装配不抛；
- observability_deletion_refs：登记→核销（合成客户端），失败重试上限。
全部合成数据（假提示词/假回答/假 trace_id），无真实凭据；DB 用例沿用测试库。
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "support"))

from backend.logging_audit.contracts import (  # noqa: E402
    ContractError,
    LogEventType,
    validate_event_name,
)
from backend.logging_audit.content_trace import (  # noqa: E402
    ContentTrace,
    content_trace_enabled,
    get_content_trace,
    reset_content_trace,
)
from backend.logging_audit.logger import MemoryLogStream, StructuredLogger  # noqa: E402
from backend.logging_audit.otel_setup import (  # noqa: E402
    configure_otel,
    record_model_span,
    reset_otel,
    resolve_otlp_endpoint,
)


def _make_logger() -> tuple[StructuredLogger, MemoryLogStream, MemoryLogStream]:
    main = MemoryLogStream()
    content = MemoryLogStream()
    logger = StructuredLogger(service="api", stream=main, content_stream=content)
    return logger, main, content


# ---------------------------------------------------------------------------
# 事件注册与路由
# ---------------------------------------------------------------------------

def test_content_event_registered_with_content_type():
    spec = validate_event_name("model.call.content")
    assert LogEventType.CONTENT in spec.types
    assert "model_prompt" in spec.optional_fields
    assert "model_answer" in spec.optional_fields
    assert "user_question" in spec.optional_fields


def test_content_event_routes_to_content_stream_only():
    logger, main, content = _make_logger()
    logger.log(
        "model.call.content", level="INFO", stage="generate", status="succeeded",
        model_prompt="合成提示词", model_answer="合成回答",
    )
    assert len(content.records()) == 1
    assert len(main.records()) == 0
    rec = content.records()[0]
    assert rec["model_prompt"] == "合成提示词"
    assert rec["model_answer"] == "合成回答"


def test_non_content_event_never_routes_to_content_stream():
    logger, main, content = _make_logger()
    logger.log(
        "model.call.completed", level="INFO", stage="generate",
        status="succeeded", duration_ms=5,
    )
    assert len(main.records()) == 1
    assert len(content.records()) == 0
    assert "model_prompt" not in main.records()[0]


def test_content_stream_unset_falls_back_to_main():
    main = MemoryLogStream()
    logger = StructuredLogger(service="api", stream=main)
    logger.log(
        "model.call.content", level="INFO", stage="generate", status="succeeded",
        model_answer="x",
    )
    assert len(main.records()) == 1


# ---------------------------------------------------------------------------
# 脱敏边界（内容三项放行；凭据类仍然 fail-closed）
# ---------------------------------------------------------------------------

def test_content_fields_keep_newlines_strip_control_chars_and_truncate():
    logger, _, content = _make_logger()
    prompt = "line1\nline2\tend" + "\x00\x1b[31m" + "y" * 20000
    logger.log(
        "model.call.content", level="INFO", stage="generate", status="succeeded",
        model_prompt=prompt,
    )
    line = content.lines()[0]
    assert "\n" not in line  # JSON 单行输出
    rec = json.loads(line)
    assert "line1" in rec["model_prompt"] and "line2" in rec["model_prompt"]
    assert "\x00" not in rec["model_prompt"] and "\x1b" not in rec["model_prompt"]
    assert len(rec["model_prompt"]) == 16384  # 截断 + 省略号


def test_forbidden_credential_fields_rejected_even_on_content_event():
    logger, _, _ = _make_logger()
    for bad in ("prompt", "response", "api_key", "authorization", "password"):
        with pytest.raises(ContractError):
            logger.log(
                "model.call.content", level="INFO", stage="generate",
                status="succeeded", **{bad: "x"},
            )


def test_content_fields_rejected_on_non_content_event():
    """内容字段名合法但只属于内容事件；普通事件带上即契约错误（未知组合防御）。"""
    logger, main, _ = _make_logger()
    with pytest.raises(ContractError):
        logger.log(
            "model.call.completed", level="INFO", stage="generate",
            status="succeeded", duration_ms=1, model_prompt="x",
        )
    assert len(main.records()) == 0


# ---------------------------------------------------------------------------
# 开关与发射器
# ---------------------------------------------------------------------------

def test_content_trace_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CONTENT_TRACE_ENABLED", raising=False)
    reset_content_trace()
    assert content_trace_enabled() is False
    main = MemoryLogStream()
    content = MemoryLogStream()
    trace = ContentTrace()
    trace.attach_logger(StructuredLogger(service="api", stream=main, content_stream=content))
    trace.emit("generate", "succeeded", prompt_messages=[{"role": "user", "content": "q"}], answer="a")
    assert main.records() == [] and content.records() == []
    assert trace.emit_errors == 0


def test_content_trace_enabled_emits_event(monkeypatch):
    monkeypatch.setenv("CONTENT_TRACE_ENABLED", "1")
    reset_content_trace()
    assert content_trace_enabled() is True
    main = MemoryLogStream()
    content = MemoryLogStream()
    trace = get_content_trace()
    trace.attach_logger(StructuredLogger(service="api", stream=main, content_stream=content))
    trace.emit(
        "generate", "succeeded", mode="tool", task_id="task_synthetic_1",
        user_question="合成用户问题",
        prompt_messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}],
        answer={"steps": []},
    )
    assert len(content.records()) == 1 and len(main.records()) == 0
    rec = content.records()[0]
    assert rec["event"] == "model.call.content"
    assert rec["task_id"] == "task_synthetic_1"
    assert "sys" in rec["model_prompt"] and "合成用户问题" in rec["user_question"]
    assert rec["model_answer"].startswith("{")
    reset_content_trace()


# ---------------------------------------------------------------------------
# OTel 装配
# ---------------------------------------------------------------------------

def test_resolve_otlp_endpoint_appends_default_path():
    assert resolve_otlp_endpoint("http://langfuse:3000").endswith("/api/public/otel/v1/traces")
    full = "https://obs.example/api/public/otel/v1/traces"
    assert resolve_otlp_endpoint(full) == full


def test_configure_otel_disabled_is_noop():
    reset_otel()
    assert configure_otel(enabled=False, endpoint="http://x:3000") is False
    from backend.logging_audit.otel_setup import otel_configured

    assert otel_configured() is False
    record_model_span("model.call", prompt="x")  # 未装配：no-op 不抛


def test_configure_otel_enabled_with_in_memory_exporter():
    reset_otel()
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    provider = TracerProvider()
    mem = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(mem))
    trace.set_tracer_provider(provider)
    from backend.logging_audit import otel_setup as mod

    mod._tracer = trace.get_tracer("t")
    try:
        record_model_span("model.call", task_id="task_synthetic_2", prompt="p", answer="a")
        spans = mem.get_finished_spans()
        assert len(spans) == 1
        attrs = spans[0].attributes
        assert attrs["gen_ai.prompt"] == "p"
        assert attrs["gen_ai.completion"] == "a"
        assert attrs["task.id"] == "task_synthetic_2"
    finally:
        reset_otel()


# ---------------------------------------------------------------------------
# 删除联动（需要测试库 PostgreSQL；沿用套件 LEARNING_DATABASE_URL 约定）
# ---------------------------------------------------------------------------

class _OkClient:
    configured = True

    def delete_trace(self, trace_id):
        return True, ""


class _FailClient:
    configured = True

    def delete_trace(self, trace_id):
        return False, "http_500"


def _make_deletion_service(client):
    from backend.cleanup.models import ObservabilityDeletionRefRow
    from backend.cleanup.observability_deletion import ObservabilityDeletionService
    from backend.logging_audit.db import Database
    from backend.logging_audit.models import Base

    db = Database()
    ObservabilityDeletionRefRow.__table__.create(db.engine, checkfirst=True)
    main = MemoryLogStream()
    logger = StructuredLogger(service="api", stream=main)
    return ObservabilityDeletionService(db, logger=logger, client=client), db


def test_deletion_register_and_process_roundtrip():
    import sqlalchemy as sa

    svc, db = _make_deletion_service(_OkClient())
    owner = f"u_{uuid.uuid4().hex[:8]}"
    ref_ids = svc.register_deletion(
        owner_user_id=owner, resource_type="material",
        resource_id="mat_synth_1", trace_ids=["trace_synth_a", "trace_synth_b"],
    )
    assert len(ref_ids) == 2
    result = svc.process_pending()
    assert result["completed"] == 2 and result["failed"] == 0
    from backend.cleanup.models import ObservabilityDeletionRefRow

    with db.session() as sess:
        rows = sess.execute(
            sa.select(ObservabilityDeletionRefRow).where(
                ObservabilityDeletionRefRow.owner_user_id == owner
            )
        ).scalars().all()
        assert all(r.status == "completed" for r in rows)


def test_deletion_failure_marks_failed_after_attempts():
    svc, db = _make_deletion_service(_FailClient())
    resource_id = f"mat_synth_{uuid.uuid4().hex[:8]}"
    svc.register_deletion(
        owner_user_id="u_synth_fail", resource_type="material",
        resource_id=resource_id, trace_ids=["trace_synth_c"],
    )
    from backend.cleanup.observability_deletion import MAX_ATTEMPTS

    for _ in range(MAX_ATTEMPTS):
        result = svc.process_pending()
    assert result["failed"] >= 1
    import sqlalchemy as sa

    from backend.cleanup.models import ObservabilityDeletionRefRow

    with db.session() as sess:
        row = sess.execute(
            sa.select(ObservabilityDeletionRefRow).where(
                ObservabilityDeletionRefRow.resource_id == resource_id
            )
        ).scalars().first()
        assert row is not None
        assert row.status == "failed" and row.reason == "http_500"
