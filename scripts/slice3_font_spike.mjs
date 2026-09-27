/**
 * 切片 3 T02：Excalidraw 场景转换与中文字体 spike（可复跑）。
 *
 * 目标（交接文档 T02 / AC-02）：
 * 1. 清单化 @excalidraw/excalidraw 0.18.1 内置字体（含 CJK 回退 Xiaolai）与许可证链；
 * 2. 用合成结构化数据（4 阶段×3 课程、3 单元×4 讲、含先修边）跑确定性层级布局原型；
 * 3. 验证长中文节点（含标点）、中英混排的预换行策略在常数内不溢出；
 * 4. 输出锁定常数清单与元素白名单场景样本，交 T03 冻结、T06 实现。
 *
 * 运行：node scripts/slice3_font_spike.mjs
 * 输出：控制台摘要 + %TEMP%/slice3-font-spike/ 下的 constants.json、scene-sample.json、demo.html（演示文件不入仓库）。
 */

import { existsSync, mkdirSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

const FRONTEND_NM = fileURLToPath(new URL("../frontend/node_modules/@excalidraw/excalidraw", import.meta.url));

// ---------------------------------------------------------------------------
// 1. 字体清单与许可证
// ---------------------------------------------------------------------------

function fontInventory() {
  const base = FRONTEND_NM;
  const fontsDir = join(base, "dist", "prod", "fonts");
  const out = { packageLicense: "MIT", fonts: [], notes: [] };
  if (!existsSync(fontsDir)) {
    out.notes.push("fonts dir not found; run npm install in frontend/ first");
    return out;
  }
  const families = {
    Excalifont: { license: "OFL-1.1", role: "默认手写体（拉丁）" },
    Xiaolai: { license: "OFL-1.1（上游小赖字体，Seto 改造扩展）", role: "CJK 回退（unicode-range 子集，按需加载）" },
    Nunito: { license: "OFL-1.1", role: "normal 字体（拉丁）" },
    Lilita: { license: "OFL-1.1", role: "code 之外的展示体" },
    ComicShanns: { license: "OFL-1.1", role: "code 字体" },
    Cascadia: { license: "OFL-1.1（Microsoft Cascadia Code）", role: "code 字体（备选）" },
    Assistant: { license: "OFL-1.1", role: "UI 字体" },
    Liberation: { license: "OFL-1.1", role: "回退字体" },
    Virgil: { license: "OFL-1.1", role: "旧版手写体（兼容场景）" },
  };
  for (const fam of Object.keys(families)) {
    const dir = join(fontsDir, fam);
    if (!existsSync(dir)) continue;
    const files = readdirCount(dir);
    out.fonts.push({ family: fam, ...families[fam], files: files });
  }
  out.notes.push("npm 包本体 MIT；内嵌字体各自 OFL-1.1，随包再分发（excalidraw 0.18.1 package.json license=MIT）。");
  out.notes.push("CJK 无需自托管字体：Xiaolai 子集由包内 unicode-range @font-face 按需加载；加载失败的降级是浏览器系统字体，不阻塞画布。");
  return out;
}

function readdirCount(dir) {
  try {
    return readdirSync(dir).length;
  } catch {
    return -1;
  }
}

// ---------------------------------------------------------------------------
// 2. 锁定常数（T03 冻结、后端 scene.py 镜像实现）
// ---------------------------------------------------------------------------

export const LAYOUT = {
  fontFamily: 1, // 1 = hand-drawn（Excalifont + Xiaolai CJK 回退）
  fontSize: 16,
  lineHeight: 1.25,
  // CJK 字宽≈1.0×fontSize、ASCII≈0.6×fontSize 的度量模型（浏览器 canvas 前的保守近似）
  CHAR_W: { cjk: 1.0, ascii: 0.6 },
  NODE_INNER_PAD: 12,
  STAGE_W: 260,
  STAGE_H: 56,
  COURSE_W: 220,
  COURSE_H: 64,
  UNIT_W: 240,
  UNIT_H: 56,
  LESSON_W: 200,
  LESSON_H: 56,
  COLUMN_GAP: 64,
  ROW_GAP: 24,
  MARGIN_X: 80,
  MARGIN_Y: 80,
  MAX_LINES: 2, // 超出两行截断加省略号
  PREREQ_DASH: { name: "dashed", gap: 8 },
  ARROW_WIDTH: 1,
};

// ---------------------------------------------------------------------------
// 3. 文本度量与预换行
// ---------------------------------------------------------------------------

export function measureTextWidth(text, fontSize = LAYOUT.fontSize) {
  let w = 0;
  for (const ch of text) {
    w += fontSize * (/[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef\u3000-\u303f]/.test(ch)
      ? LAYOUT.CHAR_W.cjk
      : LAYOUT.CHAR_W.ascii);
  }
  return w;
}

export function wrapLabel(text, innerWidth, maxLines = LAYOUT.MAX_LINES, fontSize = LAYOUT.fontSize) {
  const lines = [];
  let current = "";
  for (const ch of text) {
    const candidate = current + ch;
    if (measureTextWidth(candidate, fontSize) > innerWidth && current) {
      lines.push(current);
      current = ch;
      if (lines.length === maxLines) break;
    } else {
      current = candidate;
    }
  }
  if (lines.length < maxLines && current) lines.push(current);
  if (lines.length === maxLines) {
    const consumed = lines.join("").length;
    if (consumed < [...text].length) lines[maxLines - 1] = [...lines[maxLines - 1]].slice(0, -1).join("") + "…";
  }
  return lines;
}

// ---------------------------------------------------------------------------
// 4. 确定性层级布局原型（与后端 app/planning/scene.py 同规则）
// ---------------------------------------------------------------------------

export function layoutRoute(route) {
  const elements = [];
  let idSeq = 0;
  const id = (p) => `${p}_${(++idSeq).toString(36).padStart(4, "0")}`;
  const colW = LAYOUT.STAGE_W + LAYOUT.COLUMN_GAP;
  const positions = {}; // business_id -> {x,y,w,h}
  route.stages.forEach((stage, i) => {
    const sx = LAYOUT.MARGIN_X + i * colW;
    const sy = LAYOUT.MARGIN_Y;
    positions[stage.id] = { x: sx, y: sy, w: LAYOUT.STAGE_W, h: LAYOUT.STAGE_H };
    elements.push({
      type: "rectangle", id: id("rect"), x: sx, y: sy,
      width: LAYOUT.STAGE_W, height: LAYOUT.STAGE_H,
      strokeColor: "#1b1b1f", backgroundColor: "#e3e9ff", fillStyle: "solid",
    });
    const stageLines = wrapLabel(stage.title, LAYOUT.STAGE_W - 2 * LAYOUT.NODE_INNER_PAD);
    elements.push(textElement(id("txt"), sx + LAYOUT.NODE_INNER_PAD, sy + 8, stageLines));
    stage.courses.forEach((course, j) => {
      const cx = sx + 20;
      const cy = sy + LAYOUT.STAGE_H + LAYOUT.ROW_GAP + j * (LAYOUT.COURSE_H + LAYOUT.ROW_GAP);
      positions[course.id] = { x: cx, y: cy, w: LAYOUT.COURSE_W, h: LAYOUT.COURSE_H };
      elements.push({
        type: "rectangle", id: id("rect"), x: cx, y: cy,
        width: LAYOUT.COURSE_W, height: LAYOUT.COURSE_H,
        strokeColor: "#1b1b1f", backgroundColor: "#fff8e0", fillStyle: "solid",
      });
      const lines = wrapLabel(course.title, LAYOUT.COURSE_W - 2 * LAYOUT.NODE_INNER_PAD);
      elements.push(textElement(id("txt"), cx + LAYOUT.NODE_INNER_PAD, cy + 8, lines));
    });
  });
  // 先修边：与树形归属分开表达（虚线箭头，独立图层）
  for (const edge of route.prerequisites || []) {
    const a = positions[edge.from];
    const b = positions[edge.to];
    if (!a || !b) continue;
    elements.push({
      type: "arrow", id: id("arr"),
      x: a.x + a.w, y: a.y + a.h / 2,
      points: [[0, 0], [b.x - a.x - a.w, b.y + b.h / 2 - (a.y + a.h / 2)]],
      strokeColor: "#8a8f98", strokeWidth: LAYOUT.ARROW_WIDTH,
      strokeStyle: "dashed", // 先修边与树形归属（实线框）分层表达
    });
  }
  return { elements, positions };
}

export function layoutOutline(outline) {
  const elements = [];
  let idSeq = 0;
  const id = (p) => `${p}_${(++idSeq).toString(36).padStart(4, "0")}`;
  const colW = LAYOUT.UNIT_W + LAYOUT.COLUMN_GAP;
  const positions = {};
  outline.units.forEach((unit, i) => {
    const ux = LAYOUT.MARGIN_X + i * colW;
    const uy = LAYOUT.MARGIN_Y;
    positions[unit.id] = { x: ux, y: uy, w: LAYOUT.UNIT_W, h: LAYOUT.UNIT_H };
    elements.push({
      type: "rectangle", id: id("rect"), x: ux, y: uy,
      width: LAYOUT.UNIT_W, height: LAYOUT.UNIT_H,
      strokeColor: "#1b1b1f", backgroundColor: "#e3f7e8", fillStyle: "solid",
    });
    elements.push(textElement(id("txt"), ux + LAYOUT.NODE_INNER_PAD, uy + 8,
      wrapLabel(unit.title, LAYOUT.UNIT_W - 2 * LAYOUT.NODE_INNER_PAD)));
    unit.lessons.forEach((lesson, j) => {
      const lx = ux + 16;
      const ly = uy + LAYOUT.UNIT_H + LAYOUT.ROW_GAP + j * (LAYOUT.LESSON_H + LAYOUT.ROW_GAP);
      positions[lesson.id] = { x: lx, y: ly, w: LAYOUT.LESSON_W, h: LAYOUT.LESSON_H };
      elements.push({
        type: "ellipse", id: id("ell"), x: lx, y: ly,
        width: LAYOUT.LESSON_W, height: LAYOUT.LESSON_H,
        strokeColor: "#1b1b1f", backgroundColor: "#ffffff", fillStyle: "solid",
      });
      elements.push(textElement(id("txt"), lx + LAYOUT.NODE_INNER_PAD, ly + 8,
        wrapLabel(lesson.title, LAYOUT.LESSON_W - 2 * LAYOUT.NODE_INNER_PAD)));
    });
  });
  for (const edge of outline.prerequisites || []) {
    const a = positions[edge.from];
    const b = positions[edge.to];
    if (!a || !b) continue;
    elements.push({
      type: "arrow", id: id("arr"),
      x: a.x + a.w / 2, y: a.y + a.h,
      points: [[0, 0], [b.x + b.w / 2 - (a.x + a.w / 2), b.y - (a.y + a.h)]],
      strokeColor: "#8a8f98", strokeWidth: LAYOUT.ARROW_WIDTH, strokeStyle: "dashed",
    });
  }
  return { elements, positions };
}

function textElement(id, x, y, lines) {
  return {
    type: "text", id, x, y,
    text: lines.join("\n"),
    fontSize: LAYOUT.fontSize,
    fontFamily: LAYOUT.fontFamily,
    lineHeight: LAYOUT.lineHeight,
    strokeColor: "#1b1b1f",
  };
}

// ---------------------------------------------------------------------------
// 5. 合成样本 + 溢出检查
// ---------------------------------------------------------------------------

function syntheticRoute() {
  const stages = [];
  const courseIds = [];
  for (let s = 0; s < 4; s++) {
    const courses = [];
    for (let c = 0; c < 3; c++) {
      const cid = `course_${s}_${c}`;
      courseIds.push(cid);
      courses.push({ id: cid, title: c === 1 ? "数据库系统实现与事务并发控制（含中英文 Mixed Terms 与长标题换行观察）" : `阶段${s + 1}课程${c + 1}：核心概念与实践` });
    }
    stages.push({ id: `stage_${s}`, title: `第${s + 1}阶段：由浅入深、目标驱动的学习（超长标题观察标点换行与截断）`, courses });
  }
  const prerequisites = [
    { from: courseIds[0], to: courseIds[4] },
    { from: courseIds[4], to: courseIds[9] },
    { from: courseIds[1], to: courseIds[6] },
  ];
  return { stages, prerequisites };
}

function syntheticOutline() {
  const units = [];
  const lessonIds = [];
  for (let u = 0; u < 3; u++) {
    const lessons = [];
    for (let l = 0; l < 4; l++) {
      const lid = `lesson_${u}_${l}`;
      lessonIds.push(lid);
      lessons.push({ id: lid, title: l === 2 ? "事务隔离级别与并发异常：脏读、不可重复读、幻读的判定（长中文讲次标题）" : `第${u + 1}单元第${l + 1}讲` });
    }
    units.push({ id: `unit_${u}`, title: `单元${u + 1}：主题划分（长单元标题观察）`, lessons });
  }
  const prerequisites = [
    { from: lessonIds[0], to: lessonIds[5] },
    { from: lessonIds[5], to: lessonIds[9] },
  ];
  return { units, prerequisites };
}

function checkNoOverflow(label, elements, positions) {
  // 预换行后的每行宽度必须 ≤ 节点内宽（度量模型下）
  let worst = 0;
  for (const el of elements) {
    if (el.type !== "text") continue;
    for (const line of el.text.split("\n")) {
      worst = Math.max(worst, measureTextWidth(line, el.fontSize));
    }
  }
  const maxInner = Math.max(
    LAYOUT.STAGE_W, LAYOUT.COURSE_W, LAYOUT.UNIT_W, LAYOUT.LESSON_W
  ) - 2 * LAYOUT.NODE_INNER_PAD;
  return { label, worstTextWidth: Math.ceil(worst), maxInner, ok: worst <= maxInner, positions: Object.keys(positions).length };
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

const outDir = join(tmpdir(), "slice3-font-spike");
mkdirSync(outDir, { recursive: true });

const inventory = fontInventory();
const route = syntheticRoute();
const outline = syntheticOutline();
const routeScene = layoutRoute(route);
const outlineScene = layoutOutline(outline);

const checks = [
  checkNoOverflow("route", routeScene.elements, routeScene.positions),
  checkNoOverflow("outline", outlineScene.elements, outlineScene.positions),
];

const constants = { layout: LAYOUT, inventory, checks };
writeFileSync(join(outDir, "constants.json"), JSON.stringify(constants, null, 1));
writeFileSync(join(outDir, "scene-sample.json"), JSON.stringify({ route: routeScene, outline: outlineScene }, null, 1));

// 演示页（本地临时文件，不提交仓库）：把场景样本交给 restore/Excalidraw 渲染
const demo = `<!doctype html>
<html><head><meta charset="utf-8"><title>slice3 font spike demo</title></head>
<body style="margin:0">
<div id="app" style="height:100vh"></div>
<script type="module">
  import { Excalidraw } from "${new URL("../frontend/node_modules/@excalidraw/excalidraw/dist/prod/index.js", import.meta.url).href.replace(/\\\\/g, "/")}";
  import React from "${new URL("../frontend/node_modules/react/umd/react.production.min.js", import.meta.url).href.replace(/\\\\/g, "/")}";
  import { createRoot } from "${new URL("../frontend/node_modules/react-dom/umd/react-dom.production.min.js", import.meta.url).href.replace(/\\\\/g, "/")}";
  const sample = ${JSON.stringify({ route: routeScene, outline: outlineScene })};
  const app = { elements: [...sample.route.elements, ...sample.outline.elements].map(e => ({ ...e, isDeleted: false })),
    appState: { viewBackgroundColor: "#ffffff" } };
  function Root() {
    const [data, setData] = React.useState(app);
    return React.createElement(Excalidraw, {
      initialData: app,
      viewModeEnabled: true,
      gridModeEnabled: false,
      onChange: (elements) => setData((d) => ({ ...d, elements })),
    });
  }
  createRoot(document.getElementById("app")).render(React.createElement(Root));
</script>
</body></html>`;
writeFileSync(join(outDir, "demo.html"), demo);

console.log("== slice3 font spike ==");
console.log(`fonts: ${inventory.fonts.map((f) => `${f.family}(${f.files} files, ${f.license})`).join("; ")}`);
for (const c of checks) {
  console.log(`layout[${c.label}]: nodes=${c.positions}, worstTextWidth=${c.worstTextWidth} <= maxInner=${c.maxInner} -> ${c.ok ? "OK" : "OVERFLOW"}`);
}
console.log(`outputs in ${outDir} (constants.json / scene-sample.json / demo.html)`);
