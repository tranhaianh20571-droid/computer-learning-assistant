# 教学授权恢复与异步封面修复执行记录

日期：2026-10-08。范围：按已确认方案修复教学卡住与课程封面流程；不配置 Logfire。代码与本地合成回归通过，最终全量 exit 0。本文不把合成验证当成真实供应商验收。

## 改动与影响范围

- 共享任务服务增加截止扫描、排队拒绝的原子终态写入与有界 worker 失败收尾；覆盖 queued/leased/running，清除租约、关闭尝试并发布受控事件。重试只递增任务执行代次，既有课程内容代次与持久 Agent 预算不清零。
- 错误归一化使用已注册代码，披露错误不再造成 UNKNOWN_ERROR_CODE 二次异常；任务失败、撤销、超时不被后台吞掉，也不能进入内容或封面 fallback。
- 教学开始、预热和恢复采用只读预检与显式确认；固定课程/讲次/大纲/资料/配置版本和 fingerprint。内容、TTS、预热分别选择，任务、根授权、派生授权和审计在同一事务提交。每次模型/TTS 请求与结果发布均检查固定根链，不采用无关新授权替换已撤销授权。
- 已有音频幂等任务重复请求核验原派生授权；只对新任务派生。撤销子授权后重复点击不创建授权、不合成、不改已保存文字；显式恢复确认才可重建授权。当前会话没有 TTS 同意时不转入预热音频。
- 工作区提供受控错误、截止时间、租约时间、可重试信息与恢复动作。前端区分首段文本失败、授权等待、配置缺失和语音等待，恢复先确认，不以“继续文字讲解”代替生成缺失正文。
- 图片封面采用独立持久后台扫描与有界线程池；GET 只读。显式确认固定图片配置版本、目标标题摘要和路线主题集合，派生课程必须属于当前确认路线并匹配已审批主题。真实生图调用在后台进行；供应商端当前仍为同步 HTTP 接口，未冒充提供方异步任务 API。
- 图像出口限制 HTTPS、公共 DNS、连接 IP 固定与原 Host/TLS SNI、代理/重定向/重试、字节上限，DNS 后和响应期间再次检查授权。仅受控临时供应商失败允许程序封面，授权/配置/任务失效直接拒绝。UI 展示请求方式、实际方式、降级原因和重新生成入口；无图片配置时可明确选择本地封面。
- 新增 k5e6f7a8b9c0 披露父授权迁移、l6f7a8b9c0d1 讲次授权迁移；旧授权父指针为空，不自动补用户同意。生产/测试镜像逐字节同步。

共享任务、权限、迁移与跨模块流程都受到影响，因此执行权威根 pytest 全量，以及 PostgreSQL 迁移/撤销/事务/权限专项、前端单元与真实应用装配浏览器验证。

## 执行隔离

每轮使用随机 PostgreSQL schema、完整生产/模板元数据和合成管理员。浏览器使用实际 assembler.create_app、显式 SDK FunctionModel 和合成图像适配器，随机后端/前端端口。测试仅删除自己创建的 schema 与进程。不请求真实模型、图像或 TTS，不导出真实凭据或生产正文。

此前挂起的音频专项仅取消已验证属于本轮测试 schema 的阻塞查询；最终核实无活动连接后删除该精确 schema。生产连接、业务任务与既有本地运行服务未关闭或修改。

## 最终命令与结果

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| 权威根 pytest 全量 | 1360 passed / 8 skipped / 2 subtests passed，921 warnings，280.43 秒，exit 0 | `.backup/native-pydanticai/native_validation_0e394ce2c15540b88e7074861f628403/pytest.log` |
| 根授权/迁移/任务收敛专项 | 87 passed，3.84 秒 | `native_validation_8c2a60549511408b9b749e5fdb0076e9` |
| 教学授权/绑定/精炼/迁移/ASGI | 103 passed，30.20 秒 | `native_validation_ff22f872abdb48c5b42068ad0938c6a6` |
| 封面/回退/图像出口/精确根授权 | 202 passed，27.18 秒 | `native_validation_138ddbaa351f49438de64ac1905a5cfb` |
| 最终音频同意与幂等撤销 | 37 passed，52 弃用警告，23.23 秒 | `native_validation_25a377e28dc24cef99d6b66643a2ffa2` |
| 最终教学授权与恢复元数据 | 27 passed，19.96 秒 | `native_validation_fcc681e3fd9a478081773f2d2c22c1be` |
| 最终真实装配 Chromium | 19 checks passed，pageErrors=[]，11 次 SDK/合成调用 | `.backup/native-browser/course_library_validation_3478efa9f43848e8b8e130e73626911d/native-browser-result.json` |
| 确认 UI 浏览器专项 | 8 checks passed，合成 API | `tests/consent_browser.mjs` |
| 前端 Node 单元 | 全部 10 套通过，含 consent 17 assertions | `tests/*_unit.mjs` |
| TypeScript / Vite | tsc -b exit 0；Vite 8.3.3 build exit 0，1.58 秒 | 分块 >500 kB 警告仍存在 |
| 差异格式 | git diff --check exit 0 | 未暂存、提交或回滚用户变更 |

