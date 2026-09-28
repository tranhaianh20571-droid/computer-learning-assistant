# Agent 框架效果导向选型报告：pi agent SDK / PydanticAI / CrewAI

> 版本：v2.0｜2026-09-28。性质：**只读调研 + 选型建议，未做任何实现**。
> **本版取代同日 v1.0**。评价基准已按新要求重置：不再考虑工程约束（语言栈、Python 版本、安全边界、遥测合规、部署形态），仅从**最终生成效果**出发，并纳入"未来多 agent 协同"与"自动重试、降级策略灵活度、项目目标匹配度"三个维度。v1.0 的结论一句话存档：在工程约束基准（Python 3.14 / 切片 1 适配器边界 / 日志规范 / 单进程部署）下同样推荐 PydanticAI，否决 CrewAI（3.14 不兼容 + LiteLLM 旁路 + 默认遥测）与 pi（TypeScript-only + 无权限模型）。
> 外部资料检索时间：2026-09-28（Tavily）。版本号/star 数为公开渠道检索时数据；本文不将建议写成已实施事实。

---

## 0. 评价基准重定义：这个项目里"效果"由什么决定

学习助手伙伴的核心产物是**强 schema 约束的教学内容**：教学步骤（30–60 秒讲解 + 板书声明）、路线/大纲树、追问回答。它们不是自由文本，"效果好"意味着：一次生成即合规、不合规时能被具体错误指引着改对、改不对时能换更合适的模型或输出模式再来。据此定义六个效果维度：

| # | 维度 | 为什么决定效果 |
| --- | --- | --- |
| E1 | 结构化输出质量与可靠性 | 教学步骤/板书/路线全部是 pydantic 强约束 JSON；schema 遵从度 = 可用率 |
| E2 | 自纠循环质量 | 错误回喂的**粒度**（字段级 vs "失败了"二值）、失败历史是否可回放/清洗，直接决定第二次尝试的修正率 |
| E3 | 重试与降级的分层控制 | 传输层/模型层/工具层/输出层能否独立配置；降级能否**按输出质量触发**（而不只是按异常） |
| E4 | 检索增强与工具循环 | "先查教材/大纲再作答"是质量上限的主要来源 |
| E5 | 多 agent 协同上限（未来） | 规划/教学/校审/追问多个 agent 的协作能力天花板 |
| E6 | 与目标形态的匹配度 | 本项目的多 agent 形态是**类型化流水线**（上游产出 schema 化交接物给下游），不是角色自由辩论 |

当前基线（model_runner 原生实现）在 E2/E3 的差距是已知瓶颈：prompted 档盲重试 ≤2 次，错误信息只有 `unparseable_output`/`schema_violation` 两个粒度；无模型级降级。

## 1. 候选逐项评估

### 1.1 PydanticAI（pydantic/pydantic-ai，MIT，~18.3k star，v2.2x @2026-08）

**E1 结构化输出：三模式 + schema 即规范。** 官方实现 Tool/Native/Prompted 三种输出模式（与本项目 native→tool→prompted 降级链同构），pydantic 字段 description 直接进入模型可见的 schema——字段级写作规范（如"节点 ≤12、label ≤60 字"）能被模型看到，直接提高一次通过率。v2 默认 `end_strategy='graceful'`：结构化输出与函数工具同轮共存，工具结果（如检索到的教材片段）能反哺最终输出，而不是"出了草稿就截止"。

**E2 自纠循环：字段级错误回喂，三家里最细。** 官方 retries 文档定义了 `RetryPromptPart`：失败信息可以是 `ModelRetry` 字符串（工具/校验器主动抛）**或 Pydantic ValidationError 的错误明细列表**（字段级、逐条），渲染为"Fix the errors and try again"回喂模型。失败历史默认保留在对话里（模型能看到自己错在哪、改过什么），需要清洗时用 `ProcessHistory` 能力过滤——这是"板书节点超上限→模型精确删节点"这种修正的前置条件，比基线的二值盲重试强一个量级。

**E3 重试/降级：唯一把重试当作一等分类学设计的。** 官方明确分层，且各层独立可配：

