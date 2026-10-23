# 市集对接说明（给协作 Agent）

本文件是一份自包含的对接说明。**不包含任何密码、授权码或令牌**；需要凭据时向账号所有者索取，不要写进仓库。

## 1. 拓扑

```
浏览器 ── HTTPS ──> Tailscale Funnel ──> Caddy 127.0.0.1:80 ──> 市集服务 127.0.0.1:8081
                                                                    └─ SQLite + 卡片包目录
桌面端 ── HTTPS ──> 同一地址 /api/*（设备令牌认证）
```

- **市集公网地址**：`https://studypal.tailf6f37a.ts.net`
- 市集服务代码：`server/market/app.py`（独立 FastAPI，不依赖主库）
- 静态页与说明页：`server/market/web/`（`index.html`、`help-import.html`、`logo.png`）
- 桌面端桥接：`server/backend/app/market_link.py`、`server/backend/app/market_router.py`
- 卡片包实现：`server/backend/app/characters/package.py`

## 2. 身份模型（关键）

市集是唯一的账号主体，桌面端不注册、不存密码。

2026-10-22 网页登录与用户名同步：
本机 `GET /api/market/profile` 使用当前属主的绑定读取市集
`GET /api/local/profile`，核对市集 ID 和本机邮箱后只更新当前本地用户名。
界面启动、重新聚焦时同步，离线保留已有用户名。
本机 `POST /api/market/web-login` 使用完整设备授权申请市集
`POST /api/web-login/tickets`，返回 `/#login=<一次性票据>`。
票据只保存哈希，60 秒有效、原子消费、关联签发设备；接收授权不能兑换网页登录。
市集首页立即移除 fragment，再 POST `/api/web-login/consume` 建立 HttpOnly Cookie。
消费前重新核对设备未撤销及账号已验证；不得把长期设备令牌交给浏览器。
新增响应均禁止缓存，消费接口要求同源 Origin，票据不进入查询参数或日志。
未绑定或接收授权账号点击时返回受控错误，不借用其他账号网页登录。

2026-10-22 账号接收契约（替代“仅人物页面在线”行为）：

- 本机启动后按已授权账号后台领取任务，与当前页面、本机会话及退出登录无关。
  网页 A、本机 B 的任务只写入 A 的本地属主空间；未授权 A 不自动写入 B。
- `/api/market/bind/start?purpose=receive` 与 `/bind/status?purpose=receive&code=`
  为匿名的一次性授权流程，必须由本机生成并持有的挑战码发起；
  确认后只建立/复用 A 的本地身份并保存接收授权，绝不签发或替换本机 Cookie。
  原默认注册流程保持建立本机会话的兼容语义。
- 市集 `/bind?code=&purpose=receive` 明示接收授权，签发仅可领取/回写任务及下载包的
  接收凭据，不能上传/删除卡片；网页离线提示链接到本机 `#/market-receive` 开始授权。
- 本机 `/api/market/reception` 的 GET/PATCH/DELETE 仅允许操作当前会话账号：
  查看、暂停/恢复、撤销。退出登录不撤销；停用用户不再接收；授权变更记录审计。
  不增加匿名账号列表、全局接收控制或跨账号导入。
- 已有加密绑定按账号继续使用，新增授权有独立版本，暂停与撤销在导入发布前再次校验。
  后台每账号单飞，其他账号独立心跳；生产装配显式启停，浏览器只读取本人导入通知。
- 市集任务领取为原子租约，只有领取凭据可回写；失败回写可重试、重启不重复导入。
  本机持久回执去重；导入中断且结果未知时停止重放并报告需检查，不伪称成功。
  本机接收回执与授权存储在忽略的 storage 目录，令牌加密、正文不进入日志。

2026-10-22 注册入口约定：本机 `/bind/start` 返回市集首页 `/#bind=<code>`。
首页从 fragment 取得一次性码，立即清除地址栏 fragment，并在当前标签页的
sessionStorage 中最多保留 10 分钟；市集导航及刷新保留上下文，过期或注册成功后清除。
首页「注册账号」进入既有 `/bind?code=` 页面；注册接口已确认绑定，页面不得再次确认，
只需建立市集 Cookie 并返回市集首页。本机轮询成功必须同时保存加密设备令牌并建立本地
Cookie 会话后才返回 `bound:true`；失败须报告受控错误，不得宣称已连接。

2026-10-22 上传管理增量：`/uploads` 复用市集导航与卡片网格，
`GET /api/cards?scope=mine` 要求 Cookie 或设备认证，查询仅返回当前属主的最新 200 张已发布卡片。
网页删除复用 `DELETE /api/cards/{id}`，确认后提交；无权或不存在统一为 404，
Cookie 外站来源拒绝 403。删除只影响市集上传包，不删除本机人物及已下载副本。
删除成功、拒绝与失败写入市集 SQLite 追加式 `audit_logs`，
不记录邮箱、名称、包正文或凭据；审计写入失败不得声明成功。

