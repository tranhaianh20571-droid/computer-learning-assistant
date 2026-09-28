# PydanticAI v2 重构执行方案与项目结构优化

> 版本：v1.1｜2026-09-28。**这是执行清单不是现状描述**；v1.0 同日定稿后，ADR-022 已实施（6f4ca8c/5085c39）、测试结构已整理（d58f33e）、文档已入 git（a366427），v1.1 据此修订：已完成项打 ✅、漂移校准、S1 拆分为 S1a/S1b、新增风险 R1–R6。
> 依据：同目录四份调研报告（三框架选型 v2.0、pi 头对头、PydanticAI 深挖）+ 2026-09-28 只读调研与可行性评估。
> 本文件自 a366427 起入 git；文档改动随代码流程提交。
> 状态标记：✅ 已完成｜⬜ 待执行｜⏸ 暂缓（附重议信号）。未实际运行的命令一律标"待执行"。

---

## 0. 决策基线（不需要再讨论）

| 决策 | 依据 |
| --- | --- |
| 采用 PydanticAI（`pydantic-ai-slim`，锁 minor 版本） | 四份调研报告收敛结论；ADR-019 预留回迁路径 |
| FunctionModel 桥：全部模型调用仍经切片 1 适配器，凭据/SSRF/外发核验/错误码映射不动 | ADR-019 决定原文 + 选型报告 §6 |
| 生成入口收敛为 `app/agents/` 包：五+1 个 Agent（任务规格，非角色）+ 三受控工具 | 2026-09-28 调用点盘点（`run_structured` ×5、`run_chat` ×2） |
| 降级链语义保留：能力探测 + 失败缓存 → `output_mode` 选择；`FallbackModel` 仅做模型级兜底 | model_runner T04 语义与 PydanticAI retries 分类学对齐 |
| 观测双层：自建 `logging_audit` 承载无内容事件与聚合指标；OTel 埋点启用，trace 经 OTLP 出口 Langfuse，内容三项进内容通道 | ADR-022（已实施）+ 规范 §6 通道化 |
| 任务租约/幂等/generation 围栏、SSE 事件契约、前端零依赖、审计追加式——全程不动 | 架构 §3.2-3.3 + AGENTS.md |

## 1. 当前项目结构诊断与优化建议（管理视角）

### 1.1 结构现状（事实；v1.1 注：镜像治理已落地）

```
server/backend/app/
├── main.py                 # 聚合 api_router（模板路由）+ create_slice1_app
├── slice0_app.py ~ slice3_app.py   # 层叠式应用：逐片挂载域路由
├── api/                    # FastAPI 模板路由（items/users/login/utils/private/learning）
├── core/                   # config/db/security（模板骨架）
├── auth/ cleanup/ materials/ planning/ teaching/ tasks/ slice1/ logging_audit/ prompts/ agents/
├── crud.py models.py db.py # 模板残骸（68/137/7 行，item/user CRUD）
└── tests/                  # 模板测试残骸（api/crud/utils/test_user）
tests/                      # 仓库可执行测试唯一真源（conftest.py + test_mirror_sync.py 守护）
└── support/backend/        # 生产 app/ 的镜像（白名单制哈希校验，见其 README）
```

### 1.2 诊断清单（编号 D*；v1.1：D2 已由 O-D 消解）

| # | 问题 | 证据 | 状态 |
| --- | --- | --- | --- |
| D1 | 产品级唯一入口缺失：教学/删除路由无聚合入口 | main.py:14；api/main.py 全模板路由 | ⬜ → O-A |
| D2 | 测试镜像双写漂移风险 | support/backend 全量镜像 | ✅ 已消解（test_mirror_sync 白名单守护，d58f33e） |
| D3 | 模板残骸未清 | app/crud.py、models.py、db.py、app/tests/、api/routes/items.py | ⬜ → O-B |
| D4 | 分层倒置：业务模块反向依赖 logging_audit 的 Database/Base | auth/cleanup/materials/prompts/alembic import 链 | ⏸ → O-C |
| D5 | planning 包 17 文件过载 | 目录清单 | S3 后自然分层 |
| D6 | 提示词来源不一致：访谈追问内联 | planning/service.py 内联 messages vs 7 个模板文件 | ⬜ → S3 补模板 |
| D7 | 双测试目录 | app/tests vs 根 tests/ | ⬜ → O-B |

