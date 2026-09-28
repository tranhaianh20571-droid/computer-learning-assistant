# 切片 2 OCR 外部契约核对表（T01 准入闸门）

> 状态：**未通过 / 被外部条件阻断（2026-09-28）**
> 交接基线：[slice-2-materials-parse-handoff.md](../../架构/slice-2-materials-parse-handoff.md) T01
> 检查器：`scripts/slice2_ocr_spike.py`（可复跑）
> 样本：`scripts/slice2_sample_generate.py` → `.slice2-samples/`（11 个合成 PDF，程序生成）

## 1. 阻断结论

T01 要求使用**真实** AI Studio PaddleOCR-VL-1.6（或百度智能云商业 API）账户凭据，
对合成样本逐页核验结构化结果。当前执行环境**不存在**任何 OCR 供应商凭据：

| 检查项 | 结果 |
| --- | --- |
| `PADDLEOCR_ENDPOINT` / `PADDLEOCR_API_KEY` | 未设置 |
| `BAIDU_OCR_API_KEY` / `BAIDU_OCR_SECRET_KEY` | 未设置 |
| WSL / Windows 环境变量（OCR/PADDLE/BAIDU/AISTUDIO/QIANFAN/DASHSCOPE/VOLC） | 均无 |
| `~/.config/codeg-secrets`、`~/.config/codex/secrets`、`~/.t3/userdata/secrets` | 无 OCR 条目 |
| `server/.env` | 仅模板必需项（SECRET_KEY / DATABASE_URL / SMTP 等），无 OCR 凭据 |
| 唯一存在的 `GOOGLE_API_KEY` | 属 Gemini 内容模型，**不是** PaddleOCR/AI Studio 凭据 |

因此：

- **T01 未执行，未通过。** 本文不写入任何未经真实核验的“通过”结论。
- **T03 / T04 / T05 的 OCR 写路径必须保持关闭**（与交接文档 §1.4、§4 一致）。
- 需要真实账户凭据与网络访问才能继续；这是**外部阻断**，不是代码缺陷。

复跑命令（凭据只从环境变量读取，禁止写入命令行或仓库）：

```text
python scripts/slice2_sample_generate.py --out .slice2-samples
python scripts/slice2_ocr_spike.py --provider aistudio --samples .slice2-samples
```

实测输出（无凭据）：

```text
BLOCKED: provider=aistudio missing env: PADDLEOCR_ENDPOINT, PADDLEOCR_API_KEY
T01 未执行：缺少真实 OCR 凭据。T03/T04/T05 的 OCR 写路径必须保持关闭。
exit code = 2
```

## 2. 已就绪的核验前置件（已完成部分）

| 前置件 | 状态 | 说明 |
| --- | --- | --- |
| 合成样本矩阵生成 | 完成 | 11 个 PDF：`text_layer` / `scanned_text` / `handwriting` / `table_formula` / `circuit_diagram` / `neural_network` / `rotated_90` / `rotated_180` / `rotated_270` / `large_page` / `multi_column` |
| 样本生成命令 | 完成 | `python scripts/slice2_sample_generate.py --out .slice2-samples` |
| 样本字节/页数记录 | 完成 | `.slice2-samples/manifest.json` |
| 检查器骨架 + 受控退出码 | 完成 | `exit 0` 通过 / `2` 缺凭据 / `3` 缺样本 / `4` 契约核验失败 |
| pypdfium2 200 DPI 渲染 | 完成（本地） | 1654×2339 px（A4 @200 DPI） |
| 文本层可信判定 | 完成（本地） | 见 §3，扫描/手写正确判为不可信 |
| 真实端点逐接口调用 | **未执行** | 待凭据 |

## 3. 本地可验证部分（非外部契约，仅供参考）

以下为本机合成样本上的渲染/判定实测，**不能替代 T01 外部契约核验**：

| 样本 | 页数 | 文本可信 | 依据 | 渲染像素 | 降级 |
| --- | --- | --- | --- | --- | --- |
| text_layer | 1 | 是 | ok | 1654×2339 | 否 |
| scanned_text | 1 | 否 | too_few_chars / low_effective_ratio / high_garbage_ratio | 1654×2339 | 否 |
| handwriting | 1 | 否 | 同上 | 1654×2339 | 否 |
| table_formula | 1 | 是 | ok | 1654×2339 | 否 |
| circuit_diagram | 1 | 否 | too_few_chars / bad_reading_order | 1654×2339 | 否 |
| neural_network | 1 | 是 | ok | 1654×2339 | 否 |
| rotated_90/180/270 | 1 | 是 | ok | 1654×2339 | 否 |
| large_page | 1 | 是 | ok | 2339×3308 | 否 |
| multi_column | 1 | 是 | ok | 2339×1654 | 否 |

## 4. 待核验契约字段（T01 通过前全部为 pending）

| 字段 | 状态 | 备注 |
| --- | --- | --- |
| http_submit_path | pending | 切片 1 骨架假定 `{endpoint}/ocr/jobs`，未核对官方文档 |
| http_status_path | pending | 骨架假定 `{endpoint}/ocr/jobs/{id}` |
| http_result_path | pending | 骨架假定 `{endpoint}/ocr/jobs/{id}/result` |
| coordinate_unit | pending | 像素 / 归一化 / 点？ |
| coordinate_system | pending | top_left / bottom_left |
| page_width_height | pending | 是否逐页返回 |
| rotation_handling | pending | 0/90/180/270 语义与方向 |
| reading_order | pending | 形式与稳定性 |
| figure_independent_id | pending | 图块是否有独立 ID |
| caption_association | pending | 图题关联方式 |
| per_page_failure_info | pending | 逐页失败信息粒度 |
| single_page_timeout_180s | pending | 真实端到端时延是否 <180 s |
| quota_and_retention | pending | 配额断面与结果保留期 |

## 5. 影响与后续

- **阻断 T03/T04/T05**：逐页解析 worker、原图裁切、检索的 OCR 关联部分。
- **不阻断 T02**：数据模型、上传校验、配额账本、渲染契约、文本可信判定、
  内置检索已独立完成并通过测试（见 `slice2-execution-record.md`）。
- 为 T01 补齐凭据后，需：①运行检查器完成真实调用；②按官方文档对齐
  `PaddleOCRAdapter` 的 HTTP 路径与响应字段；③把本表逐项改为通过/失败；
  ④若 AI Studio 不成立则用同一样本集复测百度商业 API；⑤均不成立则交回规划者。

## 6. 合成数据声明

样本由 `scripts/slice2_sample_generate.py` 程序生成，不含真实教材、个人信息、
真实凭据或供应商响应。本文不含任何密钥值。
