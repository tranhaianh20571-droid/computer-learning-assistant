# 切片 3 执行记录（T01～T09）

> 执行日期：2026-09-27。执行环境：Windows 11 10.0.26200 x64，Git Bash；Python 3.14.7（`uv run --project server/backend`）；Node ≥22 + Vite 8.3.1 + `@excalidraw/excalidraw` 0.18.1；PostgreSQL 开发库 `learning_assistant_tpl`（唯一库，沿用切片 2 基线）。
> 交接文档：[slice-3-planning-dialogue-handoff.md](../../../架构/slice-3-planning-dialogue-handoff.md)。

## 1. 结果总览

| 任务 | 状态 | 证据 |
| --- | --- | --- |
| T01 模型能力探测 spike | **脚本就绪并验证可运行；真实探测外部阻断**（同切片 2 T01 的前置类别） | [slice3-model-capability-spike.md](slice3-model-capability-spike.md) |
| T02 Excalidraw/字体 spike | **完成（Accept）**：字体清单、许可证链、布局常数锁定；浏览器渲染走查随 T06 完成 | [slice3-excalidraw-font-spike.md](slice3-excalidraw-font-spike.md) |
| T03 契约冻结 | **完成**：20 张新表迁移 `d3f6a9b2c4d5`，upgrade/downgrade/upgrade 往返通过；12 个新错误码注册；Pydantic 输出模型与预算常量冻结 | 本文 §3 |
| T04 规划生成 worker | **完成**：四入口、auto 范围解析与澄清等待、真实搜索尝试与决策等待、prompt 绑定固定、manifest 不含正文、降级链、候选/草稿落库 | `tests/test_slice3_planning.py` 15 项 |
| T05 草稿与确认 | **完成**：乐观锁、确认事务、幂等键、结构/DAG 校验、路线展开大纲继承、教学门槛 | `tests/test_slice3_draft_confirm.py` 11 项 |
| T06 场景与导图 | **完成**：白名单场景转换、稳定 ID 双向映射、确定性布局、只读画布、层级列表编辑、中文不溢出 | `tests/test_slice3_scene.py` + 浏览器走查 §5 |
| T07 总览与导航 | **完成**：同版口径、无整体百分比、准备状态、教学门槛禁用、导航状态、360px 窄屏 | `tests/test_slice3_overview.py` + 走查 §5 |
| T08 即时文字对话 | **完成**：generation 递增、刷新恢复不重发、资料快照、预算 manifest、等待决策恢复、无音频/板书/播放 | `tests/test_slice3_chat.py` 10 项 + 走查 §5 |
| T09 故障注入与总验收 | **完成**：九项注入通过；基线 277 项不回归；全量 **372 passed** | `tests/test_slice3_faults.py` 13 项 |

最终命令与结果（仓库根）：

```text
uv run --project server/backend pytest tests -q
# 372 passed, 1 warning in ~35s   （277 基线 + 95 新增）
cd frontend && npm run build      # ✓ built（仅 chunk 体积告警）
cd frontend && npm run lint       # 0 error, 6 warning（1 条为切片 0 原有）
```

## 2. 改动清单

后端（`server/backend/`，全部镜像至 `tests/support/backend/`）：

- `app/planning/`（新增模块）：`models.py`（20 张表行模型）、`errors.py`、`outputs.py`（RouteCandidate/OutlineCandidate/ChatAnswer + 预算常量 + DAG/结构校验）、`scope.py`、`retrieval.py`、`search.py`、`model_runner.py`（native→tool→prompted 降级链）、`scene.py`（确定性布局与白名单转换）、`drafts.py`、`service.py`、`chat.py`、`overview.py`、`router.py`。
- `app/slice3_app.py`（新增）：切片 2 之上装配规划/聊天路由与错误处理；`LEARNING_DEV_SYNTHETIC_MODEL=1` 时启用**仅本地走查**的合成适配器（不外发、无凭据）。
- `app/alembic/versions/d3f6a9b2c4d5_slice3_planning_chat.py`（新增迁移）；`app/alembic/env.py` 注册 planning 模型。
- `app/logging_audit/contracts.py`：新增 12 个受控错误码（等待态 alert_level=none、不重试）。
- `app/tasks/service.py`：新增 `requeue_after_wait`（等待决策后 generation+1 重排队，围栏语义延续）。
- `app/prompts/fallback_templates/`：新增 `route_generate_v1` / `outline_generate_v1` / `planning_revise_v1` / `chat_answer_v1`（含 SHA-256，本地后备）。

