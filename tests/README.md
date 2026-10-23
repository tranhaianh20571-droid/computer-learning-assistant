# 测试执行入口

## Electron 桌面运行环境

`desktop_browsers_unit.mjs` 检查用户所选浏览器的程序路径、HTTPS 参数和无 Shell 启动。
`desktop_electron_browser.mjs` 验证实际安装后的窗口、资料上传、设置，以及浏览器选择、取消、
手动定位和启动失败恢复；浏览器进程分派使用合成目标与可观察替身，不调用真实市集登录。
`node --test tests/desktop_install_unit.mjs` 覆盖下载续传、校验、清单来源与路径边界。
`test_desktop_runtime.py` 联合认证、生命周期、视频、课堂执行和镜像专项，通过受管隔离入口执行。
`desktop_native_live.py --root ... --work ...` 显式验证真实 Windows AppContainer、
Java/C/C++/CPU torch、绘图和文件/网络隔离，不使用个人资料或真实模型。
干净环境安装验证和真实供应商结果分别归档，不能由单元检查代替。

## PaddleOCR 四项能力与问题页

默认文档导入只绑定 OCR，识别完成并建立本地来源索引即可使用，不自动抽题或复核。
默认 PDF 在 50 页、45 MB 目标范围内整份提交，不追加阻塞发布的图表裁片识别；
主 OCR 能力与原 PDF 保留，旧任务继续使用原快照。
`test_material_questions.py` 与 `test_remote_document_pipeline.py` 覆盖本地上传、
远程文档、默认配置零抽题调用，以及已有显式抽题任务的兼容与授权检查。

`test_paddle_crops.py`、`test_paddle_chart_crops.py` 联合附件、抽题、
课堂与测验专项，检查本地裁片、哈希、参数快照、单次 chart 补取、
不同坐标系独立图片、真实二进制多模态输入及答案隔离，不默认全量。
`paddle_crops_live.py --live --raw ... --scope ... --pages ... --out .backup/...`
复用旧响应取图，不重复主 OCR。
`paddle_chart_crops_live.py --live --upload ... --out .backup/...`
复用已完成补取；`--supplement-run` 指定另一个已捕获补取结果，
只有显式 `--fresh-crop-pass` 才新增裁片任务。
`paddle_crops_browser.mjs` 配置 `PW_ROOT`、`TEST_BROWSER`、
`TEST_CROP`、`TEST_PDF`；可用 `TEST_CLASSROOM_CROP` 单独回放讲解图。
记录与限制见 [裁片、波形及原图验收](../文档/测试归档/paddle-crops-layout-20261023.md)。

`test_paddle_ocr_features.py` 与 `test_paddle_document_parser.py` 验证开关、
图片 OCR/印章来源、去重、处理页坐标、创建/重试快照及旧任务兼容；
联合附件、导入、检索、抽题、课堂原图区、测验和镜像专项，不默认全量。
`paddle_ocr_features_live.py --live --pdf ... --pages ... --baseline ... --config-id ... --out .backup/...`
在独立 schema 中重新 OCR 指定原页，比较既有基线；成功后清理本次 schema。
已存在成功结果时只重算比较，不重新付费上传。
`document_attachments_files_live.py --capture-provider-results` 仅把原始 JSONL 写入私有输出目录。
真实开关生效、文字召回和图题正确率分别记录，见
[问题页复测](../文档/测试归档/paddle-features-problem-pages-20261023.md)。

`somark_problem_pages_live.py --live --pdf ... --pages ... --paddle-run ... --out .backup/...`
用于授权后的 SoMark 同页比较。密钥通过无回显终端输入，不放命令行；
成功检查点可离线重算，失败不自动重新提交。结果区分 OCR 与非逐字图像理解，
不写入业务配置或自动更改供应商路由。

`mineru_problem_pages_live.py --live --baseline .backup/... --out .backup/...`
复用已完成 SoMark 实验的同一份样本，测试 MinerU `vlm`、OCR、公式与表格解析。
密钥无回显输入，成功结果离线重算，失败检查点阻止自动重复提交；
仅已诊断的完成后下载连接故障可显式 `--resume-download --direct-cdn-dns` 恢复。
图注、图内文字和图片裁片分开统计，未接入生产路由，见
[三家问题页对照](../文档/测试归档/mineru-problem-pages-20261023.md)。