仓库根全量命令：

```powershell
.\server\.venv\Scripts\python.exe tests/run_native_regression.py tests -q --tb=short -rs
```

浏览器命令：设置现有 TEST_NODE、PW_ROOT 和可选 TEST_BROWSER 后执行 `server/.venv/Scripts/python.exe tests/run_native_browser_regression.py`。个人路径和数据库凭据不进入归档。前端在 frontend 执行 `node node_modules/typescript/bin/tsc -b` 与 `node node_modules/vite/bin/vite.js build`，根目录逐一执行全部 `tests/*_unit.mjs`。

浏览器验证：访谈、规划确认、普通/流式聊天持久化、取消同意零确认请求、固定配置/主题封面的真实后台消费、教学取消零确认请求、仅内容同意且 TTS/预热关闭、首段持久化、授权过期后的明确恢复且保留已提交文字、文字播放、画布真实绘制、390px 手机无横向溢出、刷新恢复。画布 620×720、暗色像素 1296。未验证整节课完成、测验提交、真实语音或真实供应商。

## 跳过与警告

8 项跳过包括未配置 TTS 的两个跨模块音频检查，以及用户已删除旧演示页的六个检查；本轮另有专门音频授权与前端浏览器验证，但未执行真实 TTS。921 条警告主要是重复应用装配产生的 FastAPI on_event 弃用提示，另有 Google SDK 弃用提示；仍需后续迁移 lifespan，未计为已消除。

## 失败轮次与修复证据

- 首轮修复全量：52 failed / 1248 passed / 8 skipped。旧 TTS 夹具未显式确认、旧封面 fallback/GET 触发断言和 refinement 简化夹具不满足新安全合同，按真实授权和持久任务语义更新，没有放松生产安全。
- 后续全量：3 failed / 1345 passed / 8 skipped / 2 subtests passed，238.74 秒。两个合成期间变更用例应保留更具体的 DISCLOSURE_REQUIRED / DISCLOSURE_REVOKED；另一个暴露生产幂等音频任务重复派生授权的撤销绕过，已修复并增加授权数量不变、无外发、文字不变断言。证据：`.backup/native-pydanticai/native_validation_f402f8246f1d43c6b4b9dafcb121882e/pytest.log`。
- PostgreSQL 专项还发现持锁事务内另开会话核验授权的死锁；已改为传入同一 session，事务与安全审计一起提交，后续专项及最终全量验证这一流程。

## 部署与未验证边界

- 用户随后授权“部署升级吧”。已停止旧后端，创建 1,802,156,061 字节 custom-format PostgreSQL 备份，pg_restore --list 及完整读取到 NUL 均 exit 0，SHA-256 为 `561b97c9087853bb8fa5c162253621706683202ba0ac877c87479aab3ea27a46`。在同一事务运行两项正式迁移的 upgrade，版本 j4d5e6f7a8b9 → l6f7a8b9c0d1；父授权 FK/CASCADE、新讲次授权表及无隐式同意检查通过。课程、讲次、配置、授权、会话和步骤计数前后一致。
- 最新 8000 后端已启动；现有 5173 Vite 曾返回旧转换缓存，重启后核验新版授权说明已生效、旧隐式生图说明已消失。/healthz、/api/v1/utils/health-check/、前端代理及首页均 200。部署证据位于 `.backup/local-startup/consent-deploy-20261008T160227Z/deployment-result.json`，同目录保存迁移前完整备份。本次是本地服务升级，未配置机器开机自启动。
- 原卡住会话与任务未自动重跑、未补根同意、未触发外发。新后台启动可收敛超期任务；继续生成须在界面完成新的明确确认。
- Logfire 不配置；原有观测应用接线保持，本次不代表平台已收数或有新增 dashboard。
- 供应商 API 可用性、真实生图/语音质量、账单、负载与进程异常强杀后的 HTTP 取消未验收。图片 HTTP/DNS 调用本身有阻塞上限局限；调度器关闭使用有界等待、停止发布和取消未开始任务，不宣称线程可强制终止。