前端（`frontend/src/`）：

- `excalidraw/ReadonlyCanvas.tsx`（新增）：只读画布封装，显式引入 `@excalidraw/excalidraw/index.css`（0.18 样式不随 JS 注入），restore 带 `refreshDimensions`，回到初始视角、加载失败降级。
- `PlanningView.tsx`（新增）：任务流（范围选择/澄清与搜索等待决策）、草稿层级列表（重命名/上下移/1 秒防抖自动保存/失败可见可重试）、确认、范围切换。
- `Overview.tsx`（新增）：路线总览与课程总览（同版导图、准备状态徽章、先修提示、开始学习禁用门槛、无整体百分比）。
- `ChatView.tsx`（新增）：即时协助（generation、历史、搜索开关、等待决策、刷新恢复不重发）。
- `App.tsx` / `tokens.css`：顶部模式切换 + 侧栏分组 + 新路由；全局元素样式进 `@layer base` 并以 `:not(.excalidraw *)` 隔离画布。
- `api.ts`：切片 3 端点扩展。

脚本与测试：

- `scripts/slice3_model_spike.py`、`scripts/slice3_font_spike.mjs`（新增）。
- `tests/slice3_common.py`（合成适配器）与 `tests/test_slice3_contracts.py`（31）、`test_slice3_planning.py`（15）、`test_slice3_draft_confirm.py`（11）、`test_slice3_scene.py`（8）、`test_slice3_overview.py`（7）、`test_slice3_chat.py`（10）、`test_slice3_faults.py`（13）。

## 3. 契约冻结要点（T03）

- 迁移 `c2d3e4f5a6b7 → d3f6a9b2c4d5 → c2d3e4f5a6b7 → d3f6a9b2c4d5` 往返通过（2026-09-27，`uv run alembic` 输出留档于命令历史）；`learning_tasks` 列不变，等待态/范围记录在 `planning_task_states`（实现层新增表，见 §6 差异）。
- 上下文预算五参数默认值：`max_input_tokens=8000`、`max_retrieval_chars=24000`、`max_retrieval_chunks=24`、`max_images=4`、`max_history_messages=12`；截断优先级：历史消息 > 片段长度 > 片段数量 > 图片。
- 结构上限：路线阶段 ≤12、每阶段课程 ≤8、课程总数 ≤40；大纲单元 ≤12、每单元讲次 ≤20、讲次总数 ≤120；文本 ≤120 字；聊天回答 ≤4000 字。
- 场景白名单：rectangle / ellipse / text / arrow / line；布局常数与 T02 证据一致（契约测试断言）。
- 幂等：确认接口以「草稿 revision + 幂等键」防重，幂等标记落在版本行 `source_task_id = "confirm:<key>"`。

## 4. 与交接文档的差异（交规划者评审，未改架构版本号）

1. **`planning_task_states` 实现表**：交接 §3.1 未列该表。因 `learning_tasks` 列不变是硬约束，而等待态（`awaiting_scope_clarification` / `awaiting_search_decision`）、`resolved_scope`、搜索决策、范围信号需要一个归属位置；本实现新增 side 表并以 `task_id` 主键关联。语义与 §3.1「规划任务记录 resolved_scope…」一致。
2. **等待态的恢复机制**：任务行状态机沿用切片 0 六态；等待通过 `planning_task_states.wait_state` + `partial` 任务事件（受控错误码）表达，用户决策后走新增的 `TaskService.requeue_after_wait`（generation+1、状态回 queued）再恢复执行，迟到 worker 仍被围栏拒绝。
3. **PydanticAI 未引入**：模型调用经切片 1 适配器契约（`generate(messages, **options)` + `probe`）实现输出模式选择与降级（T01 结论参数化），未添加 pydantic-ai 依赖（Python 3.14 兼容性未验证，且交接明确要求「复用切片 1 适配器与核验链路，不旁路」）。Langfuse trace 侧沿用切片 0 exporter 与本地后备模板（`source=local_fallback`），真实 Langfuse 拉取路径仍需后续切片接线。
4. **课程标题展示来源**：路线场景节点文本取 `courses.title_ref`（缺省退回目标快照）；`route_course_nodes` 无独立标题列，与 §3.1 字段清单一致。
5. **切片 2 正文存储缺口**：`text_chunks` 只有引用与 tsvector，无分块正文（切片 2 OCR worker 未完成）。`PlanningRetrievalService` 以可注入 `text_resolver` 解耦：缺省只组装引用与 manifest（不含正文），切片 2 补齐后注入真实 resolver 即可，检索引用契约不变。

