# 生产代码镜像树（backend 包）

本目录是 `server/backend/app/` 的**镜像副本**，测试导入的是这里（`from backend.xxx import ...`），生产运行只用 `server/backend/`。镜像不是独立实现——**生产代码改动时必须同步拷贝到本目录**（双写），否则测试在跑旧代码。

## 镜像契约

- 除下述**登记过的例外**外，本目录每个 `.py` 文件必须与 `server/backend/app/` 对应文件**逐字节一致**；由仓库根 `tests/test_mirror_sync.py` 以哈希校验守护，不一致即测试失败。
- 例外清单（有意 divergence，新增例外必须在此登记并说明理由）：
  - `__init__.py`：包级垫片，额外导出 `Database/get_db/set_db` 供部分测试与 alembic 环境使用；生产 `__init__.py` 为空。
  - `alembic/versions/11fd41567866_task_events_audit_logs_foundation.py`、`alembic/versions/e3d25b84ccbb_slice0_identity_tasks_prompts.py`：**测试专用迁移夹具**（`test_ac03_worker_chain.py` 使用），生产迁移链中已被后续版本取代，仅存于镜像。
- 镜像允许落后于生产（生产有镜像没有的文件，如 `core/`、`api/`、模板残骸）——测试需要的模块才进镜像；但**已有文件不允许内容漂移**。

## 同步方式

```bash
# 改动生产文件后（仓库根执行）
cp server/backend/app/<path> tests/support/backend/<path>
uv run --project server/backend pytest tests/test_mirror_sync.py -q
```

统一验证命令（仓库根）：`uv run --project server/backend pytest tests`。
生产应用运行时使用 `uv run`（`server/backend`），数据库迁移使用 `server/backend/app/alembic/versions/`。不要把本目录作为 ASGI 应用启动。
