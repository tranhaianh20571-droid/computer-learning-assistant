# 观测收尾、文档整理与前端拆分 · 执行方案

> 版本：v1.0｜2026-09-28。基于 2026-09-28 只读调查结论整合而成，是执行清单不是现状描述。
> 所在工作 `文档/架构/v2/` 不入 git（`.gitignore` 忽略整个 `文档/`）；完成后无需提交。
> 状态标记：✅ 已完成｜⬜ 待执行｜⏸ 暂缓（附重议信号）。
> 规则：未实际运行的命令一律标"待执行"；执行后在第 5 节补记录（运行目录、版本、命令、结果、证据路径）。

## 0. 已定决策基线（不需要再讨论）

| 决策 | 状态 | 载体 |
| --- | --- | --- |
| 移除 pydantic-ai-slim，结构化输出由 model_runner 原生实现 | ✅ 提交 cc0e53b | `server/backend/pyproject.toml`、`server/uv.lock`；ADR-019（`文档/架构/computer-learning-assistant-architecture.md（附录·决策记录）`） |
| 不接 Langfuse（SDK 与内容级 trace 均不做） | ✅ 已决策，⬜ 待归档 ADR-020 | 本文件 A2 |
| 日志规范泛化为通用规范 v2.0，去内部链接 | ✅ 2026-09-28 完成 | `文档/规范/logging-and-audit-spec.md` |
| 前端不引入状态库/路由库/请求库，用零依赖拆分 | ✅ 已决策 | 本文件第 4 节 |

项目侧具体绑定（阶段枚举、错误码）的权威位置：`server/backend/app/logging_audit/contracts.py`；快照：`文档/规范/tests/logging-audit/schemas/registry-v1.json`；摘要：工作区 AGENTS.md。

---

## 1. 观测层现状（调查结论，2026-09-28）

规范定义四类数据流，对照实现：

| 数据流 | 实现状态 |
| --- | --- |
| 运行日志（JSON 行 + 允许名单 + 脱敏 + 注入防护） | ✅ `logging_audit/logger.py` + `sanitize.py` + trace 中间件 |
| 任务事件（单调 sequence、事务写入、SSE 补取、410） | ✅ `task_events.py` + 专项测试（t03、integration_http） |
| 安全审计（追加式、PG 触发器、查询留痕、写失败可见） | ✅ `audit.py` + t04 + AC-05/06/09 |
| 指标与追踪 | ⚠️ 仅骨架：`exporter.py` 有界队列+熔断框架，默认 sink 为空函数；全仓库无 `/metrics`、无 Prometheus、无 OTel collector；指标仅以 JSON 日志行 + `ExporterStats` 计数器 + `task_events.duration_ms` 零散存在 |

提示词链路独立成层：`task_prompt_bindings`（模板快照 + SHA-256 + `source: langfuse|local_fallback`），现恒走 `local_fallback`。Langfuse 全部现状 = `deploy/langfuse-compose.yml`（不在任何启动路径）+ 该元数据字段。规范 v2.0 本身从未指定观测后端，"不接 Langfuse"与规范零冲突。

## 2. 观测层任务

### A1 ✅ 模型调用聚合指标（2026-09-28 完成，提交 4da861c）

**缺口**：按规范 §7，模型/外部调用应有 `task_stage_duration_ms{stage,status}`、`task_failures_total{stage,error_code}` 一类聚合；当前模型调用（规划生成、教学 TTS、结构化输出降级链）只有任务事件，没有可 grep 的调用级汇总。

**做法**（不引入新组件）：
1. 在 `server/backend/app/planning/model_runner.py` 的 `run_structured`/`run_chat` 出口与 `teaching/service.py` TTS 调用出口，经 `StructuredLogger` 写 `model.call.completed` / `model.call.failed` 事件：字段 `stage`、`mode`（native/tool/prompted）、`error_code`、`duration_ms`、`config_id`、`capability`、token 用量（供应商响应里有则取，无则省）。
2. 同处在进程内维护 `ExporterStats` 式计数器（按 stage×mode×error_code），供后续告警脚本读取。
3. 单元测试：断言字段齐全、无 prompt/回答正文/用户问题（符合测试要求中的敏感字段泄漏检查），放 `tests/` 根，遵循现有 t0X 命名风格。

