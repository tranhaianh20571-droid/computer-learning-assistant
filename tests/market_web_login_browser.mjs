// Real built frontend and market HTML; all identities and API responses synthetic.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile, mkdir } from "node:fs/promises";
import { mockBuiltFrontend } from "./frontend_dist_mock.mjs";

const { chromium } = createRequire(import.meta.url)(process.env.PW_ROOT + "/playwright-core");
const marketHtml = await readFile("server/market/web/index.html", "utf8");
const logo = await readFile("server/market/web/logo.png");
const output = process.env.TEST_OUTPUT || ".backup/market-web-login";
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
try {
  for (const width of [1440, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 960 } });
    const errors = [];
    let name = "carorfox", failed = false, authenticated = false, consumed = 0;
    context.on("page", page => page.on("pageerror", error => errors.push(error.message)));
    await context.route("https://market.synthetic.test/**", async route => {
      const url = new URL(route.request().url());
      if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: marketHtml });
      if (url.pathname === "/api/web-login/consume") {
        assert.equal(route.request().headers()["content-type"], "application/json");
        assert.equal(route.request().postDataJSON().ticket, "synthetic-short-lived-ticket");
        consumed++;
        authenticated = true;
        return route.fulfill({ json: { user: { display_name: name } } });
      }
      if (url.pathname === "/api/me") return route.fulfill({ json: { authenticated, user: { display_name: name } } });
      if (url.pathname === "/api/cards") return route.fulfill({ json: { data: [] } });
      if (url.pathname === "/assets/logo.png") return route.fulfill({ contentType: "image/png", body: logo });
      return route.fulfill({ status: 404 });
    });
    const page = await context.newPage();
    await mockBuiltFrontend(page);
    await page.route("**/api/**", async route => {
      const url = new URL(route.request().url());
      if (url.origin === "https://market.synthetic.test") return route.fallback();
      if (url.pathname === "/api/auth/me") return route.fulfill({ json: {
        user_id: "synthetic-owner", email: "34292@example.invalid", display_name: "Old", is_admin: false, status: "approved",
      } });
      if (url.pathname === "/api/market/profile") return route.fulfill({ json: { display_name: name } });
      if (url.pathname === "/api/market/web-login") return route.fulfill(failed
        ? { status: 502, json: { detail: { message: "市集暂时不可达，请稍后重试。" } } }
        : { json: { display_name: name, url: "https://market.synthetic.test/#login=synthetic-short-lived-ticket" } });
      return route.fulfill({ json: { data: [], items: [] } });
    });
    await page.goto("https://frontend.synthetic.test/#/home");
    await page.getByLabel("carorfox 的工作空间", { exact: true }).waitFor();
    assert.equal(await page.locator(".revision-avatar").textContent(), "c");
    name = "Changed";
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await page.getByLabel("Changed 的工作空间", { exact: true }).waitFor();
    name = "carorfox";
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await page.getByLabel("carorfox 的工作空间", { exact: true }).waitFor();
    if (width === 390) await page.getByRole("button", { name: "打开导航", exact: true }).click();
    if (width === 390) await page.waitForFunction(() => document.querySelector("#side").getBoundingClientRect().x >= 0);
    await page.screenshot({ path: `${output}/local-${width}.png`, fullPage: true });
    const popupPromise = context.waitForEvent("page");
    await page.getByRole("button", { name: "卡片市集", exact: true }).click();
    const popup = await popupPromise;
    await popup.locator("#account-toggle").filter({ hasText: "carorfox" }).waitFor();
    assert.equal(new URL(popup.url()).hash, "");
    assert.equal(consumed, 1);
    assert.equal(await popup.evaluate(() => window.opener), null);
    assert.equal(page.url(), "https://frontend.synthetic.test/#/home");
    await popup.screenshot({ path: `${output}/market-${width}.png`, fullPage: true });
    await popup.close();
    if (width === 390) await page.getByRole("button", { name: "打开导航", exact: true }).click();
    failed = true;
    const failedPopup = context.waitForEvent("page");
    await page.getByRole("button", { name: "卡片市集", exact: true }).click();
    const blank = await failedPopup;
    await page.getByText("市集暂时不可达，请稍后重试。", { exact: true }).waitFor();
    assert.equal(blank.isClosed(), true);
    assert.equal(await page.getByRole("button", { name: "卡片市集", exact: true }).isDisabled(), false);
    failed = false;
    await page.reload();
    await page.getByLabel("carorfox 的工作空间", { exact: true }).waitFor();
    await page.unroute("**/*");
    await page.evaluate(() => { window.open = () => null; });
    if (width === 390) await page.getByRole("button", { name: "打开导航", exact: true }).click();
    await page.getByRole("button", { name: "卡片市集", exact: true }).click();
    await page.locator("#account-toggle").filter({ hasText: "carorfox" }).waitFor();
    assert.equal(new URL(page.url()).origin, "https://market.synthetic.test");
    assert.equal(new URL(page.url()).hash, "");
    assert.equal(consumed, 2);
    assert.deepEqual(errors, []);
    await context.close();
  }
  console.log("PASS: desktop/mobile shared name and initial, focus sync, popup login, fragment removal, opener isolation and failure recovery.");
} finally { await browser.close(); }
