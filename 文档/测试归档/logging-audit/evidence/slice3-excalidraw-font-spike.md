# 切片 3 T02 证据：Excalidraw 场景转换与中文字体 spike

> 执行日期：2026-09-27。执行环境：Windows 11 10.0.26200 x64，Node（v22.x，`node scripts/slice3_font_spike.mjs`），`@excalidraw/excalidraw` 0.18.1（`frontend/node_modules`）。
> 状态：**转换契约、布局常数与字体方案已锁定（Accept）**；浏览器内渲染走查在 T06/T07 前端页面完成后随 AC-06 一并复核（demo.html 已生成于本机临时目录，不入仓库）。

## 1. 可复跑脚本与产物

- 脚本：`scripts/slice3_font_spike.mjs`
- 产物（本机临时目录 `%TEMP%/slice3-font-spike/`，不提交仓库）：
  - `constants.json`：布局常数、字体清单、溢出检查结果；
  - `scene-sample.json`：合成路线（4 阶段×3 课程 + 3 条先修）与大纲（3 单元×4 讲 + 2 条先修）的白名单场景样本；
  - `demo.html`：只读画布演示页。

运行记录（2026-09-27）：

```text
== slice3 font spike ==
fonts: Excalifont(7 files, OFL-1.1); Xiaolai(209 files, OFL-1.1); Nunito(5); Lilita(2);
       ComicShanns(4); Cascadia(1); Assistant(4); Liberation(1); Virgil(1)
layout[route]:   nodes=16, worstTextWidth=234 <= maxInner=236 -> OK
layout[outline]: nodes=15, worstTextWidth=202 <= maxInner=236 -> OK
```

## 2. 字体方案（Accept，锁定）

| 项 | 结论 |
| --- | --- |
| CJK 回退 | **使用包内自带 Xiaolai（小赖字体）**，209 个 woff2 子集按 unicode-range 按需加载，0.18.1 生产包已内置（`dist/prod/fonts/Xiaolai/`），无需自托管、无 CDN 依赖 |
| 拉丁字体 | Excalifont（默认手写体，`fontFamily=1`） |
| 许可证链 | npm 包本体 MIT；Excalifont / Xiaolai / Nunito / Lilita / ComicShanns / Cascadia / Assistant / Liberation / Virgil 均 OFL-1.1（Xiaolai 为 Seto 改造扩展的 CJK 手写体）。OFL 允许随包再分发；SBOM 按 §7 记录 |
| 加载失败降级 | 子集缺失时浏览器回退系统字体，画布仍可用；不阻塞业务功能（本切片不把字体当作学习功能的前提） |
| 长文本 | 转换层预换行：CJK 字宽≈1.0×fontSize、ASCII≈0.6×fontSize 的度量模型，每节点最多 2 行，超出截断加省略号「…」 |

## 3. 确定性层级布局常数（Accept，锁定并进 T03 契约测试夹具）

| 常数 | 值 | 说明 |
| --- | --- | --- |
| `fontSize` / `lineHeight` | 16 / 1.25 | 所有节点文本 |
| `NODE_INNER_PAD` | 12 | 文本内边距 |
| `STAGE_W×H` | 260×56 | 路线阶段节点 |
| `COURSE_W×H` | 220×64 | 路线课程节点 |
| `UNIT_W×H` | 240×56 | 大纲单元节点 |
| `LESSON_W×H` | 200×56 | 大纲讲次节点 |
| `COLUMN_GAP` / `ROW_GAP` | 64 / 24 | 层列间距 / 行间距 |
| `MARGIN_X` / `MARGIN_Y` | 80 / 80 | 场景原点边距 |
| `MAX_LINES` | 2 | 超出截断 |
| 先修边 | 虚线箭头 `strokeStyle=dashed` | 与树形归属（实线框）分层表达 |

布局规则（后端 `app/planning/scene.py` 与前端按同一常数实现）：同层等距、父子在归属维度相邻（子节点缩进 16–20px 挂在父列下）、先修边独立样式；元素 ID 由业务对象 ID 派生稳定映射。

## 4. 观察结论

- **长中文节点**：最坏样本（25+ 字含全角标点与英文混排）在 2 行内宽度 234px ≤ 内宽 236px，不溢出（度量模型保守，真实 canvas 宽度更小）。
- **只读画布行为**：选择/缩放/平移/复制文本为 Excalidraw `viewModeEnabled=true` 原生能力；业务操作不进画布（T06 以层级列表承接）。
- **画布缩放**：元素绝对坐标布局，缩放/平移只影响视角（写 `planning_view_states`），不改业务顺序。
- **截图**：浏览器内渲染走查截图随 AC-06 前端走查一并归档（本机临时目录，不入仓库）。

## 5. Accept/Adjust 结论汇总

| 项 | 结论 |
| --- | --- |
| 字体来源与许可证 | Accept（包内 Xiaolai，OFL-1.1，无自托管） |
| 字号/行高/节点尺寸常数 | Accept（上表，进契约夹具） |
| 长中文换行策略 | Accept（预换行 + 2 行截断省略号） |
| 先修边表达 | Accept（虚线箭头独立图层） |
| 确定性布局 | Accept（同规则镜像实现于后端 scene.py，契约测试比对稳定性） |