### 1.3 结构优化项（O*）

| # | 优化 | 做法 | 时机 | 状态 |
| --- | --- | --- | --- | --- |
| O-A | 入口收敛 | `app/assembler.py` 的 `create_app()` 固定顺序挂载全部域路由；main.py 改薄；sliceN_app 留别名；compose 启动命令验证 | S0 | ⬜ |
| O-B | 模板残骸清理 | 零引用核对后删 app/tests/、crud.py、models.py、db.py、api/routes/items.py 等 | S0 | ⬜ |
| O-C | 基础设施上提 | Database/Base 上提 `app/core/db.py`，logging_audit 留 re-export | ⏸ 重议信号=重构期间第二次触碰这些 import | ⏸ |
| O-D | 镜像治理 | 同步校验脚本 | — | ✅ 完成（conftest + test_mirror_sync + 例外白名单，d58f33e） |
| O-E | planning 分层 | agents 承担生成入口后 planning 保留域服务 | S3 后 | ⬜ |
| O-F | 权威指针固化 | AGENTS.md 增补唯一入口/测试真源/镜像/决策记录（架构正文附录）/规范位置 | S4 | ⬜（部分已随 d58f33e 完成） |

## 2. 目标架构

### 2.1 分层与调用时序（v1.1：补内容捕获发射点）

```
任务 worker（tasks/service.py 租约/attempt 围栏，不动）
   │
AgentRunner（app/agents/runner.py：UsageLimits→错误码、事件→task_events/model.call.*、
   │          content_trace 发射点——替代 model_runner 出口捕获，保证迁移后覆盖不断链）
PydanticAI Agent（五+1 个任务规格）
   │  tools: search_materials / validate_board / get_outline_context
FunctionModel 桥（app/agents/bridge.py：能力门控+失败缓存→output_mode、
   │          请求装配、stream_function 流式、消息/错误码翻译）
切片 1 适配器（不动：凭据解密、HTTPS 强制、SSRF 校验、探测样例、超时）
```

### 2.2 `app/agents/` 包（v1.1：标注现状与缺口）

| 文件 | 职责 | 现状 |
| --- | --- | --- |
| `bridge.py` | FunctionModel：包装适配器 generate/stream；能力门控+失败缓存→output_mode；请求装配（forced tool_choice / response_format）；错误码翻译 | **部分落地**（6f4ca8c）：仅消息翻译+错误映射+输出工具名动态解析；缺门控/缓存/请求装配/流式 → S1a 补全 |
| `agents.py` | 五+1 Agent 定义；instructions 从模板快照加载 | ⬜ |
| `tools.py` | 三个受控工具（入出参全 pydantic、经 sanitize、不落正文） | ⬜ |
| `runner.py` | 统一调用入口：deps 装配、UsageLimits、事件钩子、**content_trace 发射** | ⬜ |
| `limits.py` | 循环上限→`AGENT_LOOP_LIMIT` 映射（错误码尚未注册进 contracts） | ⬜ |

### 2.3 Agent 清单（复用现有 Candidate 模型，零新 schema）

| Agent | instructions | output_type | 替换的调用点 |
| --- | --- | --- | --- |
| 路线生成/修订 | `route_generate_v1`/`planning_revise_v1` | `RouteCandidate` | planning/service.py:331 |
| 大纲生成 | `outline_generate_v1` | `OutlineCandidate` | planning/service.py:374 |
| 访谈追问 | 补 `intake_followup_v1` 模板（消 D6） | `IntakeQuestionSet` | planning/service.py:485 |
| 教学步骤 | `teaching_step_v1` | `LessonPlanCandidate` | teaching/service.py:150 |
| 追问回答 | `followup_v1` | `FollowupCandidate` | teaching/service.py:654 |
| 即时对话 | `chat_answer_v1` | `str` | planning/chat.py:297/422（含 291 stream 路径） |

## 3. 执行切片（v1.1：S0 校准、S1 拆分）

