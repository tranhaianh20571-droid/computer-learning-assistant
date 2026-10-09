// Real frontend + isolated backend; no model calls, credential fixtures or user-data mutations.
// PW_ROOT, TEST_PYTHON and PYTHONPATH supply already installed local tooling.
import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import assert from "node:assert/strict";
const require = createRequire(import.meta.url);
const { chromium } = require(resolve(process.env.PW_ROOT, "playwright"));
const base = process.env.TEST_FRONTEND || "http://127.0.0.1:5173";
const backend = process.env.TEST_BACKEND || "http://127.0.0.1:8001";
assert.equal(new URL(backend).hostname, "127.0.0.1");
assert.notEqual(new URL(backend).port, "8000");
const output = resolve(process.env.TEST_OUTPUT || ".backup/course-library-browser");
mkdirSync(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const guest = await context.request.get(`${backend}/api/auth/guest`);
assert.equal(guest.status(), 200);
const user = await guest.json();
const seeded = spawnSync(process.env.TEST_PYTHON, ["tests/course_library_browser_fixture.py", user.user_id], {
  encoding: "utf8", env: process.env,
});
assert.equal(seeded.status, 0, seeded.stderr);
const fixture = JSON.parse(seeded.stdout);
const page = await context.newPage();
const checks = [];
const errors = [];
page.on("pageerror", e => errors.push(e.message));
await page.route("**/*", async route => {
  const url = new URL(route.request().url());
  if (url.hostname !== "127.0.0.1" && url.hostname !== "localhost") return route.abort();
  if (url.pathname.startsWith("/api/")) {
    const response = await route.fetch({ url: backend + url.pathname + url.search });
    return route.fulfill({ response });
  }
  return route.continue();
});
try {
  await page.goto(`${base}/#/courses`);
  await page.locator(".ccard").first().waitFor();
  assert.equal(await page.locator(".ccard").count(), 2);
  assert.equal(await page.locator(".ccard h2").filter({ hasText: "合成操作系统" }).count(), 0);
  checks.push("grouped_library_and_independent_course");
  await page.getByRole("searchbox", { name: "搜索课程" }).fill("操作系统");
  assert.equal(await page.locator(".ccard").count(), 1);
  await page.getByRole("link", { name: "进入课程", exact: true }).click();
  await page.waitForURL("**/#/route/**");
  await page.locator(".crs > li").nth(1).waitFor();
  await page.getByRole("heading", { name: "合成浏览器后端", exact: true }).waitFor();
  assert.equal(await page.locator(".crs > li").count(), 2);
  assert.equal(await page.locator(".library-match").count(), 1);
  await page.locator(".crs > li").filter({ hasText: "合成操作系统" }).getByRole("link", { name: "进入课程", exact: true }).click();
  await page.getByRole("button", { name: "返回课程合集" }).waitFor();
  await page.reload();
  await page.getByRole("button", { name: "返回课程合集" }).click();
  await page.getByRole("heading", { name: "合成浏览器后端", exact: true }).waitFor();
  checks.push("search_child_highlight_and_refresh_return");
  await page.goto(`${base}/#/courses`);
  await page.getByRole("searchbox", { name: "搜索课程" }).fill("");
  await page.locator(".ccard").filter({ hasText: "合成独立 Python" }).getByRole("link", { name: "进入课程", exact: true }).click();
  await page.getByRole("button", { name: "返回我的课程" }).waitFor();
  checks.push("independent_course_direct_entry");
  await page.evaluate(task => localStorage.setItem("slice3.lastPlanningTask", task), fixture.task_id);
  await page.goto(`${base}/#/planning`);
  await page.getByRole("button", { name: "没问题，确认草稿" }).click();
  await page.getByRole("dialog", { name: "已有相同课程" }).waitFor();
  await page.getByRole("button", { name: "返回修改", exact: true }).click();
  await page.getByRole("button", { name: "没问题，确认草稿" }).click();
  await page.getByRole("dialog", { name: "已有相同课程" }).waitFor();
  await page.getByRole("button", { name: "查看已有课程", exact: true }).click();
  await page.getByRole("heading", { name: "合成浏览器后端", exact: true }).waitFor();
  checks.push("duplicate_dialog_edit_and_view_existing");
  await page.goto(`${base}/#/planning`);
  await page.getByRole("button", { name: "没问题，确认草稿" }).click();
  await page.getByRole("dialog", { name: "已有相同课程" }).waitFor();
  await page.screenshot({ path: resolve(output, "duplicate-dialog.png") });
  await page.getByRole("button", { name: "仍然创建", exact: true }).click();
  await page.getByText("已确认为不可变版本，导图与总览同版。", { exact: true }).waitFor();
  await page.goto(`${base}/#/courses`);
  await page.locator(".ccard").nth(2).waitFor();
  assert.equal(await page.locator(".ccard").count(), 3);
  checks.push("explicit_duplicate_copy");
  await page.goto(`${base}/#/home`);
  await page.getByLabel("学习目标", { exact: true }).fill("合成浏览器学习目标");
  await page.getByRole("button", { name: "生成规划草稿", exact: true }).click();
  await page.getByRole("dialog", { name: "可能已有相同规划" }).waitFor();
  await page.getByRole("button", { name: "返回修改", exact: true }).click();
  checks.push("early_input_reminder");
  await page.goto(`${base}/#/courses`);
  await page.locator(".ccard").nth(2).waitFor();
  await page.screenshot({ path: resolve(output, "library-desktop.png") });
  await page.setViewportSize({ width: 360, height: 800 });
  await page.waitForFunction(() => document.querySelector(".side").getBoundingClientRect().right <= 1);
  await page.screenshot({ path: resolve(output, "library-mobile.png") });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  checks.push("mobile_no_horizontal_overflow");
  assert.deepEqual(errors, []);
  writeFileSync(resolve(output, "result.json"), JSON.stringify({ checks, pageErrors: errors, synthetic: true }, null, 2));
  console.log(JSON.stringify({ passed: checks.length, checks, pageErrors: errors }));
} catch (error) {
  await page.screenshot({ path: resolve(output, "failure.png") }).catch(() => {});
  console.log(JSON.stringify({ failedAfter: checks, error: error.name,
    assertion: error.code === "ERR_ASSERTION" ? { actual: error.actual, expected: error.expected } : undefined }));
  process.exitCode = 1;
} finally {
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await browser.close();
}
