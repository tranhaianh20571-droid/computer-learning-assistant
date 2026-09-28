# 切片 1 执行记录与验收证据

> 状态：**已完成（2026-09-28）**
> 交接基线：[slice-1-capability-config-handoff.md](../../架构/slice-1-capability-config-handoff.md)
> 架构基线：computer-learning-assistant-architecture.md v1.7 §3.1、§3.2、§6、§8
> 环境：Windows 11 + Python 3.14.7（`uv run --project server/backend`）+ PostgreSQL 17（Docker/本机 5432）+ Node/Vite（frontend）

## 0. 本次实际改动文件

**生产代码（server/backend/app）**

| 文件 | 用途 |
| --- | --- |
| `slice1/__init__.py` | 包入口，注册 SQLAlchemy 元数据 |
| `slice1/errors.py` | 切片 1 受控错误码与异常（`config_not_found`、`credential_invalid`、`capability_test_failed`、`connector_offline`、`nonce_replay`、`disclosure_expired`、`disclosure_revoked` 等） |
| `slice1/crypto.py` | AES-256-GCM 凭据加密/解密、掩码函数，主密钥从 `APP_ENCRYPTION_KEY` 读取 |
| `slice1/models.py` | `service_configs`、`connector_bindings`、`disclosure_grants`、`connector_requests` 表 |
| `slice1/config_service.py` | 配置 CRUD、版本控制、软停用、管理员/用户隔离、worker 启用校验 |
| `slice1/disclosure_service.py` | 外发确认创建/查询/撤销、配置变更失效、worker 出站前核验 |
| `slice1/connector_service.py` | 配对码、一次性绑定、请求围栏、nonce 防重放、撤销、离线状态、回环目标校验 |
| `slice1/connector/gateway.py` | WSS 服务端网关（bind/auth/request/ping） |
| `slice1/adapters/base.py` | 适配器基类：超时、响应大小、错误映射、端点校验、能力探测契约 |
| `slice1/adapters/openai.py` / `anthropic.py` / `gemini.py` | 三类内容模型协议适配器 + 能力探测 |
| `slice1/adapters/minimax.py` | MiniMax TTS（`speech-2.8-hd`、音色、字幕、文本哈希） |
| `slice1/adapters/paddleocr.py` | PaddleOCR 提交/查询/结果获取骨架（结构化 spike 属切片 2） |
| `slice1/adapters/tavily_hikari.py` | Tavily Hikari 搜索适配器（20 秒超时、统一结果格式） |
| `slice1/adapters/__init__.py` | 适配器工厂、能力探测服务、`require_capability` 降级门禁 |
| `slice1/router.py` | 能力配置、外发确认、连接器 API 路由 |
| `slice1_app.py` | 切片 1 应用装配（配置变更失效授权 + 审计） |
| `app/main.py` | 挂载切片 1 应用 |
| `app/core/config.py` | 新增 `APP_ENCRYPTION_KEY` 设置（环境变量，默认空） |
| `app/alembic/versions/b1c2d3e4f5a6_slice1_capability_configs_connectors.py` | 新迁移（可回退） |
| `app/alembic/env.py` | 注册切片 1 元数据 |
| `server/connector/connector_agent.py` | 本机连接器独立进程（出站 WSS、只允许回环目标） |
| `server/backend/pyproject.toml`、`server/uv.lock` | 新增 `cryptography>=44,<51`（解析为 50.0.1）；`websockets` 已由 `fastapi[standard]` 提供 16.0 |

**前端（frontend/src）**

| 文件 | 用途 |
| --- | --- |
| `CapabilityConfig.tsx`（新增） | 能力配置页：个人模型、MiniMax TTS、管理员云能力、连接器配对/撤销、外发确认查询、能力状态徽章 |
| `api.ts` | 新增切片 1 API 客户端方法 |
| `App.tsx` | 新增 `/capabilities` 路由、导航入口、切片 1 错误码文案 |

**测试与支持包（tests、tests/support/backend）**

`tests/support/backend/slice1/**` 与 `server/backend/app/slice1/**` 保持同构镜像；切片 1 路由与状态在 `tests/support/backend/slice1_app.py` 以 slice-1 应用装配（生产为挂载 `create_slice1_app()` 的子应用）。