## 考纲三轮取证与真实教材质量

`test_scope_material_rounds.py` 覆盖正文充分时零检索、每轮最多五片段、最多三轮、
失败前预留轮次、重启恢复、去重与无新证据提前停止、授权撤回。
联合 `test_classroom_examples.py`、`test_unit_scope.py`、`test_study_sources.py`、
教学/测验/图形/费曼/权限与镜像专项，使用隔离数据库入口，不默认全量。
`material_quality_live.py --live --owner ... --out .backup/... --goal ...`
提供 `--stage blind|blind_exam|material|evidence`；出题需 `--scope`，材料路径需已完成的
`--upload` 实验，`--lesson-index` 可显式选择讲次，省略则逐讲运行。
`--plan-run` 复用同属主、同目标的私有规划，`--keep-going` 仅继续独立失败项；
认证、额度或限流拒绝仍停止。`material_quality_summary.py` 汇总阶段产物，不能代替终验。
真实模型、OCR、失败候选与课堂抽段证据单列，见
[数字电路真实材料记录](../文档/测试归档/digital-material-quality-20261022.md)。
`pdf_quality_inventory.py` 只在本机忽略目录导出原 PDF 逐页文字和联系表。

## 测验批改结果页

构建后运行 `node tests/unit_assessment_browser.mjs`，使用 `PW_ROOT` 和 `TEST_BROWSER`；
覆盖整卷作答提交与重试、原作答快照、对错筛选、参考答案、本机订正及四宽结果布局。
联合 `assessment_content_browser.mjs`、`professional_graphics_browser.mjs`、
`study_sources_browser.mjs` 检查资料图、作图评分和答案隐藏边界。
接口为合成拦截，运行记录见[结果页专项](../文档/测试归档/assessment-result-20261022.md)。

## 课程复习

`test_course_review.py` 覆盖独立业务排程、原生结构化出题/评分、草稿与会话恢复、
四种 FSRS 评分、受助/跳过、幂等、并发修订、授权撤销、版本变化和删除迟到发布。
联合迁移、任务授权、课程库、课程删除、已讲知识点与镜像专项；不默认全量。
`course_review_browser.mjs` 使用构建产物、`PW_ROOT` 与 `TEST_BROWSER`，
检查 1440/768/390/320px 的页面、合集、草稿恢复、反馈与错误恢复。
浏览器 API 和模型均为合成；结果与本地迁移状态见[专项记录](../文档/测试归档/course-review-20261022.md)。
显式真实供应商检查为 `./server/.venv/Scripts/python.exe tests/course_review_live.py --live`：
只读当前教学连接配置，在独立 schema 中使用合成讲解和答案，结束后删除该 schema；
不将真实账号课程或成绩用于实验，不纳入默认 pytest。

## 应用生命周期

`test_app_lifespan.py` 检查子应用/生产挂载的 worker 启停次数与顺序，
以及启动失败、请求异常、关闭异常时的资源回收。
与生产装配和教学授权专项联合执行，可显式加入
`-W error::DeprecationWarning -W error::sqlalchemy.exc.SAWarning`，
防止生命周期弃用和空主键查询警告回归；不屏蔽警告。

## 本讲持续缓冲

`test_lesson_buffering.py` 覆盖不推进播放的文字/板书/音频连续准备、
费曼边界与恢复、暂停保留缓存、深层任务重试和属主授权/生命周期围栏。
使用受管隔离 PostgreSQL 入口，与教学恢复、语音、首段预热及镜像专项联合运行；
模型与语音为合成供应商。

## 2026-10-22 市集账号后台接收

`test_market_reception.py` 联合市集注册/管理/桥接、卡片包、认证和镜像专项，
受管隔离运行命令及 54 项结果见[执行记录](../文档/测试归档/market-reception-20261022.md)。
`market_reception_browser.mjs` 在构建后使用既有 PW_ROOT/TEST_BROWSER，覆盖匿名、
B 会话与网页 A 授权的桌面/手机流程；仅合成身份，不全量。

## 2026-10-22 记忆生命周期

