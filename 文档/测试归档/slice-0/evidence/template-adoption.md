# 采用 full-stack-fastapi-template 的执行记录

> 日期：2026-09-27  
> 模板：`https://github.com/fastapi/full-stack-fastapi-template.git`  
> 锁定 commit：`cb740b656d7a0a6c5e12c7bf8e50343ec94ee9c7`（cb740b6）  
> Python：3.14.7（uv 安装）｜uv 0.12.19

## 按架构执行的内容

| 架构要求 | 执行 |
| --- | --- |
| 以 `full-stack-fastapi-template` 为工程起点 | ✅ `server/`，uv 工作区 |
| 复用工程能力 | ✅ `app.core`（config/security/db）、`app.api.deps` JWT、Alembic、crud |
| **移除模板前端界面/组件库** | ✅ 删除 `server/frontend/` 与 `app/frontend/`；`main.py` 去掉 `app.frontend()` |
| 前端自行实现 | ✅ 产品 UI 仍在 `frontend/`（React+Radix，无模板组件库） |
| 账户机制按 PRD 改造 | ✅ User 增加 `account_status` 状态机字段 |
| 产品身份/会话接入模板主应用 | ✅ `app/auth` + `app/slice0_app.py`，`/api/auth/*`、`/api/tasks/*` 挂载到 `app.main:app` |
| 学习业务自行实现 | ✅ `app/logging_audit`、`app/tasks`、`app/prompts`、`api/routes/learning.py` |

## 模板复用清单

- `app.core.security`：pwdlib Argon2id/Bcrypt、JWT `create_access_token`
- `app.core.config`：Settings / `.env` / SECRET_KEY
- `app.core.db`：SQLModel engine + `init_db` 首管理员
- `app.api.deps`：`get_current_user` / OAuth2PasswordBearer
- Alembic 迁移链（含我们的 `f2f15dbd597f_slice0_learning_tasks_prompts_audit`）
- 工程脚本、Docker compose、许可证（模板 LICENSE）

## 新增（学习业务）

| 模块 | 说明 |
| --- | --- |
| `app/logging_audit/` | 日志契约、脱敏、SSE、审计、观测降级 |
| `app/tasks/` | 租约围栏、幂等、条件更新 |
| `app/prompts/` | 提示词绑定、local fallback |
| `app/api/routes/learning.py` | 任务/SSE/claim/result/prompt API |
| `app/auth/` + `app/slice0_app.py` | 产品注册、验证、审批、会话、任务和 SSE API |

## 验证

```text
# 模板环境（uv + Python 3.14）
cd server/backend
uv run python -m pytest ../../tests/test_template_integration.py
→ 6 passed
```

覆盖：模板 JWT 登录 → 创建任务（提示词绑定）→ SSE → claim/result 条件更新 → 过期 lease 拒绝 → 未认证 401。

全套 150 项契约/专项/模板测试使用 `uv run --project server/backend pytest tests` 通过。

## 数据库

- 模板库：`learning_assistant_tpl@127.0.0.1:5432`
- 迁移：模板基线 + `f2f15dbd597f`（学习任务/审计/提示词/账户状态列）+ `a42e8c1f0d9a`（产品身份/会话表）
- 环境变量：`DATABASE_URL`、`SECRET_KEY`、`FIRST_SUPERUSER*`（见 `.env`，值不入库）

## 与自建骨架的关系

| 路径 | 角色 |
| --- | --- |
| `server/backend` | **生产工程基线**（架构要求） |
| `tests/support/backend/logging_audit` 等 | 契约与测试实现；已镜像进 `server/backend/app/` |
| `frontend/` | 产品 UI（自建，无模板组件库） |

## 回退

```text
cd server/backend
uv run alembic downgrade fe56fa70289e   # 移除学习业务表
# 或完全重建：DROP DATABASE learning_assistant_tpl
```
