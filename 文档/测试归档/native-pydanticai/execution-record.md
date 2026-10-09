# 原生 PydanticAI 重构执行记录

日期：2026-10-08。结论：代码实施和本地合成自动化回归通过；真实供应商与 Logfire 平台验收未执行。本文是执行证据，不把设计目标或早期专项结果视为最终验收。

## 范围与隔离

本次修改共享 AI 执行、任务租约/事务、安全出口、披露与监控，因此执行完整根 pytest，并补充原生 SDK、ASGI 断连、PostgreSQL 迁移、前端和浏览器验证。覆盖访谈、规划/细化、教学规划/步骤/追问、测验/评价、对话/修订/封面、聊天及后台教案精炼。应用 core 更名 infrastructure；生产镜像逐字节同步。保留原有 staged/untracked/deleted 用户改动，未提交、推送或还原旧演示页。

数据库均为本机随机隔离 schema；根套件入口初始化完整业务元数据、SQLModel 和合成管理员，注入 LEARNING_DATABASE_URL 与 DATABASE_URL，最后只删除自己创建的 schema。未对生产数据或生产数据库执行迁移。浏览器使用实际 assembler.create_app 和显式 SDK FunctionModel，独立随机端口与 schema；完成后清理自有进程和 schema。

## 工具版本

| 工具 | 版本 |
| --- | --- |
| Python | 3.14.7 |
| pydantic-ai-slim | 2.51.0，依赖区间 >=2.51.0,<2.52.0 |
| Pydantic | 2.13.4 |
| httpx2 | 2.13.1 |
| OpenTelemetry SDK | 1.45.0 |
| pytest | 9.1.1 |
| Vite | 8.3.1 |

## 最终结果

| 验证 | 结果 | 证据/入口 |
| --- | --- | --- |
| 根 pytest 全量，含最终 Schema/SSE/检索补丁 | 1165 passed / 8 skipped / 2 subtests passed，9 warnings，181.59 秒，exit 0 | `.backup/native-pydanticai/native_validation_89c9462d9dcc47dd8680850eb9eebb20/pytest.log` |
| Schema/回退/精炼/原生执行/任务/T03/T04 最终专项 | 318 passed，1 warning，9.63 秒 | 下方专项命令 |
| 真实 ASGI 2.3/2.4 断连与聊天专项 | 40 passed，13.44 秒 | `.backup/native-pydanticai/native_validation_9fdab9123bc84d29b9e2977e0aaa46af/pytest.log` |
| 检索状态/旧桥/回退/镜像专项 | 110 passed，2.29 秒 | 最终根套件再次包含这些测试 |
| 原生浏览器最终复验 | 14/14，通过；pageErrors=[]，9 次真实 SDK 合成调用 | `.backup/native-browser/course_library_validation_27f4e19c99c14e3ea884210081b987fc/native-browser-result.json` |
| 前端 TypeScript | tsc -b，exit 0 | 本次最终执行 |
| 前端生产构建 | Vite build，exit 0，1.99 秒 | 仅 >500 kB 分块警告 |
| 前端 Node 单元 | 全部 9 套通过，exit 0 | 根 tests 下全部 *_unit.mjs |
| 镜像/数据库迁移/隐私 | 完整根套件通过 | test_mirror_sync / test_native_migrations / test_native_observability / test_native_task_observability |

全量命令（仓库根执行）：

```powershell
.\server\.venv\Scripts\python.exe tests/run_native_regression.py tests -q --tb=short -rs
```

最终合成专项命令：

```powershell
.\server\.venv\Scripts\python.exe -m pytest tests/test_native_candidate_schema.py tests/test_native_fallback_policy.py tests/test_native_lesson_refinement.py tests/test_native_execution.py tests/test_native_task_contracts.py tests/test_t03_task_events.py tests/test_t04_audit.py -q --tb=short
```

前端命令：在 frontend 执行 `node node_modules/typescript/bin/tsc -b` 与 `node node_modules/vite/bin/vite.js build`；在仓库根逐一执行全部 `tests/*_unit.mjs`，本次共九套。浏览器命令：`server/.venv/Scripts/python.exe tests/run_native_browser_regression.py`，运行前设置 TEST_NODE/PW_ROOT/可选 TEST_BROWSER，并先完成前端构建。不在归档中记录个人运行时路径或数据库凭据。

## 浏览器覆盖

最终运行在严格生成 Schema 与最新前端构建之后。覆盖四个访谈问题及两次追问、六题后课程草稿持久化、课程确认、普通与流式聊天、刷新不重复请求、确认后测验入口门控、原生教学首段、无 TTS 的明确文字恢复、桌面及 390px 手机布局、刷新保留文本。真实画布为 620×720，暗色像素 1296，桌面/手机截图无空白主内容或遮挡。合成角色包含 intake/planning/chat/chat_stream/teaching；没有启用生产 duck 桥或开发兜底。

## 跳过与警告

- test_slice5_regression 的两项音频检查：没有配置 TTS；不能计为音频通过。
- test_t05_frontend_boundary 的六项旧演示检查：用户原先删除的文档/ui/index.html 不存在；未恢复用户删除。实际 frontend 已单独完成构建、单元和浏览器验证。
- 9 条 warning 为既有 FastAPI on_event 和 Google SDK 弃用提示；不是失败，不代表已消除这些技术债。

## 失败轮次与修复

早期全量 85 failed / 726 passed / 25 errors；原因包括旧 tools-only 合成夹具、旧 mode/缓存断言、虚构披露任务和缺失旧演示。第二轮 952 passed / 3 failed / 19 errors；剩余配置工厂和跨账户夹具更新为真实任务/租约与显式 SDK 模型，不放松生产边界。此前 1105 passed / 8 skipped 为最终 Schema/SSE 补丁前基线，已被本记录的 1165 基线替代。

专项暴露并修复的实际缺陷：Anthropic 所需 httpx2 客户端、Google 默认同步客户端环境代理、DNS 后撤销重新核验、非输出参数不得触发降档、逻辑预算 phantom debit、chat validator 签名、PostgreSQL subject 长度、严格安全错误不可回退、ASGI 断连生产者和租约清理。新候选额外字段通过递归生成专用 subclass 拒绝，旧持久模型解析不变。

## 实施与外部验收边界

- 原生 Instrumentation → OTel → Logfire 的应用端采集、Trace/Metrics、上下文、有界导出、健康信号和隐私已落地并合成验证；真实 Logfire 项目收数、仪表盘/告警、权限、留存和远端删除未配置或验证。部署说明见 ../../部署/operational-monitoring.md。
- 未访问真实模型、TTS、搜索、OCR、图像供应商，也未验证教学质量、真实音频、完整课程、浏览器测验提交、负载或供应商账单。
- 默认检索没有正文 text_resolver；明确报告不可用状态。真实搜索/OCR 等外部装配缺口未冒充完成。
- 两个新 Alembic 迁移已在隔离 PostgreSQL 专项核验；生产升级必须另行部署执行。旧桥模块保留兼容/测试入口，但当前业务不调用。

## 结论

本次代码实施、权威全量根套件及本次原生业务浏览器专项通过。外部部署验收仍单独成立，不因自动化通过数而完成。
