# 测试执行入口

本目录是仓库可执行测试的唯一真源。`tests/support/backend/` 与生产 `server/backend/app/` 保持镜像同步（契约与例外清单见 `support/backend/README.md`）：测试导入的是镜像实现，生产代码只从 `server/backend/` 运行，同步由 `test_mirror_sync.py` 哈希校验守护。执行记录与 AC 验收归档在 `文档/测试归档/`（不入 git）；组织约定见通用规范 `文档/规范/testing-spec.md`。

最近全量回归：**1360 passed / 8 skipped / 2 subtests passed**（2026-10-08，本地隔离随机 PostgreSQL schema，280.43 秒，921 条弃用警告），含教学授权、任务超时收敛、音频幂等撤销、封面后台、迁移和镜像验证。未请求真实供应商或 Logfire。跳过两项未配置 TTS 音频检查及六项已删除离线演示页旧检查。证据：`.backup/native-pydanticai/native_validation_0e394ce2c15540b88e7074861f628403/pytest.log`。最终真实应用装配浏览器 **19/19 项通过**、零页面错误；前端类型构建、生产构建和全部 10 套 Node 单元通过。修复、历史失败和部署边界见 [教学与封面修复执行记录](../文档/测试归档/teaching-cover-recovery/execution-record.md)。

历史原生重构基线：**1165 passed / 8 skipped / 2 subtests passed**，181.59 秒，浏览器 14/14、9 套 Node 单元；见 [原生重构执行记录](../文档/测试归档/native-pydanticai/execution-record.md)。本地结果不替代真实供应商或 Logfire 平台验收。

历史全量回归：**705 passed / 2 skipped / 2 subtests passed**（模型配置故障修复，174.09 秒），在独立 PostgreSQL schema 初始化全链表和合成管理员后执行；两个音频检查缺少 TTS 音频跳过，未请求真实供应商。新增25项配置/端点/启动代理合成回归，最终专项59项通过；详见 [模型配置执行记录](../文档/测试归档/model-capabilities/execution-record.md)。

历史全量回归：**621 passed / 2 skipped / 2 subtests passed**（2026-10-04，ADR-032 两栏白板与连续讲解，129.52 秒），在独立 PostgreSQL schema 初始化全链表和合成管理员后执行；两个音频检查缺少 TTS 音频跳过。新增浏览器 13 项、既有学习测验六项与真实模型同阶段两段验证通过，详见 [连续教学记录](../文档/测试归档/continuous-teaching/evidence/execution-record.md)。

历史功能验证（精简前，本次未重跑浏览器与真实模型）：**599 passed / 2 skipped**（2026-10-03，ADR-029 确认锁定、五个独立 Agent、直觉教学与单元测验）。教学浏览器 **6 项通过**，规划/封面浏览器 **6 项通过**；隔离合成课程的真实模型 11 阶段讲解、5 题测验和评分通过。证据见 `文档/测试归档/teaching-assessment/evidence/execution-record.md`。跳过项、真实 TTS/生图及负载检查不计该轮通过。

## 单顶层封面与后台跨讲预载专项