## 5. 浏览器走查记录（T06/T07/T08，2026-09-27）

环境：vite dev（5173，代理 /api → 8000）+ `LEARNING_DEV_SYNTHETIC_MODEL=1` 的后端；合成账户（synthetic 邮箱，已批准）；截图存本机临时目录，不入仓库。

1. **导航壳**：顶部模式切换（课程规划/即时协助）+ 侧栏分组渲染正常。
2. **范围澄清等待**：`auto` + 歧义信号 → 任务进入 `awaiting_scope_clarification`，UI 出现两个决策按钮；选择「完整学习路线」后任务恢复并产出草稿。
3. **草稿与确认**：层级列表（阶段/课程重命名、上下移）、1 秒自动保存徽标（已保存/保存失败）、确认后提示「已确认并创建不可变版本 v1」。
4. **路线导图**：画布渲染中文手写体节点（两行换行 + 省略号截断生效）、虚线先修箭头独立表达、只读视图（缩放/平移/回到初始视角）可用；业务顺序不受画布影响。
5. **路线总览**：版本 v1、阶段/课程列表、「未确认大纲」徽章、按课进度 0/0 讲；**无路线整体百分比**。
6. **课程总览**（确认大纲后）：单元/讲次列表、「未生成」准备状态徽章、「开始学习」按钮全部禁用（讲次未就绪）、「有先修」提示徽章；未确认大纲课程页无可用教学入口。
7. **即时协助**：发送 → generation 1→2 递增、旧回答留历史、新回答纯文字（无音频/板书/播放请求路径）；刷新后 generation=2 与消息恢复、不自动重发。
8. **360×740 窄屏**：模式切换整行、导航换行、内容单列、无横向溢出。
9. **控制台**：遍历 /planning、/route/…、/course/…、/chat 后 `console.error` 与 `window.onerror` 均为空。

走查中发现并修复的实现缺陷（均已回归测试覆盖）：

- `restore()` 第三参误传 config 导致白屏 → 改为 `(data, null, null, {repairBindings:false, refreshDimensions:true})`；
- 转换层文本缺度量导致 restore 后宽高 0、文本不可见 → `scene.py` 显式产出文本 width/height；
- `@excalidraw/excalidraw` 0.18 样式不随 JS 注入 → 显式 `import "@excalidraw/excalidraw/index.css"`；
- 全局 token 泄漏进画布 UI → `@layer base` + `:not(.excalidraw *)` 隔离；
- 决策恢复后草稿保存徽标未回填；dev 合成适配器聊天路径补 `plain_text` 分支。

## 6. 边界与遗留

- **T01 真实矩阵待补跑**：设置 `SPIKE_*` 环境变量后重跑 `scripts/slice3_model_spike.py`，把矩阵回填证据文档 §3；降级链已按保守默认实现并通过合成测试，真实供应商行为一致性待复核。
- **教学启动**：`POST /api/courses/{id}/lessons/{id}/start` 只做门槛校验（大纲确认 + 讲次就绪），返回 `started=false, reason=teaching_generation_in_slice4`；首段教学生成归切片 4。
- **开发用合成适配器**：`LEARNING_DEV_SYNTHETIC_MODEL=1` 仅用于本地走查，生产不得启用；测试夹具全部合成数据。
- 走查服务（uvicorn 8000 / vite 5173）为临时进程，证据截图与本机临时文件不入仓库；`frontend/dist` 构建产物不入仓库。
- 资料正文 resolver、OCR 来源区域消费、删除墓碑恢复、Langfuse 真实拉取：分别归切片 2 余量与后续切片。

