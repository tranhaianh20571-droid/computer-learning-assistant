// Production UI, synthetic intercepted APIs only; no production user or model calls.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import path from "node:path";
import { mockBuiltFrontend } from "./frontend_dist_mock.mjs";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PW_ROOT + "/playwright-core");
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: "reduce" });
const page = await context.newPage();
page.setDefaultTimeout(12000);
const output = process.env.TEST_OUTPUT || ".backup/planning-settings-updates";
await mkdir(output, { recursive: true });
const errors = [], feedback = [], tested = [], requests = [];
page.on("pageerror", error => errors.push(error.message));
const served = await mockBuiltFrontend(page);
let board = false, legacySearch = false, turns = [], revision = 1, answerRevision = 0;
let answers = [], changed = false;
const questions = Array.from({ length: 4 }, (_, i) => ({ question_id: "q" + i,
  question: "合成学习问题 " + i, kind: i === 1 ? "multi" : "single", skippable: true,
  options: [{ key: "a", label: "合成选项甲 " + i }, { key: "b", label: "合成选项乙 " + i }] }));
const task = () => ({ task_id: "synthetic-plan", generation: 1, target_kind: "route",
  target_id: "synthetic-route", title: "合成规划", task_status: board ? "succeeded" : "partial",
  confirmed: false, wait_state: board ? null : "awaiting_intake",
  draft_id: board ? "synthetic-draft" : null, answer_draft: answers, answer_draft_revision: answerRevision,
  intake: { schema_version: 3, questions, shown_count: 4,
    answers: board ? Array.from({ length: 24 }, (_, i) => ({ question: "合成访谈记录 " + i, labels: ["合成回答"] })) : [] } });
const draft = () => ({ draft_id: "synthetic-draft", revision, target_id: "synthetic-route",
  draft_kind: "route", content: { stages: [] }, saved_state: "saved", locked: false });