`test_memory_lifecycle.py` 联合记忆投递、个人模型迁移、原生迁移、费曼及镜像专项，
使用 `tests/run_native_regression.py` 的随机 PostgreSQL schema。
`memory_lifecycle_native.py` 用安装了已验证 Hindsight runtime 的解释器显式执行，
创建独占 pg0 实例并停机删除该实例；内容判定与向量为合成，存储、级联删除及目录树为真实。
命令、结果及教学旧夹具失败单列于[专项记录](../文档/测试归档/memory-lifecycle-20261022.md)。

本目录是仓库唯一可执行测试真源。本页维护运行方法、环境要求和测试入口；
历次通过数、失败、跳过、警告及验收结论集中在[测试归档](../文档/测试归档/README.md)。
整理前的完整记录见[历史汇总](../文档/测试归档/测试执行历史汇总-20261022.md)。

## 目录与真源

| 位置 | 职责 |
| --- | --- |
| `tests/test_*.py` | 后端契约、业务、迁移、权限和故障专项 |
| `tests/*_unit.mjs`、`tests/*_browser.mjs` | Node 单元和浏览器交互专项 |
| `tests/common_fixtures.py`、`tests/conftest.py` | 合成夹具、公共装配和测试环境默认值 |
| `tests/support/backend/` | 生产实现镜像；契约与例外见[镜像说明](support/backend/README.md) |
| `server/backend/app/` | 生产实现，由 `test_mirror_sync.py` 校验镜像同步 |
| `server/backend/tests/` | 遗留模板参考，禁止作为项目测试入口 |
| `文档/测试归档/` | 执行记录、验收结论和历史结果索引 |
| `实验/` | 本机历史实验材料，受 Git 忽略，不作为默认运行入口 |

## 运行

以下命令从仓库根目录执行。先按[开发指南](../文档/开发/README.md)准备
Python 工作区、本地受忽略配置和前端依赖；前端使用 npm，Python 使用 server 的 uv 工作区。
未准备本机 `server/.venv/` 时先按开发指南安装，不将真实配置或凭据写入测试源码。

### 回归范围选择

