# T01～T06 执行证据（FastAPI + PostgreSQL）
> 范围说明：本文件是 T01–T06 日志专项记录（2026-09-26）。切片 0 完整验收证据（更新，2026-09-27）见 `../slice-0/evidence/execution-record.md`。

> 执行日期：2026-09-26  
> 环境：Windows 11 / Python 3.12.3 / **FastAPI 0.141.1 + SQLAlchemy 2.1.1 + Alembic 1.20.0 + PostgreSQL 17.11** / Playwright CLI 0.1.21  
> 依赖清单：`server/backend/pyproject.toml`、`server/uv.lock`

## 运行命令与结果

| 命令 | 目录 | 结果 |
| --- | --- | --- |
| `py -3 -m pytest tests` | 仓库根 | **123 passed**（5.7s） |
| `py -3 -m alembic upgrade head` | 仓库根 | 迁移 `11fd41567866` 已应用 |
| `py -3 -m pytest tests/test_fastapi_pg.py tests/test_ac03_worker_chain.py` | 仓库根 | FastAPI/PG + worker 链路 + 角色权限 |
| `npx --package @playwright/cli playwright-cli …` | 仓库根 | AC-07 浏览器回归通过 |

## 生产栈落地

| 组件 | 版本 | 用途 |
| --- | --- | --- |
| FastAPI | 0.141.1 | HTTP/SSE 路由、异常映射、trace 中间件 |
| uvicorn | 0.42.0 | ASGI 服务器 |
| SQLAlchemy | 2.1.1 | ORM（2.0 风格 Mapped） |
| Alembic | 1.20.0 | 生产迁移 `server/backend/app/alembic/versions/a42e8c1f0d9a_*.py`；契约测试迁移在 `tests/support/backend/alembic/versions/` |
| psycopg 3 | 3.3.6 | PostgreSQL 驱动 |
| PostgreSQL | 17.11 | 本地实例 `learning_assistant@127.0.0.1:5432` |
| pytest | 9.1.1 | 测试 |

环境变量（不含密钥明文入库）：

```text
LEARNING_DATABASE_URL=postgresql+psycopg://learning_assistant:***@127.0.0.1:5432/learning_assistant
```

## 模块映射

| 模块 | 职责 |
| --- | --- |
| `contracts.py` / `sanitize.py` | T01 字段允许名单、事件/错误码注册表、脱敏 |
| `trace.py` / `logger.py` / `exporter.py` | T02 W3C trace、结构化日志、观测降级 |
| `task_events.py` | T03 事件契约与围栏语义 |
| `models.py` / `db.py` / `repositories.py` | T03/T04 **PostgreSQL 模型与仓储** |
| `app.py` | T02/T03 **FastAPI 路由**（SSE/REST/审计） |
| `audit.py` | T04 追加式审计契约 |
| `server/backend/app/alembic/` | 生产迁移 + 审计不可变触发器 |
| `文档/ui/index.html` | T05 `taskEventClient` 适配边界 |

## PostgreSQL 约束与角色（实测）

| 约束 | 实现 |
| --- | --- |
| `UNIQUE (task_id, sequence)` | `uq_task_events_task_sequence` |
| 所有权索引 | `idx_task_events_owner` / `ix_task_states_owner_user_id` |
| 审计禁止 UPDATE/DELETE | 触发器 `audit_logs_block_mutation()` + 角色层 `REVOKE` |
| `learning_app_ro` | 仅 `SELECT` |
| `learning_app_rw` | `SELECT, INSERT`，无 `UPDATE/DELETE`（`test_ac03_worker_chain.py` 实测） |
| 状态+事件同事务 | SQLAlchemy `session` 事务内 flush/commit |
| 旧 generation 围栏 | 仓储层强制 `stale_discarded`，不改业务状态 |

## 回退说明

| 步骤 | 操作 |
| --- | --- |
| 迁移版本 | `11fd41567866_task_events_audit_logs_foundation` |
| 回退迁移 | `py -3 -m alembic downgrade base`（删除三张表及触发器） |
| 应用回退 | 停止 API/worker → downgrade → 切换代码版本 |
| 数据回退 | 开发库可 `DROP TABLE task_events, task_states, audit_logs CASCADE`；生产先备份 |
| 环境变量 | `LEARNING_DATABASE_URL`（值不入库） |
| 依赖回退 | `uv sync --project server/backend` 可复现；依赖版本由 `server/uv.lock` 锁定 |

## AC-07 浏览器回归（Playwright）

`/course` → `/course/detail` → `/lecture` → `#/settings/models` → `#/admin` 审批：

- fetch/XHR 钩子：**0 次外发**
- `App.taskEventClient.mode = demo-offline`
- `U.audit.isDemoOnly = true`
- 控制台仅 favicon 404 + DOM 提示，无密钥/正文/请求体

## 故障注入（单元 + SQLite 契约 + FastAPI/PG）

| 注入 | 结果 |
| --- | --- |
| 旧 generation 迟到 worker | 只写 `stale_discarded` |
| OTel/Langfuse 不可用 | 业务继续 + `observability.export.failed` |
| 审计写入失败 | 抛错 / **HTTP 503 AUDIT_WRITE_FAILED** |
| 审计 SQL UPDATE | PG 触发器 `audit_immutable` |
| 跨账户 | 403 |
| 敏感字段泄漏 | 密钥/邮箱/带查询 URL 全遮蔽 |
| SSE 断线/过期 | 序号连续 / **410 events_expired** |

## 遗留（非本阶段）

1. `full-stack-fastapi-template` 工程骨架与正式账户/会话 JWT（切片 0 后半段身份）。
2. 真实 Langfuse/OTel SDK 联调。
3. 备份恢复墓碑重放（切片 5）。