| 层 | 语义 | 控制点 |
| --- | --- | --- |
| 传输重试 | 同一供应商瞬时失败再试 | provider SDK 配置 |
| 模型降级 | **换模型**，绝不重试同一个 | `FallbackModel`，`fallback_on` 可为异常类型元组/异常处理器/**响应处理器** |
| 工具重试 | 单次工具调用让模型修正 | `retries={'tools': N}` + 每工具限额 |
| 输出重试 | 最终答案让模型修正 | `retries={'output': N}`、`ToolOutput(max_retries=N)` |
| 持久执行 | 整个模型请求由工作流引擎重放 | Temporal/DBOS/Prefect 适配 |

关键亮点是 `FallbackModel.fallback_on` 的**响应处理器**：入参是 `ModelResponse`，可以基于"这次输出的内容质量"（JSON 不可解析、关键字段缺失）触发降级换模型，配合 `ResponseRejected` 异常甚至能让响应处理器主动拒绝响应——**按质量降级**这个本项目降级链想做而没做到的形态，在这里是现成原语。

**E4 工具循环**：`@agent.tool` + 依赖注入 + graceful 循环，工具签名即 schema（docstring 作为工具说明传给模型）。

**E5 多 agent 上限：五种官方模式，活跃投入中。** ① 单 agent；② Agent Delegation（agent 作为另一个 agent 的工具，控制权返回）；③ Programmatic Hand-off（应用代码按输出条件交接）；④ pydantic-graph 类型化状态机（Graph/BaseNode，分支/并发/持久状态）；⑤ Deep Agents（规划+文件操作+沙箱执行，内部即图执行引擎 UserPromptNode→ModelRequestNode→CallToolsNode→End）。路线图活跃：native handoffs/子代理委派（#1978，2026-04 里程碑）、嵌套延迟工具调用（#4302，2026-05）、多 agent HITL 审批（#3274）、A2A（#4101）。外部共识（ZenML/aiagentskit 2026）：最复杂的编排场景推荐 PydanticAI（单体逻辑）+ LangGraph（跨 agent 状态编排）组合——即它的多 agent 是"够用且在变强"，天花板之上还有明确的加购路径。

**E6 匹配度**：类型化交接正是流水线协同需要的——规划 agent 的输出是 `RouteCandidate` 模型，教学 agent 的输入也是它，边界处永远有校验。

**弱点（效果视角）**：多 agent 编排需要自己选型协调模式（框架不替你决定）；原生 handoff 2026 年中才落定，深度未久经考验；复杂持久状态场景官方自己也指向外部（LangGraph/Temporal）。

### 1.2 CrewAI（crewAIInc/crewAI，MIT，54.4k star，v1.15.22 @2026-09-16）

**E1 结构化输出：能力存在，可靠性有实证裂缝。** 两条路径：全量 `Agent + Task(output_pydantic) + Crew`，或轻量 LiteAgent/Flow 内直接调用。社区实证（官方论坛 2026）：**LiteAgent/Flows 路径的结构化输出不可靠**，官方答复是退回全量 Crew——"Flows 本为可靠性而来，关键时刻却要退回 Crews"是用户原话的讽刺。对以结构化 JSON 为产物的项目，这是直接命中要害的证据。

**E2 自纠循环：guardrails 体系，概念等价、粒度较粗。** Task 级 guardrails（函数护栏查硬规则、LLM 护栏查主观质量，链式执行）+ `guardrail_max_retries`：失败时错误字符串回喂 agent 修复重试（旧 `max_retries` 已弃用）。**LLM 护栏评主观质量**（"讲解是否易懂"）是三家里唯一的亮点——函数校验之外多了一层内容质量闸门。但回喂粒度是任务级字符串，无字段级 ValidationError 明细；无失败历史的回放/清洗控制。

**E3 重试/降级：分层最弱。** 任务级 guardrail 重试之外没有模型级降级原语（LiteLLM 的 router/fallback 在网关配置层，控制点不在应用代码）；输出模式降级（native→tool→prompted）无对应概念，需完全自建。降级链这种本项目核心依赖的机制，在 CrewAI 里是"自己写"。

**E4 工具循环**：预置工具生态最丰富（网站搜索、文件读写等开箱即用），原型速度三家里最快。

**E5 多 agent 上限：角色协同的原型之王，但形态特定。** role/goal/backstory 三件套 + sequential/hierarchical/consensual 进程；Flows（@start/@listen/@router 事件驱动状态机 + SQLite 持久化 + 状态恢复）补上了确定性编排。**它的甜区是"研究/写作/审校"式开放协作原型**——角色间传递的是文本上下文。

**E6 匹配度：token 成本与介质双错配。** role/goal/backstory 每次调用前置注入，2026 年独立对比中简单重复任务 token 足迹最重——教学步骤生成恰是高频简单结构化任务；协作介质是文本而非类型，流水线边界无校验保障。

### 1.3 pi agent SDK（earendil-works/pi，MIT，~104k star）

**效果视角下重新审视**：pi 的真实强项是**验证闭环**——极简四工具（read/write/edit/bash）+ 树状会话 + compaction + "让 agent 自己跑 `npm test` 验证"的循环哲学。对编码任务这是实打实的效果来源（产出即被验证）。

**但对本项目**：产出物是结构化教学 JSON 而非文件编辑，pi 没有结构化输出机制（无 schema 遵从、无校验回喂）、没有降级链概念；更关键的是它**设计上反对多 agent**（README 明确：no MCP、no sub-agents、no plan mode——"缺什么就让 pi 自己写什么"）。在"未来多 agent 协同"这个前提下，pi 不是弱，是把门焊死了。**纯效果维度仍然出局，且这次出局与语言栈无关。**

可借鉴：其"循环+工具+验证"的分层极简主义，与 PydanticAI 的 retries 分类学互为印证——效果来自循环里的验证反馈，不来自框架的体积。

## 2. 对比总表（E1–E6）

| 维度 | PydanticAI | CrewAI | pi agent SDK |
| --- | --- | --- | --- |
| E1 结构化输出可靠性 | ✅ 三模式 + 字段描述进 schema + graceful | ⚠️ 全量 Crew 可用；Flows/LiteAgent 路径实证不可靠 | ❌ 无此机制 |
| E2 错误回喂粒度 | ✅ 字段级 ValidationError 明细；历史可回放/清洗 | ⚠️ 任务级字符串；LLM 护栏评主观质量是亮点 | — |
| E3 重试/降级灵活度 | ✅ 五层独立控制；`fallback_on` 响应处理器支持按质量降级 | ⚠️ 仅任务级 guardrail 重试；降级全自建 | ❌ 无 |
| E4 检索/工具循环 | ✅ 类型化工具 + DI | ✅ 预置工具生态最丰富 | ✅（面向终端/文件） |
| E5 多 agent 上限 | ✅ 五模式 + graph；原生 handoff 2026 年中落地，上限可加购 LangGraph | ✅ 角色协同原型最快；Flows 补确定性编排 | ❌ 设计上反对 |
| E6 流水线形态匹配 | ✅ 类型化交接，边界有校验 | ⚠️ 文本介质 + 角色上下文 token 开销最重 | ❌ 领域错配 |

## 3. 结论

**纯效果基准下，推荐依然是 PydanticAI——且这次不是约束妥协下的次优解，而是效果维度的正选。**

用户提出的警觉——"多 agent 角色协同并不代表 CrewAI 效果就一定好"——经调研成立，原因有三层：

1. **本项目的协同形态不是 CrewAI 的甜区。** 学习助手的多 agent 未来是"规划→教学→校审→追问"的**类型化流水线**：上游产出 schema 化交接物、下游消费并校验。CrewAI 优化的是角色间文本上下文的自由协作（研究/写作/审校辩论），其 role/backstory 前置注入在高频简单结构化任务上反而贡献最重的 token 开销。
2. **效果的决定因素在 E1–E3，CrewAI 三项皆不占优。** 结构化可靠性有官方论坛实证裂缝（Flows 路径退回 Crews）；错误回喂是任务级字符串而非字段级；降级无原语全靠自建。多 agent 编排能力只有在 E1–E3 托底时才能转化为最终效果——底座不牢，协同再花哨也是把不可靠的输出在 agent 间传递。
3. **PydanticAI 的 `FallbackModel(fallback_on=响应处理器)` 恰好实现了最灵活的降级形态**——按输出质量（而非仅异常）换模型，加上五层独立重试控制，对"生成质量不稳定就要有系统性兜底"的本项目是最直接的药。

**pi 在效果维度同样出局**：无结构化机制、无降级、设计上拒绝多 agent；它的验证闭环思想值得吸收，依赖不值得引入。

**两版报告（约束基准 v1.0 / 效果基准 v2.0）收敛到同一结论，结论稳健性高。** 何时需要重看 CrewAI：产品形态演化为开放式研究型多 agent 协作（多角色辩论产出报告类内容）时。何时考虑 PydanticAI 之外的加购：长周期复杂状态编排（业界共识 PydanticAI + LangGraph 组合）。

## 4. 效果导向的架构建议（若采纳；均为提案，未实施）

按"对效果的边际贡献"排序：

1. **用字段级错误回喂替换盲重试（E2，最大单项收益）**：`BoardDeclaration` 的校验失败（节点/边上限、边引用不存在、reveal_ms 不对齐）经 `ModelRetry`/ValidationError 明细回喂，模型按字段修正——预期显著提高第二次尝试的通过率；
2. **按质量降级（E3）**：`FallbackModel` 响应处理器判定"JSON 不可解析/关键字段缺失"→切换模型；输出模式降级（native→tool→prompted）仍按能力探测先行，形成 模式降级×模型降级 二维兜底；
3. **guardrail 式主观质量闸门（借 CrewAI 之长）**：在输出重试层加一个 LLM 护栏（"讲解是否超纲/是否贴合大纲"），失败经 ModelRetry 回喂——CrewAI 的 LLM 护栏思想可以平移到 PydanticAI 的循环里，不需要为此引入 CrewAI；
4. **多 agent 演进三阶段**：① 单 agent + `validate_board`/`search_materials` 工具（E4 补齐）；② planner→teacher→reviewer 用 Agent Delegation 串联（类型化交接）；③ 出现分支/并发/持久状态需求时上 pydantic-graph，仍不够再评估 LangGraph 编排层；
5. **重申独立工作项**：提示词 few-shot、板书/导图布局引擎、网关档位探测——三项不被任何框架解决，效果收益不亚于换框架。

工程落地路径（FunctionModel 桥接切片 1 适配器、日志/测试对齐）见 v1.0 报告第 6 节设计，本版不重复；两版建议不冲突，效果优先时可先在旁路验证 E2/E3 收益再全量接入。

## 5. 参考来源（检索于 2026-09-28）

- **PydanticAI 重试/降级**：pydantic.dev/docs/ai/core-concepts/retries（重试分类学、RetryPromptPart、与 FallbackModel 的边界）；pydantic.dev/docs/ai/api/models/fallback（FallbackOn 类型、响应处理器、ResponseRejected）；issues #2837、#3267（FallbackModel 触发条件与 SDK 重试冲突）
- **PydanticAI 输出/多 agent**：pydantic.dev/docs/ai/core-concepts/output（三模式）；pydantic.dev/docs/ai/api/pydantic-ai/agent（end_strategy）；deepwiki.com/pydantic/pydantic-ai §Multi-Agent（五模式、graph 引擎）；issue #5039（多 agent 路线图依赖图）；ZenML 与 aiagentskit 2026 对比（+LangGraph 组合共识）
- **CrewAI**：docs.crewai.com v1.15.22 Tasks（guardrails、guardrail_max_retries、max_retries 弃用）；docs.crewai.com Flows（状态/持久化）；官方论坛帖 7016（Flows/LiteAgent 结构化输出不可靠、退回 Crews）；analyticsvidhya guardrails 指南（函数/LLM 护栏、重试语义）；truefoundry/gurusup 2026 对比（token 足迹）
- **pi**：earendil-works/pi（GitHub）、pi.dev；thomas-wiegold.com 评测（no MCP/sub-agents/permissions 引 README）；explainx.ai 2026 harness 评测
- **项目内部**：`server/backend/app/planning/model_runner.py`（基线重试/降级现状）、`teaching/outputs.py`（BoardDeclaration 校验点）、2026-09-28 生成链路只读调查（本会话）、本报告 v1.0（同日，工程约束基准）