**验收**：跑一次真实规划任务后，运行日志可按 `event=model.call.completed` grep 出全链路调用汇总；泄漏检查测试通过。

### A2 ✅ ADR-020：Langfuse 决策归档（2026-09-28 追加；**同日被 ADR-022 取代**）

> 2026-09-28 更新：本决策自列的重议信号当日触发，ADR-022 接管——自托管 Langfuse 转入启动路径、OTel 埋点启用（OTLP 出口）、提示词/回答/用户问题三项进内容通道（规范 §6 通道化重写）、新建 `observability_deletion_refs` 删除联动。以下原文保留作历史记录。

追加到 `文档/架构/computer-learning-assistant-architecture.md（附录·决策记录）`（与 ADR-019 配对）。要点：
- **决定**：不接入 Langfuse SDK，不建内容级 trace 上报/查看器；`deploy/langfuse-compose.yml` 保留为重议备忘（不在任何启动路径）；`PromptService` 维持 `local_fallback` + 未配置时受控报错；提示词版本管理继续由 `task_prompt_bindings`（快照+SHA-256+版本绑定）承载。
- **理由**：可复现性已由"提示词快照 + 业务表输出 + trace_id 关联"保证；内容级 trace 与规范 §6 脱敏名单正面冲突；v3 全家桶约 2.5 核/5.3 GiB 挤占单机部署资源。
- **重议信号**：正式试用阶段生成质量调优成为主要瓶颈、需要跨提示词版本 A/B 平台、多人协作管理提示词。届时先部署 compose、定内容脱敏策略与删除管线（`observability_deletion_refs` 已预留），顺序先 prompts 后 traces。

### A3 ⏸ 告警承载面与指标留存（正式试用前）

最轻方案：定时把聚合计数写成 WARN/INFO 日志 + 一个检查脚本；可选加仅管理员可见的 `/metrics` 文本端点。不引入 Prometheus/Grafana。指标 90 天留存等有载体再定。**现在不做。**

### A4 ⏸ 明确不做清单（防反复，2026-09-28 按 ADR-022 修订）

仍不做：引入 Langfuse **Python SDK**（trace 走 OTLP、提示词/删除走 REST 即可）；自建 trace 查看器；删 `deploy/langfuse-compose.yml`；Sentry 之外再新增其他观测组件（Sentry 已按 `SENTRY_DSN` 条件启用）。**已解除**（ADR-022）：自托管 Langfuse 与 OTel 导出链路。

---

## 3. 文档与测试档案整理

### B1 ✅ 日志规范泛化 v2.0（2026-09-28 完成）

`文档/规范/logging-and-audit-spec.md` 重写：内部链接全除、项目枚举改注册表规则、场景表泛化为 7 类、SSE 补取/410/所有权规则并入 §4、PRD 承诺数字改为"按业务承诺确定"。位置不变，AGENTS.md 必读基线链接仍有效。

### B2 ✅ `架构/v1/logging-and-audit-spec.md` 改名 + 断链修复（2026-09-28 完成）

> 2026-09-28 晚注：v1 目录已于同日清理（架构正文上提至 `架构/` 根、ADR 并入其附录、交接文档删除）；本节路径为当时历史记录，文件已不存在。

该文件实为切片 0 专项交接（v1.3，标题"第一阶段执行交接"），与规范正本同名混淆。改名 `slice-0-logging-audit-handoff.md`（对齐 slice-1/2/3/4 handoff 命名）。需同步修改的引用（2026-09-28 grep 全量）：