## 2026-10-09：取消教学弹窗、首段恢复与预加载

本轮按用户要求完成修改并只做局部回归；上文 2026-10-08 的逐讲确认描述为历史行为，教学现改为使用当前启用配置直接开始、预生成和恢复。封面确认行为保留。

- 生产装配默认启用 `TEACHING_AUTOMATIC_AUTHORIZATION=1`。服务端仍固定配置版本、课程/讲次/资料和模板快照，在任务事务中创建或复用精确短期根许可及派生许可；撤销的同版本根或子许可不能通过自动恢复绕过。许可到期可由明确入口更新快照。后台使用已创建的任务许可，不自行补许可。
- 自动预加载可在大纲确认、课程总览和下一讲选择时创建首段任务；同键重复读取复用待处理或就绪缓存。失败自动重试在有效缓存期内最多两个尝试，并保留 60 秒冷却、每日和队列上限；用户主动预生成仍受每日和队列限制。预加载状态区分失败、未配置、关闭和许可阻断，超时不再误报模型能力不可用。
- 任务成功而缺少对应正文/板书时，工作区返回明确的产物缺失状态。开始/恢复与下一段调度可以创建独立的 `artifact-repair` 任务，每段最多两次；保留原成功任务、业务内容代次、已有文字/板书版本和原任务预算。恢复达到上限后停止暴露重试动作。
- 工作区 `tts_authorized` 与保存的 TTS 配置分别投影。根或语音子任务许可失效时，前端无需等待语音即可展示文字；迟到的音频也不会自动播放。板书字体网络加载失败回退到系统字体，避免未捕获的页面异常。

### 局部验证

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| 自动教学、产物恢复、旧显式许可、首段音频和预加载五个模块 | 61 passed，49.86 秒，exit 0 | `.backup/native-pydanticai/native_validation_d51e4bedf38e4fa7869d923d1e6f52bd/pytest.log` |
| 最终自动教学与产物恢复，含新增直接恢复、到期更新及语音子任务撤销 | 26 passed，16.54 秒，exit 0 | `.backup/native-pydanticai/native_validation_66c62e95959e479dba0a876a11c9a707/pytest.log` |
| 教学前端单元 | 5 组通过 | `tests/teaching_entry_unit.mjs` |
| 教学页面 Chromium | 7 项通过，pageErrors=[]，全部 API 与外域请求合成拦截 | `tests/teaching_entry_browser.mjs` |
| 教学入口及保留的封面确认 Chromium | 6 项通过，pageErrors=[]，合成 API | `tests/consent_browser.mjs` |
| TypeScript / Vite | tsc -b exit 0；Vite 8.3.3 build exit 0，15.78 秒 | 原有大分块提示仍存在 |
| 生产与测试镜像 | 本轮五个后端文件 SHA-256 逐一相同 | authorization、preparation、router、service、slice3_app |

两轮后端验证合计覆盖 64 个不同用例；第二轮含重复用例，未将通过次数相加当成独立用例。所有后端测试使用随机 `native_validation_*` schema 与合成模型/TTS，仅清理自己的测试 schema。未运行全量回归，未验收真实供应商讲解/语音的可用性或质量。

前后端服务恢复到现有 5173 / 8000 地址，后端启动日志确认 application startup complete；前端、后端健康检查及前端代理健康检查返回 200，OpenAPI 已装配直接预生成和恢复路由。本轮未执行生产数据库迁移、删除、重置或手工改写成功任务/教学产物。原卡住讲次在用户再次开始或恢复时通过新补生成任务处理。

## 单顶层封面与默认跨讲预载

