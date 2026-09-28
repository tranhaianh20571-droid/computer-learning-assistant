"""ADR-022：OTel 装配（OTLP/HTTP → 自托管 Langfuse）与内容 span 辅助。

- `OTEL_ENABLED` 与 `OTEL_EXPORTER_OTLP_ENDPOINT` 同时设置才装配，否则完全
  no-op：不建 TracerProvider、不影响业务导入路径与测试。
- Langfuse 认证：OTLP 端点 Basic 头（public key:secret key），密钥只从环境
  变量读取，不写日志、不进事件。
- 导出失败由 SDK 批处理器丢弃并计入 exporter 统计；核心任务永不因导出失败
  而失败（规范 §1 降级语义不变）。
- 内容 span 遵循 GenAI 语义约定属性（`gen_ai.prompt` / `gen_ai.completion`），
  仅当 `CONTENT_TRACE_ENABLED` 开启时由 `content_trace` 调用（ADR-022 内容三项）。
"""

from __future__ import annotations

import base64
import os
from typing import Optional

_tracer = None
_configured_endpoint: Optional[str] = None

DEFAULT_LANGFUSE_OTLP_PATH = "/api/public/otel/v1/traces"


def _basic_auth_header(public_key: str, secret_key: str) -> str:
    raw = f"{public_key}:{secret_key}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def resolve_otlp_endpoint(endpoint: str) -> str:
    """补全 Langfuse 默认 OTLP 路径：允许只配 base URL。"""
    if endpoint.rstrip("/").endswith("/traces"):
        return endpoint
    return endpoint.rstrip("/") + DEFAULT_LANGFUSE_OTLP_PATH


def configure_otel(
    *,
    enabled: bool,
    endpoint: Optional[str],
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    service_name: str = "learning-assistant",
) -> bool:
    """装配全局 TracerProvider；未启用/缺端点时保持 no-op 并返回 False。

    重复调用是幂等的：仅在尚未装配时创建 provider（测试用 reset_otel 复位）。
    """
    global _tracer, _configured_endpoint
    if _tracer is not None:
        return True
    if not enabled or not endpoint:
        return False
    # 延迟导入：未启用 OTel 的环境（含部分测试）不支付导入成本
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    headers = None
    if public_key and secret_key:
        headers = {"authorization": _basic_auth_header(public_key, secret_key)}
    exporter = OTLPSpanExporter(
        endpoint=resolve_otlp_endpoint(endpoint), headers=headers, timeout=5
    )
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("learning-assistant.content")
    _configured_endpoint = endpoint
    return True


def get_tracer():
    """返回已装配 tracer；未装配时返回 None（调用方需自行跳过 span）。"""
    return _tracer


def otel_configured() -> bool:
    return _tracer is not None


def record_model_span(
    name: str,
    *,
    task_id: Optional[str] = None,
    mode: Optional[str] = None,
    prompt: Optional[str] = None,
    answer: Optional[str] = None,
    user_question: Optional[str] = None,
) -> None:
    """内容 span：GenAI 语义约定属性。未装配 tracer 或超长时静默跳过。"""
    tracer = _tracer
    if tracer is None:
        return
    try:
        with tracer.start_as_current_span(name) as span:
            if task_id:
                span.set_attribute("task.id", task_id)
            if mode:
                span.set_attribute("gen_ai.operation.mode", mode)
            if prompt:
                span.set_attribute("gen_ai.prompt", prompt[:16384])
            if answer:
                span.set_attribute("gen_ai.completion", answer[:16384])
            if user_question:
                span.set_attribute("user.question", user_question[:16384])
    except Exception:  # noqa: BLE001 — span 失败绝不影响业务
        pass


def reset_otel() -> None:
    """测试复位：关闭 provider 并清空全局 tracer。"""
    global _tracer, _configured_endpoint
    if _tracer is not None:
        try:
            from opentelemetry import trace

            provider = trace.get_tracer_provider()
            shutdown = getattr(provider, "shutdown", None)
            if callable(shutdown):
                shutdown()
        except Exception:  # noqa: BLE001
            pass
    _tracer = None
    _configured_endpoint = None


def otel_from_env(service_name: str = "learning-assistant") -> bool:
    """按环境变量装配（main 启动入口用）。"""
    return configure_otel(
        enabled=os.environ.get("OTEL_ENABLED", "").lower() in ("1", "true", "yes"),
        endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None,
        public_key=os.environ.get("LANGFUSE_PUBLIC_KEY") or None,
        secret_key=os.environ.get("LANGFUSE_SECRET_KEY") or None,
        service_name=service_name,
    )


__all__ = [
    "configure_otel", "otel_from_env", "get_tracer", "otel_configured",
    "record_model_span", "reset_otel", "resolve_otlp_endpoint",
]
