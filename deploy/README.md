# 部署配置

## 课堂 Java / C / C++ / PyTorch

从仓库根构建隔离运行镜像（CPU 版，无第三方代码执行服务）：

```console
docker build -t learning-classroom:1 -f server/classroom-runner/Dockerfile server/classroom-runner
```

后端需能访问本机 Linux Docker，默认调用 `docker`；Windows 没有 `docker.exe` 时
使用默认 WSL 分发的 `wsl.exe -- docker`。可用进程环境
`CLASSROOM_DOCKER_COMMAND_JSON` 配置命令参数数组，用 `CLASSROOM_RUNNER_IMAGE`
配置本地镜像名；用户请求不接受这些参数。运行时禁止自动拉取镜像。
后端单 API worker 最多并发 3 个、每用户 1 个；多 worker 或多副本需统一入口限制。
构建后的执行本身无需代理/外网。宿主编译器和 Python 不用于执行用户代码。

现有 Compose backend 镜像没有 Docker CLI/socket，不能仅增加上述环境变量便声称
已部署；需由运维提供专用执行宿主及可信 Docker 控制通道，并按上述单进程边界部署。
浏览器端的 Python/JavaScript 在容器运行环境不可用时仍可使用。
CPU PyTorch、Java 17、C17、C++17 的限制见[课堂契约](../文档/开发/课堂代码与原文精读.md)。

| 文件 | 用途 |
| --- | --- |
| compose.yml | 后端与数据库的基础配置 |
| compose.override.yml | 本地端口、热更新与 Mailpit |
| compose.deploy.yml | HTTPS 部署覆盖配置，仍需独立验收 |
| compose.sources.yml | 私有视频适配 worker、模型与资料卷；见[部署步骤](../文档/部署/多来源资料.md) |
| enable-pgvector.ps1 | 本机管理员交互启用 vector；仅请求密码，不保存管理员凭据 |
| langfuse-compose.yml | 独立观测栈 |

从仓库根显式指定环境文件与配置；不要依赖当前目录自动加载：

```console
docker compose --env-file server/.env -f deploy/compose.yml -f deploy/compose.override.yml config --quiet
```

仅在需要启动本地容器时，将上述命令末尾改为 `up -d`。部署覆盖用 compose.deploy.yml 替代本地 override，不同时加载两者。实际生产运行、数据库迁移和镜像构建需单独验证。

基础配置固定项目名 server，保持原 server 目录的默认容器/卷命名，避免目录迁移造成新空数据库卷；已有环境显式使用其他 project name 时继续传入原来的 `-p`，不能静默改名。目录整理不删除或迁移数据库卷。

后端构建上下文是 ../server，Dockerfile 为 backend/Dockerfile；产品前端独立构建、部署。旧模板 Playwright 服务引用不存在的 Dockerfile，已从本地覆盖中移除；产品浏览器专项入口在根 tests/。

观测栈继续使用其独立 project name；实际凭据只放受忽略的本地环境文件。不要输出展开后含秘密的 Compose 配置，检查使用 config --quiet。

## 独立记忆服务

