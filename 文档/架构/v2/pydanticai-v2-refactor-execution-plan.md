# PydanticAI v2 重构执行方案与项目结构优化

> 版本：v1.0｜2026-09-28。**这是执行清单不是现状描述**：本方案及其全部子任务均为 ⬜ 待执行，截稿时未实施任何代码改动。
> 依据：同目录四份调研报告（三框架选型 v2.0、pi 头对头、PydanticAI 深挖）+ 2026-09-28 只读调研（调用点盘点、测试结构、结构诊断）。
> 所在工作 `文档/架构/v2/` 不入 git（`.gitignore` 忽略整个 `文档/`）；执行产生的代码改动按 AGENTS.md 流程另行提交。
> 状态标记：✅ 已完成｜⬜ 待执行｜⏸ 暂缓（附重议信号）。未实际运行的命令一律标"待执行"。

---

## 0. 决策基线（不需要再讨论）

| 决策 | 依据 |
| --- | --- |
| 采用 PydanticAI（`pydantic-ai-slim`，锁 minor 版本） | 四份调研报告收敛结论；ADR-019 预留回迁路径 |
| FunctionModel 桥：全部模型调用仍经切片 1 适配器，凭据/SSRF/外发核验/错误码映射不动 | ADR-019 决定原文 + 选型报告 §6 |
| 生成入口收敛为 `app/agents/` 包：五+1 个 Agent（任务规格，非角色）+ 三受控工具 | 2026-09-28 调用点盘点（`run_structured` ×5、`run_chat` ×2） |
| 降级链语义保留：能力探测 + 失败缓存 → `output_mode` 选择；`FallbackModel` 仅做模型级兜底 | model_runner T04 语义与 PydanticAI retries 分类学对齐 |
| 观测双层：自建 `logging_audit` 承载无内容事件与聚合指标；OTel 埋点启用，trace 经 OTLP 出口 Langfuse，内容三项（提示词/回答/用户问题）进内容通道 | ADR-022（取代 ADR-020）+ 规范 §6 通道化重写 |
| 任务租约/幂等/generation 围栏、SSE 事件契约、前端零依赖、审计追加式——全程不动 | 架构 §3.2-3.3 + AGENTS.md |

## 1. 当前项目结构诊断与优化建议（管理视角）

### 1.1 结构现状（事实）

```
server/backend/app/
├── main.py                 # 聚合 api_router（模板路由）+ create_slice1_app
├── slice0_app.py ~ slice3_app.py   # 层叠式应用：逐片挂载域路由
├── api/                    # FastAPI 模板路由（items/users/login/utils/private/learning）
├── core/                   # config/db/security（模板骨架）
├── auth/ cleanup/ materials/ planning/ teaching/ tasks/ slice1/ logging_audit/ prompts/
├── crud.py models.py db.py # 模板残骸（68/137/7 行，item/user CRUD）
└── tests/                  # 模板测试残骸（api/crud/utils/test_user）
tests/                      # 仓库可执行测试唯一真源
└── support/backend/        # 生产 app/ 的全量镜像（测试导入镜像实现）
```

### 1.2 诊断清单（编号 D*，均附代码证据）

| # | 问题 | 证据 | 管理代价 |
| --- | --- | --- | --- |
| D1 | **产品级唯一入口缺失**：main.py 只聚合模板路由 + 切片 1；切片 2/3 靠 sliceN_app 层叠；教学（切片 4）/删除（切片 5）路由未见于任何聚合入口 | main.py:14 仅 `create_slice1_app`；api/main.py 全为模板路由；无 slice4/slice5 app | 启动哪个 app 说不清；新路由挂载位置靠口口相传；compose 启动命令待执行验证 |
| D2 | **测试镜像双写**：`tests/support/backend/` 全量镜像生产代码，每次改动双写 | tests/README.md"镜像同步"约定；support/backend 含 planning/teaching 等全套 | 双写遗漏=测试跑旧代码；漂移无自动检测 |
| D3 | **模板残骸未清**：full-stack-fastapi-template 的 item/user CRUD、模板测试、模板路由仍在线 | app/crud.py、app/models.py（item/user）、app/db.py（7 行）、app/tests/、api/routes/items.py | 认知噪音；新贡献者误改无关文件；安全审计面虚大 |
| D4 | **分层倒置**：业务模块反向依赖观测模块的基础设施 | auth/cleanup/materials/prompts/alembic 均 `from ..logging_audit.db import Database`、`..logging_audit.models import Base` | 观测包成为全局耦合点；重构 logging_audit 牵一发动全身 |
| D5 | **planning 包过载**：17 个文件混合域服务、输出契约、场景渲染、检索、对话 | app/planning/ 目录清单 | 找文件靠记忆；agents 化后职责应再分层 |
| D6 | **提示词来源不一致**：6 个模板文件走快照绑定，访谈追问是内联 prompt | fallback_templates/ 7 个 json vs planning/service.py:476-484 内联 messages | 版本绑定/哈希审计对内联 prompt 失效 |
| D7 | **双测试目录**：app/tests（模板）与根 tests/（真源）并存 | 两处目录 | 入口歧义 |

