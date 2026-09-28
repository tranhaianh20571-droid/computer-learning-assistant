# 测试结构整理 · 执行记录（2026-09-28，提交 d58f33e）

- 迁移：`文档/规范/tests/` → `文档/测试归档/`（规范只留规范）；新增通用规范 `文档/规范/testing-spec.md`（无内链）。
- 重写 `tests/support/backend/README.md`（修正镜像方向：生产镜像进测试树，逐字节一致；登记例外：`__init__.py` 垫片、2 个 ac03 专用 alembic 夹具）。
- `tests/README.md` 分层表补 t07/t08、v2 桥、镜像守护行；基线更新。
- 新增 `tests/conftest.py`（集中 support 路径 + LEARNING_DATABASE_URL + banner 开关）与 `tests/test_mirror_sync.py`（白名单制哈希校验）。
- `slice3_common.py` 更名 `common_fixtures.py`，13 个测试文件 import 同步。
- 验证：`uv run --project server/backend pytest tests` → **497 passed / 2 skipped**（495 基线 + 2 项镜像守护）。迁移前镜像预检：94 同/1 异（垫片）/2 镜像独有（ac03 夹具）——均已进白名单。
- 未做（维持评估结论）：tests 根目录物理分组（收益不足）；模板测试处置随 O-B 执行方案走。
