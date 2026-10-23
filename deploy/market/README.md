# 市集站部署（DuckDNS + Caddy）

这是市集站的**部署骨架**，当前只负责静态前端和 HTTPS。市集 API（账号、卡片、下载记录）尚未在这个目录内实现；等 API 落地后，把 Caddy 的 `/api/*` 反代补上即可。

适用场景：个人使用 + 少数同学，非盈利；不购买域名，用 DuckDNS 免费子域名 + Let's Encrypt。

## 测试基线（2026-10-21）

全量 `pytest tests`：2835 passed / 95 failed / 37 errors / 138 skipped。
本次修复的失败根因（都在工作区既有未提交改动里，非市集功能本身）：

- `create_all()` 未注册 `teaching.unit_scope.UnitScopeRow` → 测试库没有 `unit_scopes` 表（约 19 个 error）。
- `teaching/service.py` 缺 `CourseUnitRow`、`LearningTaskRow` 导入；`teaching/background.py` 缺 `LessonBlueprintRow` 导入 → `NameError` 500。
- `planning/outputs.py` 新增必填 `scope_contract` / `coverage_areas` 后，`tests/common_fixtures.py` 的合成适配器仍返回线路由结构 → 覆盖规划校验失败、路线草稿无法确认。
- 蓝图在等待考纲时会写入 `plan_json="{}"`，`teaching/feynman.py` 的 `checkpoints()` / `lesson_checkpoint()` 无条件解析 → 检查点接口 500。已改为可容忍空蓝图。

仍未通过且与市集无关的既有失败：`test_teaching_recovery.py` 5 个（恢复代次断言）、其余约 90 个分散用例。

## 市集 API（已部署）

- 代码：`server/market/app.py`（独立 FastAPI，SQLite 存储，不依赖主库）。
- 服务器：`/opt/studypal-market/service`，独立 venv，systemd 服务 `studypal-market`，监听 `127.0.0.1:8081`。
- 配置：`/etc/studypal-market.env`（600），含 `MARKET_BASE_URL`、QQ SMTP。
- 数据：`/opt/studypal-market/data`（`market.db` + `cards/` 卡片包与封面）。
- Caddy 已把全部流量反代到 8081；前端页面由市集服务托管（`/opt/studypal-market/service/web`）。
- 视觉：按钮统一胶囊圆角（999px）、卡片/弹层 18–24px 圆角、输入框 12px、标签 chip 胶囊形；不再使用直角方框。
- 页面：`/` 全部卡片（含大标题区）、`/featured` 官方精选、`/categories` 分类，三个独立 URL，前端 `pushState` 切换、可直接访问刷新；非首页隐藏顶部大标题区。`/help/import` 是「如何导入」独立说明页。
- 导入为先：卡片按钮与详情主按钮都是「导入」，弹窗可选「直接导入到本机」（走 import job）或右下角小字「下载 ZIP」；网页端不再提供注册，注册入口只在 `/bind`（从桌面端发起，必须带 bind_code）。
- 桌面端登录：`POST /api/market/login`（本地）→ 市集验证邮箱密码 → `POST /api/devices` 换设备令牌 → 本地 `issue_market_session` 建立会话（同邮箱的旧本地账号会被复用，并清空本地密码哈希）。`/api/bind/status` 确认后也会建立本地会话。
- 注册入口只在本地：桌面端 `#/register` 调 `marketBindStart` 打开 `/bind?code=...`，浏览器完成验证码注册后自动绑定并登录；本地登录页不再接受本地密码。
- 导入 job：`POST /api/imports`（要求本地设备最近 90 秒活跃，否则 409 `LOCAL_OFFLINE`）、`GET /api/imports/{id}` 轮询状态、`GET /api/local/imports` 由桌面端领取、`POST /api/local/imports/{id}` 回写结果。卡片列表读 `/api/cards?scope=`；分类页把真实标签成排铺开（类似参考图的分类网格），支持多选（AND 关系）和「全部」重置；详情弹层登录后可下载；`/bind?code=...` 绑定页自带注册/登录，同为暗色画廊样式。
- 官方标记：`MARKET_OFFICIAL_EMAILS`（逗号分隔）里的账号上传的卡片会标为官方并进入 `/featured`；在自己的部署环境中填写运营账号邮箱。
- 接口：注册（6 位邮箱验证码，15 分钟有效、最多尝试 6 次、可重发）、`/api/verify-code`、`/api/verify-code/resend`、登录/登出、`/api/me`、`/api/username`（用户名设置）、绑定码确认与轮询、卡片上传（含去重）、列表（近 72 小时按账号去重排序）、封面、卡片包下载（记录下载）、删除自己的卡片。
- 验证码模式（常用方案）：注册表单里邮箱下方就是「验证码输入框 + 发送验证码按钮」并排；点发送才发邮件，60 秒倒计时；填写 6 位码后点注册即完成注册并自动登录，不再有单独验证步骤。`/api/send-code` 支持注册前发码，`/api/register` 必须携带 `code`；仍保留 `/verify?token=` 兼容旧链接。
- 绑定页 `/bind` 使用同样的「输入框 + 发送验证码」布局，并含用户名与密码字段。
- 用户名唯一：`lower(display_name)` 唯一索引（空值除外）；注册与改名校验大小写不敏感重名，返回 `409 USERNAME_TAKEN`；`GET /api/username/available?name=` 供页面实时查重（注册页与账号菜单都会提示「可以使用 / 已被使用」）。用户名只允许中文、字母、数字、空格、点和短横线，最多 24 字。
- 本地桥接：`/api/market/*`（绑定状态、生成绑定链接、轮询、批量上传、导入到本机、市集列表）。
- 上传去重键：`(owner_id, package_sha)`（优先用包内 `content_sha256`）；下载计数键：`(user_id, card_id, day)`，同一账号同一天只计一次。
- 卡片介绍：上传时从包内 `profile.identity` + `personality` 生成 `summary`（≤400 字），详情弹层直接显示；缺失时回退到一段说明而不是通用文案。已有卡片可用同样逻辑回填。