### 1.3 结构优化项（编号 O*；并入重构切片执行，单独排期也可）

| # | 优化 | 做法 | 时机 | 风险 |
| --- | --- | --- | --- | --- |
| O-A | **入口收敛** | 新建 `app/assembler.py`：`create_app()` 按固定顺序挂载全部域路由（auth→materials→planning→teaching→cleanup）；main.py 改为 `app = create_app()`；sliceN_app 保留为薄别名（`create_sliceN_app = create_app` 分步别名）避免破坏既有测试导入；compose 启动命令同步改并验证 | S0（先行，给重构一个确定入口） | 低；别名保兼容 |
| O-B | **模板残骸清理** | 核对零引用后删除：app/tests/、app/crud.py、app/models.py、app/db.py、api/routes/items.py（及 users/utils 中纯模板部分，learning/login/private 保留）；alembic env 引用核查 | S0 | 低；删前 grep 引用 + 全量测试 |
| O-C | **基础设施上提** | `Database`/`Base` 上提到 `app/core/db.py`；logging_audit 保留 re-export（`from ..core.db import Database`）兼容既有 import；新代码一律从 core 导入 | ⏸ 暂缓：改动面大（5+ 模块），与 agent 重构正交；重议信号=重构期间第二次触碰这些 import 时顺手做 | 中 |
| O-D | **镜像治理** | 短期：新增同步校验脚本（对比 `server/backend/app` 与 `tests/support/backend` 各文件 SHA-256，差异即失败），挂入 pytest 顶层收集；中期 ⏸：评估改真源导入（需验证当初镜像的包路径原因） | S0（脚本）/⏸（真源化） | 低/中 |
| O-E | **planning 分层** | agents 包承担生成入口后，planning 保留域服务（drafts/scope/overview/scene/deletion）；不强拆 | S3 后自然形成 | — |
| O-F | **权威指针固化** | AGENTS.md 增补：唯一入口=assembler、测试真源=根 tests/、镜像=support/backend、ADR=v1/adr-ai-stack-differences.md、规范=文档/规范/ | S4 | 低 |

## 2. 目标架构

### 2.1 分层与调用时序

```
任务 worker（tasks/service.py 租约/attempt 围栏，不动）
   │
AgentRunner（app/agents/runner.py：UsageLimits→错误码、事件→task_events/model.call.*）
   │
PydanticAI Agent（五+1 个任务规格）
   │  tools: search_materials / validate_board / get_outline_context
FunctionModel 桥（app/agents/bridge.py：能力门控+失败缓存→output_mode；消息/错误码翻译）
   │
切片 1 适配器（不动：凭据解密、HTTPS 强制、SSRF 校验、探测样例、超时）
```

### 2.2 `app/agents/` 包文件清单

| 文件 | 职责 |
| --- | --- |
| `bridge.py` | FunctionModel 实现：包装适配器 `generate`/`generate_stream_text`；`ModelCapabilityGate` 逻辑迁入（含 6h 失败缓存）；供应商异常→`MODEL_TIMEOUT`/`MODEL_CAPABILITY_UNAVAILABLE` |
| `agents.py` | 五+1 Agent 定义（见下表）；instructions 从模板快照加载（含 `agent_code_version` 记入绑定） |
| `tools.py` | 三个受控工具：入参出参全 pydantic，返回值经 sanitize，不落正文 |
| `runner.py` | 统一调用入口：装配 deps（DB 句柄/检索器）、UsageLimits、事件钩子 |
| `limits.py` | 循环上限→受控错误码映射（新增 `AGENT_LOOP_LIMIT`） |

