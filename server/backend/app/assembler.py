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

import sentry_sdk
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.middleware.cors import CORSMiddleware

from app.api.main import api_router
from app.core.config import settings


def _custom_generate_unique_id(route: APIRoute) -> str:
    return f"{route.tags[0]}-{route.name}"


def create_app() -> FastAPI:
    if settings.SENTRY_DSN and settings.FASTAPI_ENV != "development":
        sentry_sdk.init(dsn=str(settings.SENTRY_DSN), enable_tracing=True)

    # ADR-022：OTel 装配（OTLP → Langfuse）。未启用/缺端点时 no-op；
    # 导出失败由批处理器丢弃，核心任务不受影响。
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
    app.include_router(api_router, prefix=settings.API_V1_STR)

    # 切片 0→3 全链（含教学与清理路由）挂载在 "/"；/api/v1 路由先注册先匹配
    os.environ.setdefault("LEARNING_DATABASE_URL", str(settings.DATABASE_URL))
    # ADR-022：内容通道开关经 settings 解析（真实环境变量 > .env）后同步到
    # 进程环境，供 logging_audit.content_trace 读取（沿用桥接模式）。
    os.environ["CONTENT_TRACE_ENABLED"] = "1" if settings.CONTENT_TRACE_ENABLED else "0"

    from app.slice3_app import create_slice3_app

    app.mount("/", create_slice3_app())
    return app


__all__ = ["create_app"]