新增测试：`test_slice1_config.py`（11）、`test_slice1_adapters.py`（12）、`test_slice1_cloud_tts.py`（12）、`test_slice1_disclosure.py`（10）、`test_slice1_connector.py`（18）、`test_slice1_faults.py`（13）、`test_slice1_frontend_boundary.py`（8）= **84 项**。

## 1. 执行任务记录

### T01 契约冻结与数据模型 — 已完成

- 表结构：`service_configs`（`config_id`、`owner_user_id`、`owner_scope`、`kind`、`protocol`、`endpoint`、`model_name`、`encrypted_credentials`、`credential_mask`、`capability_status`、`config_version`、`is_active`、`created_at`、`updated_at`）。
- `connector_bindings`（绑定令牌哈希、配对码哈希与过期、状态 pending/bound/revoked、`last_seen_at`）。
- `disclosure_grants`（用户、任务、配置 ID + 版本、服务类型/端点、内容类别、范围快照、签发/过期/撤销时间）。
- `connector_requests`（绑定、任务、`nonce` 唯一、状态、过期时间）。
- 错误码沿用交接文档定义。迁移 `b1c2d3e4f5a6` 已执行 upgrade/downgrade 往返验证。

### T02 凭据加密与配置存储 — 已完成

- AES-256-GCM；密文为 `base64(nonce(12) || ciphertext)`；主密钥来自 `APP_ENCRYPTION_KEY`（支持 base64 32 字节或任意口令 SHA-256 派生），不落盘、不写日志。
- 查询/列表只返回 `credential_mask`（如 `sk-****1234`）；数据库与 API 响应均无明文（测试直接扫描 `encrypted_credentials` 列）。
- 更新凭据/端点/模型名 → `config_version + 1` 且能力状态重置为 `unknown`。
- 停用（软删除）后 `active_config_for_worker` 抛 `config_not_found`，worker 拒绝调用。
- 管理员 OCR/搜索配置 `owner_scope="admin"`，普通用户列表不可见、单条返回 404、不可修改。

### T03 模型适配器与能力探测 — 已完成

- 统一接口：`test_connection()`、`probe(capability)`、`probe_all()`、`generate()`、`stream()`、`cancel()`。
- OpenAI 兼容：`/chat/completions`，探测文本、图像（base64 data URL）、工具调用、`response_format.json_schema`、流式、取消。
- Anthropic：`/v1/messages` + `x-api-key` + `anthropic-version`，图像用 `source.base64`，结构化用 `tool_choice`。
- Gemini：`generateContent` / `streamGenerateContent`，key 走查询参数，图像用 `inline_data`，结构化用 `responseSchema`。
- 探测只发固定非私人样例（`你好，请回复 OK`、1×1 PNG）；结果写入 `capability_status`。
- 错误映射：超时→`timeout`、连接失败→`connection_error`、401/403→`credential_invalid`、429→`rate_limited`、5xx→`capability_test_failed`、不支持→`unavailable`；原始异常不外泄。
- 端点策略：公网模型要求 HTTPS；拒绝云元数据地址（169.254.169.254 / metadata.google.internal）、回环与私网地址；本地 TTS/OCR/搜索允许私网（部署在本机网关）。

### T04 TTS 与管理员云能力适配器 — 已完成

- MiniMax：`POST {endpoint}/v1/t2a_v2`，固定 `model=speech-2.8-hd`、`voice_id`、`subtitle_enable=true`、`subtitle_type`、音频格式与请求文本 SHA-256；返回 `hex` 音频与供应商 `trace_id`；支持自定义区域端点与多音色。
- PaddleOCR：任务提交、状态查询、结果获取骨架；探测用合成 `material_id`。
- Tavily Hikari：统一结果（title/url/snippet/fetched_at/source=external_supplement）、20 秒超时、失败受控错误码、只记录 `query_id` 与结果数。
- 管理员配置权限已在配置服务与 HTTP 层双重校验。

### T05 外发确认与 worker 核验 — 已完成

- 授权绑定用户、任务、配置 ID + `config_version`、服务类型/端点、内容类别、范围快照、签发/过期时间。
- `verify()` 在 worker 出站前检查：配置启用、授权存在、未撤销、未过期、类别覆盖、`config_version` 与端点未变；失败抛 `disclosure_required` / `disclosure_revoked` / `disclosure_expired` / `config_not_found`。
- 配置变更/停用通过 `on_config_changed` 回调批量撤销相关授权；普通用户不可修改既有授权（仅可撤销自己的）。
- 审计：`external.disclosure.confirmed`（granted/revoked）、`config.changed`、核验失败写 `access.denied`。

