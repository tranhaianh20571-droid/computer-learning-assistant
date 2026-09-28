# PydanticAI vs pi：当前项目需求效果导向对比报告

> 版本：v1.0｜2026-09-28。性质：**只读调研 + 选型建议，未做任何实现**。
> 范围：CrewAI 已确认暂不引入，退出比较。本报告只回答一个问题——**仅从满足当前项目需求的效果出发，PydanticAI 和 pi 推荐哪个**。前置报告：`agent-framework-selection-report.md`（v2.0，三框架效果基准）。
> 本报告含一处对前置报告的修正：pi 的多 agent 能力评估（见 §1）。
> 外部资料检索时间：2026-09-28（Tavily）。

---

## 0. 当前项目需求清单（效果视角，源自代码事实）

"当前需求"不是泛化的 agent 能力，而是学习助手伙伴**今天就在跑的生成链路**：

| # | 需求 | 载体 |
| --- | --- | --- |
| N1 | 规划访谈与路线/大纲生成（强 schema 树形结构 + 先修关系） | `RouteCandidate`/`OutlineCandidate`/`IntakeQuestionSet`（planning/outputs.py） |
| N2 | 教学步骤生成（30–60 秒讲解 + 板书声明：节点/边/公式/代码白名单校验） | `LessonPlanCandidate`/`BoardDeclaration`（teaching/outputs.py） |
| N3 | 追问回答（纯文字 + 可选板书） | `FollowupCandidate` |
| N4 | 检索增强（教材/大纲先查再用） | `planning/retrieval.py` |
| N5 | 结构化输出降级链（native→tool→prompted + 能力探测 + 失败缓存） | `model_runner.py` |
| N6 | 校验失败后的重试效果（当前瓶颈：错误粒度仅二值，盲重试 ≤2 次） | model_runner prompted 档 |

共同特征：**产物是 schema 受限的短生命周期任务型 JSON，不是文件工作区里的长会话产物**。这个特征是两框架效果分化的根源。

## 1. 先修正前置报告：pi 的多 agent 论点成立

前置报告 v2.0 称 pi"设计上拒绝多 agent，把门焊死"——**这个表述需要修正**。核心确实刻意不做（README：no MCP、no sub-agents），但 pi 的设计哲学是"扩展性优于内置"，门是留着的，且社区已经把门打开了：

- **`pi-a2a-communication`**（pi.dev 官方包仓库，npm 第三方包）：实现 A2A Protocol 规范——`.well-known/agent-card` 发现注册、五种任务执行模式（single / parallel / chain / async / streaming）、`/a2a-server` 服务模式、`/a2a-chain scout | planner | worker` 式流水线；
- **`pi-collaborating-agents`**（GitHub）：orchestrator 派生多个协作 pi 实例（进程模式后台子进程 / cmux 可视分屏模式），父会话自动收集子代理产出，消息路由支持 `followUp` 排队与 `urgent: true` 打断（steer）——这就是 Claude Code team 式的多实例协作；
- **OpenClaw**（pi SDK 的旗舰应用）：四级多 agent——兄弟主 agent 间 `sessions_send` 乒乓消息（可配 maxPingPongTurns）、`sessions_spawn` 子代理、直至 A2A 跨框架互联（a2a-adapter 兼容 n8n/CrewAI/LangChain/LangGraph）。

所以"pi 不能做多 agent / A2A / team"不成立。**但据此推不出"为多 agent 选 pi"**，原因见 §3.3：A2A 是框架无关的开放协议（Google 2025-04 发起、现由 Linux 基金会托管，IBM/Mastra/OpenAgents 等生态通用），PydanticAI 同样在路线图上（#4101 A2A deps、#4330 向 A2A 暴露工具调用），OpenAgents 2026-01 已把 Pydantic AI 列为可互联框架之一。"想做 A2A"是选**协议**的问题，不是选框架的问题——任何框架都能挂 A2A，选谁都不锁死这个未来。

另需注明：pi 的 A2A/协作扩展均为**社区第三方包**（pi-a2a-communication v1.0.1，个人维护；pi 官方安全提示"包可执行代码、装前审源"），与 PydanticAI 由 pydantic 团队官方推进的路线图，维护主体和成熟度不同。

