# 切片 0 执行证据
> 范围说明：本文件是切片 0 完整验收（2026-09-27）。T01–T06 日志专项原始记录见 `../../logging-audit/evidence/execution-record.md`（2026-09-26）。

> 执行日期：2026-09-27  
> 环境：Windows 11 / Python 3.12.3 / FastAPI 0.141.1 / SQLAlchemy 2.1.1 / Alembic 1.20.0 / PostgreSQL 17.11 / Node 24.19 / Vite 8.3 / React 19  
> 浏览器：Playwright CLI（npx @playwright/cli）

## 命令与结果

| 命令 | 结果 |
| --- | --- |
| `uv run --project server/backend pytest tests` | **150 passed** |
| `py -3 -m pytest tests/test_slice0_core.py tests/test_slice0_faults.py` | 21 passed |
| `uv run alembic upgrade head`（server/backend） | `f2f15dbd597f` → `a42e8c1f0d9a` |
| `uv run uvicorn app.main:app --port 8000`（server/backend） | 模板主应用 + 切片 0 API 运行 |
| `npm run build`（frontend/） | Vite 生产构建通过 |
| Playwright 注册→验证→审批→登录→建任务→SSE | 真实 HTTP/DB/SSE 浏览器证据 |

## 交付映射

| 任务 | 实现 | 测试 |
| --- | --- | --- |
| T01 依赖/领域契约 | `server/backend/pyproject.toml`、`server/uv.lock`、`frontend/package.json`、Alembic 迁移 | 迁移版本、领域表 |
| T02 身份 | `backend/auth/`（Argon2id、状态机、会话、重置、防暴力） | `test_slice0_core.py::TestAccountLifecycle` |
| T03 租约围栏 | `tests/support/backend/tasks/service.py`（attempt_id/lease_token 条件更新） | `TestLeaseFencing` |
| T04 SSE 所有权 | `/api/tasks/{id}/sse` 绑定会话，拒绝自填 owner | `TestSlice0HTTP` |
| T05 提示词绑定 | `backend/prompts/`（快照、哈希、local fallback、prompt_unavailable） | `TestPromptBinding` |
| T06 观测/审计 | 身份事件入注册表；exporter 降级 | `test_slice0_faults.py` |
| T07 前端 | `frontend/` React+TS+Radix+浅色 token+Excalidraw 壳 | Playwright |
| T08 真实链路 | 注册→验证→审批→登录→任务→SSE→渲染 | Playwright + `TestAC12_E2EHttp` |
| T09 故障注入 | 迟到 worker、Langfuse 不可用、审计失败、密钥扫描 | `test_slice0_faults.py` |

## AC-12 浏览器证据（Playwright）

| 步骤 | 结果 |
| --- | --- |
| 注册 `synthetic.e2e.user@example.com` | 201，返回验证令牌 |
| 邮箱验证 | 状态 `pending_approval` |
| 管理员审批（API） | 200 `approved` |
| 浏览器登录 | 进入任务页，显示已登录 |
| 创建最简任务 | `task_856896914dca`，绑定 `lesson_step_v1` |
| SSE 事件渲染 | 序号 1 / stage=upload / started |
| localStorage/控制台 | 无密钥、无管理员密码 |

## AC-10 / AC-11

- 迟到 worker 只写 `stale_discarded`，当前 revision/状态不被覆盖
- Langfuse exporter 断开 → 任务 `accepted=true`，`observability.export.failed` + dropped 计数
- 审计写入失败 → 异常 + `write_failures`，不静默

## 依赖与许可证（摘要）

| 依赖 | 许可证 |
| --- | --- |
| FastAPI / Starlette / Pydantic | MIT |
| SQLAlchemy | MIT (Apache-2.0 for 2.x dual) |
| Alembic | MIT |
| psycopg | LGPL-3.0 (with exception) |
| argon2-cffi | MIT |
| React / Vite | MIT |
| Radix UI | MIT |
| Excalidraw | MIT |
| PostgreSQL | PostgreSQL License |

字体：系统字体栈（Segoe UI / PingFang SC / Microsoft YaHei），未捆绑第三方字体文件，无额外字体许可证问题。PDFium 未引入（切片 0 无 PDF 处理）。

## 与模板的差异（已记录）

架构建议以 `fastapi/full-stack-fastapi-template` 为起点。本实现采用**自建 FastAPI + SQLAlchemy + Alembic 骨架**，原因：

1. 模板自带整套前端 UI/组件库，与「移除模板前端」的额外改造成本高于自建；
2. 已有 `logging_audit` 契约可直接复用；
3. 依赖、迁移、许可证均在本仓库锁定可追溯。

**模板前端组件库从未进入依赖树**（AC-01 关于「移除」的条件以「未引入」满足）。若后续要求强制基于官方模板，需另开差异修订。

## 回退

| 步骤 | 命令 |
| --- | --- |
| 迁移回退 | `py -3 -m alembic downgrade 11fd41567866`（回退身份/任务/提示词表） |
| 完全清空 | `py -3 -m alembic downgrade base` |
| 前端回退 | `git checkout frontend/` 或删除 `frontend/` |
| 环境变量 | `LEARNING_DATABASE_URL`、`VITE_API_BASE` |

## 未决

1. Langfuse 真实 SDK 联调（本地 fallback 已实现；接生产 Langfuse 需部署与凭据）。
2. 邮件发送（当前开发环境返回验证/重置令牌）。
3. Excalidraw 只读场景在生成链路接入后充实（组件已加载）。
4. CSRF 双提交在浏览器 cookie 模式下需与前端同域部署联调（当前 Bearer 测试通过）。