本地 `--start` 的标准输入和启动输出保存在根目录
`.runtime/product-memory/`，不放在开发备份目录；该目录受 Git 忽略。
它不包含 Hindsight 数据库，数据库仍按独立 pg0 实例配置存放。
其他本地产物位置见[开发目录约定](../文档/开发/README.md#本地产物目录)。

### 2026-10-20 个人模型

普通成员现在在「设置 → 记忆模型」保存个人内容、Embedding 和普通重排/Jev 配置。凭据经应用主密钥加密，资料检索直接使用本人版本；历史管理员引擎接口仅维护旧部署，不能作为个人配置回退。

先执行应用 Alembic 升级。使用已安装 `hindsight-api-slim==1.0.0` 的独立 Python 环境生成另一份受忽略配置并启动：

```console
python deploy/memory_service.py --configure-personal --config server/.env.personal-memory --application-url http://127.0.0.1:8000
python deploy/memory_service.py --start --config server/.env.personal-memory
```

生成器拒绝覆盖已有文件，不打印密钥。新服务固定独立 `learning-assistant-personal-memory` pg0 数据库和 9079 端口，原部署及 bank 不变。应用从该文件读取内部服务地址、服务凭据和代理认证，不向网页返回。服务器若使用容器，需将内部地址、独立 PostgreSQL 数据库及相应凭据改为部署环境；API 和代理均只允许受控内网访问。不能复用旧数据库后直接把向量维度改成 2000。

内部代理使用官方银行级 `user`/重排头进行路由，外发前删除内部身份；不接受仅靠前端提供的属主。本人停用、资料或记忆删除会阻止调用。官方 Embedding/重排适配器的并发协议测试使用合成供应商，不等于完整引擎或真实模型质量验收。

切换 Embedding 后资料建立独立索引。已有记忆继续原 bank；用户点击「重建记忆索引」将已完成、仍有效的私有来源重放到影子 bank，再刷新画像、校验来源版本并切换；旧 bank 清理独立进行，失败保留旧 bank。该实现重新提炼来源，不能承诺逐条复现旧模型的提炼文字。旧记录无可重放来源时需专门导入，不自动删除或伪造迁移完成。

资料向量写入私有业务表 `corpus_vectors`，主库需安装 `vector` 扩展并以精确余弦 `<=>` 检索；扩展或表不可用时回退旧 JSON 路径。用户不再手工配置维度：系统默认目标 1024 维，模型支持缩短时通过 `dimensions=1024` 请求，不支持或忽略参数时回退原生维度。pgvector 的 HNSW/IVFFlat 索引只支持 `vector` ≤2000 维、`halfvec` ≤4000 维，更高维度只能精确扫描；大规模部署仍需对真实资料容量、内存与延迟单独压测并安装 ANN 索引方案，不把功能专项当作容量验收。

2026-10-20 本机业务库已启用 pgvector 0.8.7 并完成迁移，后端已重新加载，普通应用账号专项通过。新服务器仍需先安装扩展文件，再由管理员启用业务库扩展；本机可运行 `deploy/enable-pgvector.ps1` 输入管理员密码，随后以应用账号执行迁移。按资料完整索引选择 SQL，并可通过 `CORPUS_VECTOR_BACKEND=json` 回退读取，保留 JSON 检查点；详见[切换记录](../文档/测试归档/pgvector-switch-20261020.md)。不要为安装扩展提升应用账号。用户资料需配置 Embedding 并完成索引后才能使用语义检索；本机通过不代表远程服务器已部署。

以下为旧部署说明，供历史 bank 维护：

产品记忆不使用开发助手的 bank 或数据库。本次本机采用已有 Hindsight API 1.0.0 独立运行时，应用仅安装锁定的客户端 1.0.0 和官方 PydanticAI 集成 0.4.20。应用迁移 head 为 `x8c9d0e1f2a3`。

配置模板为 [memory.env.example](memory.env.example)，实际配置放受忽略的 `server/.env.memory`。使用装有 `hindsight-api-slim[embedded-db]==1.0.0` 与 `python-dotenv` 的独立 Python 环境，从仓库根执行：

```console
python deploy/memory_service.py --secure
python deploy/memory_service.py --start
```

第一条生成服务认证凭据并配置原生 ApiKeyTenantExtension，不打印密钥；第二条仅启动独立产品实例，后台隐藏运行。仅绑定本机 9078，数据库为独立 pg0 实例；应用仍使用原 PostgreSQL。API 调试、访问日志和 MCP 默认关闭，第三方内容日志禁止输出。操作使用的 Python 必须来自上述独立环境，不是只安装客户端的应用环境。

本机配置使用指定的 gpt-6-luna、Qwen3-Embedding-8B（目标 1024 维）与 TypeSafe Jev。不要把 Jev 配成 Cohere `/rerank`，实际协议为 `/v1/systemone`。记忆默认由用户在个人记忆页或设置的记忆分页启用。

SiliconFlow 的 Qwen3-Embedding-8B 实测接受 `dimensions` 256/512/768/1024/1536/2048/4096，拒绝 2000 和 3072（HTTP 400，code 20015）。系统目标是 1024，记忆库固定 2000 宽并由代理补零，因此不要向该供应商请求 2000。1024 输出是 4096 输出的 MRL 前缀，检索排序在合成中文样本上与更高维度一致。

管理员可在「设置 → 记忆」编辑部署的内容模型、OpenAI 兼容 Embedding，以及 Jev 或标准 rerank。URL 填服务基础地址或 `/v1` 即可，程序拼接操作路径；标准 rerank 复用 Hindsight 原生 SiliconFlow/Cohere 兼容实现。密钥留空保留，换服务地址时必须重填。普通用户只能管理本人启停与记忆，不能读取引擎模型配置。

`--start` 现在启动配置监听 supervisor：只重启其拥有的 Hindsight 子进程；配置文件修订变化后重启，健康成功后写入受忽略的 `.env.memory.applied`。设置区分「已保存/等待加载」和「已生效」。配置 supervisor 不是独立系统服务管理器；生产应由进程管理器托管，禁止重复启动多个 supervisor。Hindsight 地址或服务认证凭据仍由部署管理，变更这两项时需要重启主应用。

Embedding 模型、基础地址或自动探测到的维度变化时，已有应用 bank 会阻止保存，须先进行独立向量迁移，不能混合旧向量。正式部署应使用私网、受保护的配置存储，并单独验收独立记忆数据库备份和恢复后遗忘核销；本次本机验证不等于该部署验收。