## 2. 逐需求对比

### 2.1 PydanticAI 对 N1–N6 的覆盖

| 需求 | 机制 | 状态 |
| --- | --- | --- |
| N1–N3 强 schema 生成 | `output_type=Candidate模型`；字段 description 进模型可见 schema；三输出模式（Tool/Native/Prompted） | ✅ 现成，与降级链同构 |
| N4 检索增强 | `@agent.tool` 包装 retrieval + DI；v2 graceful：工具结果反哺最终输出 | ✅ 现成 |
| N5 降级链 | 三模式 + `FallbackModel`（`fallback_on` 支持按**响应质量**换模型） | ✅ 现成，且强于现有实现 |
| N6 校验回喂 | `RetryPromptPart` 携带**字段级** ValidationError 明细，"Fix the errors and try again"回喂；失败历史可回放/清洗 | ✅ 现成，直击当前瓶颈 |

从需求到机制是**声明式映射**：六个需求对应六个内置原语，没有一个需要自建。

### 2.2 pi 对 N1–N6 的覆盖

pi 的分层（pi-ai 统一 LLM 抽象 / pi-agent-core 通用循环+工具+事件 / pi-coding-agent 会话+扩展 / pi-tui）确实提供了地基，逐项看：

| 需求 | pi 提供什么 | 缺口（需自建） |
| --- | --- | --- |
| N1–N3 强 schema 生成 | pi-ai 能调模型、能发请求 | ❌ 无 schema 受限生成机制：response_format/tools 参数拼装、JSON 提取解析、pydantic 校验、失败重试——等于把 `model_runner.py` 的 native/tool/prompted 三档用 TypeScript 重写一遍 |
| N4 检索增强 | pi-agent-core 工具执行 + 自定义工具 | ⚠️ 工具要用 TS 写并跨进程访问 Python 侧的 DB/retrieval（经 RPC 桥），或另建 TS 数据访问层 |
| N5 降级链 | 无 | ❌ 能力探测、模式降级、失败缓存全部自建 |
| N6 校验回喂 | 无（循环有，反馈语义无） | ❌ ValidationError→消息回喂、字段级明细格式化、历史管理自建 |

pi **有**而本项目当前用不上的：read/write/edit/bash 四工具、文件树会话、compaction、TUI、skills、`pi install` 生态——这些效果护城河全部绑定"终端编码工作形态"（产物是文件、可跑命令验证），而本项目产物是数据库里的结构化行（§0 特征）。

### 2.3 效果论：差距不在循环，在循环里的反馈质量

两家都有 agent 循环（pi-agent-core 的循环甚至更精炼）。但 2026-09-28 调查确认的当前瓶颈恰恰不是"没有循环"，而是**反馈质量**：错误回喂粒度二值化、无字段级指引、无按质量降级。PydanticAI 的全部价值恰好堆叠在这一层（RetryPromptPart 字段级明细 / 五层重试 / 质量触发 FallbackModel）；pi 在这一层是空白——选择 pi 意味着用自建代码去逼近 PydanticAI 经 18k star 社区打磨的现成机制，**效果上限 = 自建投入的上限**。

一个类比：pi 卖的是一台优秀的发动机（循环+工具+会话），PydanticAI 卖的是整车（发动机 + schema 遵从变速箱 + 校验回喂 ABS + 降级安全带）。本项目要造的是车；发动机再好，其余部件自己造的成本和打磨度决定了最终效果。

## 3. 对"为未来选 pi"三个论点的正面回答

### 3.1 "pi 可以自己加 agent / A2A / team"——成立，但不改变结论

成立（§1 证据）。但注意两点：其一，这些是第三方扩展而非核心，承担相应的维护与安全审查成本；其二，**Claude Code team 式协作的价值前提本项目不具备**——那类协作（多实例 + 共享工作区 + 消息协调）优化的是"多个自主 agent 长期并行操作文件、以工作区为协调介质"的场景。学习助手的多 agent 未来是**进程内类型化流水线**（规划→教学→校审，交接物是 pydantic 模型），PydanticAI 的 delegation/graph 正是这个形态的原生表达，无需引入网络介质与消息队列。

