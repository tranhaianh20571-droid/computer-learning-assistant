// Synthetic API fixtures; this script never accesses production accounts or providers.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import { mockBuiltFrontend } from "./frontend_dist_mock.mjs";

const { chromium } = createRequire(import.meta.url)(process.env.PW_ROOT + "/playwright-core");
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
const output = process.env.TEST_OUTPUT || ".backup/planning-presentation";
await mkdir(output, { recursive: true });
const checks = [];
try {
  for (const width of [1440, 390, 320]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, reducedMotion: "reduce" });
    const page = await context.newPage(), errors = [];
    page.on("pageerror", error => errors.push(error.message));
    page.setDefaultTimeout(10000);
    const served = await mockBuiltFrontend(page);
    let mode = "intake";
    const scene = { elements: [
      { id: "root", type: "rectangle", x: 0, y: 0, width: 220, height: 70,
        customData: { role: "node", path: "route_root", business_id: "root", title: "合成学习路线", kind: "route" } },
      { id: "course", type: "rectangle", x: 300, y: 0, width: 220, height: 70,
        customData: { role: "node", path: "stage_0course_0", business_id: "course", title: "合成基础课程", kind: "course" } },
      { id: "link", type: "line", x: 0, y: 0, customData: { parent_id: "root", child_id: "course" } },
    ] };
    const task = () => ({
      task_id: "synthetic-plan", target_id: "synthetic-route", target_kind: "route", title: "合成学习路线",
      generation: 1, task_status: mode === "retry" || mode === "generating" ? "running" : mode === "failed" ? "failed" : "partial",
      wait_state: mode === "intake" || mode === "intake-board" ? "awaiting_intake" : null, confirmed: false,
      draft_id: ["intake", "generating", "failed"].includes(mode) ? null : "synthetic-draft",
      auto_retry_pending: mode === "retry", auto_retry_count: mode === "retry" ? 2 : mode === "failed" ? 5 : 0,
      auto_retry_limit: 5, auto_retry_exhausted: mode === "failed", error_code: mode === "failed" ? "MODEL_TIMEOUT" : null,
      intake: { schema_version: 3, shown_count: 4, questions: Array.from({ length: 4 }, (_, index) => ({
        question_id: `q${index}`, question: `合成学习问题 ${index}`, kind: "single", skippable: true,
        options: [{ key: "a", label: "先理解原理" }, { key: "b", label: "先实践" }],
      })), answers: [] },
    });
    await page.route("**/api/**", async route => {
      const p = new URL(route.request().url()).pathname;
      const json = body => route.fulfill({ json: body });
      if (p === "/api/auth/me") return json({ user_id: "synthetic-owner", status: "approved", is_admin: false });
      if (p === "/api/navigation") return json({});
      if (p === "/api/planning/current") return json({ planning: task() });
      if (p === "/api/planning/tasks/synthetic-plan") return json(task());
      if (p.endsWith("/resume")) return json(task());
      if (p.endsWith("/process")) return json({ process: { ...task(), sequence: 1,
        steps: [{ key: "structure", label: "整理课程结构", status: mode === "retry" ? "running" : "succeeded" }] } });
      if (p.endsWith("/synthetic-draft/scene")) return json(scene);
      if (p === "/api/planning/drafts/synthetic-draft") return json({
        draft_id: "synthetic-draft", target_id: "synthetic-route", draft_kind: "route",
        revision: 1, saved_state: "saved", content: { stages: [] }, locked: false,
        review: { verdict: "pass", acknowledgement_required: false },
        coverage: { coverage_ratio: 1 }, scope_contract: { assumptions: ["Synthetic assumption"] },
      });
      if (p.includes("/viewstates/")) return json({ canvas: {} });
      if (p === "/api/mentor-preference") return json({ version: 1, card_id: null, revision: null });
      if (p === "/api/cards" || p === "/api/card-market") return json({ data: [{
        card_id: "synthetic-card", revision: 1, editable: true, collected: false, published_revision: 1,
        profile: { name: "合成导师", identity: "合成身份", tags: [] }, assets: [],
      }] });
      if (["/api/materials", "/api/connectors", "/api/capabilities/configs"].includes(p)) return json({ data: [] });
      throw Error("Unexpected synthetic endpoint: " + p);
    });
    await page.goto(served.origin + "/#/home");
    await page.getByRole("button", { name: /继续规划/ }).click();
    await page.locator(".planning-intake-questions").waitFor();
    assert.equal(await page.getByRole("heading", { name: "课程结构草稿" }).count(), 0);
    assert.equal(await page.getByLabel("路线标题").count(), 0, "Existing task does not duplicate the topic input");
    const preview = page.getByRole("complementary", { name: "课程结构预览" });
    assert.equal(await preview.count(), 0);
    await page.screenshot({ path: `${output}/intake-${width}.png`, fullPage: true });
    mode = "generating"; await page.reload();
    assert.equal(await preview.count(), 0);
    await page.getByRole("region", { name: "规划生成过程" }).waitFor();
    mode = "intake-board"; await page.reload();
    await page.locator(".planning-intake-questions").waitFor();
    assert.equal(await preview.count(), 0);
    assert.equal(await page.locator(".mindmap-svg").count(), 0);
    mode = "retry"; await page.reload();
    await page.getByLabel("自动重试 2/5", { exact: true }).waitFor();
    await page.locator(".mindmap-svg").waitFor();
    assert.equal(await page.getByLabel("规划体检", { exact: true }).count(), 0);
    assert.equal(await page.getByText(/覆盖区域|规划假设/).count(), 0);
    assert.equal(await page.getByText("后台自动重试 2/5", { exact: true }).count(), 0);
    await page.screenshot({ path: `${output}/retry-${width}.png`, fullPage: true });
    mode = "failed"; await page.reload();
    await page.getByRole("status", { name: "规划需要处理" }).waitFor();
    assert.equal(await page.getByRole("dialog").count(), 0, "Recovery stays inline");
    await page.getByLabel("自动重试 5/5", { exact: true }).waitFor();
    await page.goto(served.origin + "/#/cards");
    await page.getByRole("button", { name: "查看 合成导师" }).waitFor();
    assert.equal(await page.getByRole("button", { name: /收录/ }).count(), 0, "My characters has no bookmark control");
    await page.goto(served.origin + "/#/card-market");
    await page.getByRole("button", { name: "收录人物", exact: true }).waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.deepEqual(errors, []);
    checks.push({ width, intakePreviewRemoved: true, preservedMap: true, inlineRetry: true, inlineFailure: true, collectionScope: true });
    await context.close();
  }
  console.log(JSON.stringify({ checks, result: "passed", providers: "synthetic-only" }));
} finally {
  await browser.close();
}