## Tailscale Funnel（当前使用的公网入口）

- 服务器已安装 Tailscale 1.104.2，节点名 `studypal`，systemd 托管。
- Caddy 只监听 `127.0.0.1:80`（`bind 127.0.0.1`），公网 IP 的 80 已关闭；Funnel 是唯一公网入口。
- 公网地址：`https://studypal.tailf6f37a.ts.net/`（固定，只要 tailnet 不改名）。
- 启用步骤：Tailscale 管理台 DNS → Enable HTTPS；Access controls → JSON editor 加 `nodeAttrs` 的 `funnel` 属性；服务器执行
  `sudo tailscale funnel --bg http://127.0.0.1:80`。
- Funnel 配置在 tailscaled 重启后仍保留；已实测重启后 `tailscale funnel status` 仍为 on。
- 服务器 VPC DNS 无法解析 ACME 域名，已通过 `/etc/netplan/99-dns.yaml` 固定为 `223.5.5.5 / 119.29.29.29`，保证证书续期。
- 不需要域名、ICP 备案或开放公网 80/443；公网流量经 Tailscale 入口。
- 当前是公开地址，任何人拿到链接都能访问；市集 API 接入后必须加登录/邀请码。
- 1.7MB 原型通过 Funnel 加载偏慢，生产前端应拆分图片、按需加载。

## Cloudflare Tunnel（已验证可用）

- 服务器已安装 `cloudflared 2026.10.2`（`/usr/local/bin/cloudflared`）。
- 临时隧道命令：`cloudflared tunnel --url http://127.0.0.1:80 --no-autoupdate`，会生成 `https://<random>.trycloudflare.com`。
- 实测公网 HTTPS 返回 200，页面正常渲染；从本机访问 1.7MB 原型约 3–5 秒，生产前端应改为按需加载图片。
- **临时 URL 在进程重启后会变化**，且当前进程不是 systemd 服务；要稳定地址需要买域名并创建 named tunnel。
- named tunnel 需要把域名托管到 Cloudflare，然后把域名路由到本机 `http://127.0.0.1:80`；不需要开放公网 80/443。
- 大陆服务器通过 Cloudflare Tunnel 暴露公网，技术上绕过了接入商对未备案域名的拦截；但大陆网站的 ICP 合规义务仍然存在，公开运营不建议以此规避备案。