const scene = (candidate = false) => ({ elements: [
  { id: "node_root", type: "rectangle", x: 20, y: 20, width: 230, height: 75,
    customData: { role: "node", path: "route_root", business_id: "root", title: "合成学习路线", kind: "route" } },
  { id: "node_course", type: "rectangle", x: 330, y: 20, width: 230, height: 75,
    customData: { role: "node", path: "stage_0course_0", business_id: "course", title: changed || candidate ? "合成更新课程" : "合成基础课程", kind: "course" } },
  { id: "link", type: "line", x: 0, y: 0, customData: { parent_id: "root", child_id: "course" } },
] });
await page.route("**/api/**", async route => {
  const request = route.request(), url = new URL(request.url()), p = url.pathname, method = request.method();
  requests.push(p);
  const json = (body, status = 200) => route.fulfill({ json: body, status });
  if (p === "/api/auth/me") return json({ user_id: "synthetic-owner", email: "owner@example.test", status: "approved", is_admin: false });
  if (p === "/api/navigation") return json({});
  if (["/api/materials", "/api/connectors"].includes(p)) return json({ data: [] });
  if (p === "/api/cards") return json({ data: [{ card_id: "synthetic-card", revision: 1, profile: { name: "合成导师" } }] });
  if (p === "/api/memory/settings") return json({ enabled: true, revision: 1 });
  if (p === "/api/memory/profile") return json({ data: { items: [{ id: "profile", content: "合成偏好：从实例理解概念。" }] } });
  if (p === "/api/memory/characters/synthetic-card") return json({ data: { items: [] } });
  if (p.endsWith("/feedback")) { feedback.push(request.postDataJSON()); return json({ status: "pending" }); }
  if (p === "/api/capabilities/configs") return json({ data: [{
    config_id: "synthetic-config", kind: "content_model", protocol: "openai", owner_scope: "user",
    endpoint: "https://model.synthetic.test/v1", model_name: "synthetic-visible-model",
    credential_mask: "********", capability_status: {}, config_version: 1, is_active: true,
  }] });
  if (p.endsWith("/synthetic-config/test")) { tested.push(p); return json({ capability_status: { text: "available" } }); }
  if (p.endsWith("/reasoning/describe")) return json({ source: "unknown", supported_modes: ["default"],
    effort_values: [], summary_supported: false, budget: null });
  if (p === "/api/planning/current") return json({ planning: task() });
  if (p === "/api/planning/tasks/synthetic-plan") return json(task());
  if (p.endsWith("/answer-draft")) { answers = request.postDataJSON().answers; answerRevision++; return json({ answers, revision: answerRevision }); }
  if (p.endsWith("/process")) return json({ process: {
    ...task(), task_status: "succeeded", sequence: 1,
    steps: Array.from({ length: 20 }, (_, i) => ({ key: "step" + i, label: "合成生成阶段 " + i, status: "succeeded" })),
    search: { status: "succeeded", result_count: 10, sources: legacySearch ? [] : [{ title: "合成参考资料", url: "https://reference.synthetic.test", domain: "reference.synthetic.test", used: true }] },
    thinking: { show_summary: true, available: true, text: "synthetic-private-thinking-must-not-render" },
  } });
  if (p === "/api/planning/drafts/synthetic-draft") return json(draft());
  if (p.endsWith("/synthetic-draft/scene")) return json(scene());
  if (p.includes("/viewstates/")) return json({ canvas: {} });
  if (p.endsWith("/synthetic-draft/dialogue") && method === "POST") {
    turns = [{ task_id: "synthetic-turn", content: request.postDataJSON().content, status: "succeeded",
      decision: "pending", current: true, result: { answer: "已按要求准备修改。",
        candidate: {}, changes: [{ node_ref: "course", action: "updated", title: "合成更新课程" }] }, scene: scene(true) }];
    return json({ task_id: "synthetic-turn" }, 202);
  }
  if (p.endsWith("/synthetic-draft/dialogue")) return json({ turns, draft: draft() });
  if (p.endsWith("/synthetic-turn/adopt")) { changed = true; revision++; turns[0].decision = "adopted"; return json({ draft: draft() }); }
  throw Error("Unexpected synthetic API " + method + " " + p);
});
try {
  await page.goto(served.origin + "/#/home");
  const nav = page.getByRole("navigation", { name: "主导航" });
  assert.equal(await nav.getByRole("button", { name: "课程规划", exact: true }).count(), 0);
  assert.equal(await nav.getByRole("button", { name: "个人记忆", exact: true }).count(), 0);
  await page.getByRole("button", { name: /继续规划/ }).click();
  await page.locator(".q-card").nth(3).waitFor();
  const first = page.locator(".q-card").first();
  const firstOption = first.getByRole("button", { name: "合成选项甲 0", exact: true });
  await firstOption.click(); await firstOption.click();
  assert.equal(await firstOption.getAttribute("aria-pressed"), "false");
  await firstOption.click();
  const other = first.getByRole("button", { name: "都不太符合？自己补充" });
  await other.click(); assert.equal(await firstOption.getAttribute("aria-pressed"), "false");
  await first.getByRole("textbox").fill("合成补充");
  await firstOption.click(); assert.equal(await first.getByRole("textbox").count(), 0);
  await other.click(); await other.click(); assert.equal(await first.getByRole("textbox").count(), 0);
  const multi = page.locator(".q-card").nth(1);
  await multi.getByRole("button", { name: "合成选项甲 1", exact: true }).click();
  await multi.getByRole("button", { name: "都不太符合？自己补充" }).click();
  await multi.getByRole("button", { name: "合成选项甲 1", exact: true }).click();
  assert.equal(await multi.getByRole("button", { name: "合成选项甲 1", exact: true }).getAttribute("aria-pressed"), "false");
  board = true; await page.reload();
  const confirm = page.getByRole("button", { name: "没问题，确认草稿" });
  await confirm.waitFor();
  await page.getByRole("button", { name: "展开 ⌄", exact: true }).click();
  assert.equal(await page.locator(".generation-thinking").count(), 0);
  assert.equal(await page.getByText("synthetic-private-thinking-must-not-render").count(), 0);
  for (const width of [1440, 1100]) {
    await page.setViewportSize({ width, height: 900 });
    const bounds = await confirm.boundingBox();
    assert(bounds && bounds.y >= 0 && bounds.y + bounds.height <= 900, "Confirmation stays in viewport");
    await page.screenshot({ path: path.join(output, "expanded-" + width + ".png"), fullPage: true });
  }
  await page.getByRole("button", { name: "我想修改讲解结构" }).click();
  await page.getByLabel("规划修改要求").fill("请修改第一门课程");
  await page.getByRole("button", { name: "发送修改要求" }).click();
  await page.getByRole("button", { name: "采用修改", exact: true }).click();
  await page.getByText("已采用到草稿，仍需最终确认。").waitFor();
  await page.locator(".mindmap-svg tspan").filter({ hasText: "合成更新课程" }).waitFor();
  legacySearch = true;
  await page.reload();
  await page.getByRole("button", { name: "展开 ⌄", exact: true }).click();
  await page.getByText("搜索已完成，找到 10 条结果，当前没有可展示的来源列表。", { exact: true }).waitFor();
  if (!process.env.TEST_PLANNING_ONLY) {
  await page.goto(served.origin + "/#/capabilities");
  const tabs = page.getByRole("navigation", { name: "设置分类" });
  assert.equal(await tabs.getByRole("button", { name: "已保存配置", exact: true }).count(), 0);
  await page.getByText("https://model.synthetic.test/v1 · synthetic-visible-model", { exact: true }).waitFor();
  assert.equal(await page.getByRole("button", { name: "测试 synthetic-visible-model", exact: true }).getAttribute("title"), "测试");
  await page.getByRole("button", { name: "测试 synthetic-visible-model", exact: true }).click();
  assert.equal(tested.length, 1);
  await tabs.getByRole("button", { name: "个人记忆", exact: true }).click();
  await page.getByRole("heading", { name: "个人记忆", exact: true }).waitFor();
  assert.equal(await page.getByRole("heading", { name: "记忆来源" }).count(), 0);
  await page.getByLabel("人物", { exact: true }).selectOption("synthetic-card");
  assert.equal(await page.getByLabel("表达方面", { exact: true }).locator("option").count(), 14);
  assert.equal(await page.getByLabel("反馈", { exact: true }).locator("option").count(), 10);
  await page.getByLabel("表达方面", { exact: true }).selectOption("persona");
  await page.getByLabel("反馈", { exact: true }).selectOption("warmer");
  await page.getByRole("button", { name: "保存反馈" }).click();
  await page.waitForFunction(() => ![...document.querySelectorAll("button")].some(b => b.textContent === "保存反馈" && b.disabled));
  assert.equal(feedback[0].aspect, "persona"); assert.equal(feedback[0].preference, "warmer");
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 900 });
    if (width < 1024) await page.waitForFunction(() => document.querySelector(".side").getBoundingClientRect().right <= 1);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: path.join(output, "memory-" + width + ".png"), fullPage: true });
  }
  assert(!requests.includes("/api/memory/sources"));
  }
  assert.deepEqual(errors, []); assert.deepEqual(served.unexpected, []);
  console.log(process.env.TEST_PLANNING_ONLY
    ? "PASS home recovery/navigation, single/multi/custom toggles, expanded progress/footer, AI candidate/adoption, saved search sources (planning-only)."
    : "PASS home recovery/navigation, single/multi/custom toggles, expanded progress/footer, AI candidate/adoption, inline config/test icon, personal memory/expanded feedback, desktop/mobile.");
} finally { await context.close(); await browser.close(); }
