# ADR-022 内容 trace 通道 + OTel/Langfuse + PydanticAI 桥 · 执行记录

- 日期：2026-09-28｜执行环境：Windows 11 / Git Bash / uv（pydantic-ai-slim 2.51.0 + opentelemetry 1.45.0 + Python 3.14）+ 本机 PostgreSQL 17
- 决策依据：ADR-022（`文档/架构/computer-learning-assistant-architecture.md（附录·决策记录）`）、规范 §6 通道化重写、`文档/架构/v2/pydanticai-v2-refactor-execution-plan.md`

## 实际运行的命令与结果

| 命令 | 结果 |
| --- | --- |
| `uv add --project server/backend "pydantic-ai-slim>=2.51.0" opentelemetry-sdk opentelemetry-exporter-otlp-proto-http` | ✅ 安装 19 包（pydantic-ai-slim==2.51.0、pydantic-graph==2.51.0、truststore==0.10.4、otel 1.45.0 全套） |
| 桥冒烟（合成适配器 → FunctionModel → Agent(output_type=Out)） | ✅ 输出 `Out(name='ok')`；期间修复：输出工具名从 `final_output` → 动态取 `AgentInfo.output_tools`（2.51 默认 `final_result`） |
| `uv run --project server/backend pytest tests/test_v2_pydanticai_bridge.py tests/test_t08_content_trace.py -q` | ✅ **19 passed**（迭代过程修复 3 处：InMemorySpanExporter 子模块导入、DB 用例唯一 ID、事件级内容字段强制补齐） |
| `uv run alembic upgrade head`（c7e9f1a3b5d7 → a8d0f2b4c6e8） | ✅ 迁移落库（注：首次因测试 create_all 抢先建表报 DuplicateTable，drop 后正式迁移成功） |
| `uv run --project server/backend pytest tests -q`（全量） | ✅ **495 passed, 2 skipped**（基线 451 → 495；修复 t01 两处语义翻转断言：未知字段示例换名、`platform` 进允许名单） |
| 注册表快照 | ✅ `schemas/registry-v2.json` 生成（62 允许字段 / 33 事件 / 38 错误码） |

## 新增/修改清单（代码侧，均已镜像到 tests/support/backend/）

- 新增：`logging_audit/otel_setup.py`（OTLP 装配 + 内容 span）、`logging_audit/content_trace.py`（开关 + model.call.content 发射）、`cleanup/observability_deletion.py`（删除联动服务 + REST 客户端）、`prompts/langfuse_client.py`（提示词 REST 拉取器）、`agents/`（FunctionModel 桥包）、alembic `a8d0f2b4c6e8`
- 修改：contracts（CONTENT 类型/3 内容字段/platform/2 事件/1 错误码 + 事件级内容字段强制在 logger）、sanitize（内容字段保结构清理）、logger（content_stream 第二出口）、model_runner（三出口内容捕获）、config（6 新设置）、main（otel_from_env）、cleanup/models（observability_deletion_refs 表）、prompts/service（langfuse_fetch 注入点）、compose.yml（6 环境变量，默认全关）
- 测试：新增 `test_t08_content_trace.py`（14 用例）+ `test_v2_pydanticai_bridge.py`（5 用例，TestModel 冒烟 + FunctionModel 桥主力）；t01 两处断言随语义翻转更新；t02/t06/t07 未动（默认关状态下语义不变，通道行为由 t08 覆盖）

## 待执行（未验证项，如实标注）

- Langfuse 真实实例联调：`deploy/langfuse-compose.yml` 启动、OTLP 导出端到端、`/api/public/v2/prompts` 拉取、`DELETE /api/public/traces/{id}` 对端契约
- 单机合并负载验证（ADR-022 接受的 +2.5 核/5.3 GiB 代价）
- 删除联动挂入切片 5 实际删除事务（当前为独立服务 + 测试，`register_deletion` 的调用点接线随 S3/S4 落位）
- PydanticAI `InstrumentationSettings` 启用点（2.51 无 `pydantic_ai.settings.InstrumentationSettings`，待确认迁移路径后接入）

## 追加（2026-09-28 同日）：本机开关落地

- `server/.env` 增 `CONTENT_TRACE_ENABLED=1`（gitignore 覆盖，不入库）；`main.py` 按既有桥接模式把 settings 解析值（真实环境变量 > .env）同步进 `os.environ`；OTel/Langfuse 保持关闭。
- 端到端验证：`settings.CONTENT_TRACE_ENABLED=True` → `content_trace_enabled()=True` → `emit()` 产出 `model.call.content` 单行 JSON（含 model_prompt/model_answer，零凭据字段，emit_errors=0）。
- 回归：t08+t07+t02+v2 桥 42 passed；提交（main.py 桥接 + 镜像）。
