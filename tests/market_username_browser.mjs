// Exercise the real market HTML with isolated synthetic account responses.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile, mkdir } from "node:fs/promises";

const { chromium } = createRequire(import.meta.url)(process.env.PW_ROOT + "/playwright-core");
const home = await readFile("server/market/web/index.html", "utf8");
const logo = await readFile("server/market/web/logo.png");
const output = process.env.TEST_OUTPUT || ".backup/market-username";
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
try {
  for (const width of [1440, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 960 } });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const user = { user_id: "synthetic-user", display_name: "Synthetic Own" };
    const queries = [];
    let releaseOld, oldFinished;
    const oldGate = new Promise(resolve => { releaseOld = resolve; });
    const oldDone = new Promise(resolve => { oldFinished = resolve; });
    await page.route("**/*", async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.pathname === "/api/me") return route.fulfill({ json: { authenticated: true, user } });
      if (url.pathname === "/api/cards") return route.fulfill({ json: { data: [] } });
      if (url.pathname === "/api/username/available") {
        const name = url.searchParams.get("name");
        queries.push(name);
        if (name === "Synthetic Slow") await oldGate;
        await route.fulfill({ json: { available: !["Synthetic Taken", "Synthetic Slow"].includes(name), reason: "taken" } });
        if (name === "Synthetic Slow") oldFinished();
        return;
      }
      if (url.pathname === "/api/username") {
        const name = request.postDataJSON().display_name;
        if (name === "Synthetic Taken") return route.fulfill({ status: 409, json: { detail: { error_code: "USERNAME_TAKEN", message: "这个用户名已被使用。" } } });
        user.display_name = name;
        return route.fulfill({ json: { ok: true, display_name: name } });
      }
      if (url.pathname === "/assets/logo.png") return route.fulfill({ contentType: "image/png", body: logo });
      if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: home });
      return route.fulfill({ status: 404 });
    });
    await page.goto("https://market.synthetic.test/");
    await page.locator("#account-toggle").click();
    const input = page.locator("#account-name"), hint = page.locator("#account-name-hint");
    await input.fill("Synthetic Taken");
    await page.getByText("这个用户名已被使用", { exact: true }).waitFor();
    await page.locator("#account-save").click();
    await page.locator("#account-msg").getByText("这个用户名已被使用。", { exact: true }).waitFor();
    assert.match(await page.locator("#account-toggle").textContent(), /Synthetic Own/);
    await input.fill("Synthetic Slow");
    await page.waitForFunction(() => document.querySelector("#account-name-hint").textContent === "检查中…");
    await page.waitForRequest(request => new URL(request.url()).searchParams.get("name") === "Synthetic Slow");
    await input.fill("Synthetic Fresh");
    await page.getByText("可以使用", { exact: true }).waitFor();
    releaseOld();
    await oldDone;
    assert.equal(await hint.textContent(), "可以使用");
    await input.fill("Synthetic Taken");
    await page.getByText("这个用户名已被使用", { exact: true }).waitFor();
    assert.equal(queries.filter(name => name === "Synthetic Taken").length, 2);
    await page.screenshot({ path: `${output}/taken-${width}.png`, fullPage: true });
    await input.fill("Synthetic Own");
    await page.getByText("当前用户名", { exact: true }).waitFor();
    await page.locator("#account-save").click();
    await page.locator("#account-menu").waitFor({ state: "hidden" });
    assert.match(await page.locator("#account-toggle").textContent(), /Synthetic Own/);
    assert.deepEqual(errors, []);
    await context.close();
  }
  console.log("PASS: username uniqueness, original name, fresh checks and stale-response protection at 1440/390.");
} finally {
  await browser.close();
}
