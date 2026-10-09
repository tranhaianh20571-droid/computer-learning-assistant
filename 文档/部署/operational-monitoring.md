# Operational Monitoring Deployment

## Scope And Privacy Boundary

The application-owned `ObservabilityRuntime` creates one trace provider and one
meter provider without replacing process-wide OpenTelemetry globals. Native
PydanticAI 2.51 instrumentation is installed as a capability on the existing Agent,
not via `instrument_all` or a second monitoring Agent. All three content flags are
false: `include_content`, `include_binary_content`, and
`include_model_request_parameters`.

Operational OTLP is separate from the controlled legacy content channel. This
change neither migrates historical content nor configures/deletes a Langfuse
project. `CONTENT_TRACE_ENABLED`, legacy Langfuse credentials, and
`OTEL_EXPORTER_OTLP_ENDPOINT` do not opt operational monitoring in.

A span processor sanitizes before its export queue. Span names become controlled
operation/stage names. Only controlled enum/error dimensions, generated opaque
application IDs, and nonnegative finite usage/timing values survive. Prompts,
answers, tool names/definitions/arguments/results, arbitrary metadata, model names,
URLs, exception events/messages, status descriptions, resource host information,
scope metadata, baggage and tracestate are not exported. Trace parent/link IDs and
the sampled flag are retained; link attributes are discarded. Persist only the
W3C header returned by `capture_task_traceparent()` for durable task correlation.
`business_span(parent_traceparent=..., linked_traceparent=...)` accepts ID-only
parents/links; an explicit durable parent takes precedence over an active span.

Metrics are filtered before aggregation. Task/request/config IDs, model names,
URLs and arbitrary labels cannot increase metric series cardinality. Metric names,
descriptions and units are controlled. Exemplars are disabled, and exporter-side
sanitization is an additional boundary. No raw transport response/URL/exception is
logged; fixed local health counters replace upstream OTLP transport logs.

## Deployment Opt-In

Inject credentials through deployment secrets, never source files or test fixtures.
Use the Logfire write token as the OTLP `authorization` header value. Do not put it
in an endpoint URL. Use the base OTLP endpoint for the correct Logfire region/project.

```text
OTEL_ENABLED=true
LOGFIRE_OTLP_ENDPOINT=https://logfire-api.pydantic.dev
LOGFIRE_TOKEN=<deployment-injected-write-token>
ENVIRONMENT=production
```

The base endpoint resolves to `/v1/traces` and `/v1/metrics`. A `/v1` suffix or an
existing signal suffix is also accepted. Optional
`LOGFIRE_OTLP_TRACES_ENDPOINT` and `LOGFIRE_OTLP_METRICS_ENDPOINT` override signals;
without a base endpoint both overrides must be present. Endpoint userinfo, query
strings and fragments are rejected. No region is hardcoded in application code.
HTTP endpoints exist for isolated collector deployments; cloud traffic should use
HTTPS. If opt-in/endpoint is absent, native telemetry uses explicit no-op providers,
not an unrelated global provider. Configuration failures degrade without exposing
credentials or raw errors.

Each application process owns its own runtime. Configure once at startup and call
`shutdown_otel(timeout_millis=2000)` at shutdown, or `runtime.close(...)` for an
explicitly owned instance. `force_flush` and `close` return booleans indicating
whether the deadline was met. Shutdown is idempotent and retains its first result.
A false result must not be reported as successful delivery.

## Bounded Failure Behavior

Defaults: span FIFO 256, batches 32, one daemon export worker, OTLP HTTP timeout
1 second, metric export interval 10 seconds, flush/close deadline 2 seconds. Span
completion never waits for transport I/O. Queue saturation drops telemetry and
increments local counters. Sanitized spans only are retained in the queue.

A transport that ignores its timeout can hold one export worker/batch plus the
bounded queue, not spawn a thread per span. Metrics use one SDK periodic worker.
Lifecycle helpers are bounded to one in-flight helper per provider. A caller's
flush/close deadline returns false even when a transport ignores its timeout;
Python cannot forcibly stop that third-party I/O thread. A stuck worker may remain
daemonized until transport recovery/process exit. Business exceptions continue to
propagate unchanged, while monitoring failures never become business failures.