## 7. UI 对齐设计规范 ds-1（2026-09-27 追加）

按 `文档/ui/index.html`（OD-LAYOUT-PRIMITIVES + ds-1「工程制图 / 技术手册」）重构前端：

- **登录/注册/验证/重置为独立整页路由**（`#/login` 等，`.auth` 双栏：深色品牌面板 + 步骤编号 + 右侧表单列，无侧栏，参考 Google 登录式布局）。
- **应用骨架**：`.app` 双栏（236px 深色侧栏：品牌 + 分组导航「空间/学习/管理/配置」+ 账户脚注；`.top` 面包屑 + `.page`）；窄屏侧栏抽屉化。
- **首页即四入口**（`.home` composer）：「我要 [规划学习|即时对话]，场景是 [自学|学校课程]，范围 [自动|单课程|路线]」seg 句式 + 大输入框 + 示例 hint-row + 资料 chip（`.st-mat` 状态字形）+ `.suggest` 范围说明；规划提交后自动跳转规划页执行任务。
- **规划工作台**（`.mm-clean`）：`.verbar` 三格版本栏（基线版本 ver-conf / 编辑中草稿 ver-draft 斜纹 + `.save` 自动保存状态机 / 确认与切换操作）+ 左画布右层级列表（`.tree`/`.ti` 重命名与上下移）；范围澄清为 `.clarify` radio-row；搜索等待为 `.note-warn`。
- **路线总览**：`.route-sum` 概况（已确认/未确认门数，无整体百分比）+ `.stage`/`.crs` 行（序号、衬线标题、`.ol-st` 状态徽章、进度、`.next` 动作 + `.why`）。
- **课程总览**：`.course` 双栏（`.c-info` 版本/进度/单元 + `.lec` 讲次行），讲次状态用三层字形 `.st-prep`（菱形：未生成/生成中/已就绪/失败）与 `.st-learn`（圆点）；教学门槛以禁用按钮 + `.why` 呈现。
- **即时对话**（`.chat`）：turn me/ai 消息流 + `.composer2` 底栏（Ctrl+Enter）+ `.searchok` 外部来源标记；无音频/板书/播放。
- 全局元素样式置于 `@layer base` 并以 `:not(.excalidraw *)` 隔离画布；旧类名（card/badge/table 等）保留别名对齐 ds-1 视觉，CapabilityConfig 与任务状态演示页无需改结构。

走查（Browser Use，截图存本机临时目录）：登录页、首页 composer、规划工作台（澄清→草稿→确认→导图）、路线/课程总览（含状态字形与门槛）、即时对话（turns）、360px 窄屏；`console.error` 与 `window.onerror` 全空。`npm run build` 通过（0 error / 7 warning，均为既有类）。

## 8. 规划生成 v2（2026-09-29 追加）：骨架+逐课细化、四层场景（ADR-024）

- 契约：`RouteCourseOut` 内嵌可选 `units[]→lessons[]`（复用 `OutlineUnitOut`）；`RouteCandidate`/`OutlineCandidate` 增可选 `title`（导图大标题/课程根卡）；全路线讲次总量上限沿用 `OUTLINE_LIMITS["max_lessons"]`。
- 生成链路：route_generate 骨架后逐课细化 fan-out（`_refine_course_units`，单课失败降级为课程粒度）；模板 `route_generate_v2` / `outline_refine_v1` 入 `fallback_templates/`（含 sha256，refine 按本地后备快照加载、不入任务绑定，同 intake_followup_v1 口径）。
- 场景：`_hier_scene` 通用 N 层缩进树；路线=大标题→课程→单元→讲次（阶段仅承载数据不单独成卡），单课程=课程→单元→讲次；确认路线时逐课落不可变大纲版本（`_materialize_course_outline`）。
- 前端：无需改动（导图为数据驱动渲染）。
- 测试：`uv run python -m pytest ../tests -q`（server/ 目录，Windows 11 / Python 3.14 / uv）→ **537 passed / 2 skipped**（2026-09-29，含 test_mirror_sync）。新增/更新：contracts 嵌套 schema 与逐课/总量上限、planning fan-out 合并与降级（SkeletonThenOutlineAdapter）、scene 四层与根卡、draft_confirm 逐课落版本、overview 同版断言改为根卡口径。
