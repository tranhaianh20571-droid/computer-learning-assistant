# 切片 2 执行记录与验收证据

> 状态：**部分完成（T02 完成；T01 外部阻断；T03～T08 未开始）**
> 交接基线：[slice-2-materials-parse-handoff.md](../../架构/slice-2-materials-parse-handoff.md)
> OCR 契约核对：[slice2-ocr-external-contract.md](slice2-ocr-external-contract.md)（T01 未通过）
> 环境：Windows 11 + Python（`uv run --project server/backend`）+ PostgreSQL 17（本机 5432）

## 0. 结论摘要

| 任务 | 状态 | 说明 |
| --- | --- | --- |
| T01 OCR 外部契约 spike | **阻断** | 环境中不存在 AI Studio/Baidu OCR 凭据；检查器已就绪，退出码 2 |
| T02 契约冻结与 PDF 渲染底层 | **完成** | 数据模型、迁移、上传校验、配额账本、渲染、文本可信判定、jieba 索引 |
| T03 逐页解析与 OCR worker | 未开始 | 受 T01 门禁阻断 |
| T04 原图裁切 | 未开始 | 受 T01 门禁阻断 |
| T05 检索索引（OCR 关联部分） | 部分 | 分词/tsvector/查询/删除账本已完成；OCR 区域来源受 T01 阻断 |
| T06 调度四类与并发隔离 | 未开始 | 交接文档要求 T03 运行中已有 OCR 任务 |
| T07 配额/删除/前端资料页 | 部分 | 配额账本与删除边界后端已完成；前端资料页未开始 |
| T08 故障注入与总验收 | 未开始 | 依赖 T03～T07 |

**基线不回归**：切片 0/1 的 234 项测试全部通过；新增 43 项；合计 **277 passed**。

## 1. 本次实际改动文件

**生产代码（server/backend/app）**

| 文件 | 用途 |
| --- | --- |
| `materials/__init__.py` | 包入口，注册 SQLAlchemy 元数据 |
| `materials/errors.py` | 切片 2 受控错误码（10 个冻结码 + 扩展码）与异常 |
| `materials/models.py` | `materials`/`material_pages`/`material_regions`/`figure_assets`/`text_chunks`/`material_upload_quotas`/`deletion_tombstones`/`asset_usage_ledger` |
| `materials/upload.py` | 扩展名+MIME+签名三重校验、UTF-8 校验、页数检查、配额账本、删除边界 |
| `materials/parse.py` | pypdfium2 200 DPI 渲染、像素/边长预算降级、文本层可信判定 |
| `materials/chunk_index.py` | jieba 锁定分词、tsvector 写入、按 owner/材料/区域检索、删除账本 |
| `materials/router.py` | `/api/materials` 上传/列表/状态/重试/删除/检索路由 |
| `slice2_app.py` | 切片 2 应用装配 |
| `alembic/versions/c2d3e4f5a6b7_slice2_materials_parse_index.py` | 切片 2 数据模型 + `learning_tasks` 字段迁移（可回退） |
| `alembic/env.py` | 注册切片 2 元数据 |
| `tasks/service.py` | `learning_tasks` 新增 `lane`/`material_id`/`page_no` 与 `(status, lane)` 索引 |
| `pyproject.toml` / `server/uv.lock` | 新增 `pypdfium2==5.13.0`、`jieba==0.42.1`、`pillow==12.3.0`；dev 组 `reportlab==5.0.1` |

**脚本与测试**

| 文件 | 用途 |
| --- | --- |
| `scripts/slice2_sample_generate.py` | 生成 11 个合成 PDF 样本矩阵（T01 与回归共用） |
| `scripts/slice2_ocr_spike.py` | T01 可复跑检查器（受控退出码，凭据仅从环境变量读取） |
| `tests/test_slice2_contracts.py` | 31 项：错误码、上传校验、配额、渲染、可信判定、分词 |
| `tests/test_slice2_api.py` | 12 项：上传 API、ownership、检索、删除即时脱离、墓碑/账本 |
| `tests/support/backend/materials/**`、`slice2_app.py` | 与生产代码逐字节同构的测试支持镜像 |

## 2. 实际运行的命令与结果

| 命令 | 目录 | 结果 |
| --- | --- | --- |
| `uv run --project server/backend pytest tests -q` | 仓库根 | **277 passed, 1 warning in 15.96s**（234 基线不回归 + 43 新增） |
| `uv run --project server/backend pytest tests/test_slice2_api.py tests/test_slice2_contracts.py -q` | 仓库根 | **43 passed** |
| `uv run --project server/backend python scripts/slice2_sample_generate.py --out .slice2-samples` | 仓库根 | 11 个 PDF 生成成功（1532～5991 bytes/个） |
| `uv run --project server/backend python scripts/slice2_ocr_spike.py --provider aistudio ...` | 仓库根 | **exit 2**：缺 `PADDLEOCR_ENDPOINT`/`PADDLEOCR_API_KEY`，T01 未执行 |
| `uv run alembic upgrade head` / `downgrade -1` / `upgrade head` | `server/backend` | `b1c2d3e4f5a6 → c2d3e4f5a6b7`，回退 `c2d3e4f5a6b7 → b1c2d3e4f5a6`，再升回 head；往返可复现 |

