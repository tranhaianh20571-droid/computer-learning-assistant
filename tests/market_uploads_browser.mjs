// Market HTML and production registration markup with synthetic API responses only.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile, mkdir } from "node:fs/promises";
import { execFileSync } from "node:child_process";

const { chromium } = createRequire(import.meta.url)(process.env.PW_ROOT + "/playwright-core");
const output = process.env.TEST_OUTPUT || ".backup/market-uploads";
await mkdir(output, { recursive: true });
const home = await readFile("server/market/web/index.html", "utf8");
const logo = await readFile("server/market/web/logo.png");
const bind = execFileSync(process.env.TEST_PYTHON || "server/.venv/Scripts/python.exe", ["-c", `
import importlib.util, tempfile
from pathlib import Path
from starlette.requests import Request
spec=importlib.util.spec_from_file_location("market_ui", "server/market/app.py")
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
with tempfile.TemporaryDirectory() as folder:
    m.DATA_DIR=Path(folder); m.DB_PATH=Path(folder)/"market.db"; m.STORAGE=Path(folder)/"cards"
    m.init_db()
    print(m.bind_page("Synthetic123", Request({"type":"http","headers":[]})).body.decode())
`], { encoding: "utf8", env: { ...process.env, PYTHONIOENCODING: "utf-8" } });
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
const checks = [];
try {
  for (const width of [1440, 768, 390, 320]) {
    const context = await browser.newContext({ viewport: { width, height: 960 }, reducedMotion: "reduce" });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const user = { user_id: "synthetic-user", email: "synthetic@example.invalid", display_name: "合成用户" };
    const owned = { card_id: "card_synthetic_owned", title: "合成导师 · 我的上传", tags: ["合成"], downloads_72h: 2, cover_url: "/assets/logo.png", created_at: "2026-10-22T00:00:00Z" };
    const other = { ...owned, card_id: "card_synthetic_other", title: "合成导师 · 其他上传" };
    let items = [owned], signed = true, deletes = 0, failList = false, mineGate = null;
    let releaseMine;
    await page.route("**/*", async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.pathname === "/api/me") return route.fulfill({ json: signed ? { authenticated: true, user } : { authenticated: false } });
      if (url.pathname === "/api/login") { signed = true; return route.fulfill({ json: user }); }
      if (url.pathname === "/api/logout") { signed = false; return route.fulfill({ json: { ok: true } }); }
      if (url.pathname === "/api/cards") {
        if (url.searchParams.get("scope") === "mine") {
          if (mineGate) await mineGate;
          if (!signed) return route.fulfill({ status: 401, json: { detail: { error_code: "SESSION_INVALID" } } });
          if (failList) return route.fulfill({ status: 503, json: {} });
          return route.fulfill({ json: { data: items } });
        }
        return route.fulfill({ json: { data: [owned, other] } });
      }
      if (url.pathname === "/api/cards/" + owned.card_id && request.method() === "DELETE") {
        deletes++;
        if (deletes === 1) return route.fulfill({ status: 503, json: { detail: { message: "删除未完成，请稍后重试。" } } });
        items = [];
        return route.fulfill({ json: { ok: true } });
      }
      if (url.pathname === "/assets/logo.png") return route.fulfill({ contentType: "image/png", body: logo });
      if (url.pathname === "/bind") return route.fulfill({ contentType: "text/html", body: bind });
      if (["/", "/uploads", "/categories", "/featured"].includes(url.pathname)) return route.fulfill({ contentType: "text/html", body: home });
      return route.fulfill({ status: 404 });
    });
    await page.goto("https://market.synthetic.test/");
    await page.getByRole("link", { name: "我上传的", exact: true }).click();
    await page.locator("[data-delete]").waitFor();
    assert.equal(new URL(page.url()).pathname, "/uploads");
    assert.deepEqual(await page.locator("nav.main a").allTextContents(), ["全部卡片", "官方精选", "分类", "我上传的"]);
    assert.equal(await page.locator(".card").count(), 1);
    assert.equal(await page.getByRole("heading", { name: other.title, exact: true }).count(), 0);
    await page.screenshot({ path: `${output}/uploads-${width}.png`, fullPage: true });
    await page.locator("[data-delete]").click();
    assert.equal(await page.locator("#delete-cancel").evaluate(el => document.activeElement === el), true);
    await page.keyboard.press("Enter");
    assert.equal(deletes, 0);
    assert.equal(await page.locator("#modal").isVisible(), false);
    await page.locator("[data-delete]").click();
    await page.screenshot({ path: `${output}/delete-${width}.png`, fullPage: true });
    await page.locator("#delete-confirm").click();
    await page.getByText("删除未完成，请稍后重试。", { exact: true }).waitFor();
    assert.equal(await page.locator(".card").count(), 1);
    await page.locator("#delete-confirm").click();
    await page.getByRole("heading", { name: "还没有上传的卡片", exact: true }).waitFor();
    await page.getByText("已删除上传。", { exact: true }).waitFor();
    assert.equal(deletes, 2);
    failList = true;
    await page.reload();
    await page.getByRole("button", { name: "重试", exact: true }).waitFor();
    failList = false;
    await page.getByRole("button", { name: "重试", exact: true }).click();
    await page.getByRole("heading", { name: "还没有上传的卡片", exact: true }).waitFor();
    signed = false;
    await page.reload();
    await page.getByRole("heading", { name: "登录后查看你的上传", exact: true }).waitFor();
    await page.locator("#state").getByRole("button", { name: "登录", exact: true }).click();
    await page.locator("#auth-email").fill(user.email);
    await page.locator("#auth-password").fill("synthetic-password");
    await page.locator("#auth-submit").click();
    await page.getByRole("heading", { name: "还没有上传的卡片", exact: true }).waitFor();
    items = [owned];
    mineGate = new Promise(resolve => { releaseMine = resolve; });
    const requested = page.waitForRequest(request => new URL(request.url()).searchParams.get("scope") === "mine");
    await page.getByRole("link", { name: "我上传的", exact: true }).click();
    await requested;
    await page.getByRole("link", { name: "全部卡片", exact: true }).click();
    await page.locator(".card").nth(1).waitFor();
    releaseMine();
    mineGate = null;
    await page.waitForLoadState("networkidle");
    assert.equal(await page.locator(".card").count(), 2);
    assert.equal(await page.locator("[data-delete]").count(), 0);
    await page.goto("https://market.synthetic.test/bind?code=Synthetic123");
    await page.getByRole("heading", { name: "注册并绑定这台电脑", exact: true }).waitFor();
    const radii = await page.evaluate(() => ({
      panel: getComputedStyle(document.querySelector("main")).borderRadius,
      input: getComputedStyle(document.querySelector("#email")).borderRadius,
      button: getComputedStyle(document.querySelector('button[onclick^="submitAuth"]')).borderRadius,
    }));
    assert.deepEqual(radii, { panel: "24px", input: "12px", button: "999px" });
    await page.screenshot({ path: `${output}/register-${width}.png`, fullPage: true, mask: [page.locator(".hint b")] });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(errors, []);
    checks.push({ width, ownedOnly: true, cancel: true, retryDelete: true, empty: true, listRetry: true, login: true, staleResponse: true, roundedRegister: true });
    await context.close();
  }
  console.log(JSON.stringify({ synthetic: true, checks }));
} finally {
  await browser.close();
}
