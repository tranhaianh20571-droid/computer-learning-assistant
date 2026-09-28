# 测试执行入口

本目录是仓库可执行测试的唯一真源。`tests/support/backend/` 与生产 `server/backend/app/` 保持镜像同步（契约层说明见 `support/backend/README.md`）：测试导入的是镜像实现，生产代码只从 `server/backend/` 运行。执行记录与 AC 验收归档在 `文档/规范/tests/`（不入 git）。

## 运行

仓库根目录：

```text
uv run --project server/backend pytest tests
```

## 数据库准备

需要本机 PostgreSQL。连接串优先级：`LEARNING_DATABASE_URL` 环境变量 > `server/.env` 的 `DATABASE_URL` > `server/backend/app/logging_audit/db.py` 内置开发默认值。

```powershell
Start-Service postgresql-x64-17
Set-Location server/backend
uv run alembic upgrade head
Set-Location ../..
# 可选：显式指定测试库
$env:LEARNING_DATABASE_URL="postgresql+psycopg://learning_assistant:<密码>@127.0.0.1:5432/learning_assistant"
```

## 测试分层

| 分层 | 文件 | 覆盖 |
| --- | --- | --- |
| 日志审计地基 | `test_t01_contracts` / `test_t02_logger` / `test_t03_task_events` / `test_t04_audit` / `test_t05_frontend_boundary` / `test_t06_fault_injection` | 字段允许名单、脱敏、日志注入、trace 关联、任务序号、审计追加保护、观测降级 |
| 切片 0–5 | `test_slice0_*` ～ `test_slice5_*`、`test_ac03_worker_chain` | 各切片核心、契约、调度与故障 |
| 集成与故障注入 | `test_integration_http` / `test_integration_faults` / `test_fastapi_pg` / `test_template_integration` | 真实 HTTP、SSE 补取与 `410 events_expired`、跨账户、PG 触发器 |
| 共享夹具 | `slice3_common.py`、`support/` | 合成模型适配器、测试存储目录 |

## 浏览器 / 前端回归

- 前端应用（`frontend/`）：`npm run build`；`node scripts/slice1_frontend_check.mjs <baseUrl> <outDir>`（默认 `http://127.0.0.1:4173`，需先 `vite preview` 或 dev server；检查登录页可达、匿名不渲染能力配置、窄屏可用、无真实外发、无敏感控制台输出）；再手动走查 `/course` → `/course/detail` → `/lecture`、设置页与 360px 窄屏布局。
- 离线原型（`文档/ui/index.html`，AC-07）：`py -3 -m http.server 8765 --directory 文档/ui` 后用 Playwright 检查课程列表/详情/讲次/设置页无真实网络请求、无敏感控制台输出。

## 证据与验收归档

各切片执行记录与 AC 汇总在 `文档/规范/tests/`（`logging-audit/evidence|reports`、`slice-0/`），注册表快照在 `logging-audit/schemas/`；这些目录不入 git，索引见 `文档/规范/tests/logging-audit/README.md`。
