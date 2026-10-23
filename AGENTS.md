# Repository Guidelines

本文件是本仓库的中文版贡献与协作指南。开始工作前阅读 [README](README.md)、[文档导航](文档/README.md)、[测试规范](文档/规范/testing-spec.md)和[日志与审计规范](文档/规范/logging-and-audit-spec.md)。

## 项目结构

- `frontend/src/`：React、TypeScript 产品界面；静态资源位于 `frontend/public/`。
- `server/backend/app/`：FastAPI 生产代码；装配入口为 `assembler.py`，启动目标为 `app.main:app`。
- `tests/`：唯一可执行测试真源；`support/backend/` 是受哈希校验守护的生产镜像。`server/backend/tests/` 仅作模板参考。
- `deploy/`：部署配置；`文档/ui/`：设计原型；`文档/测试归档/`：执行结果；`实验/`：受 Git 忽略的私人实验材料。
- `文档/历史/` 和实验源码快照不作为当前启动、规范或测试入口。

## 开发与检查命令

| 工作目录 | 命令 | 用途 |
| --- | --- | --- |
| `frontend/` | `npm ci`、`npm run dev` | 安装锁定依赖，启动 Vite |
| `frontend/` | `npm run build`、`npm run lint` | TypeScript/生产构建，Oxlint 检查 |
| `server/backend/` | `uv sync --frozen` | 安装 Python 工作区依赖 |
| `server/backend/` | `uv run uvicorn app.main:app --host 127.0.0.1 --port 8000` | 启动完整后端 |
| `server/backend/` | `uv run ruff check app/main.py` | 示例：检查指定 Python 文件 |

前端默认端口为 5173。先准备本地 PostgreSQL 和受忽略的 `server/.env`；启动或测试不得隐式迁移业务数据库。

## 编码与命名

Python 使用四空格缩进、类型注解和 `snake_case`；React/TypeScript 通常使用两空格，组件采用 `PascalCase`，Hooks 采用 `useXxx`。遵循相邻代码格式与现有抽象；Python 检查遵循 `server/backend/pyproject.toml` 的 Ruff 配置。避免无关重构、整库格式化及依赖锁文件无故变动。

## 测试要求

后端使用 pytest，前端使用 Node 单元脚本和 Playwright 浏览器专项。命名采用 `test_*.py`、`*_unit.mjs`、`*_browser.mjs`。从根目录运行受影响专项，例如：

```powershell
./server/.venv/Scripts/python.exe tests/run_native_regression.py tests/test_slice2_contracts.py tests/test_mirror_sync.py -q --tb=short
```

该入口创建并清理独立 PostgreSQL schema；必须显式传入范围。**只有用户明确要求时才执行全量，不得分批变相全量。** 覆盖受影响调用方、下游和权限边界；生产镜像修改须同步并校验。UI 改动检查桌面与手机交互；纯文档只做静态检查。

运行方法维护于 [tests/README.md](tests/README.md)，结果集中到测试归档。分别记录通过、失败、跳过、警告和未验证项；合成测试不替代真实供应商或浏览器验收。

## 提交与 Pull Request

沿用 `feat(planning): ...`、`fix(ui): ...`、`chore: ...` 等类型和范围式提交信息。提交前检查 `git diff`、`git status --short`、`git diff --cached --check`，移除临时文件并扫描敏感信息；保留他人未提交改动。

PR 说明问题、改动、验证范围和限制，关联相关 Issue，UI 变更附截图。仅在用户明确要求上传 GitHub 后确认远程和分支并推送，禁止强制推送。

## 安全与变更边界

凭据只放受忽略配置，不提交密钥、教材正文、完整供应商响应或个人路径。日志使用受控 JSON；提示词、模型回答和用户问题仅按 ADR-022 进入独立内容通道，审计保持追加式写入。

保留属主、版本、删除墓碑和任务租约围栏；修改接口、状态码、审计或敏感数据边界时，先更新相关规范并记录差异。计划能力和未执行检查不得描述为已实现或已通过。