### T06 本机连接器 — 已完成

- 配对码：`secrets.token_urlsafe(18)`，10 分钟有效、一次性（兑换后 `pairing_code_hash` 置空）；绑定后发放 `binding_token`，只存哈希。
- WSS 网关：连接器主动 `bind`（配对码）或 `auth`（绑定令牌）；服务端注册连接并 `mark_seen`；断开时移除连接（离线）。
- 请求围栏：`call_type` 白名单（`model.generate` / `model.stream` / `tts.synthesize` / `health`）、payload ≤64 KiB、`expires_at`、`nonce` 唯一约束。
- nonce 防重放：已投递/已完成的 nonce 重复投递返回 `nonce_replay`；并发重复由唯一约束 + `IntegrityError` 兜底。
- 跨账户：绑定所有者与任务所有者必须一致，否则 `access_denied`。
- 撤销：绑定状态置 `revoked`，在途请求标记 `rejected`，新请求 `connector_revoked`，令牌解析同样拒绝。
- 回环限制：`is_loopback_target` 只接受 `127.0.0.1`、`::1`、`localhost`（含解析后回环），其它地址 `invalid_target`。
- 独立进程：`server/connector/connector_agent.py`（`websockets` 同步客户端，只连用户指定的 ws/wss 服务端，只回调本机回环目标，不打印令牌）。

### T07 前端配置页面 — 已完成

- `/capabilities` 页面：个人内容模型（OpenAI 兼容 / Anthropic / Gemini，协议+地址+模型+凭据）、MiniMax TTS（地址+音色 ID+凭据，默认 `speech-2.8-hd`）、管理员云能力（OCR/搜索，仅管理员可见）、已保存配置列表（掩码 + 能力徽章 + 测试 + 删除确认）、连接器配对（显示一次性配对码）/状态/撤销、外发确认记录查询与撤销。
- 能力状态徽章覆盖 `text`/`image`/`tool_call`/`json_schema`/`streaming`/`cancel`，状态 `available`/`unavailable`/`unknown`。
- 凭据输入框 `type=password` 且 `autoComplete=off`；保存后清空本地状态；只显示掩码；无 `console.log`；无硬编码密钥。
- `npm run build` 通过（tsc + vite）。

### T08 故障注入与总验收 — 已完成

见下节 AC 汇总。

## 2. 实际运行的命令与结果

| 命令 | 目录 | 结果 |
| --- | --- | --- |
| `uv run --project server/backend pytest tests -q` | 仓库根 | **234 passed, 1 warning in 13.21s**（切片 0 150 项不回归 + 切片 1 84 项） |
| `uv run alembic upgrade head` / `downgrade -1` / `upgrade head` | `server/backend` | `a42e8c1f0d9a → b1c2d3e4f5a6`，回退 `b1c2d3e4f5a6 → a42e8c1f0d9a`，再升回 head；`alembic current` = `b1c2d3e4f5a6 (head)` |
| `npm run build` | `frontend` | `✓ built in 962ms`（含既有的 >500 kB chunk 提示） |
| `node scripts/slice1_frontend_check.mjs http://127.0.0.1:4173 .playwright-cli` | 仓库根 | login=true、能力配置对匿名用户不可见=true、键盘焦点=true、外部请求 0、pageerror 0、无敏感控制台输出；截图 `.playwright-cli/slice1-narrow-login.png`、报告 `.playwright-cli/slice1-frontend-report.json`（需先 `cd frontend && npx vite preview --port 4173`，并设置 `PW_ROOT` 指向含 playwright 的 node_modules） |
| `uv run python -c "from app.main import app; ..."` | `server/backend` | 生产应用挂载成功，`/api/capabilities/*`、`/api/disclosures*`、`/api/connectors/*` 共 16 条路由可见 |

测试环境变量：`APP_ENCRYPTION_KEY=test-encryption-key-material`（合成值，仅本地测试）。
依赖版本：cryptography 50.0.1、websockets 16.0、httpx 0.28.1、Python 3.14.7、FastAPI 0.141.1、SQLAlchemy 2.x、Playwright 1.63.0（系统 Chrome 执行）。