### 2.3 Agent 清单（复用现有 Candidate 模型，零新 schema）

| Agent | instructions | output_type | 替换的调用点 |
| --- | --- | --- | --- |
| 路线生成/修订 | `route_generate_v1`/`planning_revise_v1` | `RouteCandidate` | planning/service.py:331 |
| 大纲生成 | `outline_generate_v1` | `OutlineCandidate` | planning/service.py:374 |
| 访谈追问 | 补 `intake_followup_v1` 模板（消 D6） | `IntakeQuestionSet` | planning/service.py:485 |
| 教学步骤 | `teaching_step_v1` | `LessonPlanCandidate` | teaching/service.py:150 |
| 追问回答 | `followup_v1` | `FollowupCandidate` | teaching/service.py:654 |
| 即时对话 | `chat_answer_v1` | `str` | planning/chat.py:297/422（含 291 的 stream 路径→Agent 流式） |

## 3. 执行切片（S0–S4；每片独立提交、独立回滚）

### S0 ⬜ 准备与围栏（不动生成行为）

1. ADR-021 追加至 `文档/架构/computer-learning-assistant-architecture.md（附录·决策记录）`（装回 pydantic-ai-slim、桥设计、与 ADR-019/020 关系）；
2. `uv add "pydantic-ai-slim[openai,anthropic,gemini]"` 锁定 minor（Python 3.14 兼容已在 ADR-019 验证，仍需重跑冒烟）——待执行；
3. O-D 同步校验脚本 + O-B 模板残骸清理 + O-A 入口收敛（assembler.py + main.py 改造 + compose 启动验证——待执行）；
4. 全量基线：`uv run --project server/backend pytest tests`（当前基线 451 passed，重跑确认）——待执行。
   **验收**：基线不降；镜像校验脚本通过；`uvicorn app.main:app` 可启动并挂载全部域路由（待执行验证）。

### S1 ⬜ 桥与最小替换（爆炸半径=追问回答+即时对话）

1. `bridge.py`+`runner.py`+`limits.py` 落地；合成适配器经 FunctionModel 持有，复用现有测试构造；
2. Followup Agent 与 chat 文字 Agent 替换对应调用点；`model_runner.run_structured/run_chat` 其余调用点不动（桥与旧路径并存过渡）；
3. 生产+镜像双写；新增测试：桥消息形状/错误映射、`AGENT_LOOP_LIMIT`、泄漏检查（agent 历史不进日志）。
   **验收**：`test_slice4_teaching`（追问部分）、`test_slice3_chat`、`test_v2_model_streaming` 通过；`model.call.*` 事件字段断言更新后通过。**回滚**：调用点还原单 commit revert。

### S2 ⬜ 教学步骤 Agent + 字段级回喂（效果主收益）

1. 教学步骤 Agent 替换 teaching/service.py:150；`validate_board` 工具（跑 BoardDeclaration 校验：节点/边上限、边引用、reveal_ms 对齐）以 `ModelRetry` 结构化错误清单回喂；
2. `retries={'output': N}` 与现有 prompted 重试上限语义对齐（N=2）；
3. 必测已知坑：Tool Output 无增量流（上游 #4260）→ 桥内保留 `generate_stream_text` 路径验证；网关读超时场景故障注入。
   **验收**：`test_slice4_contracts/teaching` 通过 + 新增回喂断言测试（错误明细字段级出现在 RetryPromptPart）。

### S3 ⬜ 规划 Agent 族 + 检索工具