## 当前实际部署（2026-10-21）

- 服务器：Ubuntu 24.04，`admin` 用户可免密 sudo；已加 2G swap。
- Caddy 使用 Ubuntu 阿里云镜像的 `caddy 2.6.2`（apt 安装，systemd 托管），不是 Docker compose。
- 静态站点目录：`/var/www/studypal-market`，当前为市集原型（演示数据）。
- DuckDNS 子域名证书曾成功签发，但阿里云对未备案域名的公网访问返回 `403 Forbidden`（`Server: Beaver`）或重置 443；因此当前改用**公网 IP + HTTP** 预览，仅用于看 UI。
- **HTTP 不能用于正式账号登录/密码**；市集 API 接入前必须决定 HTTPS 方案（备案域名、境外服务器、Tailscale/Cloudflare，或自签名证书并分发 CA）。
- 市集 API、账号、下载记录尚未实现。



## 前置条件

- 一台 Ubuntu 24.04 服务器，已安装 Docker 和 Docker Compose 插件。
- 一个 DuckDNS 子域名（例如 `studypal.duckdns.org`）和对应的 token。
- 腾讯云安全组放行 80/443；SSH 22 只对你自己的 IP 开放。
- 大陆服务器公开域名通常需要 ICP 备案；DuckDNS 子域名无法备案，可能被拦截。先测试；失败就改用 Tailscale Serve（见文末）。

## 目录

```
deploy/market/
├─ compose.yml          # Caddy + 静态站点
├─ Caddyfile            # DuckDNS 域名 + 自动 HTTPS
├─ .env.example         # 复制为 .env，填入 DuckDNS / 邮箱配置
├─ duckdns.sh           # DuckDNS IP 更新脚本
├─ systemd/             # DuckDNS 定时器
├─ backup.sh            # 站点与（未来）数据库备份
└─ site/index.html      # 占位页；用前端构建产物替换
```

## 1. DuckDNS

1. 打开 DuckDNS，创建一个子域名，例如 `studypal`。
2. 把它的当前 IP 填成服务器公网 IP，或先留空，稍后用 token 自动更新。
3. 记下子域名和 token；token 只写服务器 `.env`，不要提交。

## 2. 上传部署目录

```bash
# 在服务器上
sudo mkdir -p /opt/studypal-market
sudo chown "$USER:$USER" /opt/studypal-market

# 从本机仓库根目录（注意结尾的 /.，会连 .env.example 一起复制）
scp -r deploy/market/. <user>@<SERVER_IP>:/opt/studypal-market/
```

或者直接在服务器上 `git clone` 仓库后把 `deploy/market` 复制过去。

```bash
cd /opt/studypal-market
chmod +x duckdns.sh backup.sh
```

## 3. 配置

```bash
cd /opt/studypal-market
cp .env.example .env
# 编辑 .env：MARKET_DOMAIN、DUCKDNS_DOMAIN、DUCKDNS_TOKEN
# QQ/Gmail 授权码等先留空，等市集 API 接入后再填
chmod 600 .env
```

`MARKET_DOMAIN` 必须是 DuckDNS 的完整域名，Caddy 用它申请证书。

## 4. 放前端

把市集前端构建产物放到 `site/`，入口必须是 `site/index.html`。现在可以先用原型页预览：

```bash
scp <本机原型.html> <user>@<SERVER_IP>:/opt/studypal-market/site/index.html
```

注意：原型里的卡片和下载是演示数据，不是真实市集。

## 5. 启动

```bash
cd /opt/studypal-market
docker compose up -d
docker compose logs -f caddy
```

安全组放行 80/443 后，打开 `https://<MARKET_DOMAIN>`。Caddy 会自动申请并续期证书。

如果证书申请失败：

