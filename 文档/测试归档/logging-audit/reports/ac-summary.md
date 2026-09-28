# AC-01～AC-09 验收汇总（FastAPI + PostgreSQL）
> 范围说明：本表覆盖 T01–T06 的 AC-01～AC-09（2026-09-26）。切片 0 完整 AC-01～AC-12 见 `../slice-0/reports/ac-summary.md`（2026-09-27）。

> 执行日期：2026-09-26  
> 命令：`py -3 -m pytest tests`（**119 passed**）+ Playwright 浏览器回归  
> 栈：FastAPI 0.141.1 · SQLAlchemy 2.1.1 · Alembic 1.20.0 · PostgreSQL 17.11  
> 证据：`evidence/execution-record.md`

| 验收 | 场景 | 方法与结果 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| AC-01 | 合成上传/OCR/模型失败任务 | 统一 JSON 字段、UTC、trace/task/attempt、错误码、非负 `duration_ms`；未知字段拒绝 | **通过** | `sample-runtime-logs.jsonl`、`registry-v1.json` |
| AC-02 | 假密钥、邮箱、教材、私密 URL | 单元 + PG 泄漏扫描 + 浏览器控制台：全遮蔽 | **通过** | `test_t06`、`test_fastapi_pg`、Playwright |
| AC-03 | API→worker 任务链与重试 | FastAPI 创建→事件→状态；traceparent 贯穿；`task_id` 不变 `attempt` 递增 | **通过** | `test_fastapi_pg.py` |
| AC-04 | SSE 断线、`Last-Event-ID`、过期、跨账户 | 真实 SSE：缺口补回、**410 events_expired**、他人 403 | **通过** | `test_fastapi_pg.py::TestSSE` |
| AC-05 | 登录/审批/配置/外发/连接器/删除/清理/恢复 | 11 类审计事件 + PG 追加式表 | **通过**（备份恢复属切片 5） | `sample-audit-records.json` |
| AC-06 | 账号权限与审计查询 | app 403；admin 可查；`audit.query.executed`；PG 触发器禁 UPDATE | **通过** | `test_fastapi_pg.py::TestAudit` |
| AC-07 | 原型全流程 | **Playwright**：`/course`→详情→讲次→设置→审批；0 外发；无敏感控制台 | **通过** | `.playwright-cli/` |
| AC-08 | 旧 generation 迟到 | 仓储围栏：只写 `stale_discarded` | **通过** | `test_fastapi_pg.py::TestFaultInjection` |
| AC-09 | 观测降级、审计写入失败 | 熔断+降级日志；审计失败 **HTTP 503** | **通过** | `test_fastapi_pg.py`、`test_t06` |

## 完成判定对照（交接文档 §5）

- T01～T06 均有实际执行记录：**是**
- AC-01～AC-09 均有可复核证据：**是**
- 与架构一致性：**FastAPI + PostgreSQL + Alembic + SSE 410 + 审计不可变**，对齐架构 §2/§3.2/§8 切片 0

## 后续切片（不在本阶段）

1. `full-stack-fastapi-template` 账户/审批/会话 JWT
2. Langfuse/OTel 真实 SDK
3. 删除墓碑与备份恢复（切片 5）
