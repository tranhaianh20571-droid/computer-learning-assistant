# 切片 0 契约测试支持包

生产后端基线位于 `server/backend`，由模板配置、迁移和 `app.main:app` 启动。本目录只保留切片 0 的纯契约、故障注入和 SQLite/PG 测试支持实现，并已镜像到生产应用的 `server/backend/app/` 包中；它不是生产启动入口。

统一验证命令（从仓库根目录执行）：

```text
uv run --project server/backend pytest tests
```

生产应用运行时使用 `uv run`，数据库迁移使用 `server/backend/app/alembic/versions/`。不要把本目录作为 ASGI 应用启动；模板后端依赖应通过 `server/backend` 的 uv 环境运行。
