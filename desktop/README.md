# 桌面安装与构建

## 安装

在 GitHub Releases 下载 `StudyCompanion-版本号-x64-Setup.exe`，选择安装目录后打开应用。首次启动会下载、校验并初始化独立的 Python、PostgreSQL、视频转写模型、个人记忆和课堂运行组件；中断后可以重试。无需另外安装 Conda、Python 或业务数据库，视频默认使用 CPU，不要求 CUDA。

首次组件下载约 1.9 GB，建议至少预留 8 GB 空间，课程与资料还会继续占用空间。模型服务地址、模型与 API Key 在应用设置填写；记忆还需兼容的 Embedding 与重排服务。外部服务费用由用户配置的供应商决定。

程序与个人数据分开存放。卸载默认保留 `userdata`，重新安装到原目录可以继续使用。安装包目前没有发布者代码签名，自动更新仅接收正式 Release。

新文档默认直接 OCR，识别和本地来源索引完成后即可使用，不自动调用内容模型抽题或追加阻塞发布的图表识别。PDF 在 50 页、45 MB 目标范围内整份提交。远程 OCR 的耗时受网络、页数和供应商负载影响，不保证 30 秒内完成。

## 开发

开发数据库与安装包的受管数据库分开。先准备 PostgreSQL、pgvector 和受忽略的 `server/.env`，启动时不得隐式迁移业务数据库。

```powershell
cd frontend
npm ci
npm run dev

# 另一个终端
cd server/backend
uv sync --frozen
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

前端默认端口 5173，后端健康检查为 `/healthz`。

## 构建安装包

构建环境需要 Node.js 24、uv、.NET 10 SDK 和联网环境。

```powershell
cd frontend
npm ci
npm run build
cd ../desktop
npm ci
npm run components
npm run dist
```

如果只改应用代码、未改运行依赖，可以把 `npm run components` 换成
`node scripts/fetch-components.mjs`，复用清单里已发布且通过 SHA-256 校验的运行组件。
应用版本与运行组件版本独立，不重复上传同一套运行组件。CI 默认走此路径；
手动构建可勾选 `rebuild_components` 重建运行组件。更改依赖或沙箱时必须重新构建并更新清单。

运行组件和安装包输出到受忽略的 `.backup/`。组件版本、大小、SHA-256 和下载地址位于 `desktop/manifests/runtime.json`；不得发布与清单不符的组件。

## 验证与反馈

测试统一放在 `tests/`，运行方法见 [测试入口](../tests/README.md)。例如：

```powershell
node --test tests/desktop_install_unit.mjs
```

数据库专项使用 `tests/run_native_regression.py` 创建并清理独立 schema，必须显式指定范围。开发机隔离目录模拟不能替代独立 Windows 虚拟机、不同显卡和真实供应商验证。

反馈请提供应用版本、Windows 版本、失败阶段及复现步骤；不要上传凭据、数据库或个人资料。`文档/`、教材、日志、模型与私人实验材料不进入源码发布，大型运行组件通过 Releases 分发。第三方组件保留各自许可证。