### 3.2 "A2A 是未来方向"——与框架选择正交

A2A 是开放协议，框架是实现载体。PydanticAI 有官方 A2A 路线（#4101/#4330），生态侧已可互联（OpenAgents 列表）。若未来确需跨框架/跨机 A2A，届时在 PydanticAI 外挂 A2A server 与把 pi 挂上 A2A 的工作量同级。**用 A2A 论证选 pi，等于用"能上网"论证选某台电脑。**

### 3.3 "pi 的验证闭环是真效果来源"——对，但要有用武之地

pi 哲学的精髓（agent 自己跑测试验证产物）在编码场景是实打实的效果倍增器。学习助手当前链路没有"可执行验证"环节（教学 JSON 的验证是纯 schema 校验，PydanticAI 循环内即可完成）。**若未来出现这类需求**——例如自动出题→LaTeX 编译→渲染检查→修订、板书场景→无头渲染截图→视觉校验→调整布局——pi 式的"文件+命令"验证循环将成为该环节的真效果来源。届时合理形态是以 RPC 边车把 pi 接入**那一环**，而不是用它承托整条生成链。

## 4. 结论

**仅从满足当前项目需求的效果出发，推荐 PydanticAI，且是明确胜出而非险胜。**

理由归纳：

1. **需求-机制全对齐**：N1–N6 六项当前需求在 PydanticAI 侧全部是现成原语（声明式映射），在 pi 侧四项要自建（其中 N6 恰是已识别的当前效果瓶颈）；
2. **效果瓶颈的药方在 PydanticAI 的核心里**：字段级 ValidationError 回喂、按响应质量降级的 FallbackModel——这两项直接决定"生成不好时能不能系统性变好"，pi 在此空白；
3. **pi 的效果护城河对当前工作形态不发力**：其全部独有优势（四工具/树会话/compaction/验证闭环）绑定文件+终端形态，而本项目是 schema 受限的服务端任务型生成；
4. **多 agent 与 A2A 不构成选 pi 的理由**：A2A 是框架无关协议（两家都有路径）；本项目需要的多 agent 形态（类型化流水线）恰是 PydanticAI 的原生模式、pi 扩展生态的非典型用法；
5. **pi 保留为组件级选项**：出现"需真实执行环境的验证环节"（编译/渲染/测试产物）时，以边车形式接入该环节——pi 是潜在的**好零件**，不是当前需求的**好底盘**。

## 5. 参考来源（检索于 2026-09-28）

- **pi 多 agent/A2A 生态**：pi.dev/packages/pi-a2a-communication（A2A 协议扩展，v1.0.1，五种任务模式/agent-card/server 模式/包安全提示）；github.com/baochunli/pi-collaborating-agents（多实例协作扩展，followUp/steer 消息路由）；reddit r/openclaw 多 agent 实践帖（sessions_send/sessions_spawn/A2A 四级形态，a2a-adapter 兼容 CrewAI/LangGraph 等）；nader.substack.com（pi 四层栈与 OpenClaw 架构）
- **A2A 协议**：IBM Think（Google 2025-04 发起，跨框架消息层定位）；mastra.ai（A2A 双向实现示例）；medium/@openagents（2026-01，Pydantic AI 列入可互联框架）
- **PydanticAI**：pydantic.dev/docs/ai/core-concepts/retries（RetryPromptPart/五层重试）；pydantic.dev/docs/ai/api/models/fallback（fallback_on 响应处理器）；deepwiki §Multi-Agent（delegation/graph 五模式）；issue #5039（#4101 A2A deps、#4330 A2A 暴露，路线图）
- **项目内部**：`planning/model_runner.py`（N5/N6 现状）、`teaching/outputs.py` 与 `planning/outputs.py`（N1–N3 schema）、`planning/retrieval.py`（N4）、本会话 2026-09-28 生成链路调查、前置报告 `agent-framework-selection-report.md` v2.0