| 文件 | 位置 | 问题 |
| --- | --- | --- |
| 架构/v1/logging-and-audit-spec.md | 行 6、26、96 | 链接 `../规范/...` 少一层，应为 `../../规范/...`（随改名一起修） |
| 架构/v1/slice-0-complete-handoff.md | 行 7、128、314 | 同目录链接随改名更新；两处文字路径 `文档/架构/...` 缺 `/v1/` |
| 架构/v1/slice-1-capability-config-handoff.md | 行 6 | 同目录链接随改名更新 |
| 规范/tests/logging-audit/README.md | 行 3 | 文字路径缺 `/v1/`（B4 重写 README 时一并处理） |

**验收**：`grep -rn "架构/logging-and-audit-spec" 文档` 与 `grep -rn "](logging-and-audit-spec.md)" 文档/架构/v1` 零命中。

### B3 ✅ 新建根 `tests/README.md`（2026-09-28 完成，提交 d86ae4c）

从 `规范/tests/logging-audit/README.md` 迁出"怎么跑"三节：运行命令 `uv run --project server/backend pytest tests`、数据库准备（启动 PG 服务、`alembic upgrade head`、`LEARNING_DATABASE_URL`）、浏览器回归命令、测试分层说明（t01–t06 / slice0–5 / 集成 / 故障注入）、`tests/support/backend/` 镜像同构约定。迁出后规范侧 README 不再承担运行手册职责。

### B4 ✅ `规范/tests/` 收窄为纯证据档案（2026-09-28 完成）

三个已确认漂移的处理：
1. `logging-audit/evidence/` 实际装了 slice0–5 全部执行记录与 spike 结果 → 建切片子目录（或统一 `sliceN-` 前缀）归位；README 改写为"归档说明 + AC↔测试文件映射表"。
2. slice0 证据双源：`tests/slice-0/` 与 `logging-audit/evidence/` 各有一份 execution-record 和 ac-summary → 保留更新者，另一份头部加一行指向，不删文件。
3. `schemas/registry-v1.json` 与代码注册表双源 → 文件头加注"快照副本，权威为 `server/backend/app/logging_audit/contracts.py`，生成日期 XXXX"。

### B5 ⏸ 档案整理不做的事

evidence/reports 提交进 git（保持 ignore，含执行环境细节）；`fixtures/synthetic-fixtures.json` 转成 Python 夹具（JSON 有文档价值）；为"合并"移动任何测试代码。

---

## 4. 前端拆分（零新依赖，每步一个独立提交）

### 4.1 现状

`frontend/src/App.tsx` 657 行：错误码表（19–51）+ 路由（88、101–105）+ 会话恢复（107–118）+ 移动抽屉（120–144）+ 导航/面包屑（181–203）+ 路由分发（266–294）+ 7 个内联视图（301–657：Field/Register/Verify/Login/Reset/Admin/HomeView/CanvasView）。SSE 解析重复两处：`App.tsx:501-540`（任务流）与 `ChatView.tsx:112-126`（聊天流）。

### 4.2 目标结构

```text
src/
├── App.tsx              # 收敛 ~150 行：装配 hooks + 侧栏/顶栏 + 路由分发
├── api.ts               # 不动（162 行；>400 再按域拆）
├── lib/
│   ├── errors.ts        # ERROR_TEXT + errText（App.tsx:19-56）
│   └── sse.ts           # 纯函数：\n\n 分帧 + data: 行提取 + JSON 解析
├── hooks/
│   ├── useHashRoute.ts  # route + hashchange + go()（App.tsx:88-105）
│   ├── useSession.ts    # me() 恢复 + 登录/登出（App.tsx:107-118）
│   └── useEventStream.ts# 基于 sse.ts：fetch+getReader+abort+410 回退
├── views/
│   ├── auth/            # AuthLayout(AUTH_LEFT) + Register/Verify/Login/Reset + Field
│   ├── AdminView.tsx    # App.tsx:445-485
│   ├── TasksView.tsx    # 现 HomeView（488-623），顺带改名
│   └── CanvasView.tsx   # 626-657，保留动态 import
└── ui/Badge.tsx         # App.tsx:66-69
```

### 4.3 执行顺序（每步完成即回归）