### S0 ⬜ 准备与围栏（剩余项）

已完成：✅ 依赖安装（pydantic-ai-slim 2.51.0 + otel 1.45，uv.lock 已锁）；✅ O-D 镜像守护；✅ 全量基线 **497 passed / 2 skipped**。

1. **ADR-023** 追加至架构正文附录（原计划 ADR-021 编号已被预留占用、ADR-022 被观测反转使用；内容：装回 pydantic-ai-slim、桥设计、与 ADR-019/020/022 关系）；
2. O-B 模板残骸清理 + O-A 入口收敛（assembler.py + main.py + compose 启动验证——待执行）；
   **验收**：基线不降；`uvicorn app.main:app` 挂载全部域路由（待执行验证）。

### S1a ⬜ 桥补全（不动任何调用点）

1. **content_trace 发射点迁入 bridge/runner**（R1：调用点切换会绕过 model_runner 出口捕获，不迁移则 `CONTENT_TRACE_ENABLED` 覆盖静默丢失）；
2. 能力门控 + 6h 失败缓存迁入桥；请求装配（forced tool_choice / response_format）从 AgentInfo 取 schema；
3. `stream_function` 对接适配器 `generate_stream_text`（chat SSE 路径依赖）；
4. **output_mode 运行期覆写 spike（R4）**：验证 2.51 能否按 run 动态选 Tool/Native/Prompted（能力门控语义要求按请求而非构造期）；不能则定型"模式分支包装"方案并记入本方案；
5. `AGENT_LOOP_LIMIT` 注册进 contracts + limits.py；新增测试：门控/缓存/装配/流式/上限。
   **验收**：全量测试不降；桥单测覆盖上述五项；spike 结论记录。

### S1b ⬜ 最小调用点切换（爆炸半径=追问回答+即时对话）

1. Followup Agent 与 chat 文字 Agent 替换对应调用点；其余 `run_structured/run_chat` 调用点不动（并存过渡）；
2. **调用点上下文形态冻结说明（R3）**：chat `stream_answer`（同步生成器）与 `_answer`（同步方法）须保持同步形态（`run_sync` 在无事件循环线程安全；上游 #3899 run_sync+子代理死锁未修，且 async 化将撞嵌套事件循环）——写入 agents/runner.py docstring；
3. **delta 等价验收（R5）**：chat 流式路径的 delta 粒度、空串失败语义与现路径逐项等价（`test_slice3_chat`、`test_v2_model_streaming` 通过为准）。
   **回滚**：调用点还原单 commit revert。

### S2 ⬜ 教学步骤 Agent + 字段级回喂（效果主收益）

1. 教学步骤 Agent 替换 teaching/service.py:150；`validate_board` 工具（节点/边上限、边引用、reveal_ms 对齐）以 `ModelRetry` 结构化错误清单回喂；
2. `retries={'output': 2}` 与现有 prompted 重试上限对齐；
3. 必测：Tool Output 无增量流（上游 #4260）→ 桥内流式路径故障注入；网关读超时。
   **验收**：`test_slice4_contracts/teaching` 通过 + RetryPromptPart 字段级回喂断言。

### S3 ⬜ 规划 Agent 族 + 检索工具

1. 路线/大纲/修订/访谈追问 Agent 替换 planning/service.py 三处；`search_materials` 接 retrieval（受控字段、限长摘要）；补 `intake_followup_v1` 模板消 D6；
2. `FallbackModel` 模型级兜底：`fallback_on` 仅异常类型；必测 #2837（不因重试耗尽触发）、#3267（不与适配器重试叠加）；默认关闭灰度。
   **验收**：`test_slice3_planning/contracts`、`test_slice3_draft_confirm`、`test_t07` 通过。

### S4 ⬜ 观测扩展 + 文档同步 + 结构收尾（v1.1：范围校准）

已完成：✅ 内容通道 + OTel 装配 + 删除联动（ADR-022）。剩余：

