"""T07：模型调用聚合指标（model.call.* 事件 + 进程内计数器，ADR-020 配套）。

覆盖：注册表事件与字段、成功/失败计数与事件字段、供应商失败映射、
prompted 重试的逐次调用计数、run_chat 出口、TTS 出口契约、
泄漏检查（提示词与回答标记不得出现在日志行）、发射失败降级、快照与重置。
全部合成适配器与合成数据，无真实凭据；teaching 服务的 TTS 插桩
沿用同一 record 契约（stage="tts"），由切片 4 既有测试覆盖行为不回退。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "support"))

from backend.logging_audit import metrics as model_metrics  # noqa: E402
from backend.logging_audit.contracts import (  # noqa: E402
    build_registry_document,
    validate_event_name,
)
from backend.logging_audit.logger import MemoryLogStream, StructuredLogger  # noqa: E402
from backend.planning.errors import PlanningError  # noqa: E402
from backend.planning.model_runner import run_chat, run_structured  # noqa: E402


class _Payload(BaseModel):
    name: str


class _Adapter:
    """合成内容模型适配器：按 capability_status 探测，按脚本返回输出。"""

    kind = "content_model"

    def __init__(self, *, status: dict | None = None, outputs: list | None = None,
                 error: Exception | None = None) -> None:
        self.capability_status = status or {
            "text": "available", "tool_call": "available", "json_schema": "available",
        }
        self._outputs = list(outputs or [])
        self._error = error

    def probe(self, capability: str):
        return SimpleNamespace(
            capability=capability,
            state=self.capability_status.get(capability, "unavailable"),
        )

    def generate(self, messages, **options):
        if self._error is not None:
            raise self._error
        if self._outputs:
            return self._outputs.pop(0)
        return {"content": json.dumps({"name": "ok"})}


@pytest.fixture()
def log_stream():
    model_metrics.reset_model_call_metrics()
    stream = MemoryLogStream()
    model_metrics.get_model_call_metrics().attach_logger(
        StructuredLogger("test", "test", stream=stream)
    )
    yield stream
    model_metrics.get_model_call_metrics().attach_logger(None)
    model_metrics.reset_model_call_metrics()


def _entries(**kw):
    snap = model_metrics.model_call_metrics_snapshot()
    return [c for c in snap["calls"] if all(c.get(k) == v for k, v in kw.items())]


def test_registry_contains_model_call_events():
    for name in ("model.call.completed", "model.call.failed"):
        spec = validate_event_name(name)
        assert "mode" in spec.optional_fields
        assert spec.user_visible is False


def test_native_success_records_event_and_counter(log_stream):
    result = run_structured(
        _Adapter(), _Payload,
        [{"role": "user", "content": "PROMPT-MARKER-ABC"}],
        modes=["native"], schema={"type": "object"},
    )
    assert result.mode == "native"
    entries = _entries(stage="generate", mode="native", outcome="succeeded")
    assert len(entries) == 1 and entries[0]["count"] == 1
    assert entries[0]["duration_ms"] >= 0
    events = [r for r in log_stream.records() if r["event"] == "model.call.completed"]
    assert len(events) == 1
    assert events[0]["stage"] == "generate"
    assert events[0]["status"] == "succeeded"
    assert events[0]["mode"] == "native"
    assert events[0]["duration_ms"] >= 0
    # 泄漏检查：提示词标记与回答结构不得进入日志行
    assert "PROMPT-MARKER-ABC" not in log_stream.getvalue()
    assert '"name"' not in log_stream.getvalue()


def test_vendor_failure_maps_to_controlled_code(log_stream):
    exc = Exception("synthetic boom")
    exc.code = "timeout"
    adapter = _Adapter(error=exc)
    with pytest.raises(PlanningError) as exc_info:
        run_structured(adapter, _Payload, [{"role": "user", "content": "x"}], modes=["native"])
    assert exc_info.value.code == "MODEL_TIMEOUT"
    entries = _entries(stage="generate", mode="native", outcome="failed",
                       error_code="MODEL_TIMEOUT")
    assert len(entries) == 1
    events = [r for r in log_stream.records() if r["event"] == "model.call.failed"]
    assert len(events) == 1
    assert events[0]["error_code"] == "MODEL_TIMEOUT"
    assert events[0]["level"] == "WARN"
    assert "boom" not in log_stream.getvalue()


def test_prompted_retries_counted_per_call(log_stream):
    bad = {"content": "not-json-at-all"}
    good = {"content": "```json\n" + json.dumps({"name": "ok"}) + "\n```"}
    adapter = _Adapter(status={"text": "available"}, outputs=[bad, bad, good])
    result = run_structured(adapter, _Payload, [{"role": "user", "content": "x"}])
    assert result.mode == "prompted"
    assert result.attempts == 3
    # 三次供应商调用都成功返回（内容不可用是业务校验，不是调用失败）
    entries = _entries(stage="generate", mode="prompted", outcome="succeeded")
    assert len(entries) == 1 and entries[0]["count"] == 3


def test_run_chat_records_metrics(log_stream):
    adapter = _Adapter(outputs=[{"content": "  合成回答  "}])
    text = run_chat(adapter, [{"role": "user", "content": "CHAT-MARKER-XYZ"}], max_len=100)
    assert text == "合成回答"
    entries = _entries(stage="generate", outcome="succeeded")
    assert len(entries) == 1 and entries[0]["mode"] is None
    assert "CHAT-MARKER-XYZ" not in log_stream.getvalue()


def test_tts_exit_contract(log_stream):
    model_metrics.get_model_call_metrics().record(
        stage="tts", outcome="failed", duration_ms=12, error_code="TTS_TIMEOUT",
    )
    entries = _entries(stage="tts", outcome="failed", error_code="TTS_TIMEOUT")
    assert len(entries) == 1 and entries[0]["count"] == 1
    events = [r for r in log_stream.records() if r["event"] == "model.call.failed"]
    assert len(events) == 1 and events[0]["stage"] == "tts"


def test_emit_failure_degrades_without_raising():
    model_metrics.reset_model_call_metrics()

    class _BrokenStream:
        def log(self, *args, **kwargs):
            raise RuntimeError("synthetic stream broken")

    metrics = model_metrics.get_model_call_metrics()
    metrics.attach_logger(_BrokenStream())  # type: ignore[arg-type]
    try:
        metrics.record(stage="generate", outcome="succeeded", duration_ms=1)
        snap = model_metrics.model_call_metrics_snapshot()
        assert snap["emit_errors"] == 1
        assert snap["calls"][0]["count"] == 1  # 计数不受发射失败影响
    finally:
        metrics.attach_logger(None)
        model_metrics.reset_model_call_metrics()


def test_reset_clears_state(log_stream):
    model_metrics.get_model_call_metrics().record(
        stage="generate", outcome="succeeded", duration_ms=1,
    )
    model_metrics.reset_model_call_metrics()
    assert model_metrics.model_call_metrics_snapshot() == {"calls": [], "emit_errors": 0}


def test_registry_document_exports_new_entries():
    doc = build_registry_document()
    assert "model.call.completed" in doc["events"]
    assert "model.call.failed" in doc["events"]
    assert "mode" in doc["allowed_fields"]