依赖版本：pypdfium2 5.13.0、jieba 0.42.1、pillow 12.3.0、reportlab 5.0.1、SQLAlchemy 2.0.51、FastAPI 0.141.1。

## 3. AC 汇总（仅覆盖本次实际完成部分）

| 验收编号 | 结论 | 证据 |
| --- | --- | --- |
| AC-01 OCR spike 真实核验 | **未通过（外部阻断）** | `slice2-ocr-external-contract.md`；检查器 exit 2 |
| AC-02 数据模型/上传契约冻结 | **部分通过** | 迁移往返、20 MiB/10 文件/5 GiB/1000 页/签名/编码边界契约测试（`test_slice2_contracts.py`）；加密 PDF 拒绝 |
| AC-03 逐页解析 worker | 未开始 | 受 T01 阻断 |
| AC-04 OCR 外发权限与围栏 | 未开始 | 受 T01 阻断（切片 1 disclosure 核验已就绪） |
| AC-05 原图裁切与可用状态 | 未开始 | 受 T01 阻断 |
| AC-06 内置检索 | **部分通过** | 中文/英文标识符/版本号命中、图块 ID 字段、删除后立即不再命中、跨账户返回空（`test_slice2_api.py`） |
| AC-07 调度与并发隔离 | 未开始 | 字段已落；`claim()` lane 筛选未实现 |
| AC-08 前端资料管理页 | 未开始 | — |
| AC-09 故障注入 | 未开始 | — |

## 4. 关键技术发现（供后续任务与规划者参考）

1. **数据库双实例（已在 2026-09-28 修复）**：原 alembic CLI 由 `server/.env` 的
   `DATABASE_URL` 驱动，指向 `learning_assistant_tpl`；而测试文件各自硬编码
   `LEARNING_DATABASE_URL` 指向 `learning_assistant`。两个库需分别迁移，会出现
   “迁移成功但测试仍报缺列”的假象。
   **修复方式**：`logging_audit/db.py` 的 `get_database_url()` 改为优先读
   `server/.env` 的 `DATABASE_URL`（优先级：`LEARNING_DATABASE_URL` 环境变量 >
   `server/.env` > 内置开发默认值），并把 12 个测试文件的硬编码替换为
   `os.environ.setdefault("LEARNING_DATABASE_URL", get_database_url())`。
   已验证 `app.core.config` 与 `logging_audit.db` 解析结果完全一致，且
   alembic downgrade 后测试侧立即不可见切片 2 表/列（证明共享同一库）。
   **旧库 `learning_assistant` 已删除**（2026-09-28）：删除前确认其 719 个账号
   全部为 `@example.com`/`@fake.test` 合成域名、非合成账号数为 0、数据为
   切片 0/1 测试累积物；删除后全量测试仍 277 passed，无隐性依赖。
   当前唯一开发库：`learning_assistant_tpl`。
2. **`create_all()` 不会 ALTER 既有表**：`learning_tasks` 已存在时，模型新增
   `lane` 不会自动生效，必须走迁移。
3. **tsvector 写入必须用数据库函数**：直接绑定字符串到 `TSVECTOR` 列会按字面量
   存整词，与查询侧解析不一致，导致英文标识符漏召回。已改为
   `to_tsvector('simple', :tokens)`，查询用 `plainto_tsquery('simple', :tokens)`，
   两侧解析对称。
4. **PostgreSQL simple 解析器行为**：`conv2d_block_7` 被拆为 `conv2d`/`block`/`7`，
   而 `v1.2.3` 保留整体。正则复现解析器不可靠，故查询改为交由数据库解析。
5. **字节字段需 BigInteger**：5 GiB 配额超出 `INTEGER`，已改用 `BigInteger`。
6. **jieba 切分确定性**：`反向传播` 在 jieba 0.42.1 下切为 `反向`+`传播`；
   词典/版本变化必须递增 `source_version` 并重建索引（已写入测试断言）。

## 5. 未决项与差异（需交回规划者）

1. **T01 阻断（最高优先）**：需真实 AI Studio/Baidu 凭据。在补齐前，
   `PaddleOCRAdapter` 的 HTTP 路径与响应字段仍是切片 1 的合成契约，未经官方文档核对。
2. **pg_trgm 未启用**：T05 实测前保持不启用（交接文档 §1.3 允许）。
3. **前端资料页未实现**：T07 前端部分（AC-08）未开始。
4. **调度四类未实现**：`lane` 字段已落，但 `claim()` 的 lane 筛选与保留容量未实现（T06）。
5. **删除墓碑仅为占位**：`deletion_tombstones` + `asset_usage_ledger` 已落模型与写入路径，
   恢复流程与 30 天外部删除仍归切片 5。
6. **未修改日志/审计契约**：未新增错误码到 `logging_audit.contracts`，
   未改变字段允许名单或审计不可变性。

## 6. 合成数据声明

所有测试使用合成账号（`synthetic.*@example.com`）、假凭据与程序生成的合成 PDF；
仓库、测试与遗留产物中不含真实密钥、教材正文、个人信息、真实供应商响应或
带查询参数的生产 URL。`.slice2-samples/` 为本地生成物，不应提交。
