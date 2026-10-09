"""Privacy boundary for native PydanticAI instrumentation (SDK 2.51+).

Only operational telemetry crosses this boundary. ContentTrace remains independent.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping

STAGES = frozenset({
    "queue", "auth", "domain_validate", "commit_fence", "transaction", "publish",
    "task_attempt", "receive", "validate", "parse", "convert", "external_call",
    "generate", "save", "cleanup", "model_request", "output_validate", "retrieval",
    "search", "tts", "restore", "cancel", "resume", "other",
})
ENUM_ATTRIBUTES = {
    "stage": STAGES,
    "status": frozenset({"started", "succeeded", "partial", "failed", "retry_scheduled", "cancelled", "stale_discarded"}),
    "outcome": frozenset({"succeeded", "failed", "cancelled", "stale_discarded"}),
    "mode": frozenset({"native", "tool", "prompted", "text", "stream", "offline"}),
    "gen_ai.operation.name": frozenset({"chat", "invoke_agent", "execute_tool", "text_completion", "embeddings"}),
    "gen_ai.token.type": frozenset({"input", "output"}),
    "gen_ai.provider.name": frozenset({"openai", "anthropic", "google", "google-gla", "google-vertex", "gemini", "minimax", "deepseek", "test", "function"}),
}
METRIC_NAMES = frozenset({
    "gen_ai.client.token.usage", "operation.cost", "gen_ai.client.operation.time_to_first_chunk",
    "business.stage.duration", "business.stage.calls", "model.call.duration", "model.call.count",
})
OPAQUE_KEYS = frozenset({"task.id", "request.id", "task_id", "request_id", "config_id"})
NUMERIC_KEYS = frozenset({"attempt", "duration_ms", "first_chunk_ms", "retry_count", "request_count", "usage_missing", "operation.cost", "gen_ai.client.operation.time_to_first_chunk"})
METRIC_UNITS = {
    "gen_ai.client.token.usage": "{token}", "operation.cost": "{USD}",
    "gen_ai.client.operation.time_to_first_chunk": "s",
    "business.stage.duration": "ms", "model.call.duration": "ms",
    "business.stage.calls": "", "model.call.count": "",
}
_NUMERIC_USAGE = re.compile(r"^gen_ai\.(?:aggregated_usage|usage)\.(?:input_tokens|output_tokens|cache_creation\.input_tokens|cache_read\.input_tokens|details\.(?:reasoning_tokens|input_audio_tokens|output_audio_tokens))$")
# Application IDs must be generated opaque values, never business titles or paths.
_OPAQUE = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|(?:task|req|config)_[0-9a-f]{8,64})$")


def safe_attributes(attributes: Mapping | None, *, metrics: bool = False) -> dict:
    result = {}
    for key, value in (attributes or {}).items():
        if key in ENUM_ATTRIBUTES and isinstance(value, str) and value in ENUM_ATTRIBUTES[key]:
            result[key] = value
        elif key == "error_code" and isinstance(value, str):
            from .contracts import ERROR_CODES
            if value in ERROR_CODES:
                result[key] = value
        elif not metrics and key in OPAQUE_KEYS and isinstance(value, str) and _OPAQUE.fullmatch(value):
            result[key] = value
        elif not metrics and (key in NUMERIC_KEYS or _NUMERIC_USAGE.fullmatch(key)):
            if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                result[key] = value
    return result


def safe_span_name(attributes: Mapping | None) -> str:
    attrs = safe_attributes(attributes)
    stage = attrs.get("stage")
    if stage:
        return "business." + stage
    operation = attrs.get("gen_ai.operation.name")
    return {"invoke_agent": "agent.run", "chat": "model.request", "execute_tool": "tool.execute"}.get(operation, "operation")


class SafeInstrument:
    def __init__(self, instrument):
        self._instrument = instrument

    def record(self, amount, attributes=None, context=None):
        self._measure("record", amount, attributes, context)

    def add(self, amount, attributes=None, context=None):
        self._measure("add", amount, attributes, context)

    def set(self, amount, attributes=None, context=None):
        self._measure("set", amount, attributes, context)

    def _measure(self, method, amount, attributes, context):
        from .exporter import HEALTH
        try:
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
                HEALTH.add("measurement_failures")
                return
            getattr(self._instrument, method)(amount, safe_attributes(attributes, metrics=True), context)
        except Exception:
            HEALTH.add("measurement_failures")


class SafeMeter:
    def __init__(self, meter):
        self._meter = meter

    def _create(self, method, name, unit="", description="", **kwargs):
        # Reject arbitrary names/descriptions/units as a covert content channel.
        from opentelemetry.metrics import NoOpMeter
        if name not in METRIC_NAMES:
            return SafeInstrument(getattr(NoOpMeter("safe"), method)("discarded"))
        return SafeInstrument(getattr(self._meter, method)(name, unit=METRIC_UNITS[name], description="", **kwargs))

    def create_histogram(self, name, *args, **kwargs):
        return self._create("create_histogram", name, *args, **kwargs)

    def create_counter(self, name, *args, **kwargs):
        return self._create("create_counter", name, *args, **kwargs)

    def create_up_down_counter(self, name, *args, **kwargs):
        return self._create("create_up_down_counter", name, *args, **kwargs)

    def create_gauge(self, name, *args, **kwargs):
        return self._create("create_gauge", name, *args, **kwargs)


class SafeMeterProvider:
    """Filter native measurements before aggregation; model/user/task/URL labels disappear."""
    def __init__(self, provider):
        self._provider = provider

    def get_meter(self, name, version=None, schema_url=None, attributes=None):
        scope = "pydantic-ai" if name == "pydantic-ai" else "learning-assistant"
        return SafeMeter(self._provider.get_meter(scope))


def native_instrumentation(runtime=None):
    """Return the SDK capability; add once to Agent.capabilities, never instrument_all.

    Uses application-owned providers, without changing process-wide OTel globals.
    No configured runtime means explicit no-op providers, never an unsafe global fallback.
    """
    from pydantic_ai.capabilities import Instrumentation
    from pydantic_ai.models.instrumented import InstrumentationSettings
    from opentelemetry.trace import NoOpTracerProvider
    from opentelemetry.metrics import NoOpMeterProvider
    if runtime is None:
        from .otel_setup import get_runtime
        runtime = get_runtime()
    return Instrumentation(settings=InstrumentationSettings(
        tracer_provider=runtime.tracer_provider if runtime else NoOpTracerProvider(),
        meter_provider=runtime.meter_provider if runtime else SafeMeterProvider(NoOpMeterProvider()),
        include_content=False,
        include_binary_content=False,
        include_model_request_parameters=False,
        use_aggregated_usage_attribute_names=True,
        version=5,
    ))
