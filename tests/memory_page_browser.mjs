// Production build, intercepted synthetic APIs. No real memory mutations or providers.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import path from "node:path";
import { mockBuiltFrontend } from "./frontend_dist_mock.mjs";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PW_ROOT + "/playwright-core");
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
const page = await context.newPage();
page.setDefaultTimeout(10000);
const output = process.env.TEST_OUTPUT || ".backup/memory-page";
await mkdir(output, { recursive: true });
const errors = [], feedback = [], changes = [];
page.on("pageerror", error => errors.push(error.message));
const served = await mockBuiltFrontend(page);
let settings = { enabled: true, revision: 3 };
let failSave = false, failReset = false, cleared = false, profileReads = 0, resetCalls = 0;
let profileUnavailable = false;
const profile = [
  "## 用户身份\n合成身份正文。",
  "## 背景基础\n合成基础第一段。\n\n合成基础第二段。",
  "## 用户偏好\n合成偏好正文。",
  "## 能力与困难\n合成困难正文。",
  "## 其他证据\n<img src=x onerror=alert(1)>",
].join("\n\n");
await page.route("**/api/**", async route => {
  const request = route.request(), p = new URL(request.url()).pathname, method = request.method();
  const json = (body, status = 200) => route.fulfill({ json: body, status });
  if (p === "/api/auth/me") return json({ user_id: "synthetic-memory-owner", name: "合成用户", is_admin: false, status: "approved" });
  if (p === "/api/navigation") return json({});
  if (["/api/capabilities/configs", "/api/capabilities/model-roles", "/api/connectors"].includes(p)) return json({ data: [] });
  if (p === "/api/cards") return json({ data: [{ card_id: "synthetic-card", revision: 7, profile: { name: "合成导师" } }] });
  if (p === "/api/memory/settings" && method === "GET") return json(settings);
  if (p === "/api/memory/settings" && method === "PATCH") {
    const body = request.postDataJSON();
    changes.push(body);
    if (failSave) return json({ detail: { error_code: "revision_conflict" } }, 409);
    assert.equal(body.expected_revision, settings.revision);
    settings = { enabled: body.enabled, revision: settings.revision + 1 };
    return json(settings);
  }
  if (p === "/api/memory/profile") {
    profileReads++;
    return json({ status: profileUnavailable ? "unavailable" : "available",
      data: { mental_models: cleared ? [] : [{ id: "synthetic-profile", content: profile },
        { id: "synthetic-legacy", content: "旧版无标题画像，完整保留。" }] } });
  }
  if (p === "/api/memory/characters/synthetic-card") return json({ data: { items: cleared ? [] : [
    { id: "synthetic-experience", content: feedback.length ? "合成反馈已更新。" : "合成人物经验。" },
  ] } });
  if (p.endsWith("/synthetic-card/feedback")) {
    feedback.push(request.postDataJSON());
    return json({ status: "pending" });
  }
  if (p === "/api/memory/reset") {
    resetCalls++;
    if (failReset) return json({ detail: { error_code: "memory_unavailable" } }, 503);
    cleared = true;
    return json({ status: "cleared" });
  }
  throw Error("Unexpected memory API " + method + " " + p);
});
async function closeError() {
  const dialog = page.getByRole("alertdialog");
  await dialog.waitFor();
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
}
try {
  await page.goto(served.origin + "/#/capabilities?section=personal-memory");
  const toggle = page.getByRole("switch", { name: "启用记忆", exact: true });
  await toggle.waitFor();
  const content = page.locator(".memory-profile-sheet");
  assert.deepEqual(await content.locator("h3").allTextContents(), ["用户身份", "背景基础", "用户偏好", "能力与困难", "其他证据"]);
  assert.equal(await content.getByText("合成基础第二段。", { exact: true }).count(), 1);
  assert.equal(await content.getByText("旧版无标题画像，完整保留。", { exact: true }).count(), 1);
  assert.equal(await content.locator("img").count(), 0);
  assert.equal(await content.getByText("<img src=x onerror=alert(1)>", { exact: true }).count(), 1);
  await page.getByLabel("人物", { exact: true }).selectOption("synthetic-card");
  await page.getByText("合成人物经验。", { exact: true }).waitFor();
  await toggle.click();
  await page.waitForFunction(() => document.querySelector('[role="switch"]').getAttribute("aria-checked") === "false"
    && !document.querySelector('[role="switch"]').disabled);
  assert.equal(await content.getByRole("heading", { name: "用户身份" }).count(), 1);
  assert.equal(await page.getByRole("button", { name: "保存反馈" }).isDisabled(), true);
  assert.deepEqual(changes[0], { enabled: false, expected_revision: 3 });
  await page.reload();
  await page.waitForFunction(() => document.querySelector('[role="switch"]')?.getAttribute("aria-checked") === "false");
  failSave = true;
  await toggle.click();
  await closeError();
  assert.equal(await toggle.getAttribute("aria-checked"), "false");
  failSave = false;
  await toggle.click();
  await page.waitForFunction(() => document.querySelector('[role="switch"]').getAttribute("aria-checked") === "true"
    && !document.querySelector('[role="switch"]').disabled);
  await page.getByLabel("人物", { exact: true }).selectOption("synthetic-card");
  await page.getByLabel("表达方面", { exact: true }).selectOption("persona");
  await page.getByLabel("反馈", { exact: true }).selectOption("warmer");
  await page.getByRole("button", { name: "保存反馈", exact: true }).click();
  await page.getByText("合成反馈已更新。", { exact: true }).waitFor();
  assert.equal(feedback[0].card_revision, 7);
  assert.equal(feedback[0].aspect, "persona");
  assert.equal(feedback[0].preference, "warmer");
  assert.match(feedback[0].idempotency_key, /^[0-9a-f-]{36}$/);
  assert.equal(await page.getByLabel("表达方面", { exact: true }).locator("option").count(), 14);
  assert.equal(await page.getByLabel("反馈", { exact: true }).locator("option").count(), 10);
  await page.waitForFunction(() => !document.querySelector('[role="switch"]').disabled);
  const before = profileReads;
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('[role="switch"]').disabled);
  assert.equal(profileReads, before + 1, "Refresh makes one profile request");
  for (const width of [1440, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "no horizontal overflow");
    const bounds = await toggle.boundingBox();
    assert(bounds && bounds.width >= 44 && bounds.height >= 24);
    await page.locator(".settings-scroll").evaluate(el => { el.scrollTop = 0; });
    await page.screenshot({ path: path.join(output, "memory-" + width + ".png"), fullPage: true });
  }
  await page.getByRole("button", { name: "清空个人记忆", exact: true }).click();
  const confirmation = page.getByRole("dialog", { name: "清空个人记忆？", exact: true });
  await confirmation.waitFor();
  assert.equal(await page.evaluate(() => document.activeElement.textContent), "取消");
  await page.keyboard.press("Shift+Tab");
  assert.equal(await page.evaluate(() => document.activeElement.textContent), "确认清空");
  await page.keyboard.press("Tab");
  assert.equal(await page.evaluate(() => document.activeElement.textContent), "取消");
  await page.keyboard.press("Escape");
  assert.equal(resetCalls, 0);
  assert.equal(await content.getByRole("heading", { name: "用户身份" }).count(), 1);
  failReset = true;
  await page.getByRole("button", { name: "清空个人记忆", exact: true }).click();
  await confirmation.getByRole("button", { name: "确认清空", exact: true }).click();
  await confirmation.getByRole("alert").waitFor();
  assert.equal(await page.getByRole("alertdialog").count(), 0, "Reset failure stays in confirmation dialog");
  assert.equal(await confirmation.isVisible(), true);
  assert.equal(await content.getByRole("heading", { name: "用户身份" }).count(), 1);
  failReset = false;
  await confirmation.getByRole("button", { name: "确认清空", exact: true }).click();
  await page.getByText("暂无已整理的画像。", { exact: true }).waitFor();
  await page.getByText("暂无该人物的已整理经验。", { exact: true }).waitFor();
  await page.waitForFunction(() => !document.querySelector('[role="switch"]').disabled);
  assert.equal(await page.getByRole("button", { name: "清空个人记忆", exact: true }).evaluate(e => e === document.activeElement), true);
  profileUnavailable = true;
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "记忆服务暂不可用" }).waitFor();
  assert.deepEqual(errors, []);
  assert.deepEqual(served.unexpected, []);
  console.log("PASS memory profile sections/legacy/literal HTML, persisted switch/revision/failure, feedback, single refresh, reset cancel/focus/failure/success, unavailable and four viewport layouts (synthetic APIs).");
} finally {
  await context.close();
  await browser.close();
}