`observability_health_snapshot()` is a local diagnostic API, not a newly exposed
HTTP endpoint. It includes configuration/queue state and counters for dropped
spans/metrics, export/configuration/measurement/lifecycle failures and lifecycle
timeouts. Deployment monitoring should poll the existing trusted health surface
and alert on sustained increments; no alert provisioning is claimed here.

## Platform Lifecycle Correlation

The existing HTTP entry points establish `business.receive` using validated
`traceparent` IDs only. Inbound baggage, tracestate, URLs, authorization headers
and request bodies are not attached. Unknown or malformed trace headers produce
fresh local correlation. The sampled bit is canonicalized; no other remote flags
are retained by the export boundary. Explicit no-op instrumentation preserves
valid parent IDs rather than replacing correlation with an invalid span.

`TaskService.create_task` wraps the existing insertion transaction in
`business.queue`; its active IDs are captured on the durable task budget row.
Idempotent creation does not overwrite that stored correlation. Current lease
validation emits `business.commit_fence` once, including when it owns the session.
Result submission emits `business.save` with nested `business.transaction`; the
existing same-session callback is `business.publish`. No SQL writes, authorization
checks, lease fences, commits or exception semantics move between these scopes.
A stale fence never invokes the publish callback. These business spans complement,
not duplicate, the SDK agent/model/tool spans.

The SQLite/ASGI contract suite uses real task rows and in-memory exporters to prove
parent relationships, durable binding, idempotence, stale suppression, atomic
rollback, exception identity, failed tracer startup and private-header exclusion.
An additional stalled-export case verifies that task commit completes without
waiting for the worker. Standalone ingress is exercised directly without sockets.
Recorded focused result: 96 tests passed in 6.01 seconds, including 11 new task
observability cases and the existing lease/transaction/privacy contracts.

```powershell
.\server\.venv\Scripts\python.exe -m pytest tests/test_native_task_observability.py tests/test_native_task_contracts.py tests/test_native_observability.py -q --tb=short
```

## Validation Evidence

Run from the repository root using the existing server environment:

```powershell
.\server\.venv\Scripts\python.exe -m pytest tests/test_native_observability.py -q --disable-warnings
```

The isolated contract suite uses real SDK `Agent(TestModel/FunctionModel)` and
in-memory span/metric readers. It blocks database initialization and application
socket connections (including loopback); only the Windows asyncio socketpair
self-pipe is permitted. It checks both exported objects and encoded OTLP protobuf
bytes for a synthetic private sentinel. No production data, credentials, database,
collector or network endpoint are used.

Evidence covers native agent/model/tool spans; exact two-request input/output
usage (18/8), cache-write/read and reasoning fields; token histogram counts/sums;
stream first-chunk timing; exception suppression; preaggregation label merging;
ID-only durable parents/links; queue saturation and bounded flush/close; metric
export failure; bounded repeated flush helpers; explicit no-op fallback; credential
transport-only wiring; and transport log suppression.

SDK 2.51 emits `gen_ai.client.operation.time_to_first_chunk` (seconds), not a
`time_to_first_token` alias. It measures the first streamed chunk and is not a
supplier-reported token latency. `gen_ai.client.token.usage` is a histogram with
`{token}` units and separate input/output points; each response contributes once,
while agent span totals use `gen_ai.aggregated_usage.*` to avoid double counting.
Cache fields are `gen_ai.usage.cache_creation.input_tokens` and
`gen_ai.usage.cache_read.input_tokens`. Business duration histograms use milliseconds
and business counters use controlled stage/outcome/error dimensions only.

Recorded local result: 18 observability tests passed. The affected regression plus
test-only synthetic model helper suite passed 65 tests in 3.37 seconds with:

```powershell
.\server\.venv\Scripts\python.exe -m pytest tests/test_native_model_support.py tests/test_native_observability.py tests/test_t02_logger.py tests/test_t07_model_call_metrics.py tests/test_t08_content_trace.py -q --disable-warnings
```

This is focused automated validation, not full application regression or live
Logfire acceptance. A live deployment must separately verify write-token scope,
TLS/proxy/region connectivity, dashboards, retention/access control, delivery and
shutdown under its process manager using synthetic data only. Content migration,
remote deletion and real supplier calls are not tested or performed.