- 注册**必须**携带桌面端生成的一次性 `bind_code`，否则 `409 LOCAL_REQUIRED`。
- 桌面端登录：市集验证邮箱密码 → `POST /api/devices` 换长期设备令牌 → 本地 `AuthService.issue_market_session` 建本地会话。同邮箱的旧本地账号会被复用，并**清空本地密码哈希**。
- 本地 `users.user_id` 与市集 `user_id` 通过 `storage/market_bindings.json`（AES-GCM 加密）关联。

两种凭证：

| 凭证 | 用途 | 传递方式 |
| --- | --- | --- |
| 会话 Cookie `market_session` | 浏览器页面 | httpOnly Cookie，仅市集域 |
| 设备令牌 | 桌面端调市集 API | `Authorization: Bearer <token>` |

## 3. 市集 API

所有错误统一为 `{"detail": {"error_code": "...", "message": "..."}}`。

### 账号

| 方法 | 路径 | 认证 | 请求 | 响应 |
| --- | --- | --- | --- | --- |
| POST | `/api/send-code` | 无 | `{email}` | `{ok, cooldown:60}`；60 秒内重发 `429 CODE_COOLDOWN` |
| POST | `/api/register` | 无 | `{email, password, display_name, code, bind_code}` | `{ok, verified:true, user_id, bound:true}` |
| POST | `/api/login` | 无 | `{email, password}` | `{user_id, session_id, csrf_token, expires_at}` + 设 Cookie |
| POST | `/api/logout` | Cookie | — | `{ok:true}` |
| GET | `/api/me` | Cookie | — | `{authenticated:true, user:{...}}` 或 `{authenticated:false}`（200，不报错） |
| POST | `/api/username` | Cookie | `{display_name}` | `{ok, display_name}`；重名 `409 USERNAME_TAKEN` |
| GET | `/api/username/available?name=` | 无 | — | `{available, reason}`，reason ∈ `ok/taken/too_long/invalid_chars/empty` |
| POST | `/api/devices` | Cookie | — | `{token, user}`（设备令牌，桌面端保存） |

用户名规则：≤24 字，仅中文/字母/数字/空格/点/短横线；`lower(display_name)` 唯一索引，大小写不敏感。
登录后改名沿用相同规则，排除当前账号自身；保存时再次查重，并由数据库唯一索引防止并发重名。查重响应为 `Cache-Control: private, no-store`，页面不缓存结果，旧输入的异步响应不能覆盖当前提示。

### 绑定

| 方法 | 路径 | 认证 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/bind/status?code=` | 无 | `{status:"pending"}` 或 `{status:"confirmed", token, user_id, email, display_name}` |
| POST | `/api/bind/confirm` | Cookie | 浏览器已登录时确认绑定 `{code}` |
| GET | `/bind?code=` | 无 | HTML 页面，内含注册+验证码+绑定，成功后自动回写 |

绑定码 10 分钟有效、一次性；`/api/bind/status` 确认后会签发设备令牌并把码标记为已用。

### 卡片

| 方法 | 路径 | 认证 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/cards` | 设备令牌 | multipart：`package`（ZIP，必填）、`cover`（PNG/JPEG/WebP，可选）、`title`、`tags`（JSON 数组字符串） |
| GET | `/api/cards?scope=all\|official\|mine` | mine 需认证 | `{data:[{card_id,title,tags,summary,official,cover_url,downloads_72h,created_at}]}`，公开列表按 72h 下载去重数排序；mine 按上传时间倒序，响应 private/no-store |
| GET | `/api/cards/{id}/cover` | 无 | 图片字节 |
| GET | `/api/cards/{id}/package` | 设备令牌 | ZIP；成功即记一次下载（同账号同天只记一次） |
| DELETE | `/api/cards/{id}` | Cookie / 设备令牌 | 仅限本人；网页需同源；追加删除审计 |

上传限制：包 ≤128MB，单条目 ≤32MB，条目 ≤64，封面 ≤8MB。

去重键：`(owner_id, package_sha)`，其中 `package_sha` 优先取包内 `manifest.content_sha256`（忽略时间戳、按内容排序哈希），否则回退原始字节 SHA-256。重复上传返回 `409 DUPLICATE_CARD` 并带 `card_id`。

官方标记：`MARKET_OFFICIAL_EMAILS`（逗号分隔）里的账号上传的卡片自动 `official=true`，进入 `/featured`。

### 导入任务（网页 → 本机）