- 检查 DuckDNS 是否已指向服务器 IP：`dig +short <MARKET_DOMAIN>`；
- 检查 80/443 是否从公网可达；
- 大陆服务器可能因未备案拦截 80/443，改用 Tailscale（文末）。

## 6. DuckDNS 自动更新

服务器是固定公网 IP 时，DuckDNS 记录填一次即可；如果 IP 会变，安装定时器：

```bash
sudo cp systemd/duckdns-update.service /etc/systemd/system/
sudo cp systemd/duckdns-update.timer /etc/systemd/system/
sudo cp .env /etc/studypal-market.env
sudo chmod 600 /etc/studypal-market.env
sudo systemctl daemon-reload
sudo systemctl enable --now duckdns-update.timer
```

脚本每 5 分钟更新一次；固定 IP 下它只是幂等刷新。

## 7. 邮箱配置（等市集 API 接入后使用）

代码已支持 QQ + Gmail 双通道，按收件域路由并互为回退：

- `@qq.com / @163.com / @126.com / @foxmail.com / @yeah.net` → QQ；
- `@gmail.com / @googlemail.com` → Gmail；
- 其他 → 优先 QQ，失败回退 Gmail。

在 `.env` 填：

```dotenv
QQ_SMTP_HOST=smtp.qq.com
QQ_SMTP_PORT=465
QQ_SMTP_SSL=true
QQ_SMTP_TLS=false
QQ_SMTP_USER=<你的QQ邮箱>
QQ_SMTP_PASSWORD=<QQ邮箱授权码，不是QQ密码>
QQ_FROM_EMAIL=<你的QQ邮箱>

GMAIL_SMTP_HOST=smtp.gmail.com
GMAIL_SMTP_PORT=465
GMAIL_SMTP_SSL=true
GMAIL_SMTP_TLS=false
GMAIL_SMTP_USER=<你的Gmail>
GMAIL_SMTP_PASSWORD=<Gmail App Password>
GMAIL_FROM_EMAIL=<你的Gmail>
```

- QQ：邮箱设置 → 账户 → POP3/IMAP/SMTP → 生成授权码。
- Gmail：开启两步验证 → 应用专用密码。
- Gmail 从大陆服务器到 Google 可能不通；先测，不通就只启用 QQ。
- 不要把授权码/App Password 提交到仓库或发在聊天里。

## 8. 备份

```bash
./backup.sh
```

当前只备份 `site/`；市集 API 与 Postgres 接入后，脚本会自动追加 `pg_dump`。建议把 `backups/` 同步到 COS 或另一台机器。

## 9. 安全

- SSH 已改为仅密钥登录、禁止 root 直登、MaxAuthTries=3；fail2ban sshd 已启用（10 分钟内 5 次失败封 1 小时）。
- apt 源已从内网 `mirrors.cloud.aliyuncs.com` 切到公网 `mirrors.aliyun.com`（改成公共 DNS 后内网镜像不可达）。
- 22 端口目前仍对公网开放，但只能密钥登录；可再在安全组限制来源 IP。
- 安全组只放 80/443；Postgres 和 API 端口不要暴露公网。
- 邮箱验证 + 登录/发信限流；有公开 URL 时尤其重要。
- 个人非盈利使用也不要公开传播含第三方人物形象的卡片。

## 备用方案：Tailscale Serve

如果 DuckDNS 被拦截：

1. 服务器和每个同学设备安装 Tailscale，加入同一个 tailnet；
2. 把 Caddy 替换为 Tailscale Serve，反代到未来 API 或静态目录；
3. 访问 `https://<machine>.<tailnet>.ts.net`，不暴露公网、不需要备案。

代价是每个同学要装 Tailscale，并确认免费额度够用。

## 还未包含

- 市集 API：注册/登录、邮箱验证、OAuth、卡片列表、下载记录、Top 5。
- 本地应用首次启动、SSO、`studypal://` 深链导入。
- 卡片包存储与审核。

这些完成后再把 `compose.yml` 增加 `market`/`postgres` 服务，并在 `Caddyfile` 打开 `/api/*` 反代。