2026-10-09 最终限定回归：**139 passed**，72.69 秒，随机隔离 PostgreSQL schema；未跑全量。覆盖封面所有权、重复确认与显式重生成、迟到图片取消、首讲/N+1 缓存、文字先就绪、语音故障、自动教学、缺失产物恢复、无损迁移与镜像同步。合成浏览器教学 10 项、封面确认 6 项通过，均无页面错误；TypeScript 和 Vite 构建通过。真实供应商可用性不计通过。详见 [专项执行记录](../文档/测试归档/teaching-cover-recovery/execution-record.md#单顶层封面与默认跨讲预载)。

| 当前专项 | 覆盖 |
| --- | --- |
| `test_top_level_cover.py` | 一次规划只生成顶层封面、历史成员归属、服务端成员拦截、幂等与重生成、转换时复制已有图片、删除/撤销/迟到发布围栏 |
| `test_lesson_lookahead.py` | 未学预载首讲、当前讲首段发布后预载下一讲、跨单元排序、末讲停止、版本隔离、文字不等语音、有限后台补调度 |
| `test_teaching_automatic_start.py` / `test_teaching_artifact_recovery.py` | 无逐次弹窗入口、许可版本与撤销边界、成功无产物的有限修复 |

## 板书平滑跟随专项

2026-10-09：只执行板书与播放相关的 **7 项 Node 单元脚本**，均通过；TypeScript 与 Vite 8.3.3 生产构建通过，保留既有大体积 chunk 警告。Chrome 的 1600×1000 与 390×1000 合成组件浏览器检查最终通过连续滚动、目标更新、固定高度/布局、手动接管/返回、暂停、减少动态效果、后台停止/恢复、运动中卸载、分页、表格/时序图和刷新，pageErrors=[]。修复过程见 [专项执行记录](../文档/测试归档/board-motion/execution-record.md)。未跑全量、真实供应商或真实课程生成，不替代其验收。

新增 `board_motion_unit.mjs` 覆盖单 RAF、阻尼收敛、整数 DOM、限速/限位、停止、减少动态效果、尺寸变化和阶段几何游标；`board_writing_browser.mjs` 是对应桌面/手机交互入口。

## 测试精简与入口统一

2026-10-03 精简：根套件移除 10 个无效或重复测试函数，移除 9 个未使用的 `env` 夹具请求；当前静态计数为 50 个 Python 测试文件、577 个测试函数定义（参数化展开后的执行用例数另计）。AC-12 完整链路统一由 `test_slice0_core.TestSlice0HTTP.test_full_chain_register_to_task` 覆盖；重复确认幂等、审计触发器与 T03/T06 重复场景保留较强断言，诊断事件仍覆盖同代与旧代两个分支。

遗留 `server/backend/tests/` 移除已失效 Item 功能的 11 个测试及专用夹具，剩余 47 个模板测试函数仅作迁移参考，禁止作为项目执行入口；现有后端测试脚本改为根套件。容器须挂载完整仓库资源，必要时通过 `LEARNING_TEST_ROOT` 指定根测试目录，缺失根套件时明确失败，不回退执行遗留目录。

前端源码字符串检查、迁移字符串检查以及旧 HTTP/SQLite 开发服务测试暂时保留；补齐对应行为验证或确认服务退役后再清理。浏览器交互与真实供应商验证仍单独记录。

## 运行

仓库根目录：

```text
uv run --project server/backend pytest tests
```

### 原生 AI 重构回归

```powershell
.\server\.venv\Scripts\python.exe tests/run_native_regression.py tests -q --tb=short -rs
```

该入口为每轮创建随机 PostgreSQL schema，初始化生产与模板元数据和合成管理员，覆盖两个数据库连接变量；证据只写 `.backup/native-pydanticai/`，结束后只删除自己创建的 schema。不要把根 pytest 直接指向生产 schema。新迁移的升级、旧行保留与缩窄拒绝由 `test_native_migrations.py` 独立隔离验证。

| 原生专项 | 覆盖 |
| --- | --- |
| `test_native_execution` / `test_native_role_contracts` / `test_native_candidate_schema` | 真正 SDK 执行、固定角色、完整事件、共享预算、生成期递归 extra-forbid 与旧数据兼容 |
| `test_native_task_contracts` / `test_native_task_binding` / `test_native_fallback_policy` | 当前租约、所有权、版本、撤销、精确披露、原子提交与安全错误不得回退 |
| `test_native_chat` / `test_native_chat_asgi` / `test_native_retrieval_status` | 历史与检索进入真实输入、完整结果、ASGI 2.3/2.4 断连清理、正文不可用的显式状态 |
| `test_native_lesson_refinement` / `test_native_migrations` | 三请求后台精炼仅写未来缓存、冻结内容不变、持久预算与 subject 扩容 |
| `test_native_observability` / `test_native_task_observability` | 原生采集、Trace/Metrics 隐私、上下文关联、有界导出故障和业务事务独立 |
| `test_native_model_support` | 仅测试专用合成适配；生产原生模型和安全出口不被替换 |
| `test_native_task_convergence` / `test_disclosure_roots` | 排队/租约/运行超时收敛、有限 worker 恢复、同事务根/子授权审计、精确根链撤销和配置版本 |
| `test_native_teaching_authorization` / `test_slice1_tts_config` / `test_lecture_audio` / `test_first_segment_audio` | 开始/预热/恢复显式同意，TTS 单独确认、逐次外发与发布围栏、幂等子授权撤销不可重建、既有文字保留 |
| `test_automatic_knowledge_cover` / `test_cover_image_transport` | GET 只读、持久有界封面后台、固定配置/主题范围、DNS IP 固定与字节/授权围栏、可见本地降级 |

### 回归范围选择

范围判定以 [通用测试规范第 3.1 节](../文档/规范/testing-spec.md#31-回归范围选择) 为准：局部改动做专项与受影响模块回归，跨模块改动补充完整链路验证，共享基础能力变更以及阶段验收、发布前执行上面的全量命令并补充对应专项。纯文档改动做静态检查；影响范围不清时扩大回归。

例如，仅修改规划对话或封面行为时，可从相关专项开始，并根据调用方与下游影响补充其他测试：

```text
uv run --project server/backend pytest tests/test_planning_dialogue_cover.py tests/test_mirror_sync.py
```

以上示例不代表所有规划改动均可限定为这两个文件。修改共享模型调用层、鉴权或数据库迁移时应执行全量；涉及浏览器交互时另按下方“浏览器 / 前端回归”验证。数据库测试仍须完成下方的数据库准备，浏览器夹具仍须使用隔离环境。

本页的全量命令覆盖根 `tests/` 下的 pytest 套件，不自动执行浏览器脚本或真实供应商验证。记录实际范围、选择理由、命令、结果及未验证项；专项通过不更新本页“最近全量回归”的日期和通过数，新增或变更测试仍须维护测试索引与当前用例基线。

## 数据库准备

需要本机 PostgreSQL。连接串优先级：`LEARNING_DATABASE_URL` 环境变量 > `server/.env` 的 `DATABASE_URL` > `server/backend/app/logging_audit/db.py` 内置开发默认值。

```powershell
Start-Service postgresql-x64-17
Set-Location server/backend
uv run alembic upgrade head
Set-Location ../..
# 可选：显式指定测试库
$env:LEARNING_DATABASE_URL="postgresql+psycopg://learning_assistant:<密码>@127.0.0.1:5432/learning_assistant"
```

## 测试分层

| 分层 | 文件 | 覆盖 |
| --- | --- | --- |
| 日志审计地基 | `test_t01_contracts` ～ `test_t06_fault_injection` | 字段允许名单、脱敏、日志注入、trace 关联、任务序号、审计追加保护、观测降级 |
| 观测与内容通道 | `test_t07_model_call_metrics` / `test_t08_content_trace` | 模型调用聚合指标、内容通道路由与脱敏边界、OTel 装配、删除联动 |
| 切片 0–5 | `test_slice0_*` ～ `test_slice5_*`、`test_ac03_worker_chain` | 各切片核心、契约、调度与故障 |
| 架构 v2 与 Agent 层 | `test_v2_model_streaming` / `test_v2_gemini_adapter` / `test_v2_intake` / `test_v2_ui_support` / `test_v2_pydanticai_bridge` | 流式生成、Gemini 适配器、访谈、UI 支撑、PydanticAI 桥（TestModel 冒烟 + FunctionModel 合成适配器） |
| 集成与故障注入 | `test_integration_http` / `test_integration_faults` / `test_fastapi_pg` / `test_template_integration` | 真实 HTTP、SSE 补取与 `410 events_expired`、跨账户、PG 触发器 |
| 结构守护 | `test_mirror_sync` | 镜像树与生产树逐字节一致（登记例外白名单） |
| 模型配置故障 | `test_slice1_model_config_regressions` | Gemini/Anthropic 版本后缀、探测/运行工厂路径一致性、加密缺失/解密失败的受控响应、启动代理环境规范化；25项合成用例，不访问供应商与数据库 |
| 自动封面与规划恢复 | `test_automatic_knowledge_cover` / `test_planning_dialogue_cover` / `test_course_library` / `test_planning_resume` | 自动任务 queued/expired/失效租约恢复、授权先于恢复标记、配置版本/撤销、访谈路线到课程继承、本地来源不拾新配置、外发期间删除与迟到发布保护；合成数据及供应商适配器 |
| 共享夹具 | `common_fixtures.py`（原 `slice3_common.py`）、`support/` | 合成模型适配器、测试存储目录 |

## 浏览器 / 前端回归

原生执行链综合入口为 `tests/run_native_browser_regression.py`，交互脚本为 `tests/native_ai_browser.mjs`。先构建前端，设置 `TEST_NODE`（或提供 PATH 上的 Node）、`PW_ROOT`（已安装 Playwright 的 node_modules），可选 `TEST_BROWSER`。运行 `server/.venv/Scripts/python.exe tests/run_native_browser_regression.py` 后，入口创建自己的随机 `course_library_validation_*` schema、真实应用装配和随机前后端端口，仅注入显式 SDK FunctionModel 合成响应；结束后清理自己创建的 schema 与进程。验证访谈、课程规划确认、聊天普通/流式、教学首段、刷新恢复、无 TTS 的显式文字分支、桌面/390px 手机与画布像素；不代表完整教学、测验提交、真实 TTS/供应商或 Logfire 已验收。证据位于 `.backup/native-browser/`。

教学与测验回归入口为 `tests/teaching_assessment_browser.mjs`，夹具 `tests/teaching_browser_fixture.py`；隔离后端应注入 `tests/test_teaching_assessments.py` 的 LearningAdapter。验证确认后只读、阶段讲解、用户确认完成、题目/答案隔离、评价与刷新恢复；TEST_BROWSER 可指定已安装浏览器，输出只写 `.backup/`。

规划对话与封面回归入口为 `tests/planning_dialogue_browser.mjs`，复用下述隔离后端/夹具契约；隔离服务应注入 `tests/test_planning_dialogue_cover.py` 的合成 FeatureAdapter，先注册 Slice3State 的全链模型再 create_all，模板集成测试另需 SQLModel 表与合成管理员。生成过程不使用真实供应商。支持 TEST_BROWSER 指定已安装浏览器。

导图回归入口为 `tests/mindmap_browser.mjs`，使用下述课程库回归相同的隔离后端与合成夹具环境，默认结果目录为 `.backup/mindmap-browser/`；验证真实鼠标平移、缩放、编辑、保存失败、确认后只读及打印字体文字边界。

课程库回归入口为 `tests/course_library_browser.mjs`，夹具为 `tests/course_library_browser_fixture.py`。先启动隔离 PostgreSQL schema 对应的后端；设置 `TEST_BACKEND` 为该服务，不能指向真实应用的 8000 端口。`LEARNING_DATABASE_URL` 必须指向专用 `course_library_validation_*` schema，夹具写入前会检查。`TEST_PYTHON`、`PYTHONPATH` 和 `PW_ROOT` 分别指向既有解释器、生产包目录和已安装 Playwright 的 node_modules。截图和结果输出到 `.backup/`，不存储凭据。优先 Orca 内置浏览器；该脚本用于不可用时后备或后台回归。

- 前端应用（`frontend/`）：`npm run build`；`node scripts/slice1_frontend_check.mjs <baseUrl> <outDir>`（默认 `http://127.0.0.1:4173`，需先 `vite preview` 或 dev server；检查登录页可达、匿名不渲染能力配置、窄屏可用、无真实外发、无敏感控制台输出）；再手动走查 `/course` → `/course/detail` → `/lecture`、设置页与 360px 窄屏布局。
- 认证整页旅程回归：`node tests/auth_pages_browser.mjs`（`TEST_FRONTEND` 默认 `http://127.0.0.1:5173`；`PW_ROOT`、`TEST_BROWSER` 约定同上）。断言 `/login`、`/register`、`/verify`、`/reset` 四个认证路由**各自都存在可离开认证区的入口**，并实际点击验证落在非认证路由；任一页成为死胡同即失败。该断言针对的是「控件缺失」类缺陷——组件级与接口级测试断言的是既有元素存在，无法发现某个入口根本不存在。
- 离线原型（`文档/ui/index.html`，AC-07）：`py -3 -m http.server 8765 --directory 文档/ui` 后用 Playwright 检查课程列表/详情/讲次/设置页无真实网络请求、无敏感控制台输出。

## 证据与验收归档

各切片执行记录与 AC 汇总在 `文档/测试归档/`（`logging-audit/evidence|reports`、`slice-0/`），注册表快照在 `logging-audit/schemas/`（版本化，不覆盖旧快照）；这些目录不入 git，索引见 `文档/测试归档/logging-audit/README.md`。

教学流式专项：`test_teaching_latency.py`、`teaching_latency_browser.mjs`；分别验证 PydanticAI 增量/租约/缓存和浏览器实际呈现。浏览器与真实模型使用独立数据域，不与全量调度测试共享 schema。

ADR-031 教学呈现：`test_classroom_presentation.py`、`classroom_presentation_unit.mjs`、`classroom_presentation_browser.mjs`。Node 执行前按 Windows 环境规则清除代理变量；后端与浏览器均指向独立合成数据域。历史静态函数计数是 2026-10-03 测试精简快照，当前执行用例基线以最近全量为准。

ADR-032 连续教学：`test_continuous_teaching.py`、`continuous_teaching_unit.mjs`、`continuous_teaching_browser.mjs`。原浏览器断言改用内部测试属性与自然段内容，界面不显示步骤编号或逐段状态。

讲解布局局部回归（2026-10-04）：`lecture_layout_unit.mjs` 与 `lecture_layout_browser.mjs`，主题标题、阶段标签、766px默认宽度、自适应图字号与连线说明；浏览器13项及既有学习测验6项通过，未改变最近全量基线。记录见 [局部布局记录](../文档/测试归档/lecture-layout/evidence/execution-record.md)。

三栏最终偏好（2026-10-04）：列数恢复三栏，字号和766px右栏保留；前端专项、类型构建与浏览器13项通过，未改变最近全量基线。见 [三栏验证](../文档/测试归档/lecture-layout/evidence/three-column-final.md)。

内容组连续排版（2026-10-04，局部UI）：`grouped_board_unit.mjs` 与 `grouped_board_browser.mjs` 验证对应文字／图同栏、真实可用高度、超高组滚动、跨栏组和流式锚点；类型构建、四套前端专项、新浏览器19项及完整学习测验6项通过，未修改最近全量基线。记录见 [分组排版验证](../文档/测试归档/grouped-board/evidence/execution-record.md)。

白板自动跟随：`board_follow_unit.mjs` 与分组浏览器专项验证滚动目标、超高块、历史首次加载、手动暂停／恢复及实际画布绘制；属于同一前端呈现任务。

公式布局局部专项（2026-10-05）：`node tests/formula_unit.mjs` 验证 KaTeX 与文本回退、跨栏、标题/正文独立坐标及实测高度；`node tests/formula_board_browser.mjs` 验证1440px和390px下公式换行、不可拆分公式横向滚动、分数/多行、缩放、分页、渐进隐藏、刷新和画布实际绘制。浏览器使用现有 `PW_ROOT`（Playwright的node_modules）、可选 `TEST_BROWSER`、`TEST_FRONTEND`（默认本地5173），`TEST_OUTPUT` 只指向忽略的本地输出目录。脚本替换前端入口，挂载生产白板与合成场景，不访问课程API。通过结果及未验证项见 [公式布局执行记录](../文档/测试归档/formula-layout/execution-record.md)，不更新最近全量基线。

板书顺序与讲解定位专项（2026-10-05）：`node tests/board_writing_unit.mjs` 覆盖片段/旧格式顺序、代码行、图节点与连接、阶段游标、暂停、预排换行和中央定位；`node tests/board_writing_browser.mjs` 使用生产TeachingBuffer、TeachingBoard与LectureSplit的合成场景，覆盖1600px/390px、实际字体宽度、真实分栏拖动、代码滚动、当前行居中、向上回看不拉回、右下角返回箭头显隐、暂停、跨页和刷新。复用公式浏览器环境变量契约，截图只写忽略目录，不访问真实课程API。结果见 [板书呈现局部记录](../文档/测试归档/board-writing/execution-record.md)，不更新最近全量基线。

连续呈现控制专项入口：`test_lecture_playback.py`（检查点允许名单、暂停预生成门控、严格计划缓存、升级所有权与并发幂等），`lecture_playback_unit.mjs`（暂停时入站内容冻结、继续及未读缓存逐字呈现）。最终图标签与输入区追加修改未运行测试；先前执行结果与未验证项见 [连续呈现执行记录](../文档/测试归档/lecture-playback/execution-record.md)。遵从用户于2026-10-04后续明确指令，仅在其要求时运行测试。

MiniMax音色目录专项：`test_tts_voice_catalog.py` 与 `tts_voice_catalog_browser.mjs`；后端局部回归43项通过，合成浏览器8项通过，真实账号查询和浏览器确认303系统音色。未运行全量或语音合成，见 [音色目录局部验证](../文档/测试归档/tts-voice-catalog/execution-record.md)。浏览器需要独立合成schema与mock目录服务，真实密钥不进入测试源码。

连续正文／稳定白板／语音局部专项：`test_lecture_audio.py`、`lecture_audio_unit.mjs`、`lecture_audio_browser.mjs`。后端受影响模块65项及最终边界10项分别通过，六套Node专项、11项浏览器检查和非私人测试句的真实语音合成通过；不代表全量回归或用户扬声器验收。见 [语音与白板局部记录](../文档/测试归档/lecture-audio-stability/execution-record.md)。浏览器需隔离的配置工厂、合成音频与schema，并将原生Audio内容请求也指向隔离后端；源码与记录不含真实Key。

课程首段预热专项：`test_first_segment_prewarm.py`、`test_first_segment_audio.py`、`test_first_segment_migration.py`、`first_segment_prewarm_browser.mjs`。独立合成schema覆盖无进度预热、单段范围、缓存消费、进行中请求复用、版本／授权／容量、失败不重复扣费、服务端续讲与过期重试。相关模块62项及重叠的最终边界14项分别通过，浏览器7项通过；不更新最近全量基线。见 [首段预热执行记录](../文档/测试归档/first-segment-prewarm/execution-record.md)。

课程体验与自动封面回归（2026-10-05）：上述自动封面、规划对话、课程库、规划恢复四文件加 `test_mirror_sync.py` 的最终合成专项60项通过；模板合成管理员修复后专项6项通过。随机 PostgreSQL schema 初始化全链表与 approved 合成管理员，同时覆盖 `LEARNING_DATABASE_URL` 和 `DATABASE_URL`；不在生产 schema 运行根 pytest，不访问真实供应商。后端全量结果、跳过项与失败轮次见 [课程体验执行记录](../文档/测试归档/course-experience/execution-record.md)，浏览器和真实供应商验收另记。