1. `model.call.*` 扩展维度（agent 轮次/retry 计数/fallback 触发）+ `agent.turn.*` 低基数事件注册；
2. **InstrumentationSettings 落点确认（R6）**：2.51 的 `pydantic_ai.settings` 无此名，先查迁移路径再接 PydanticAI 自动 span；
3. 文档同步（§6）；O-F 权威指针；旧 `model_runner.py` 处置决策（保留 chat 兜底或删除，依 S1b–S3 结果）；
4. 全量回归 + 前端回归（AGENTS.md 清单）。

## 4. 不变量红线（每片验收必查）

切片 1 适配器安全边界不挪动；任务租约/attempt/generation 幂等语义不变；SSE 事件契约与 `Last-Event-ID` 补取不变；前端零依赖不破；规范 §6 允许名单零新增内容字段（内容只走内容通道与 OTel span）；审计追加式不变；供应商原始异常不出边界。

## 5. 测试计划

| 类别 | 内容 |
| --- | --- |
| 单元 | 桥消息形状/门控/缓存/装配、limits 映射、工具出入参、Agent 与模板快照哈希绑定 |
| 集成 | 各切片替换点的任务事务与权限（复用 slice3/4 构造） |
| 故障注入 | 桥超时、工具异常、校验死循环（上限触发）、FallbackModel（#2837/#3267）、流式中断 |
| 前端回归 | S4 全量 |
| 泄漏检查 | agent 历史/RetryPromptPart 不落无内容通道；t08 通道断言扩展 |
| 镜像 | test_mirror_sync 随全量运行（已落地） |

证据归档：`文档/测试归档/logging-audit/evidence/` 新开执行记录（v1.1 路径校准）。

## 6. 文档同步清单（S4 收口）

ADR-023 终稿核对；规范 §3.2/§3.3 增补 `AGENT_LOOP_LIMIT` 与 `agent.turn.*`（通用、无内链）；注册表快照 **registry-v2.json** 重生成（v1.1 校准：不再回 v1）；PRD 版本表补"引入受控工具循环"修订记录；AGENTS.md 基线与权威指针（O-F）；架构正文附录注明重构回归。

## 7. 风险与回滚

原五条（v1.0）仍然有效：PydanticAI 周更回归→锁 minor+独立提交；FallbackModel 叠加延迟→默认关灰度（#3267）；Tool Output 无增量流→桥内流式+故障注入（#4260）；镜像双写→✅ 已由守护测试消解；桥成新安全面→不触边界+审计评审。

v1.1 新增（按评估优先级）：

| # | 风险 | 缓解 |
| --- | --- | --- |
| R1 | **内容捕获断链**：调用点迁移绕过 model_runner 出口，CONTENT_TRACE 覆盖静默丢失 | S1a 首项：content_trace 发射点迁入 bridge/runner |
| R2 | **桥完整度差距被低估**：门控/缓存/装配/流式四件缺 | S1 拆为 S1a（补全）+S1b（切换）；S1a 不动调用点 |
| R3 | **同步/异步嵌套**：run_sync+子代理死锁（#3899）；调用点 async 化将撞运行中事件循环 | S1b 冻结调用点同步形态并写入 runner docstring |
| R4 | **output_mode 构造期 vs 运行期错配** | S1a 前置 spike；不能动态覆写则定型模式分支包装 |
| R5 | **流式等价回退**：delta 粒度/空串语义 | S1b delta 等价验收 + test_v2_model_streaming 把关 |
| R6 | **InstrumentationSettings 2.51 落点未知** | S4 先查迁移路径再接自动 span |

每片独立 commit，回滚=revert 单片；S1a 起桥与旧路径并存，任何一片可停在过渡态。

## 8. 待执行命令清单（v1.1 校准）

```powershell
# S0（剩余）：O-A/O-B 后
uv run --project server/backend pytest tests          # 基线 497 passed / 2 skipped 不降
uvicorn app.main:app --port 8000                       # 入口收敛后验证（待执行）
# S1a/S1b/S2/S3/S4 各片
uv run --project server/backend pytest tests -k "slice3 or slice4 or t07 or t08 or v2 or mirror"
```

## 9. 执行记录

- 2026-09-28 v1.1 修订（本次）：依 ADR-022 实施后评估更新漂移、拆分 S1、新增 R1–R6；未新增代码改动。
