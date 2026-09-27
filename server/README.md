# 学习助手伙伴 · 服务端

本目录是学习助手伙伴的唯一生产后端工程，源自 `fastapi/full-stack-fastapi-template` 的工程骨架。模板前端和组件库已经移除；产品前端位于仓库根目录的 `frontend/`，后端入口为 `server/backend/app/main.py`。

从仓库根目录运行后端测试：

```console
uv run --project server/backend pytest tests
```

## 技术栈和边界

- ⚡ [**FastAPI**](https://fastapi.tiangolo.com) for the Python backend API.
  - 🧰 [SQLModel](https://sqlmodel.tiangolo.com) for the Python SQL database interactions (ORM).
  - 🔍 [Pydantic](https://docs.pydantic.dev), used by FastAPI, for the data validation and settings management.
  - 💾 [PostgreSQL](https://www.postgresql.org) as the SQL database.
- 产品 React/TypeScript 前端独立位于仓库根目录的 `frontend/`，不由后端打包或托管。
- 任务、提示词、日志审计和身份模块位于 `server/backend/app/`。
- 邮件模板工程位于 `packages/react-email/`，真实投递仍待生产联调。
- 测试数据使用合成账号和资源，完整测试入口见上方命令。

## 常用命令

在 `server/backend/` 目录运行迁移和服务：

```console
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

切片 0 的首个管理员由环境变量 `FIRST_SUPERUSER` 和 `FIRST_SUPERUSER_PASSWORD` 提供，然后运行：

```console
uv run python scripts/bootstrap_slice0.py
```

## 目录说明

```text
server/
├── backend/
│   ├── app/                 # 生产 FastAPI 应用、领域模块和迁移
│   ├── scripts/             # 后端启动、测试和切片初始化脚本
│   └── tests/               # 模板后端自带测试
├── packages/react-email/    # 预留的邮件模板工程
├── pyproject.toml           # uv 工作区配置
└── uv.lock                  # Python 依赖锁定文件
```

仓库的 `tests/support/backend/` 只保留契约和故障测试实现，不是生产启动入口；生产代码只从 `server/backend/` 运行。

## 当前边界

本地验证以 `uv` 后端工作区和根目录 `frontend/` 为准。模板遗留的 Docker、CI 和部署文件仍在整理，不能把它们视为已经完成的生产部署方案；真实邮件投递、Langfuse 生产 SDK、同域 CSRF 联调和后续生成场景仍按切片 0 交接文档验收。

## 后端说明

Backend docs: [backend/README.md](./backend/README.md).

## License

工程骨架沿用 MIT 许可证；产品代码和依赖的具体许可证以仓库许可证清单为准。