| 方法 | 路径 | 认证 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/imports` | Cookie | `{card_id}` → `202 {job_id, status:"pending"}`；本地设备 90 秒内无活动则 `409 LOCAL_OFFLINE` |
| GET | `/api/imports/{job_id}` | Cookie | `{job_id, status, message}`，status ∈ `pending/taken/done/failed` |
| GET | `/api/local/imports` | 接收或完整设备令牌 | 后台领取待办：`{items:[{job_id, card_id}]}`，原子租约，更新接收心跳 |
| POST | `/api/local/imports/{job_id}` | 领取凭据 | 回写 `{status:"done"|"failed", message}`，同凭据终态幂等 |
| PATCH | `/api/local/authorization` | 接收或完整设备令牌 | `{enabled}` 暂停/恢复当前接收凭据 |
| DELETE | `/api/local/authorization` | 接收或完整设备令牌 | 撤销当前凭据，释放其未完成任务 |

## 4. 卡片包格式

`studypal.card/1`，ZIP：

```
manifest.json   { format, exported_at, content_sha256, source:{card_id,revision}, profile, persona, assets:[...] }
assets/<asset_id>
```

- `profile` 含 `name / identity / background / personality / appearance / speaking_style / tags / media_ids / base_asset_id / pointing_rig`
- 导出时清空 `voice_id`、`voice_config_id`，`voice_selection` 置 `auto`（语音凭据不跨账号）
- 导入校验：路径穿越、加密条目、条目数、解压总量、单文件大小、`sha256` 逐条比对
- 导入生成**新的** `card_id`、`editable=true`、`published_revision=None`

## 5. 桌面端桥接 API（本地）

前缀 `/api/market`，认证沿用本地会话 Cookie。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/binding` | `{bound, market_user_id, base_url}` |
| POST | `/login` | `{email, password}` → 市集验证 + 绑定本机 + 建本地会话 |
| POST | `/bind/start` | **匿名**；默认登录；`?purpose=receive` 为独立账号接收，返回 `{code, url}` |
| GET | `/bind/status?code=` | **匿名**；默认建本地会话；`purpose=receive` 只授权接收，不修改 Cookie |
| POST | `/upload` | `{card_ids:[...]}` → 逐个导出并上传，返回 `{items:[{card_id,status,market_card_id,message}]}` |
| POST | `/import` | `{card_id}` → 下载包并导入本机 |
| POST | `/imports/drain` | 兼容的本人通知读取；实际导入由后端生命周期接收器执行 |
| GET/PATCH/DELETE | `/reception` | 需本机会话，仅本人；查询、暂停/恢复及撤销 |
| GET | `/cards` | 市集列表（带绝对封面地址） |

**匿名白名单**：`slice0_app.py` 的全局中间件只放行 `/api/auth/`、`/api/market/bind/*`、`/api/market/login`。新增匿名入口必须同时改这里和 `frontend/src/api.ts` 的 401 判断，否则首次注册会被拦成「登录状态已失效」。

## 6. 部署与运维

服务器：阿里云成都轻量应用服务器（Ubuntu 24.04），SSH 用户 `admin`（仅密钥，免密 sudo）。

| 项目 | 值 |
| --- | --- |
| systemd 单元 | `studypal-market`、`caddy`、`tailscaled` |
| 服务目录 | `/opt/studypal-market/service`（`app.py`、`web/`、`.venv`） |
| 数据目录 | `/opt/studypal-market/data`（`market.db`、`cards/`） |
| 环境变量 | `/etc/studypal-market.env`（600，含 `MARKET_BASE_URL`、SMTP、`MARKET_OFFICIAL_EMAILS`） |
| Caddy | `/etc/caddy/Caddyfile`：`:80 { bind 127.0.0.1; reverse_proxy 127.0.0.1:8081 }` |
| 公网入口 | Tailscale Funnel（`tailscale funnel --bg http://127.0.0.1:80`） |

更新代码：

```bash
scp server/market/app.py      admin@8.137.204.152:/tmp/market-app.py
scp server/market/web/*       admin@8.137.204.152:/tmp/
ssh admin@8.137.204.152 'sudo mv /tmp/market-app.py /opt/studypal-market/service/app.py \
  && sudo mv /tmp/index.html /tmp/help-import.html /tmp/logo.png /opt/studypal-market/service/web/ \
  && sudo chown -R admin:admin /opt/studypal-market/service \
  && sudo systemctl restart studypal-market'
```

本地改动注意：`server/backend/app/**` 必须同步到 `tests/support/backend/**`，否则 `tests/test_mirror_sync.py` 失败。

## 7. 已知坑

1. **Funnel 延迟**：从大陆访问约 2–5 秒，查重、列表等请求慢但正常。
2. **Cookie `secure`**：必须按本地请求的 scheme 判定，不能用市集 URL（否则 `http://127.0.0.1` 下浏览器丢弃 Cookie，表现为「一直要登录」）。
3. **`window.open`**：必须在点击同步调用；`await` 之后再开会被拦。当前实现先开 `about:blank` 再 `location.replace`。
4. **React StrictMode**：effect 清理会把 `alive` 标记置 false，重挂载时必须复位，否则异步结果被丢弃、界面卡在「正在打开」。
5. **上传去重**：一定用包内 `content_sha256`，不要用 ZIP 字节哈希（`exported_at` 每次都变）。
6. **注册**：必须带 `bind_code`；网页端没有独立注册入口。
7. **测试基线**：全量 `pytest tests` 为 2880 passed / 85 failed / 140 skipped，失败集中在工作区未完成的 `unit_scope`、`assessment_*` 等新模块，与市集无关。

## 8. 需要凭据时

- 市集账号（邮箱/密码）、SMTP 授权码、设备令牌都不在仓库里。
- 设备令牌可由账号所有者调 `POST /api/login` 再 `POST /api/devices` 现场签发；测试期间签发的令牌可在服务器 `market.db` 的 `devices` 表中撤销。