| # | 任务 | 要点 |
| --- | --- | --- |
| C1 ✅ | `lib/sse.ts` + `hooks/useEventStream.ts` | 唯一有真实重复的代码，价值最高；两个消费方都换上；`410 events_expired` 回退策略统一进 hook；顺带修复 taskSse 未接 AbortSignal 的断流失效 |
| C2 ✅ | `lib/errors.ts` + `ui/Badge.tsx` | 纯搬移，零风险 |
| C3 ✅ | `hooks/useHashRoute.ts` + `useSession.ts` | App 只剩装配；注意 hooks 顺序在早退 return 之前（现 App.tsx:121 有注释提醒） |
| C4 ✅ | 视图外迁 `views/` | auth 四视图 + AdminView + TasksView（原 HomeView 改名）+ CanvasView；App.tsx 657→216 行 |
| C5 ⬜（可选） | `lib/` 纯函数上 vitest | 分帧、路由解析最值得测；前端第一套单测基建 |

**每步回归（全部必做，结果记入第 5 节）**：
1. `cd frontend && npm run build`；
2. `node scripts/slice1_frontend_check.mjs`（无真实外发、无敏感控制台输出）；
3. 手动：`/course` → `/course/detail` → `/lecture`、设置页、移动端 360px 布局、任务流与聊天流各收一次 SSE。

### 4.4 不做与触发条件

不引入状态库/路由库/react-query/Tailwind。重议信号：两个以上不相邻组件需要共享同一份高频变化状态（典型：未来做全局 TTS 播放器或多标签同步）；届时优先 React Query 类服务端状态缓存而非客户端状态库，且引入前先完成 C5 的测试基建。

---

## 5. 执行记录（2026-09-28 执行完毕）

| 任务 | 日期 | 运行目录 | 环境/版本 | 命令/操作 | 结果 | 证据路径 |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | 2026-09-28 | 仓库根 | uv 0.12.19 / Py 3.14 / PG 17.11 | `uv run --project server/backend pytest tests/test_t07_model_call_metrics.py tests/test_t01_contracts.py tests/test_t02_logger.py` | 46 passed | 测试输出 |
| A1 回归 | 2026-09-28 | 仓库根 | 同上 | `pytest tests/test_slice3_*.py tests/test_slice4_*.py`（6 文件） | 84 passed | 测试输出 |
| A1 全量 | 2026-09-28 | 仓库根 | 同上 | `uv run --project server/backend pytest tests` | **451 passed, 2 skipped** | 测试输出 |
| A2 | 2026-09-28 | — | — | ADR-020 追加到 adr-ai-stack-differences.md | 完成 | `文档/架构/computer-learning-assistant-architecture.md（附录·决策记录）` |
| B2 | 2026-09-28 | 文档/ | — | mv 改名 + sed 修 v1 嵌套断链（6 文件 0 残留） | 完成 | grep 验证零命中 |
| B3 | 2026-09-28 | — | — | tests/README.md 新建 | 完成 | 提交 d86ae4c |
| B4 | 2026-09-28 | — | — | README 重写 + 双源互指 + schemas/README + 注册表快照重导出（31 事件/36 错误码） | 完成 | `文档/规范/tests/` |
| C1–C4 | 2026-09-28 | frontend | Node 24.19 / Vite 8.3 | `npm run build`（每步） | 全部通过 | 提交 d47e2da / bf554fc / 2cfa3ee / c8d0781 |
| 前端自动化 | 2026-09-28 | 仓库根 | PW（npx 缓存）/ Chrome | `node scripts/slice1_frontend_check.mjs http://localhost:5173 .playwright-cli` | checks 全 true：登录页可达、匿名不渲染能力配置、键盘可达；无 pageerror、无意外外发、无泄漏 | `.playwright-cli/slice1-frontend-report.json` |
| 浏览器走查 | 2026-09-28 | dev server 5173 | 同上 | 登录态恢复、任务创建+SSE 事件表（新 hook）、导航骨架、390px 移动端截图 | 通过 | 会话内截图 |
| 聊天流式 | 2026-09-28 | 仓库根 | 同上 | HTTP 直连 `POST /api/chat/{id}/messages/stream` 验证 delta/done 帧契约 | CHAT_STREAM_OK | 命令输出 |
| C5 | 待执行 | | | vitest 纯函数测试（可选） | | |

