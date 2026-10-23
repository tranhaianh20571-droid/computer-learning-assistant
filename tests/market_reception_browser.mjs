// Production local frontend and market HTML; synthetic authorization, no live account traffic.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile, mkdir } from "node:fs/promises";
import { mockBuiltFrontend } from "./frontend_dist_mock.mjs";

const { chromium } = createRequire(import.meta.url)(process.env.PW_ROOT + "/playwright-core");
const output = ".backup/market-reception";
await mkdir(output, { recursive: true });
const marketHome = await readFile("server/market/web/index.html", "utf8");
const logo = await readFile("server/market/web/logo.png");
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
const checks = [];
try {
  for (const width of [1440, 390]) for (const loggedIn of [false, true]) {
    const context = await browser.newContext({ viewport: { width, height: 960 } });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const served = await mockBuiltFrontend(page);
    let confirmed = false, localLogins = 0;
    const b = { user_id: "synthetic-b", email: "synthetic-b@example.invalid", status: "approved", is_admin: false };
    await page.route("**/api/**", async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.pathname === "/api/auth/me") return route.fulfill({ status: loggedIn ? 200 : 401, json: loggedIn ? b : {} });
      if (url.pathname === "/api/market/login" || url.pathname === "/api/auth/login") {
        localLogins++;
        return route.fulfill({ status: 500, json: {} });
      }
      if (url.pathname === "/api/market/bind/start") {
        assert.equal(url.searchParams.get("purpose"), "receive");
        return route.fulfill({ json: { code: "Synthetic123", url: "https://market.synthetic.test/#receive=Synthetic123" } });
      }
      if (url.pathname === "/api/market/bind/status") {
        assert.equal(url.searchParams.get("purpose"), "receive");
        return route.fulfill({ json: { bound: confirmed, current_account: false, purpose: "receive" } });
      }
      return route.fulfill({ json: {} });
    });
    await context.route("https://market.synthetic.test/**", async route => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/me") return route.fulfill({ json: { authenticated: true, user: { email: "synthetic-a@example.invalid", display_name: "Synthetic A" } } });
      if (url.pathname === "/api/cards") return route.fulfill({ json: { data: [] } });
      if (url.pathname === "/assets/logo.png") return route.fulfill({ contentType: "image/png", body: logo });
      if (url.pathname === "/bind") {
        assert.equal(url.searchParams.get("purpose"), "receive");
        confirmed = true;
        return route.fulfill({ contentType: "text/html", body: "<h1>合成账号授权完成</h1>" });
      }
      return route.fulfill({ contentType: "text/html", body: marketHome });
    });
    await page.goto(served.origin + "/#/market-receive");
    const popupPromise = page.waitForEvent("popup");
    await page.getByRole("button", { name: "开启账号接收", exact: true }).click();
    const market = await popupPromise;
    await market.getByRole("link", { name: "开启账号接收", exact: true }).waitFor();
    await market.getByRole("link", { name: "开启账号接收", exact: true }).click();
    await page.getByText("已开启后台接收，本机未登录或切换账号也可继续接收。", { exact: true }).waitFor();
    assert.equal(localLogins, 0);
    assert.equal(new URL(page.url()).hash, "#/market-receive");
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: `${output}/receive-${width}-${loggedIn ? "b" : "anonymous"}.png`, fullPage: true });
    assert.deepEqual(errors, []);
    checks.push({ width, localIdentity: loggedIn ? "B preserved" : "anonymous preserved", receiveAuthorized: true });
    await context.close();
  }
  console.log(JSON.stringify({ synthetic: true, checks }));
} finally {
  await browser.close();
}
