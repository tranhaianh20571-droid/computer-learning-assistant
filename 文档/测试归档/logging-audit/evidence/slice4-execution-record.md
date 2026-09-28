# 切片 4 执行记录（T11～T17）

> 执行日期：2026-09-27。前置：切片 2 余量（9e1f454）、切片 3（6a9f220）。
> TTS spike（字幕时标对齐）**外部阻断**（无 MiniMax 凭据）——按架构「无音频手动推进为可交付路径」执行：MiniMax 适配器（切片 1 已落 `synthesize`）经工厂注入，测试与走查用合成/无字幕适配器覆盖全部音频分支；真实凭据后补跑矩阵。

## 结果总览

| 任务 | 状态 | 说明 |
| --- | --- | --- |
| T11 契约冻结 | 完成 | 迁移 `e5a7c9d1b3f6`（4 新表 + conversations 课程绑定/恢复点/会话状态列）往返通过；`BoardDeclaration`/`StepCandidate`/`LessonPlanCandidate`/`FollowupCandidate` 输出模型冻结；错误码 `CHECKPOINT_STALE` 注册 |
| T12 启动与步骤生成 | 完成 | `start_lesson`：门槛（大纲确认 + 讲次 ready，409 lesson_not_ready / LESSON_UNLOCK_REQUIRED）→ 课程会话绑定 → 首段任务（lane=interactive）；步骤 30–60s 目标、120s 硬上限（`validate_step_duration` 超限拆段拒绝）；计时起点 = 点击开始学习 |
| T13 板书场景 | 完成 | 板书声明（受限 JSON）→ 白名单场景转换（`declaration_to_scene`，确定性、同输入同输出）；边引用未知节点 → BOARD_SCHEMA_INVALID；original_refs 透传前端挂原图（原图必显） |
| T14 TTS 与无音频模式 | 完成 | `run_tts`：音频+字幕绑定同一 text_version（text_sha256）；无字幕 → `invalid/subtitle_missing`（不发布为可播放）；无配置 → `invalid/tts_unconfigured`；文本版本不一致 → audio_ready=False |
| T15 检查点与恢复点 | 完成 | `save_checkpoint`：client_seq 单调（旧 seq → 409 CHECKPOINT_STALE）；追问创建一次性恢复点（连续追问沿用最初）、取消排队 prefetch、generation 递增、回答后 `awaiting_resume`；`resume` → active 并重开预生成 |
| T16 讲次工作区前端 | 完成 | `LectureView.tsx`：工具栏（退出/当前段/播放状态）、板书列（只读画布 + 原图引用）、文字列（步骤流 + 追问轮次）、composer2 追问、无音频提示、窄屏板书/文字切换 |
| T17 故障注入 | 完成 | 无字幕/版本不一致 → 无音频模式；prefetch 被追问取消；迟到围栏复用切片 0 围栏；跨账户检查点/工作区 404 |

测试结果：`uv run --project server/backend pytest tests -q` → **411 passed**（396 + 15 新增：contracts 5 + teaching 10）。`npm run build` 通过（0 error）。

## 预生成窗口与调度纪律

- 首段：用户点击「开始学习」创建（lane=interactive）。
- 下一段：仅当当前段就绪且会话 `session_state=active` 时创建（lane=teaching_prefetch，窗口=1）；`MAX_STEPS_PER_LESSON=12` 封顶。
- 追问：排队中的 prefetch 任务取消（cancel_requested + cancelled），原课调度关闭；`resume` 重开调度。

## 浏览器走查（2026-09-27）

- 课程总览：讲次「◆讲解 已就绪」+ 蓝色「开始学习」；点击 → 进入工作区（计时起点）。
- 首段生成 → 板书渲染（中文手写体：标题/编号要点/节点与冲突箭头）、文字步骤流、音频就绪徽标（合成 TTS）。
- 追问 → generation=2、待恢复提示（恢复点：第 1 步·0ms）、原课步骤保持可见、「继续课程·不自动播放」恢复为 active。
- 修复的真实缺陷：板书画布懒加载依赖全局变量导致空白 → 组件内独立动态导入；追问后工作区被代次过滤隐藏原课步骤 → 原课步骤固定初始代次、追问轮次单独返回。

## 边界与遗留

- 真实 MiniMax 音色/区域/费用与字幕精度：凭据后补（TTS spike 矩阵待回填）。
- 播放控制器音频对齐：开发环境为无音频手动推进 + 合成音频路径；真实字幕驱动的板书时序呈现待真实凭据验证。
- PydanticAI/Langfuse 真接线：维持切片 3 差异记录（适配器层实现），归部署阶段。

## 8. 真实凭据接入与 PydanticAI（2026-09-28 追加）

### 8.1 PydanticAI 接入（架构 §1 已确认选型落地）

- `uv add pydantic-ai-slim==2.51.0`（Python 3.14.7 dry-run/实装解析成功，当初的兼容性阻断不复存在）；
- `planning/model_runner.py` 重构：`run_structured` 经 **PydanticAI Agent + FunctionModel 桥**执行——Agent 负责输出 schema 校验与按校验错误自动重试（prompted 上限=PROMPTED_PARSE_RETRIES，实测坏输出 2 次重试后抛 `UnexpectedModelBehavior`），FunctionModel 把消息转发给切片 1 适配器 `generate()`，凭据/端点校验/外发核验不旁路；
- 降级链语义不变：native/tool 单次尝试失败即降级，prompted 重试至上限；供应商超时映射 MODEL_TIMEOUT 直接终止；
- 切片 3/4 全部 26 项模型路径测试经 PydanticAI 路径通过（合成 FunctionModel 适配器）。

### 8.2 真实模型（deepkey.example / gemini-3.8-flash）

- 凭据经 `server/.env`（git-ignored）注入，工厂优先级：显式 > 真实 env > 合成走查；
- T01 真实矩阵：prompted 验证通过（native ReadTimeout / tool 无 tool_calls，与降级链吻合），详见 slice3 证据 §3.1；
- 端到端走查：真实生成路线（3 阶段 6 课：「基础认知与准备/核心技能与实践/综合进阶与应用」）→ 确认 v1 → 真实大纲（3 单元 6 讲）→ 确认 → 真实教学首段（「二分查找的核心思想」，13 元素板书，mode=prompted）。

### 8.3 真实 MiniMax TTS（speech-2.8-hd）

- 真实合成打通：MP3 516,660 字节（ID3 头）+ 句级字幕时标；
- **适配器修正**：MiniMax 实际经 `data.subtitle_file`（临时 URL）提供 JSON 时标，非内联字段——`minimax.py` 合成时即抓取解析为 `[{text, begin_ms, end_ms}]` 并随音频持久化（不依赖链接时效）；
- 音频经所有权校验接口回传（200 / 516KB），无音频→有音频两种模式均有测试覆盖。

### 8.4 运维注记

- 真实 TTS 任务的 90s 截止在「教学生成与 TTS 串行等待」场景偏紧（走查中因等待真实生成超时过期一次）——生产部署需常驻 worker 池即时领取，或把 TTS 截止调至 120s+；
- 密钥仅存 git-ignored `server/.env` 与（后续）加密 `service_configs`，未进任何仓库文件与日志。
