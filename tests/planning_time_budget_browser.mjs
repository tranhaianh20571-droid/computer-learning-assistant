// Synthetic business API responses; production build assets and real browser rendering.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import { mockBuiltFrontend } from "./frontend_dist_mock.mjs";

const { chromium } = createRequire(import.meta.url)(process.env.PW_ROOT + "/playwright-core");
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
const output = process.env.TEST_OUTPUT || ".backup/planning-time-budget";
await mkdir(output, { recursive: true });
const checks = [], pageErrors = [];
try {
  for (const width of [1440, 390, 320]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, reducedMotion: "reduce" });
    const page = await context.newPage();
    page.on("pageerror", error => pageErrors.push(error.message));
    const served = await mockBuiltFrontend(page);
    let created = false, submitted = null, draft = [], revision = 0;
    const questions = [
      ...Array.from({ length: 4 }, (_, i) => ({
        question_id: `q${i}`, question: `合成学习问题 ${i}`, kind: "single", skippable: true,
        options: [{ key: "a", label: "合成基础情况" }, { key: "b", label: "合成实践情况" }],
      })),
      { question_id: "learning_time_budget", question: "你计划学多久，每周能投入多少时间？",
        kind: "time_budget", purpose: "constraints", options: [], skippable: false },
    ];
    const task = () => ({
      task_id: "synthetic-time-task", target_id: "synthetic-time-route", target_kind: "route",
      title: "合成时间规划", task_status: submitted ? "succeeded" : "partial", generation: 1,
      wait_state: submitted ? null : "awaiting_intake", confirmed: false, draft_id: null,
      answer_draft: draft, answer_draft_revision: revision,
      intake: { schema_version: 3, shown_count: 5, min_questions: 5, max_questions: 7,
        questions: submitted ? [] : questions, answers: submitted || [], done: !!submitted },
    });
    await page.route("**/api/**", route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      if (path === "/api/auth/me") return route.fulfill({ json: { user_id: "time-synthetic", status: "approved", email: "" } });
      if (path === "/api/navigation") return route.fulfill({ json: {} });
      if (path === "/api/planning/current") return route.fulfill({ json: { planning: created ? task() : null } });
      if (path === "/api/planning/input-matches") return route.fulfill({ json: { matches: [] } });
      if (path === "/api/planning/routes") return route.fulfill({ json: { route_id: "synthetic-time-route" } });
      if (path === "/api/planning/tasks" && request.method() === "POST") {
        assert.equal(request.postDataJSON().constraints?.time_budget, undefined);
        created = true;
        return route.fulfill({ json: { task_id: "synthetic-time-task" } });
      }
      if (path.endsWith("/answer-draft")) {
        const body = request.postDataJSON();
        assert.equal(body.revision, revision);
        draft = body.answers; revision++;
        return route.fulfill({ json: { revision, answers: draft } });
      }
      if (path.endsWith("/decision")) {
        submitted = request.postDataJSON().answers;
        const budget = submitted.find(a => a.question_id === "learning_time_budget");
        assert.deepEqual(budget.time_budget, { weeks: 12, hours_per_week: 3.5 });
        assert.equal(budget.skipped, false);
        return route.fulfill({ json: task() });
      }
      if (path.endsWith("/synthetic-time-task") || path.endsWith("/resume")) return route.fulfill({ json: task() });
      if (path.endsWith("/process")) return route.fulfill({ json: { process: { sequence: 1, steps: [] } } });
      return route.fulfill({ json: { data: [] } });
    });
    await page.goto(served.origin + "/#/home");
    await page.getByLabel("学习目标", { exact: true }).fill("合成学习目标");
    assert.equal(await page.getByLabel("学习周期（周）", { exact: true }).count(), 0);
    assert.equal(await page.getByLabel("每周投入（小时）", { exact: true }).count(), 0);
    await page.getByRole("button", { name: "生成规划草稿", exact: true }).click();
    await page.locator(".q-card").nth(4).waitFor();
    assert.equal(await page.locator(".q-card").count(), 5);
    assert.equal(await page.getByText(/共 5～7 个/).count(), 1);
    const time = page.locator(".q-card").last();
    assert.equal(await time.getByRole("button", { name: "跳过这题" }).count(), 0);
    await page.getByRole("button", { name: "提交并继续", exact: true }).click();
    await page.getByText("请填写学习周期（1～104周）和每周投入（0.5～80小时）。", { exact: true }).waitFor();
    assert.equal(submitted, null);
    const weeks = time.getByRole("group", { name: "学习周期（周）", exact: true });
    const hours = time.getByRole("group", { name: "每周投入（小时）", exact: true });
    await weeks.getByRole("button", { name: "8周", exact: true }).click();
    await hours.getByRole("button", { name: "5小时", exact: true }).click();
    assert.equal(await weeks.getByRole("button", { name: "8周", exact: true }).getAttribute("aria-pressed"), "true");
    await page.waitForFunction(() => [...document.querySelectorAll('[role="status"]')].some(e => e.textContent.includes("回答草稿已保存")));
    await page.reload();
    assert.equal(await weeks.getByRole("button", { name: "8周", exact: true }).getAttribute("aria-pressed"), "true");
    assert.equal(await hours.getByRole("button", { name: "5小时", exact: true }).getAttribute("aria-pressed"), "true");
    await weeks.getByRole("button", { name: "都不太符合？自己补充", exact: true }).click();
    await hours.getByRole("button", { name: "都不太符合？自己补充", exact: true }).click();
    await weeks.getByRole("spinbutton", { name: "学习周期（周）", exact: true }).fill("12");
    await hours.getByRole("spinbutton", { name: "每周投入（小时）", exact: true }).fill("3.5");
    await page.waitForFunction(() => [...document.querySelectorAll('[role="status"]')].some(e => e.textContent.includes("回答草稿已保存")));
    await page.reload();
    await weeks.getByRole("spinbutton", { name: "学习周期（周）", exact: true }).waitFor();
    assert.equal(await weeks.getByRole("spinbutton", { name: "学习周期（周）", exact: true }).inputValue(), "12");
    assert.equal(await hours.getByRole("spinbutton", { name: "每周投入（小时）", exact: true }).inputValue(), "3.5");
    const overflow = await page.evaluate(() => ({
      width:innerWidth, scroll:document.documentElement.scrollWidth,
      elements:[...document.querySelectorAll("fieldset,input,label,.planning-intake-layout")]
        .filter(e=>e.getBoundingClientRect().right>innerWidth)
        .map(e=>({tag:e.tagName,cls:e.className,right:e.getBoundingClientRect().right})),
    }));
    assert.ok(overflow.scroll <= width, JSON.stringify(overflow));
    await page.locator(".q-card").last().screenshot({ path: `${output}/time-question-${width}.png` });
    await page.getByRole("button", { name: "提交并继续", exact: true }).click();
    await page.waitForFunction(() => !document.querySelector(".q-card"));
    assert.equal(submitted.length, 5);
    checks.push({ width, homepageRemoved: true, required: true, presetsAndCustom: true, refresh: true, structuredSubmission: true });
    await context.close();
  }
  assert.deepEqual(pageErrors, []);
  console.log(JSON.stringify({ checks, pageErrors, providers: "synthetic-only" }));
} finally {
  await browser.close();
}
