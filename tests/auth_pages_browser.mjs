// Synthetic auth navigation checks; API calls never reach the running backend.
import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(path.resolve(process.env.PW_ROOT || "frontend/node_modules", "package.json"));
const { chromium } = require("playwright-core");
const base = process.env.TEST_FRONTEND || "http://127.0.0.1:5173";
const output = path.resolve(process.env.TEST_OUT || ".backup/auth-pages-browser");
const routes = ["/login", "/register", "/verify", "/reset"];
const checks = [];
const errors = [];
const unexpected = [];
const browser = await chromium.launch({ executablePath: process.env.TEST_BROWSER, headless: true });
mkdirSync(output, { recursive: true });

try {
  for (const isGuest of [true, false]) {
    for (const width of [1280, 360]) {
      const identity = isGuest ? "guest" : "member";
      const context = await browser.newContext({ viewport: { width, height: 844 } });
      const page = await context.newPage();
      page.on("pageerror", (error) => errors.push(error.message));
      const principal = {
        user_id: isGuest ? "guest_synthetic" : "synthetic-member",
        email: isGuest ? "" : "member@example.invalid",
        is_admin: false,
        is_guest: isGuest,
        status: "approved",
      };
      // All API requests are intercepted, including accidental form submissions.
      await context.route("**/api/**", async (route) => {
        const request = route.request();
        const pathname = new URL(request.url()).pathname;
        if (request.method() !== "GET") {
          unexpected.push({ method: request.method(), pathname });
          return route.fulfill({ status: 405, json: {} });
        }
        if (pathname === "/api/auth/me") {
          return route.fulfill({ status: isGuest ? 401 : 200, json: isGuest ? {} : principal });
        }
        if (pathname === "/api/navigation") return route.fulfill({ json: {} });
        if (pathname === "/api/materials") return route.fulfill({ json: { data: [] } });
        unexpected.push({ method: request.method(), pathname });
        return route.fulfill({ status: 404, json: {} });
      });
      try {
        for (const route of routes) {
          await page.goto(`${base}/#${route}`, { waitUntil: "networkidle" });
          await page.locator(".auth-col").waitFor({ state: "visible" });
          const exit = page.getByRole("button", {
            name: isGuest ? "以访客身份继续" : "返回学习空间",
            exact: true,
          });
          if (isGuest) {
            assert.equal(await exit.count(), 0, "anonymous auth page must not expose guest exit");
            checks.push(`anonymous_${width}_${route.slice(1)}`);
            continue;
          }
          assert.equal(await exit.count(), 1, `${identity} ${route}: incorrect exit label`);
          await exit.scrollIntoViewIfNeeded();
          const bounds = await exit.boundingBox();
          assert.ok(bounds && bounds.x >= 0 && bounds.x + bounds.width <= width,
            `${identity} ${route}: exit exceeds viewport`);
          assert.ok(await page.evaluate(() =>
            document.documentElement.scrollWidth <= window.innerWidth),
          `${identity} ${route}: horizontal overflow`);
          if (route === "/login") {
            await page.screenshot({ path: path.join(output, `${identity}-${width}.png`), fullPage: true });
          }
          if (width === 1280) {
            // Tab from the last form link to the shared exit, then activate with Enter.
            await page.locator(".auth-col a").last().focus();
            await page.keyboard.press("Tab");
            assert.ok(await exit.evaluate((element) => element === document.activeElement),
              `${identity} ${route}: exit is not next in keyboard order`);
            await page.keyboard.press("Enter");
          } else {
            await exit.click();
          }
          await page.waitForURL("**/#/home");
          await page.locator(".app").waitFor({ state: "visible" });
          assert.equal(await page.locator(".auth-col").count(), 0);
          assert.ok(await page.getByRole("textbox", { name: "学习目标", exact: true }).isEnabled());
          assert.equal(await page.locator(".who").innerText(), isGuest ? "访客模式" : principal.email);
          assert.equal(await page.getByRole("button", { name: "退出登录", exact: true }).count(), isGuest ? 0 : 1);
          checks.push(`${identity}_${width}_${route.slice(1)}`);
        }
      } finally {
        await context.close();
      }
    }
  }
  assert.deepEqual(unexpected, [], "unexpected API calls");
  assert.deepEqual(errors, [], "page errors");
  console.log(JSON.stringify({ checks, pageErrors: errors, unexpectedRequests: unexpected, synthetic: true }));
} finally {
  await browser.close();
}
