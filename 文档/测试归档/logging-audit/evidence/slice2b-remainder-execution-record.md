# 切片 2 余量执行记录（T03～T08）

> 执行日期：2026-09-27。前置：切片 2 T02（部分完成）、切片 3（已完成）。
> T01（OCR 真实样本 spike）仍为外部阻断（无 PaddleOCR 凭据）；本记录按切片 3 先例以合成适配器锁定管线契约，真实供应商接入为人工前置。

## 结果总览

| 任务 | 状态 | 说明 |
| --- | --- | --- |
| T03 逐页解析与 OCR 调度 worker | 完成 | `app/materials/ocr.py`：上传入队（lane=ocr）→ 逐页渲染 + 文本层可信判定 → 可信直入索引（不重复 OCR）→ 不可信页外发核验后提交外部任务（单页 180s 截止）→ `ocr_result_fetch` 任务轮询 → 区域落库 |
| T04 原图裁切与可用状态 | 完成 | `app/materials/crop.py`：`map_bbox_to_pixels` 纯函数（90/180/270 + bottom_left，round-trip 可复现）；`CropService` 在受控渲染结果上裁图 → 受控对象键；结构图未命中 → 区域 partial、页最多 partial |
| T06 四类 lane 调度隔离 | 完成 | `TaskService.claim_next(lane)`：interactive 保留容量不被动 lane 侵占；全局/单 lane/单用户三层上限；队列等待计入创建时总截止（过期 → expired 不再领取） |
| T07 配额/删除/前端资料页 | 完成 | 受控存储层（`materials/storage.py`，对象键不含用户文件名）；上传即存原件、删除即下线对象；配额 reserve/settle/release 改为 SQL 级原子（修复并发丢失更新）；前端 `MaterialsPage.tsx`（上传区/状态字形/逐页网格/失败重试/删除/检索面板） |
| T08 故障注入 | 完成 | `tests/test_slice2_faults.py`：OCR 占满非交互容量后 interactive 仍可领、旋转坐标系可复现、迟到结果围栏拒绝、可信文字不重复 OCR、跨账户/扩大 material_ids 检索边界、删除即时移除、并发账本一致 |

测试结果：`uv run --project server/backend pytest tests -q` → **396 passed**（372 基线 + 24 新增：parse 13 + sched 5 + faults 6）。`npm run build` 通过。

## 实现要点与差异

1. **受控存储层（新增）**：原 PDF 与图块存 `MATERIALS_STORAGE_DIR`（默认 `server/storage/`，已入 .gitignore）；对象键 `orig/{material_id}.bin`、`fig/{material_id}/{figure_id}.png`；图块内容仅经会话 + 所有权校验的接口回传。
2. **T05 说明**：检索核心（jieba/tsvector/删除账本/`/api/materials/search`）已在切片 2 T02 落地；pg_trgm 未启用（简单召回场景够用，启用与否的取舍记录于此，未建 ADR）。
3. **调度容量默认值**：global=8、interactive=4（保留）、ocr=3、prefetch=2、maintenance=1、单用户单 lane=2；可经 `TASK_*` 环境变量覆盖。队列等待时长不新增日志字段（避免允许名单变更），由 `created_at → claimed_at` 与总截止语义覆盖。
4. **OCR 供应商契约**：`normalize_ocr_regions` 按官方 `layoutParsingResults → prunedResult → parsing_res_list`（block_bbox/block_label/block_content）归一；真实 PaddleOCR 端点凭据缺失，适配器经工厂注入（`LEARNING_DEV_SYNTHETIC_MODEL=1` 时走查用合成适配器），T01 真实矩阵待补跑后复核字段一致性。
5. **修复的真实缺陷**：并发上传下配额 reserve 的 ORM 读改写丢失更新（T08 注入暴露）→ 改为带条件谓词的单条 UPDATE；Windows 下受控存储路径前缀误判（T08 注入暴露）→ `is_relative_to` + 一次性 resolve。

## 实际命令

```text
uv run --project server/backend pytest tests -q        # 396 passed
cd frontend && npm run build                            # ✓
for i in 1..6: pytest tests/test_slice2_faults.py -q    # 6/6 稳定通过
```
