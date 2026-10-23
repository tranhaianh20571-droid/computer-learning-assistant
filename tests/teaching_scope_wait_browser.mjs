import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import { mockBuiltFrontend } from "./frontend_dist_mock.mjs";

const { chromium } = createRequire(import.meta.url)(process.env.PW_ROOT + "/playwright-core");
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
const output = ".backup/teaching-scope-wait-browser";
await mkdir(output, { recursive: true });
const checks = [];
try {
  for (const width of [1280, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 800 }, serviceWorkers: "block" });
    const page = await context.newPage(), errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const served = await mockBuiltFrontend(page);
    let starts = 0;
    let workspace = {
      conversation_id: "synthetic-scope-wait", kind: "teaching", course_id: "course-a", lesson_id: "lesson-a",
      lesson_title: "合成考纲课堂", generation: 1, session_state: "active", server_managed: true,
      audio_mode: "text", tts_authorized: false, steps: [], followups: [], queued_tasks: [],
      authorization_required: false, needs_upgrade: false,
      scope_preparation: { status: "running", error_code: null }, last_checkpoint: null
    };
    const json = (route, body) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    await page.route("**/api/**", route => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === "/api/auth/me") return json(route, { user_id: "synthetic-owner", status: "approved" });
      if (pathname === "/api/teaching/synthetic-scope-wait") return json(route, workspace);
      if (pathname === "/api/courses/course-a/lessons/lesson-a/start") {
        starts++;
        workspace = { ...workspace, scope_preparation: { status: "queued", error_code: null } };
        return json(route, { conversation_id: workspace.conversation_id, task_id: "existing-first-task" });
      }
      if (pathname.endsWith("/checkpoint")) return json(route, { saved: true });
      return json(route, { data: [] });
    });
    await page.goto(served.origin + "/#/lecture/synthetic-scope-wait");
    await page.getByText("正在准备本单元考纲，完成后继续讲解…", { exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "恢复讲解", exact: true }).count(), 0);
    assert.equal(await page.getByRole("button", { name: "重试准备考纲", exact: true }).count(), 0);
    assert.equal(starts, 0);
    checks.push(`pending-scope-never-reports-model-unavailable-${width}`);
    workspace = { ...workspace, scope_preparation: { status: "failed", error_code: "MODEL_PROVIDER_ERROR" } };
    const retry = page.getByRole("button", { name: "重试准备考纲", exact: true });
    await retry.waitFor();
    await retry.click();
    await page.getByText("正在准备本单元考纲，完成后继续讲解…", { exact: true }).waitFor();
    assert.equal(starts, 1);
    assert.equal(new URL(page.url()).hash, "#/lecture/synthetic-scope-wait");
    checks.push(`scope-retry-retains-current-classroom-${width}`);
    workspace = { ...workspace, scope_preparation: { status: "succeeded", error_code: null } };
    await page.getByText("单元考纲已就绪，正在准备首段讲解…", { exact: true }).waitFor();
    assert(!/讲解配置暂不可用/.test(await page.locator("body").innerText()));
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: `${output}/scope-ready-${width}.png` });
    assert.deepEqual(errors, []);
    assert.deepEqual(served.unexpected, []);
    await context.close();
  }
  console.log(JSON.stringify({ passed: checks.length, checks, synthetic: true }));
} finally { await browser.close(); }