### 执行中额外发现与修复

0. **页面布局对齐执行（2026-09-28 第二批，提交 58dfa5e / 5bc0334 / 529a33e）**：按"对齐原型 + hyperknow 呈现逻辑"方案完成 P0/P1/P2——
   - 后端新增 `GET /api/planning/courses`（owned 列表 + 进度 + 下一讲）与 `GET /api/auth/admin/pending`（待审批队列），`course_overview` 补 `title`；测试 `test_v2_ui_support.py` 5 项，全量基线 **456 passed, 2 skipped**。
   - 前端 P0：首页单卡片化（类型切换/附件弹层/生成按钮）、场景与范围 radio 移除（默认值 + 澄清流程承接）、ChatView 隐藏会话调试 ID、任务页撤出导航；侧栏收敛为 开始/学习/管理 三组 + 底部设置。
   - 前端 P1：新增 `/courses` 列表（过滤/搜索/色调封面 + 手绘 monogram + 进度条 + 下一讲栏）；课程详情杂志化（封面栏 + 大数字进度 + 单元圆点 + 下一步胶囊 + 上传行动卡 + 编号讲次）；讲次工作区全屏化（无骨架）+ 进度 pill + 位置已保存时间。
   - 前端 P2：管理员审批队列（列表 + 行内通过/拒绝）+ 侧栏待审批徽标；首页"继续学习"卡（基于 GET /api/navigation）。
   - **额外发现并修复：App.css 从未被导入**（孤儿样式表），已在 App.tsx 补导入。
   - 浏览器视觉验证：首页卡片/课程列表封面卡/杂志化详情/管理员队列（42 条待审批 + 徽标）截图通过；讲次全屏与确认后课程的完整布局因无已确认大纲数据未走查（待人工验证）；设置页独立化（方案第九条）未纳入本批。

1. **api.ts 默认 API 基址 bug（提交 2832c8e）**：绝对地址 `http://127.0.0.1:8000` 绕过 vite 代理，跨站 fetch 丢弃 SameSite=Lax 会话 Cookie，导致"持久 Cookie 恢复登录"（f657ff3）实际不可用。已改为默认同源相对路径（`?? ""`，VITE_API_BASE 仍可覆盖），与生产同域部署一致。浏览器实测登录→重载→会话恢复通过。
2. **slice1_frontend_check.mjs 断言过期**：ds-1 改版（6a9f220）后未更新，与本次重构无关；已按当前 UI 修正（提交 2832c8e）。
3. **未竟项**：登录表单点击与课程 `/course → /course/detail → /lecture`（需真实生成内容）未能在 IAB 自动化中驱动——自动化会话的输入管线失效（fill/cua.type 均未触达 React 受控组件，疑似浏览器面板失焦），按"不打断用户桌面操作"规则未抢焦点；已用 HTTP 直连补验聊天流式帧契约。该项留待人工登录后走查一次。

### 第三批：规划页访谈流（2026-09-28，提交 4a231a2 / ca486a3 / fa283cf）

按"先访谈、后生成、再画布"方案完成：后端新增 awaiting_intake 等待态（第一轮规则问卷含范围题、模型追问上限 4 轮、失败降级直接生成、范围答案直接落 resolved_scope），迁移 c7e9f1a3b5d7（planning_task_states.intake_json），DecisionIn 扩展 kind=intake，AWAITING_INTAKE 注册；前端 PlanningView 三态化（访谈/生成/草稿）、内部 ID 全部移除、localStorage 恢复最近任务。既有 7 处测试经 _run_with_intake 适配；新增 test_v2_intake（8 项）。基线 **464 passed, 2 skipped**。浏览器验证：访谈界面渲染与真实模型生成（119s）→ 草稿态截图通过；IAB 自动化无法驱动受控组件点击（环境限制），提交动作经 API 验证。