1. 路线/大纲/修订/访谈追问 Agent 替换 planning/service.py 三处；`search_materials` 工具接 retrieval（受控字段、限长摘要）；intake 补模板文件消 D6；
2. `FallbackModel` 引入模型级兜底：`fallback_on` 仅异常类型（`ModelAPIError` 映射）；**必测**：不因重试耗尽触发（上游 #2837）、与适配器层重试不叠加（#3267）——fallback 配置默认关闭，灰度开启。
   **验收**：`test_slice3_planning/contracts`、`test_slice3_draft_confirm`、`test_t07_model_call_metrics` 通过。

### S4 ⬜ 观测扩展 + 文档同步 + 结构收尾

1. `model.call.*` 扩展维度（agent 轮次/retry 计数/fallback 触发）+ `agent.turn.*` 低基数事件注册进 contracts.py（走允许名单，无内容字段）；
2. 文档同步（见 §6）；O-F 权威指针固化；旧 `model_runner.py` 处置决策（保留为 chat 兜底或整体删除，依 S1-S3 结果定）；
3. 全量回归 + 前端回归（`/course`→`/course/detail`→`/lecture`、设置页、移动端、无真实网络请求——AGENTS.md 前端回归清单）。

## 4. 不变量红线（每片验收必查）

切片 1 适配器安全边界（HTTPS/私网拒绝/凭据脱敏/探测样例）不挪动；任务租约/attempt/generation 幂等语义不变；SSE 事件契约与 `Last-Event-ID` 补取不变；前端零依赖不破；规范 §6 允许名单零新增内容字段；审计追加式不变；供应商原始异常不出边界。

## 5. 测试计划（映射 AGENTS.md 测试要求）

| 类别 | 内容 |
| --- | --- |
| 单元 | 桥消息形状/错误映射、limits 映射、工具出入参 schema、Agent 定义与模板快照哈希绑定 |
| 集成 | 各切片替换点的任务事务与权限（复用 slice3/4 既有集成测试构造） |
| 故障注入 | 桥超时、工具异常、校验死循环（上限触发）、FallbackModel 触发语义（#2837/#3267）、流式中断 |
| 前端回归 | S4 全量（AGENTS.md 清单） |
| 泄漏检查 | agent 历史/RetryPromptPart 内容不落日志；t02/t07 模式扩展断言 |
| 镜像 | 同步校验脚本（O-D）随每次 CI/本地全量运行 |

证据归档：`文档/规范/tests/logging-audit/evidence/` 新开执行记录，注明运行目录、依赖版本、命令、结果、证据路径；未运行标"待执行"。

## 6. 文档同步清单（S4 收口，逐项打勾）

ADR-021（S0 已写）核对终稿；`logging-and-audit-spec.md` §3.2/§3.3 增补 `AGENT_LOOP_LIMIT` 与 `agent.turn.*`（保持通用规范、无内链）；`tests/logging-audit/schemas/registry-v1.json` 快照重生成；PRD 版本表补"引入受控工具循环"修订记录（解除 ADR-019 所引"首版排除多轮工具调用"限定）；AGENTS.md 更新基线数字与权威指针（O-F）；架构正文 §3.3 与现状的衔接说明（171 行本就按此设计，无需改写，仅在 ADR-021 注明回归）。

## 7. 风险与回滚

| 风险 | 缓解 |
| --- | --- |
| PydanticAI 周更节奏引入回归 | 锁 minor；升级走独立 commit + 全量回归 |
| FallbackModel 与适配器重试叠加放大延迟 | 默认关闭，灰度；必测项 #3267 |
| Tool Output 无增量流触发网关超时 | 桥内保留流式路径；S2 故障注入覆盖 |
| 镜像双写遗漏 | O-D 校验脚本强制 |
| 桥成为新安全面 | 桥不触凭据/网络边界（仍经适配器）；纳入审计类别评审 |

每片独立 commit，回滚=revert 单片；S1 起新旧路径并存过渡，任何一片可停在过渡态。

## 8. 待执行命令清单（全部待执行）

```powershell
# S0
uv add --project server/backend "pydantic-ai-slim[openai,anthropic,gemini]"
uv run --project server/backend pytest tests
uvicorn app.main:app --port 8000   # 入口收敛后验证
# S1-S4 各片
uv run --project server/backend pytest tests -k "slice3 or slice4 or t07 or v2"
```

## 9. 执行记录（占位；执行后按"日期/切片/命令/结果/证据路径"追加）