2026-10-09 追加。用户批准实现一次规划一张顶层封面、隐藏封面生成方式、后台默认第一讲/N+1 预载，并明确本轮不用全量回归。保留已有课程、封面、配置、任务和教学记录；未重置数据库。

### 行为与降级

- 课程集只保留课程集封面，独立课程才有课程封面。成员课程在 GET、入队、worker 和发布时均复查实际归属（含历史版本及脱离关系）；旧图片与授权保留，业务响应 suppressed 不泄露图片地址。正常 UI 不展示生成方式与降级标签。
- 重复确认、随机请求键与当前有效封面复用同一任务/图片；显式 regenerate 才重新请求。规划范围变更在同所有者、同主题检查后复制已有完整图片，不继承新外发。封面显式许可保留，固定根撤销不得以新直接许可绕过。
- 封面供应商超时、连接、限流及服务错误可落到本地封面；撤销、版本变更、删除、取消与租约丢失不伪装成功。替换失败不丢既有图片。
- 已确认且已配置课程默认后台预载目标首段：尚未学习时第一讲；当前讲第一段实际发布后下一讲；按单元/讲次顺序且末讲停止。不修改学习完成状态，UI 无预载按钮/轮询/提示。
- 缓存唯一键为 owner/course/lesson/text_key/audio_key，保留不同讲次与配置版本。后台每 15 秒检查有限近期课程，每批最多 4 个目标，优先交互任务；每日/队列上限、失败最多两次及 60 秒冷却继续生效。预载失败不阻塞主动进入课堂。
- 正文先存 text_ready，TTS 异步补齐；语音配置变更或撤销仅丢弃迟到音频，不丢正文。前端等待上限 12 秒，失败或自动播放受阻改文字；迟到音频不得插播。内容成功但缺产物用独立 repair 任务最多两次，耗尽显示受控失败而不无限等待。

### 最终局部验证

运行目录为仓库根；Python 使用 server/.venv，Node 使用本机 Node；前端 Vite 8.3.3。测试根为 tests，所有供应商适配器/数据均合成，后端使用随机 schema 并只清理自己的 schema。

```powershell
.\server\.venv\Scripts\python.exe tests/run_native_regression.py tests/test_top_level_cover.py tests/test_automatic_knowledge_cover.py tests/test_planning_dialogue_cover.py tests/test_cover_image_transport.py tests/test_lesson_lookahead.py tests/test_first_segment_prewarm.py tests/test_first_segment_audio.py tests/test_teaching_automatic_start.py tests/test_native_migrations.py tests/test_mirror_sync.py tests/test_teaching_artifact_recovery.py -q --tb=short
node tests/teaching_entry_unit.mjs
node tests/consent_unit.mjs
node tests/lecture_audio_unit.mjs
node tests/teaching_entry_browser.mjs
node tests/consent_browser.mjs
```

| 检查 | 实际结果 |
| --- | --- |
| 最终后端限定套件 | 139 passed，72.69 秒，exit 0；证据 .backup/native-pydanticai/native_validation_5cb54efc25e74e57b8324533a3cc8ad4/pytest.log |
| 教学入口/封面许可/音频单元 | teaching_entry 5 组、consent 17 断言、lecture_audio 通过 |
| 合成 Chromium | 教学 10 项、封面 6 项；pageErrors=[]；含成员不取图片、无预载 UI、无教学弹窗、12 秒及迟到音频降级 |
| frontend 的 tsc -b / vite build | exit 0；仅原有大于 500 kB 分块警告 |
| 无损缓存迁移与镜像 | 包含于最终套件；保留旧行和多讲版本，拒绝有损降级，逐字节镜像一致 |

早期限定测试因新增测试导入/夹具错误、撤销错误码和取消任务终态而失败；修复后以上最终套件通过，不将早期重复运行累计为独立用例。仍有 FastAPI on_event 弃用警告及一个仅缺失 outline 场景的 SQLAlchemy NULL identity 警告。

未运行全量，历史 1360 项结果不更新。合成浏览器不代表真实供应商讲解、图片或语音质量/账单/负载已验收。恢复本地应用后默认后台预载可能使用已启用的真实配置；这与测试外发边界分别记录。

### 本机部署状态

本地数据库、备份、进程、任务标识和供应商调用属于私有运行信息，不纳入仓库验收归档；本记录仅描述隔离合成环境的验证结果。
