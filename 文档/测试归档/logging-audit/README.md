# 测试证据归档（T01～T08 专项与各切片验收）

本目录是**证据与验收归档**，不放可执行测试；测试实现与运行入口见仓库根 `tests/README.md`，组织约定见 `文档/规范/testing-spec.md`（通用规范）。
2026-09-28 由 `文档/规范/tests/` 迁移至 `文档/测试归档/`（规范目录只留规范，证据归档独立分层）；历史交接文档：（历史交接文档 slice-0-logging-audit-handoff.md，2026-09-28 随 v1 清理删除）。

## 目录约定

- `fixtures/`：只放合成用户、任务、资源 ID、假邮箱、假 API Key 和假供应商错误；不得放真实凭据、教材、模型提示词或生产 URL。
- `evidence/`：执行记录与脱敏证据。文件按 `sliceN-` 前缀区分切片；**无前缀**的 `execution-record.md`、`sample-*`、`ac-summary` 是 T01–T06 日志专项的原始记录（2026-09-26）。不提交完整生产日志。
- `schemas/`：代码注册表（`server/backend/app/logging_audit/contracts.py`）的**版本化快照**——`registry-v1.json`（2026-09-26）、`registry-v2.json`（2026-09-28，ADR-022 内容通道）；权威以代码为准，本目录不自动同步。
- `reports/`：AC 汇总报告；未执行项标"待执行"，失败项保留原因。

## 证据索引（阶段 ↔ 记录 ↔ 测试；基线截至 2026-09-29：全量 537 passed / 2 skipped）

| 阶段/切片 | 执行记录 | AC 汇总 | 主要测试（仓库根 `tests/`） |
| --- | --- | --- | --- |
| T01–T06 日志地基 | `evidence/execution-record.md`（2026-09-26，123 passed） | `reports/ac-summary.md`（AC-01～09） | `test_t01~t06_*`、`test_integration_*` |
| T07 模型调用指标（观测收尾 A1） | 架构 v2 执行方案 A1 节（提交 4da861c） | — | `test_t07_model_call_metrics.py` |
| 切片 0 完整验收 | `../slice-0/evidence/execution-record.md`（2026-09-27，150 passed） | `../slice-0/reports/ac-summary.md`（AC-01～12） | `test_slice0_*` |
| 切片 1 | `evidence/slice1-execution-record.md` | — | `test_slice1_*` |
| 切片 2 / 2b | `evidence/slice2-execution-record.md`、`slice2b-remainder-execution-record.md`、`slice2-ocr-external-contract.md` | — | `test_slice2_*` |
| 切片 3 | `evidence/slice3-execution-record.md`、`slice3-model-capability-spike.md`、`slice3-excalidraw-font-spike.md`、`slice3-t01-real-result.json` | — | `test_slice3_*`、`test_v2_model_streaming`、`test_v2_gemini_adapter`、`test_v2_intake` |
| 切片 4 | `evidence/slice4-execution-record.md` | — | `test_slice4_*` |
| 切片 5 | `evidence/slice5-execution-record.md`、`slice5-p95.json`、`slice5-p95-real.json` | — | `test_slice5_*`、`test_ac03_worker_chain` |
| ADR-022 内容通道 + OTel + PydanticAI 桥 | `evidence/adr022-content-trace-execution-record.md`（2026-09-28，提交 6f4ca8c/5085c39） | — | `test_t08_content_trace.py`、`test_v2_pydanticai_bridge.py` |
| 前端应用回归 | `scripts/slice1_frontend_check.mjs` 输出（`.playwright-cli/`） | — | — |

> 双源说明：T01–T06 专项记录与切片 0 完整记录各自保留、互相指向，均为真实历史证据；切片 0 的验收以 `../slice-0/`（更新）为准。

历史运行环境（T01–T06 原始记录）：Windows 11 / Python 3.12.3 / FastAPI 0.141.1 + SQLAlchemy 2.1.1 + Alembic 1.20.0 + PostgreSQL 17.11 / Playwright CLI 0.1.21。当前运行环境：Windows 11 / Python 3.14 / uv / PostgreSQL 17（本机测试库）；运行方式以 `tests/README.md` 为准。
