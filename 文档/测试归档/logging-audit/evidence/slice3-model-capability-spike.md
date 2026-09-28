# 切片 3 T01 证据：模型能力探测与结构化输出 spike

> 执行日期：2026-09-27。执行环境：Windows 11 10.0.26200 x64，Git Bash，Python 3.14.7（uv 项目 `server/backend`），httpx 0.28.1。
> 状态：**脚本就绪并验证可运行；真实模型探测被外部前置阻断（与切片 2 T01 同类）**。产品写路径按本文档「降级默认结论」以合成适配器实现并通过测试；真实矩阵待用户在提供凭据的环境补跑。

## 1. 门禁定义与交付物

- 脚本：`scripts/slice3_model_spike.py`（可复跑；凭据仅从 `SPIKE_OPENAI_*` / `SPIKE_ANTHROPIC_*` / `SPIKE_GEMINI_*` 环境变量读取）。
- 矩阵：逐协议（OpenAI 兼容 / Anthropic / Gemini 原生）× 逐能力（text / image / tool_call / json_schema / streaming / cancel）+ 三种输出模式（native / tool / prompted）。
- 边界验证：不支持结构化输出的配置必须落在 `model_capability_unavailable`（后端契约已实现，见 `app/planning/model_runner.py` 与 `tests/test_slice3_faults.py::test_model_capability_unavailable_*`）。

## 2. 实际运行记录

命令（仓库根）：

```text
uv run --project server/backend python scripts/slice3_model_spike.py --out <tmp>/slice3-model-spike-result.json
# 输出（stdout 摘要，无正文）：
# [openai]    SKIPPED: credentials not provided via environment (manual prerequisite)
# [anthropic] SKIPPED: credentials not provided via environment (manual prerequisite)
# [gemini]    SKIPPED: credentials not provided via environment (manual prerequisite)
```

环境内已声明但无法配对使用的通道（2026-09-27 11:14 UTC 实测）：

| 环境变量 | 实测结果 | 结论 |
| --- | --- | --- |
| `GOOGLE_GENAI_BASE_URL=https://yxai.anthropic.edu.pl` | `curl` → `Could not resolve host`（DNS 失败） | 端点不可达 |
| `LOCAL_ENDPOINT=http://127.0.0.1:8045/v1` | 连接失败（exit 7，本机无监听） | 本地网关未运行 |
| `CUSTOM_API_KEY` / `DEEPKEY_API_KEY` / `GOOGLE_API_KEY` / `GROK_*_API_KEY` / `OPENCODE_ZEN_API_KEY` | 均为 `sk-` 样式密钥；环境中**没有**与之配对的端点声明 | 不允许向未配对端点试射密钥（费用与泄露风险） |

按交接文档 T01 前置条件「真实模型端点与凭据由用户通过环境变量提供（人工前置）；核对费用风险」，本次执行判定为**外部前置阻断**：不猜测端点、不向未配对服务发送密钥。

## 3. 能力矩阵（待真实补跑）

| 协议 | text | image | tool_call | json_schema | streaming | cancel |
| --- | --- | --- | --- | --- | --- | --- |
| OpenAI 兼容 | 待补跑 | 待补跑 | 待补跑 | 待补跑 | 待补跑 | 待补跑（以流式中断近似） |
| Anthropic | 待补跑 | 待补跑 | 待补跑 | 待补跑（经工具强制 schema 近似） | 待补跑 | 待补跑（以流式中断近似） |
| Gemini 原生 | 待补跑 | 待补跑 | 待补跑 | 待补跑（responseSchema） | 待补跑（流式端点） | 待补跑 |

补跑方法：设置上文 `SPIKE_*` 变量后重跑脚本，把输出的 JSON 摘要与矩阵回填本文档。

## 4. 降级默认结论（T04 依此实现，真实矩阵回来后可参数化调整）

在真实矩阵缺失的情况下，T04 的模型写路径按**最保守**顺序实现，且每一步都可在配置中显式声明关闭：

1. **输出模式选择顺序**：`native`（JSON Schema 受限生成）→ 不可用时 `tool`（函数调用强制参数）→ 仍不可用时 `prompted`（提示词内嵌 JSON + 服务端解析，重试上限 2 次）→ 全部不可用时任务以受控错误码 `MODEL_CAPABILITY_UNAVAILABLE` 结束（用户可见、不无限重试、不重排队列）。
2. **能力状态来源**：切片 1 `service_configs.capability_status`；任一必需能力为 `unknown` 时先探测再决定，探测失败按 `unavailable` 处理；`unavailable` 直接走降级链。
3. **解析失败语义**：`prompted` 模式解析失败计一次重试；超过上限任务失败，错误码同上；不得把原始供应商异常文本透出。
4. **取消能力**：OpenAI 兼容/Anthropic 以流式读取中断近似；Gemini 无取消端点时 cancel 记 unavailable，不阻塞其他能力。
5. **model_capability_unavailable 的用户可见边界**：错误码 + 固定中文文案（前端 `ERROR_TEXT`），不包含供应商原始响应。

以上 5 条已在 `tests/test_slice3_planning.py`（模式选择与降级）与 `tests/test_slice3_faults.py`（不无限重试、错误码边界）以合成适配器验证；真实供应商行为与合成行为的一致性依赖第 3 节矩阵补跑后复核。

## 5. 费用与安全说明

- 探测样例固定且非私人（`PROBE_TEXT` / 1×1 PNG）；单协议全部探测的输出预算 ≤ 数百 token。
- 证据与日志不落任何 API Key；端点只记录主机名；响应摘要经控制字符清洗并限长 80 字符（脚本内 `redact_text`）。
