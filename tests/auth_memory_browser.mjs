// Real authentication and account settings against the owned assembler/schema.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import path from "node:path";
const require = createRequire(path.resolve(process.env.PW_ROOT, "package.json"));
const { chromium } = require("playwright-core");
const base = process.env.TEST_FRONTEND;
const browser = await chromium.launch({ executablePath: process.env.TEST_BROWSER, headless: true });
const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await context.newPage();
const errors = [];
page.on("pageerror", e => errors.push(e.message));
try {
  await page.goto(base + "/#/memory");
  await page.waitForURL("**/#/login");
  assert.equal(await page.getByRole("button", { name: "以访客身份继续" }).count(), 0);
  async function login(email) {
    await page.getByLabel("邮箱", { exact: true }).fill(email);
    await page.getByLabel("密码", { exact: true }).fill("synthetic-password-12345");
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await page.waitForURL("**/#/home");
  }
  await login("synthetic-browser-admin@example.com");
  await page.reload();
  await page.getByRole("button", { name: "退出登录" }).waitFor();
  await page.goto(base + "/#/capabilities?section=personal-memory");
  await page.getByRole("heading", { name: "个人记忆", exact: true }).waitFor();
  const toggle = page.getByRole("switch", { name: "启用记忆", exact: true });
  assert.equal(await toggle.isChecked(), false);
  await toggle.click();
  await page.waitForFunction(async () => {
    const r = await fetch("/api/memory/settings");
    return r.ok && (await r.json()).enabled;
  });
  await page.screenshot({ path: path.join(process.env.TEST_OUTPUT, "memory-desktop.png"), fullPage: true });
  await page.getByRole("button", { name: "退出登录" }).click();
  await page.waitForURL("**/#/login");
  await login("synthetic-browser-member@example.com");
  await page.goto(base + "/#/memory");
  await page.getByRole("heading", { name: "个人记忆", exact: true }).waitFor();
  assert.equal(await page.getByRole("switch", { name: "启用记忆", exact: true }).isChecked(), false, "A settings leaked into B");
  assert.equal(await page.locator("tbody tr").count(), 0);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForFunction(() => document.querySelector(".side").getBoundingClientRect().right <= 1);
  const heading = await page.getByRole("heading", { name: "个人记忆", exact: true }).boundingBox();
  assert.ok(heading && heading.x >= 0 && heading.x + heading.width <= 390);
  await page.screenshot({ path: path.join(process.env.TEST_OUTPUT, "memory-mobile.png"), fullPage: true });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.deepEqual(errors, []);
  console.log("PASS: anonymous redirect, no guest, real login, cookie restoration, logout, A/B isolation, memory settings, desktop/mobile.");
} finally {
  await context.close();
  await browser.close();
}
