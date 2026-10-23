"""产品级唯一入口装配（重构方案 O-A / D1 修复）。

`create_app()` 固定顺序装配：Sentry → OTel（ADR-022，未配置 no-op）→
模板工程路由（/api/v1：login/users/utils/learning[/private]）→
切片 0→3 全链子应用（身份/任务围栏/SSE、能力配置/连接器、资料、
规划/对话，含教学与清理路由）。

- `app.main:app` 是唯一生产启动目标（`uvicorn app.main:app`）；
- `sliceN_app.create_sliceN_app` 保留原样：既是装配链的中间层，
  也是既有测试的导入别名，不删不改。
"""

from __future__ import annotations

import os
from contextlib import AsyncExitStack, asynccontextmanager

from app.logging_audit.sdk_logging import install_sdk_logging_safety

# Install before route/SDK imports: template modules enable root INFO logging.
install_sdk_logging_safety()

import sentry_sdk
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.middleware.cors import CORSMiddleware

from app.api.main import api_router
from app.infrastructure.config import settings


def _custom_generate_unique_id(route: APIRoute) -> str:
    return f"{route.tags[0]}-{route.name}"


def _prepare_http_proxy_environment() -> None:
    # Node 专属开关不属于 Python 代理设置；标准代理仍由 httpx 读取。
    os.environ.pop("NODE_USE_ENV_PROXY", None)
    from ipaddress import IPv6Address

    for name in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(name)
        if raw is None:
            continue
        hosts = []
        seen = set()
        for value in raw.split(","):
            host = value.strip()
            if host.startswith("[") and host.endswith("]"):
                try:
                    IPv6Address(host[1:-1])
                except ValueError:
                    pass
                else:
                    # NO_PROXY 接受裸 IPv6；httpx 会自行添加 URL 所需的括号。
                    value = host[1:-1]
            key = value.strip()
            if key not in seen:
                seen.add(key)
                hosts.append(value)
        os.environ[name] = ",".join(hosts)


def create_app() -> FastAPI:
    install_sdk_logging_safety()
    _prepare_http_proxy_environment()
    if settings.SENTRY_DSN and settings.FASTAPI_ENV != "development":
        sentry_sdk.init(dsn=str(settings.SENTRY_DSN), enable_tracing=True)

    # Operational telemetry is separate from the controlled content channel.
    os.environ["OTEL_ENABLED"] = "1" if settings.OTEL_ENABLED else "0"
    for key in ("LOGFIRE_TOKEN", "LOGFIRE_OTLP_ENDPOINT", "LOGFIRE_OTLP_TRACES_ENDPOINT", "LOGFIRE_OTLP_METRICS_ENDPOINT"):
        value = getattr(settings, key, None)
        if value:
            os.environ.setdefault(key, value)
    # Missing monitoring configuration never blocks the business workflow.
    try:
        from app.logging_audit.otel_setup import otel_from_env

        otel_from_env(service_name=str(settings.PROJECT_NAME))
    except Exception:  # noqa: BLE001 — 观测装配失败不阻塞应用启动
        pass

    app = FastAPI(
        title=settings.PROJECT_NAME,
        openapi_url=f"{settings.API_V1_STR}/openapi.json",
        generate_unique_id_function=_custom_generate_unique_id,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.FRONTEND_HOST],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 模板工程路由（O-B 后仅保留 login/users/utils/learning[/private]）
    # Product identity uses /api/auth exclusively; template identity is retired.

    # 切片 0→3 全链（含教学与清理路由）挂载在 "/"；/api/v1 路由先注册先匹配
    os.environ.setdefault("LEARNING_DATABASE_URL", str(settings.DATABASE_URL))
    # ADR-022：内容通道开关经 settings 解析（真实环境变量 > .env）后同步到
    # 进程环境，供 logging_audit.content_trace 读取（沿用桥接模式）。
    os.environ["CONTENT_TRACE_ENABLED"] = "1" if settings.CONTENT_TRACE_ENABLED else "0"
    # 凭据主密钥同 pattern 桥接：slice1 crypto 只读进程环境，settings 值不会自动传播。
    if settings.APP_ENCRYPTION_KEY:
        os.environ.setdefault("APP_ENCRYPTION_KEY", settings.APP_ENCRYPTION_KEY)
    for name in ("SOURCE_VIDEO_PYTHON", "SOURCE_ASR_MODELS_DIR", "SOURCE_TRUSTED_SERVICES", "SOURCE_WORKER_MODE",
                 "CORPUS_VECTOR_BACKEND"):
        value = getattr(settings, name, None)
        if value:
            os.environ.setdefault(name, value)

    from app.slice3_app import create_slice3_app

    learning_app=create_slice3_app()
    app.mount("/", learning_app)
    from app.lifecycle import worker_lifespan
    additional_workers = worker_lifespan([
        learning_app.state.slice3.memory, learning_app.state.slice1.character_media,
        learning_app.state.slice3.planning.background, learning_app.state.slice1.market_receiver])

    @asynccontextmanager
    async def lifespan(_app):
        from app.logging_audit.otel_setup import shutdown_otel
        async with AsyncExitStack() as stack:
            stack.callback(shutdown_otel, timeout_millis=2000)
            await stack.enter_async_context(learning_app.router.lifespan_context(learning_app))
            await stack.enter_async_context(additional_workers(_app))
            yield
    app.router.lifespan_context = lifespan
    return app


__all__ = ["create_app"]
