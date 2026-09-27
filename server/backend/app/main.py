import os
from pathlib import Path

import sentry_sdk
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.middleware.cors import CORSMiddleware

from app.api.main import api_router
from app.core.config import settings

# 架构要求：不复用模板前端界面/组件库。产品前端位于仓库 frontend/。
FRONTEND_DIR = Path(__file__).parent / "frontend"


def custom_generate_unique_id(route: APIRoute) -> str:
    return f"{route.tags[0]}-{route.name}"


if settings.SENTRY_DSN and settings.FASTAPI_ENV != "development":
    sentry_sdk.init(dsn=str(settings.SENTRY_DSN), enable_tracing=True)

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    generate_unique_id_function=custom_generate_unique_id,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_HOST],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_V1_STR)
# 模板前端已按架构移除；产品 UI 由 frontend/ 独立提供
# app.frontend("/", directory=FRONTEND_DIR)

# 切片 0 产品 API：使用模板的配置/迁移/运行时作为唯一后端基线，
# 由独立子应用提供 PRD 所需的邮箱验证、审批、持久会话、任务围栏、
# 提示词绑定和 SSE 路由。模板自身的 /api/v1 路由仍保留作工程能力入口。
os.environ.setdefault("LEARNING_DATABASE_URL", str(settings.DATABASE_URL))
from app.slice0_app import app as slice0_app  # noqa: E402

app.mount("/", slice0_app)
