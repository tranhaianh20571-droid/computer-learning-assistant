# 切片 0 AC-01～AC-12 验收汇总
> 范围说明：本表是切片 0 完整验收 AC-01～AC-12（2026-09-27）。T01–T06 专项 AC-01～AC-09 见 `../../logging-audit/reports/ac-summary.md`（2026-09-26）。

> 执行日期：2026-09-27｜`uv run --project server/backend pytest tests` → **150 passed** + 模板主应用 HTTP 真实链路；`frontend/npm run build` 通过

| 验收 | 状态 | 证据 |
| --- | --- | --- |
| AC-01 模板/依赖/领域契约 | **通过**（模板后端基线 cb740b6，模板前端未引入） | `server/backend/pyproject.toml`、`frontend/package.json`、许可证摘要 |
| AC-02 注册与邮箱验证 | **通过** | `test_slice0_core.py::TestAccountLifecycle` |
| AC-03 审批/登录/登出/停用/重置 | **通过** | 同上（会话即时失效） |
| AC-04 任务创建与 worker 竞争 | **通过** | `TestLeaseFencing`（幂等、双 worker、过期 lease） |
| AC-05 SSE 断线补取/410 | **通过** | 专项 + `/api/tasks/{id}/sse` |
| AC-06 两账户交叉访问 | **通过** | `TestSlice0HTTP`、`TestAC12_E2EHttp`（403） |
| AC-07 提示词固定与后备 | **通过** | `TestPromptBinding`（快照哈希、prompt_unavailable） |
| AC-08 日志/审计/观测边界 | **通过**（当前模板 uv 环境复跑） | `150 passed` + 泄漏扫描 |
| AC-09 前端视觉与交互底座 | **通过** | React+Radix+浅色 token+Excalidraw 壳；Playwright |
| AC-10 worker 租约过期迟到 | **通过** | `TestAC10_LateWorker` |
| AC-11 Langfuse/OTel 不可用 | **通过** | `TestAC11_ObservabilityDown` |
| AC-12 真实端到端链路 | **通过** | Playwright：注册→验证→审批→登录→任务→SSE 渲染 |

## 完成判定对照

- T01～T09 执行记录齐全：**是**（见 handoff 第 4 节）
- AC-01～AC-12 当前环境可复核证据：**是**
- 真实链路跑通：**是**（非 mock）
- 故障注入通过：**是**
- 未决项已列出：**是**（邮件发送、Langfuse 生产 SDK、Excalidraw 生成场景）
