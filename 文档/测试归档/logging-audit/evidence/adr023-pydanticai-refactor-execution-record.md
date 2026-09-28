# ADR-023 PydanticAI v2 重构（S0–S4）· 执行记录

- 日期：2026-09-28｜环境：Windows 11 / uv / Python 3.14 / pydantic-ai-slim 2.51.0 / PostgreSQL 17 本机测试库
- 方案：`文档/架构/v2/pydanticai-v2-refactor-execution-plan.md` v1.1

| 切片 | 提交 | 要点 | 验证 |
| --- | --- | --- | --- |
| S0 | b479f01 | ADR-023；O-B 残骸清理（app/tests、items、Item 模型/CRUD；D3 修正：db.py 为活垫片保留）；O-A assembler 入口收敛 | 全量 497；uvicorn 冒烟 docs/health 200、教学路由 401 |
| S1a | bf059a3 | 桥补全：请求装配（forced tools）/stream_function/内容捕获迁入（R1）/门控+失败缓存复用/AGENT_LOOP_LIMIT；R4 spike=PromptedOutput 可用 | +8 桥测试；505 |
| S1b | 7c50adf | followup+chat 同步路径切换；偏差：流式保留适配器直连（delta 等价优先） | 受影响 35 过；505 |
| S2 | 81a9f1d | 教学步骤 Agent + 板书业务校验（时长/边引用/时标）ModelRetry 字段级回喂 | +2；507 |
| S3 | ebf0a9c | 规划三调用点 + search_materials 受控检索工具 + intake_followup_v1 模板（消 D6）+ make_fallback_model（默认关）；测试语义随 ADR-023 更新（tool→prompted、形状感知合成适配器） | +5；511 |
| S4 | 本次 | agent.turn.* 事件 + model.call requests 维度 + registry-v3.json + PRD 1.15 + AGENTS.md 权威指针 + model_runner 遗产标注 | **511 passed / 2 skipped**；前端 `npm run build` 通过 |

## 待执行

- 前端浏览器走查（/course→/course/detail→/lecture、设置页、360px、无真实外发——AGENTS.md 清单；本轮仅完成构建）
- Langfuse 真实实例端到端（ADR-022 遗留，与本次正交）
- make_fallback_model 生产接线（默认关，灰度方案未启用）
