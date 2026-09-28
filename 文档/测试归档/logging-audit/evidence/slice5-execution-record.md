# 切片 5 执行记录（删除、诊断、备份恢复及参考负载）

> 执行日期：2026-09-27。前置：切片 2 余量（9e1f454）、切片 3（6a9f220）、切片 4（f17040b）。
> 交接：按规划者既定思路执行（无独立 handoff 文档；任务分解见切片 4 交接文档同批决策记录）。

## 结果总览

| 任务 | 状态 | 说明 |
| --- | --- | --- |
| 删除流 | 完成 | `app/planning/deletion.py`：`DELETE /api/planning/routes/{id}`（课程保留，编排解除）、`DELETE /api/planning/courses/{id}`（清理版本/讲次/草稿/候选，教学会话软删）；全部写墓碑（`DeletionTombstoneRow`）+ 审计（`resource.delete.requested`）+ 取消排队任务；跨账户删除 404 |
| 在线清理 | 完成 | `CleanupService.run_online_cleanup`：pending 墓碑过 24h → 推进 cleaned；对象卷（原 PDF/图块）删除；失败保留 pending 可重试 |
| 备份轮换 | 完成 | `create_backup`：对象卷打包 + 独立墓碑记录源清单；已删除满 30 天的墓碑/数据不进入新备份；`backup_runs` 账本 + 30 天轮换删旧 |
| 恢复（restore_locked） | 完成 | begin（锁定，业务访问 423，仅 /api/admin 放行）→ replay（墓碑重放：被恢复出来的已删对象重新删除，含 DB 行、索引、受控对象）→ verify（全部墓碑对象不可访问才解锁）→ unlock |
| P95 参考负载 | 完成（本地口径） | `scripts/slice5_p95_harness.py`：两账户并发 × 各 30 样例、第 29 最小值；首段可读文字 **0.249s**（阈值 30s）、音频可播放 **0.160s**（阈值 60s）——合成适配器测得服务端管线开销，正式部署以真实凭据重跑回填 |
| 全量交叉回归 | 完成 | `tests/test_slice5_regression.py`：17 项——B 账户交叉访问 A 的资料/图块/路线/课程/草稿/任务/SSE/工作区/检查点/音频/检索/删除，一律 404/403；所有者对照访问正常 |

测试结果：`uv run --project server/backend pytest tests -q` → **437 passed, 2 skipped**（411 + slice5 新增 26）。`npm run build` 通过。

## 实现要点与差异

1. **`system_state` / `backup_runs` 表（新增，迁移 `f7b9d0e2a4c1`）**：恢复锁为部署级键值（`restore_locked=1` 期间中间件对非 `/api/admin` 请求返回 423）；备份运行账本记录产物键与删除截止。
2. **墓碑重放的 DB 语义（T08 注入暴露的真实缺陷）**：初版 replay 只清受控对象不清 DB 行——被「备份恢复」复活的已删资料仍可读。修复：replay 对 material/route/course/conversation 重设 `deleted_at`、写索引删除账本、删受控对象；verify 在全部墓碑对象不可访问后才解锁。
3. **`materials.get` 未校验 deleted_at（回归发现）**：软删资料仍可读。修复：删除即撤销访问（含图块/音频等受权读取路径）。
4. **审计事件扩展**：`restore.replayed` / `restore.unlocked` / `restore.verify_failed` 注册进事件注册表与审计集合（`resource.delete.requested`、`restore.locked` 已有）。
5. **音频受权读取**：`GET /api/teaching/audio/{id}/content` 会话所有权校验后回传；跨账户 404（切片 4/5 测试覆盖）。

## 实际命令

```text
uv run --project server/backend pytest tests -q        # 437 passed, 2 skipped
cd frontend && npm run build                            # ✓
LEARNING_DEV_SYNTHETIC_MODEL=1 uv run --project server/backend \
    python scripts/slice5_p95_harness.py --samples 30 --out <evidence>/slice5-p95.json
```

## 遗留（交回规划者/部署阶段）

- P95 为本地合成口径（服务端管线开销）；正式云部署以真实模型/TTS 凭据与目标规格重跑，云厂商/规格/域名按架构在部署前锁定。
- 备份的数据库快照由部署层工具（pg_dump）补全，本切片固化对象卷打包、清单、轮换与恢复契约。
- OCR/TTS/模型三份 spike 真实矩阵：凭据就绪后重跑 `scripts/slice3_model_spike.py`、真实 OCR 样本与 MiniMax 试听，回填各证据文档。

## 9. 真实凭据 P95 抽查与 Gemini 原生矩阵（2026-09-28 追加）

- **Gemini 原生协议矩阵**（同一网关 `/v1beta`）：text 两种鉴权 200；responseSchema 对小输出被遵守（首轮误报系探针字符串匹配问题，已修）；长路线生成被网关 ~61s 上游超时截断（504×2）——生产长生成维持 OpenAI 兼容 prompted 路径。详见 slice3 证据 §3.2。
- **真实凭据 P95 抽查**：`slice5_p95_harness.py --real`（桥接回显法，两账户并发 × 各 8 样例）——首段可读文字 p29=**19.8s**（阈值 30s）、音频可播 p29=**7.6s**（阈值 60s），均通过。数据见 `slice5-p95-real.json`。正式部署仍需目标规格下全量 30 次重跑。
- **Langfuse 自托管**：`deploy/langfuse-compose.yml` 已交付（本机裁剪配额 ~2.5 核 / 5.3 GiB，生产建议 4C/16G/100G 独立部署）；当前网络无法拉取 docker.io 镜像（直连与死代理均不可达），启动流程已写入文件头注释，网络恢复后 `docker compose up -d` 即可。`PromptService` 的 langfuse 拉取路径与切片 0 脱敏 exporter 的对接保持待接线状态（本次用户指示先不启用）。