提交与推送：`55265bd feat(slice1): add capability config, disclosure grants and local connector`，已推送 `origin/main`（远程 ref 与本地 HEAD 一致）。

## 3. AC 汇总

| 验收编号 | 结论 | 证据 |
| --- | --- | --- |
| AC-01 配置数据模型与凭据加密 | 通过 | `test_slice1_config.py`（数据库无明文、API 只返回掩码、版本递增、迁移可回退 + 真实 upgrade/downgrade 往返） |
| AC-02 三类模型协议适配 | 通过 | `test_slice1_adapters.py`（OpenAI/Anthropic/Gemini mock 契约与原生请求格式、`capability_status` 写回） |
| AC-03 能力探测与降级 | 通过 | `test_slice1_adapters.py::test_json_schema_unsupported_marked_unavailable`、`test_slice1_faults.py::TestCapabilityUnavailable`（`model_capability_unavailable`，不重试） |
| AC-04 TTS 与管理员云能力 | 通过 | `test_slice1_cloud_tts.py`（MiniMax 模型/音色/字幕/文本哈希、OCR 三接口骨架、Tavily 归一化与 20 秒超时、管理员配置对普通用户不可见） |
| AC-05 外发确认与核验 | 通过 | `test_slice1_disclosure.py`（无授权拒绝、撤销后停止、过期拒绝、配置变更失效、跨账户隐藏、审计记录） |
| AC-06 连接器配对与通道 | 通过 | `test_slice1_connector.py`（配对码一次性/过期、WSS bind/auth、绑定账户任务、回环目标限制、请求大小与调用类型） |
| AC-07 连接器撤销与 nonce 重放 | 通过 | `test_slice1_connector.py::TestRevocationAndOffline`、`test_slice1_faults.py::TestConnectorFaults`（撤销拒绝新/在途/重放、离线与 revoked 状态可见） |
| AC-08 前端配置页面 | 通过 | `npm run build`、`test_slice1_frontend_boundary.py`、Playwright 报告（0 外部请求、无敏感控制台输出、窄屏截图） |
| AC-09 切片 1 故障注入 | 通过 | `test_slice1_faults.py`（凭据泄漏扫描、跨账户访问、外发失效、nonce 重放、连接器撤销、能力不可用） |

## 4. 未决项与差异

1. **主密钥管理方案**：首版采用环境变量 `APP_ENCRYPTION_KEY` + 应用层 AES-256-GCM；密钥轮换与 KMS 未实现，属架构 §6 允许的首版范围。未配置该变量时应用可正常启动，仅凭据写入返回受控错误。
2. **Tavily Hikari 日志改造**：切片 1 只接入适配层，未改动 Hikari 自身的审计日志保留策略；该前置核对项仍未关闭，交回规划者。
3. **连接器形态**：已实现为独立 Python 进程 `server/connector/connector_agent.py`；未提供打包为 Windows 可执行文件的发行流程。
4. **WSS 传输**：网关默认监听 `127.0.0.1` 且握手为 `ws://`（测试与本地部署）；生产部署需置于 TLS 终止之后使用 `wss://`，TLS 证书与跨用户隔离仍需按架构 §8 核对项 3 在目标网络实测。
5. **MiniMax 服务区域与音色**：适配器支持自定义端点，但具体区域地址与可用音色 ID 需用户账户核对，未写死为产品常量。
6. **OCR/TTS/模型 spike**：本次仅用 mock 端点验证适配器契约；真实 OCR 结构化结果、TTS 字幕时标、模型能力探测仍按架构 §8.0 分别属于切片 2/3/4 门禁，未在此声明通过。
7. **审计事件**：复用切片 0 已注册的 `config.changed`、`external.disclosure.confirmed`、`connector.bound`、`connector.revoked`、`access.denied`，未新增事件类型，也未修改审计不可变性与字段允许名单。
8. **前端交互范围**：切片 1 只提供外发确认记录查询界面；任务创建前的外发确认交互按交接文档在切片 3 接入。

## 5. 合成数据声明

所有测试使用合成账号（`synthetic.*@example.com`）、假 API Key（`sk-test-fake-key-000000000000`）、假端点（`*.example.test`）与 httpx MockTransport；仓库、日志、审计与测试证据中不含真实密钥、教材正文、提示词或供应商响应。
