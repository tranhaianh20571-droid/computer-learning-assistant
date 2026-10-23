// Production UI with synthetic API responses; no public market or email traffic.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile, mkdir } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { mockBuiltFrontend } from "./frontend_dist_mock.mjs";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PW_ROOT + "/playwright-core");
const output = process.env.TEST_OUTPUT || ".backup/market-registration";
await mkdir(output, { recursive: true });
const marketHome = await readFile("server/market/web/index.html", "utf8");
const marketLogo = await readFile("server/market/web/logo.png");
const bindPage = execFileSync(process.env.TEST_PYTHON || "server/.venv/Scripts/python.exe", ["-c", `
import importlib.util, tempfile
from pathlib import Path
from starlette.requests import Request
spec=importlib.util.spec_from_file_location("market_ui", "server/market/app.py")
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
with tempfile.TemporaryDirectory() as folder:
    m.DATA_DIR=Path(folder); m.DB_PATH=Path(folder)/"market.db"; m.STORAGE=Path(folder)/"cards"
    m.init_db()
    print(m.bind_page("Synthetic123", Request({"type":"http","headers":[]})).body.decode())
`], { encoding: "utf8", env: { ...process.env, PYTHONIOENCODING: "utf-8" } });
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
const checks = [], errors = [];
try {
  for (const width of [1440, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 960 } });
    const page = await context.newPage();
    page.on("pageerror", error => errors.push(error.message));
    const served = await mockBuiltFrontend(page);
    let connected = false, registered = false;
    let confirms = 0;
    const user = { user_id: "synthetic-user", email: "synthetic@example.invalid", status: "approved", is_admin: false };
    await context.route("https://market.synthetic.test/**", async route => {
      const url = new URL(route.request().url());
      if (url.pathname === "/assets/logo.png") return route.fulfill({ contentType: "image/png", body: marketLogo });
      if (url.pathname === "/api/me") return route.fulfill({ json: registered ? { authenticated: true, user } : { authenticated: false } });
      if (url.pathname === "/api/cards") return route.fulfill({ json: { data: [] } });
      if (url.pathname === "/api/username/available") return route.fulfill({ json: { available: true } });
      if (url.pathname === "/api/send-code") return route.fulfill({ json: { ok: true } });
      if (url.pathname === "/api/register") {
        assert.equal(route.request().postDataJSON().bind_code, "Synthetic123");
        registered = true;
        return route.fulfill({ json: { ok: true, verified: true, bound: true } });
      }
      if (url.pathname === "/api/login") return route.fulfill({ json: user });
      if (url.pathname === "/api/bind/confirm") {
        confirms++;
        return route.fulfill({ status: 410, json: { detail: { error_code: "BIND_USED" } } });
      }
      if (url.pathname === "/bind") return route.fulfill({ contentType: "text/html", body: bindPage });
      if (["/", "/featured", "/categories"].includes(url.pathname)) return route.fulfill({ contentType: "text/html", body: marketHome });
      return route.fulfill({ status: 404, body: "" });
    });
    await page.route("**/api/**", async route => {
      const { pathname } = new URL(route.request().url());
      if (pathname === "/api/auth/me") return route.fulfill({ status: connected ? 200 : 401, json: connected ? user : {} });
      if (pathname === "/api/market/bind/start") return route.fulfill({ json: { code: "Synthetic123", url: "https://market.synthetic.test/#bind=Synthetic123" } });
      if (pathname === "/api/market/bind/status") {
        connected = registered;
        return route.fulfill({ json: { bound: connected, user: connected ? user : null } });
      }
      if (pathname === "/api/navigation") return route.fulfill({ json: {} });
      if (pathname === "/api/materials") return route.fulfill({ json: { data: [] } });
      return route.fulfill({ json: {} });
    });
    await page.goto(served.origin + "/#/login");
    const popupPromise = page.waitForEvent("popup");
    await page.getByRole("button", { name: "注册账号", exact: true }).click();
    const market = await popupPromise;
    market.on("pageerror", error => errors.push(error.message));
    await market.getByRole("link", { name: "注册账号", exact: true }).first().waitFor();
    assert.equal(new URL(market.url()).pathname, "/");
    assert.equal(new URL(market.url()).hash, "");
    await market.screenshot({ path: `${output}/market-register-${width}.png`, fullPage: true });
    const nav = await market.locator("nav.main").boundingBox();
    const account = await market.locator("#account-area").boundingBox();
    assert.ok(nav && account);
    assert.ok(nav.y >= account.y + account.height || nav.x + nav.width <= account.x);
    await market.getByRole("link", { name: "官方精选", exact: true }).click();
    await market.reload();
    await market.getByRole("link", { name: "注册账号", exact: true }).first().click();
    await market.locator("#email").fill(user.email);
    await market.getByRole("button", { name: "发送验证码", exact: true }).click();
    await market.locator("#code").fill("123456");
    await market.locator("#username").fill("Synthetic");
    await market.locator("#password").fill("synthetic-password");
    await market.getByRole("button", { name: "注册并绑定这台电脑", exact: true }).click();
    await market.waitForURL("https://market.synthetic.test/");
    await page.waitForURL("**/#/home");
    assert.equal(confirms, 0);
    assert.equal(await market.evaluate(() => sessionStorage.getItem("market.pendingBinding")), null);
    assert.equal(await market.getByRole("link", { name: "注册账号", exact: true }).count(), 0);
    await page.screenshot({ path: `${output}/local-${width}.png`, fullPage: true });
    await market.screenshot({ path: `${output}/market-${width}.png`, fullPage: true });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.ok(await market.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    checks.push(`register_market_local_${width}`);
    await context.close();
  }
  const context = await browser.newContext();
  const page = await context.newPage();
  const served = await mockBuiltFrontend(page);
  await page.addInitScript(() => { window.open = () => null; });
  await page.route("**/api/**", route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === "/api/auth/me") return route.fulfill({ status: 401, json: {} });
    if (pathname === "/api/market/bind/start") return route.fulfill({ json: { code: "Synthetic123", url: "https://market.synthetic.test/#bind=Synthetic123" } });
    return route.fulfill({ json: { bound: false } });
  });
  await page.goto(served.origin + "/#/login");
  await page.getByRole("button", { name: "注册账号", exact: true }).click();
  assert.equal(await page.getByRole("link", { name: "打开市集", exact: true }).getAttribute("href"), "https://market.synthetic.test/#bind=Synthetic123");
  checks.push("popup_blocked_fallback");
  await page.clock.install();
  await page.clock.fastForward(10 * 60 * 1000 + 3000);
  await page.getByText("注册连接已过期，请重新点击注册账号。", { exact: true }).waitFor();
  assert.ok(await page.getByRole("button", { name: "注册账号", exact: true }).isEnabled());
  checks.push("expired_connection_retry");
  await context.close();
  for (const stale of [false, true]) {
    const isolated = await browser.newContext();
    const publicPage = await isolated.newPage();
    if (stale) await publicPage.addInitScript(() => sessionStorage.setItem("market.pendingBinding",
      JSON.stringify({ code: "Synthetic123", expires: Date.now() - 1000 })));
    await publicPage.route("**/*", route => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === "/api/me") return route.fulfill({ json: { authenticated: false } });
      if (pathname === "/api/cards") return route.fulfill({ json: { data: [] } });
      if (pathname === "/") return route.fulfill({ contentType: "text/html", body: marketHome });
      return route.fulfill({ status: 404, body: "" });
    });
    await publicPage.goto("https://market.synthetic.test/");
    await publicPage.getByRole("button", { name: "登录 / 注册", exact: true }).waitFor();
    assert.equal(await publicPage.getByRole("link", { name: "注册账号", exact: true }).count(), 0);
    assert.equal(await publicPage.evaluate(() => sessionStorage.getItem("market.pendingBinding")), null);
    checks.push(stale ? "market_expired_context_removed" : "market_no_independent_registration");
    await isolated.close();
  }
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ checks, pageErrors: errors, synthetic: true }));
} finally {
  await browser.close();
}