按[测试规范第 3.1 节](../文档/规范/testing-spec.md#31-回归范围选择)选择范围。
**只有用户明确要求时才执行全量回归**，也不得分批运行全部专项变相执行全量。
默认验证本次改动、受影响调用方和下游流程；共享能力、迁移或发布前检查不自动触发全量。
纯文档改动执行差异、链接与规则一致性检查。

后端业务与数据库专项优先使用创建随机隔离 schema 的受管入口，明确传入专项文件：

```powershell
./server/.venv/Scripts/python.exe tests/run_native_regression.py tests/test_planning_dialogue_cover.py tests/test_mirror_sync.py -q --tb=short
```

这是选择范围的示例，不能替代对实际调用方和下游影响的检查。
仅镜像静态校验可直接执行，不调用业务数据库：

```powershell
./server/.venv/Scripts/python.exe -m pytest tests/test_mirror_sync.py -q
```

### 原生 AI 重构回归

`run_native_regression.py` 为每次执行创建随机 PostgreSQL schema，
初始化生产与模板元数据及合成管理员，同时覆盖 `LEARNING_DATABASE_URL` 和 `DATABASE_URL`。
结果输出至 `.backup/native-pydanticai/`，结束时只删除本次创建的 schema。
该 runner 未传参数时会选择完整根套件，因此平时必须显式传入专项文件或选择器。

**下面的全量命令仅供用户明确要求全量时使用：**

```powershell
./server/.venv/Scripts/python.exe tests/run_native_regression.py tests -q --tb=short -rs
```

这只覆盖根 pytest 套件，不自动运行 Node 单元、浏览器或真实供应商实验。
执行记录必须区分专项与全量、合成与真实，并保留失败、跳过、警告和未验证项；
局部通过不得更新最近全量结果。

## 数据库准备

需要可用的 PostgreSQL。基础连接串优先级为 `LEARNING_DATABASE_URL` 环境变量、
`server/.env` 的 `DATABASE_URL`、生产 `logging_audit/db.py` 的开发默认值。
使用已配置的受管入口或独立测试数据库；不要把根 pytest 直接指向业务 schema。
需要 pgvector 的专项须预先准备扩展，不通过提高应用账号权限解决。

Alembic 迁移验证由 `test_native_migrations.py` 在隔离环境执行。
普通业务专项的元数据初始化不等于完成生产升级；不得为了运行测试自动迁移业务数据库。
容器必须提供完整仓库的根测试资源，必要时设置 `LEARNING_TEST_ROOT`；
根目录缺失时明确失败，不回退到模板测试目录。

## 测试分层

课堂例题与考纲资料检索入口为 `test_classroom_examples.py`，联合
`test_study_sources.py`、`test_unit_scope.py`、`test_scope_teaching_bridge.py`、
`test_teaching_assessments.py`、`test_assessment_content_v2.py` 验证资料到测验的链路。
涉及课堂持久化、预生成或权限时，加 `test_taught_knowledge.py`、
`test_native_lesson_refinement.py`、`test_native_teaching_authorization.py` 和镜像守护。
合成模型验证流程约束，不能代替真实教材与真实模型的教学质量验收。

下表是选择入口的导航，不是全部文件清单；具体组合依调用方、共享依赖及下游影响确定。

| 范围 | 主要入口 |
| --- | --- |
| 日志、审计与观测 | `test_t01_*` 至 `test_t08_*`、`test_runtime_sink.py`、`test_output_diagnostics.py` |
| 原生 SDK、角色、预算和授权 | `test_native_*.py`、`test_config_model_factory.py`、`test_disclosure_roots.py` |
| 规划、恢复与搜索 | `test_slice3_planning.py`、`test_v2_intake.py`、`test_planning_*.py`、`test_anysearch_*.py` |
| 资料、网页、视频与检索 | `test_slice2_contracts.py`、`test_document_attachments.py`、`test_source_imports.py`、`test_source_video_worker.py`、`test_personal_retrieval.py`、`test_corpus_pgvector.py` |
| 教学、测评与考纲 | `test_teaching_*.py`、`test_unit_scope.py`、`test_scope_teaching_bridge.py`、`test_assessment_content_v2.py`、`test_professional_graphics.py` |
| 人物、记忆与设置 | `test_character_*.py`、`test_persona_*.py`、`test_memory_*.py`、`test_settings_*.py`、`test_reasoning_*.py` |
| 删除、对象存储与封面 | `test_slice5_deletion.py`、`test_local_resource_*.py`、`test_top_level_cover.py`、`test_lesson_lookahead.py`、`test_automatic_knowledge_cover.py` |
| 市集注册、桥接与上传 | `test_market_registration.py`、`test_market_bridge.py`、`test_market_web_login.py`、`test_market_uploads.py`、`test_character_package.py` |
| HTTP、故障注入与镜像 | `test_integration_http.py`、`test_integration_faults.py`、`test_fastapi_pg.py`、`test_template_integration.py`、`test_mirror_sync.py` |

人物创建对话与材料解析专项：`test_persona_dialogue.py`，联合人物快速构建、
验收与镜像检查；前端同测 `persona_workflows_browser.mjs`，覆盖合并后的 AI 对话、
DOCX/TXT 上传入口、自定义编辑器和 1440/390/320px 布局。
对话搜索专项使用实际 SearchService、任务/权限绑定与合成传输，覆盖首次搜索、
第二轮搜索、恢复不重复请求、非搜索阶段拒绝，以及取消/删除/配置变更围栏。
联合 `test_anysearch_service.py`、`test_anysearch_budget_v2.py` 和既有人物搜索专项执行，
不把聊天接口拦截或合成传输当作真实搜索供应商验收。
紧凑输入框专项同入口验证：不显示额外状态行、外发勾选和搜索说明；
点击发送提交本轮确认，右下角方形停止按钮覆盖取消失败与再次取消。
`test_persona_dialogue_stream.py` 使用真实 PydanticAI SDK 和合成模型片段，
验证结束前的私有回复快照、结构化结果保存、取消/租约/属主/配置/删除围栏，
与上述人物专项及镜像校验联合运行。浏览器专项使用本机合成 SSE 服务，
覆盖实际分片到达、刷新恢复、清空重试、断线重连、停止和最终替换；不调用真实供应商。
对话确认与完整创作入口为 `test_character_complete_creation.py`，联合上述对话/流式、
快速构建、人物卡、声音策略、授课绑定/授权、交付与生命周期专项及镜像校验。
检查最终聊天中的人物介绍、下一轮明确确认前零蒸馏/媒体调用、模糊回应/修改/新材料、
八次材料追问与 finish 不绕过确认、确认属主/幂等/旧轮次拒绝和既定画像复用。
用户通过聊天确认后检查六项媒体自动接续、同一基础形象引用、私有素材统一挂载、
语言/性别匹配、断点重试以及取消/删除/编辑/配置变化的迟到发布围栏。
前端同跑 `persona_workflows_browser.mjs`、`character_media_browser.mjs`、
`character_voice_policy_browser.mjs`；构建和浏览器接口均为合成验收，真实生成质量单列。
`persona_workflows_browser.mjs` 还验证最终介绍与确认问句出现在同一条聊天中、
等待确认的刷新恢复、通过输入框确认和修改后继续等待，不新增确认按钮或弹窗。

资料契约使用 [material_pdf_samples.py](material_pdf_samples.py) 生成合成 PDF，
不依赖旧的根目录 `.slice2-samples/`。共享夹具均为合成数据；
生产修改涉及镜像文件时同步更新并运行镜像守护。

## 浏览器 / 前端回归

先在 `frontend/` 按本次影响执行 `npm run build`、必要的 `npm run lint`，
再从根目录选择相关 Node 或浏览器专项；不要为了文档改动运行构建或全部脚本。

```powershell
node tests/board_writing_unit.mjs
node tests/auth_pages_browser.mjs
```

个人记忆页面专项：`node tests/memory_profile_unit.mjs` 验证画像分区和原文保留；
构建后使用 `PW_ROOT`、`TEST_BROWSER` 运行 `node tests/memory_page_browser.mjs`，
覆盖开关版本保存、失败恢复、反馈、清空确认及桌面/手机布局。浏览器接口为合成拦截，
不修改真实记忆，也不调用供应商。

浏览器入口要求的环境变量以脚本头部为准：

`market_web_login_browser.mjs` 使用构建产物与真实市集 HTML、合成 API，
覆盖桌面/手机用户名同步、头像首字母、弹窗网页登录与失败恢复。

| 变量 | 用途 |
| --- | --- |
| `PW_ROOT` | 已安装 Playwright / playwright-core 的 node_modules 目录 |
| `TEST_BROWSER` | 本机 Chromium 可执行文件；未配置时能否使用默认浏览器由入口决定 |
| `TEST_NODE`、`TEST_PYTHON` | 原生 runner 或子脚本使用的 Node / Python |
| `TEST_FRONTEND`、`TEST_BACKEND` | 被测前后端地址 |
| `TEST_OUTPUT`、`TEST_OUT` | 忽略的本地结果目录；不同脚本使用的变量不同 |
| `NATIVE_BROWSER_SCRIPT` | 受管浏览器 runner 执行的交互脚本，未设置时使用 `native_ai_browser.mjs` |

Windows 下先清除当前进程中影响本地浏览器的代理变量，
设置 `NO_PROXY=127.0.0.1,localhost`；不要修改系统代理设置或输出真实凭据。
截图、模型调用元数据和其他生成结果只写本地忽略目录。

原生生产装配与 SDK 合成链路使用：

```powershell
./server/.venv/Scripts/python.exe tests/run_native_browser_regression.py
```

该入口为本次执行创建独立 PostgreSQL schema 和随机端口、注入 SDK FunctionModel，
结束时只清理自己创建的 schema 和进程。可在构建后选择兼容的交互脚本：

```powershell
$env:NATIVE_BROWSER_SCRIPT = (Resolve-Path tests/persona_native_browser.mjs).Path
./server/.venv/Scripts/python.exe tests/run_native_browser_regression.py
Remove-Item Env:NATIVE_BROWSER_SCRIPT
```

API 全拦截脚本可以只使用真实前端；生产装配脚本必须使用隔离后端。
课程库专项 `course_library_browser.mjs` 与夹具要求 `course_library_validation_*` schema，
不能将合成账号准备指向业务服务。教学、规划和导图浏览器分别使用对应
`teaching_browser_fixture.py`、`test_planning_dialogue_cover.py` 或课程库合成夹具；
先注册全链模型再创建表，涉及模板账号时另初始化合成管理员。
认证出口 `auth_pages_browser.mjs` 使用 `TEST_OUT`，其余脚本不能假定沿用该变量。

课堂本地容器执行先按[部署说明](../deploy/README.md#课堂-java--c--c--pytorch)
构建 `learning-classroom:1`；`test_classroom_execution_live_api.py` 只在
`CLASSROOM_CONTAINER_LIVE=1` 时执行真实容器，独立入口为 `classroom_execution_live.py`。
容器、浏览器交互、音轨听感和真实供应商的验收分别记录。

## 真实供应商实验

`*_live.py`、`*_live.mjs` 是独立实验工具，不属于默认自动回归。
只有得到对应真实调用授权后才运行，按入口要求显式指定 `--live` 或相应开关。
导入工具不应请求供应商；凭据只来自入口规定的进程变量、标准输入或受忽略配置。
合成测试、真实后端和实际供应商结果不得互相替代。

| 实验 | 入口与准备 |
| --- | --- |
| 网页与视频 | `source_imports_live.py --live`；Python 环境为 `server/.venv-video/`，模型为 `server/asr-models/`，加密输入为 `tests/.secrets/source-imports-live.dpapi` |
| 附件、资料与测评 | `document_attachments_live.py`、`document_attachments_files_live.py`、`study_sources_pdf_live.py`、`assessment_pdf_live.py`；按入口选择已授权输入与私有输出目录 |
| 语义抽题与课堂消费 | `material_questions_live.py --live --owner ... --pdf ... --ocr ... --out ...` 复用授权 OCR，并在隔离 schema 调用抽取/复核角色；`--page` 可重复指定抽样页，`--text-only` 仅授权文字角色，`--retry-failed` 保留已完成窗口与累计预算。`material_quality_live.py --question-run ... --allow-sampled-questions` 可消费已完成的抽样题库；须明确记录抽样范围，不当作整本质量验收。`material_questions_synthetic_fixture.py --out ...` 只生成合成夹具，不调用 OCR。 |
| 独立考纲 | `unit_scope_live.py --live`；`--source` 与 `--outline` 互斥，另需 `--owner`、`--out`；恢复复用原配置与预算，已读基线时使用 `--evaluation-mode known-baseline` |
| 人物与记忆 | `persona_research_live.py`、`persona_search_live.py`、`persona_effect_live.py`、`persona_accept_live.py`、`memory_providers_live.py`、`memory_engine_live.py`、`memory_application_live.py` |
| 搜索对比 | `search_comparison_live.mjs`、`search_deepseek_live.mjs`、`grok_channel_search_live.mjs`；均支持 `--self-test` 离线自检，真实调用显式使用 `--live` |

真实实验的输入、原始产物与完整输出不提交仓库。具体目标、费用范围、
原始盲测与已知基线、失败恢复和未验证项在对应执行记录中单列。

语义抽题的定向回归入口为：

```powershell
./server/.venv/Scripts/python.exe tests/run_native_regression.py tests/test_material_questions.py tests/test_material_question_consumption.py tests/test_mirror_sync.py -q --tb=short
```

`material_questions_browser.mjs` 检查桌面、手机的目的地披露与抽取进度，
复用 `PW_ROOT`、`TEST_BROWSER` 及构建产物，使用 API 拦截；不替代真实上传或供应商测试。
`material_quality_live.py --stage feynman_source` 可单独验证冻结资料中的原问优先选择，
不生成课程、不记学生完成，结果应与完整课堂后的费曼交互验收区分。

## 证据与验收归档

`desktop_account_unit.mjs` 验证桌面登录会话恢复、退出、失效与异步保存顺序。
`teaching_scope_wait_browser.mjs` 使用构建产物与合成 API，检查考纲等待/失败/重试，
覆盖桌面与手机；不调用真实模型。`planning_authorization_browser.mjs` 同时检查长反馈
下的左栏滚动、前置提示隐藏、必要确认和 AI 修改入口。

[归档索引](../文档/测试归档/README.md)统一维护专题记录和最近已记录全量基线；
[历史汇总](../文档/测试归档/测试执行历史汇总-20261022.md)保留本次整理前的历次结果。
本页不再追加按日期堆叠的运行结果，也不复制通过数。
新增或修改入口时更新本页相关说明；执行后只更新对应专题记录和归档索引，
明确范围、选择理由、命令、结果、失败/跳过/警告与未验证项。

未来测试仍可自动创建 `.backup/` 作为忽略的本地输出目录。
已整理的旧实验在 `实验/`，需要复用时按 `整理清单.json` 查找，
不假定历史路径仍可直接使用，也不将旧产物写成当前验证通过。
