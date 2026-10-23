import path from "node:path";
import fs from "node:fs/promises";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { _electron } = require(process.env.PW_ROOT + "/playwright-core");
const root = path.resolve(import.meta.dirname, "..");
if (!process.env.STUDY_DESKTOP_HOME) throw new Error("Use the synthetic clean-install home");
const executablePath = process.env.STUDY_ELECTRON_EXE || require("../desktop/node_modules/electron");
const args = process.env.STUDY_ELECTRON_EXE ? [] : [path.join(root, "desktop")];
const electronEnv = { ...process.env };
const startedAt = Date.now();
delete electronEnv.ELECTRON_RUN_AS_NODE;
if (process.env.STUDY_ELECTRON_EXE) delete electronEnv.STUDY_DESKTOP_HOME;
const application = await _electron.launch({ executablePath, args, cwd: root,
  env: electronEnv, timeout: 60000 });
try {
  const userData = await application.evaluate(({ app }) => app.getPath("userData"));
  assert.equal(path.resolve(userData), path.resolve(process.env.STUDY_DESKTOP_HOME, "config/electron"));
  const page = await application.firstWindow();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const deadline = Date.now() + 600000;
  while (!/http:\/\/127\.0\.0\.1:\d+\/#\/home/.test(page.url())) {
    if (Date.now() >= deadline) throw new Error("Desktop startup timed out");
    const value = await page.evaluate(() => window.desktopSetup?.status()).catch(() => null);
    if (value?.stage === "failed") throw new Error("Desktop startup failed: " + value.code);
    await page.waitForTimeout(500);
  }
  const bootMilliseconds = Date.now() - startedAt;
  console.log("Installed application ready in milliseconds:", bootMilliseconds);
  await page.getByRole("button", { name: "设置", exact: true }).first().waitFor({ timeout: 30000 }).catch(() => {});
  await page.waitForTimeout(1200);
  const origin = new URL(page.url()).origin;
  const blocked = await fetch(origin + "/api/auth/me");
  assert.equal(blocked.status, 403, "browser without desktop instance credential must be rejected");
  const values = await page.evaluate(async () => {
    const profile = await fetch("/api/auth/me").then(response => response.json());
    const video = await fetch("/api/source-settings/bilinote").then(response => response.json());
    return { profile, video };
  });
  assert.ok(values.profile.user_id);
  assert.equal(values.profile.is_admin, false);
  assert.equal(values.video.configuration.endpoint, "builtin://bilinote");
  const material = await page.evaluate(async () => {
    const data = new FormData();
    data.append("files", new File(["Synthetic desktop upload verification."], "desktop-test.txt", { type: "text/plain" }));
    const uploaded = await fetch("/api/materials", { method: "POST", body: data });
    const result = await uploaded.json();
    const id = result.data?.[0]?.material_id;
    if (!id) return { status: uploaded.status };
    const read = await fetch("/api/materials/" + id);
    const removed = await fetch("/api/materials/" + id, { method: "DELETE" });
    return { status: uploaded.status, read: read.status, removed: removed.status };
  });
  assert.equal(material.status, 201, "multipart upload through the packaged gateway");
  assert.equal(material.read, 200);
  assert.ok(material.removed >= 200 && material.removed < 300);
  await fs.mkdir(path.join(root, "assets"), { recursive: true });
  await page.screenshot({ path: path.join(root, "assets/desktop-home.png") });
  await page.goto(origin + "/#/capabilities?section=memory");
  await page.getByRole("heading", { name: "记忆模型", exact: true }).waitFor({ timeout: 30000 });
  await page.waitForTimeout(700);
  await page.screenshot({ path: path.join(root, "assets/desktop-settings.png") });
  await page.goto(origin + "/#/home");
  await page.route("**/api/market/web-login", route => route.fulfill({ json: {
    display_name: "我", url: "https://market.synthetic.test/#login=synthetic-ticket"
  } }));
  const detected = await page.evaluate(() => window.desktopBrowser.listBrowsers());
  assert.ok(detected.length, "real installed browsers should be detected");
  assert.equal(detected.some(item => "executable" in item), false, "renderer receives browser IDs only");
  const selected = detected.find(item => /Edge/.test(item.name)) || detected[0];
  await application.evaluate(async ({ app, dialog }) => {
    const { createRequire } = process.getBuiltinModule("module");
    const load = createRequire(app.getAppPath() + "/package.json");
    const browserModule = load("./src/browsers.cjs");
    globalThis.browserTestModule = browserModule;
    globalThis.originalBrowserLaunch = browserModule.launchBrowser;
    globalThis.originalBrowserDialog = dialog.showOpenDialog;
    globalThis.syntheticBrowserUrls = [];
    browserModule.launchBrowser = async (executable, url) => {
      globalThis.syntheticBrowserUrls.push({ executable, url });
    };
  });
  try {
    await page.getByRole("button", { name: "卡片市集", exact: true }).click();
    const picker = page.getByRole("dialog", { name: "选择浏览器", exact: true });
    await picker.getByRole("button", { name: `使用 ${selected.name}`, exact: true }).waitFor();
    assert.equal(await application.evaluate(() => globalThis.syntheticBrowserUrls.length), 0);
    await picker.getByRole("button", { name: "取消", exact: true }).click();
    assert.equal(await application.evaluate(() => globalThis.syntheticBrowserUrls.length), 0);
    await page.getByRole("button", { name: "卡片市集", exact: true }).click();
    await picker.getByRole("button", { name: `使用 ${selected.name}`, exact: true }).waitFor();
    await page.screenshot({ path: path.join(root, ".backup/browser-picker.png") });
    await picker.getByRole("button", { name: `使用 ${selected.name}`, exact: true }).click();
    const browserDeadline = Date.now() + 10000;
    while (!(await application.evaluate(() => globalThis.syntheticBrowserUrls.length))) {
      if (Date.now() > browserDeadline) throw new Error("Market did not launch the selected browser");
      await page.waitForTimeout(50);
    }
    const opened = await application.evaluate(() => globalThis.syntheticBrowserUrls);
    assert.equal(opened[0].url, "https://market.synthetic.test/#login=synthetic-ticket");
    if (/Edge/.test(selected.name)) assert.equal(path.basename(opened[0].executable).toLowerCase(), "msedge.exe");
    assert.equal(page.url(), origin + "/#/home");
    await assert.rejects(page.evaluate(id => window.desktopBrowser.openExternal("file:///C:/Windows/explorer.exe", id), selected.id));
    await assert.rejects(page.evaluate(() => window.desktopBrowser.openExternal("https://example.test/", "unknown-browser")));
    await application.evaluate(() => {
      globalThis.browserTestModule.launchBrowser = async () => { throw new Error("SYNTHETIC_UNAVAILABLE_BROWSER"); };
    });
    await page.getByRole("button", { name: "卡片市集", exact: true }).click();
    await picker.getByRole("button", { name: `使用 ${selected.name}`, exact: true }).click();
    await picker.getByText("无法启动所选浏览器，请尝试其他浏览器。", { exact: true }).waitFor();
    await picker.getByRole("button", { name: "取消", exact: true }).click();
    assert.equal(page.url(), origin + "/#/home");
    await application.evaluate(async ({ dialog, app }) => {
      const script = app.isPackaged ? process.resourcesPath + "/browser-tools/detect-browsers.ps1" : undefined;
      const choices = await globalThis.browserTestModule.discoverBrowsers(script);
      globalThis.browserTestModule.launchBrowser = async (executable, url) => {
        globalThis.syntheticBrowserUrls.push({ executable, url });
      };
      dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [choices[0].executable] });
    });
    await page.getByRole("button", { name: "卡片市集", exact: true }).click();
    await picker.getByRole("button", { name: "选择浏览器程序", exact: true }).click();
    await picker.waitFor({ state: "hidden" });
    assert.equal(await application.evaluate(() => globalThis.syntheticBrowserUrls.length), 2);
    await page.unroute("**/api/market/web-login");
    await page.route("**/api/market/web-login", route => route.fulfill({
      status: 409, json: { detail: { error_code: "BIND_REQUIRED", message: "请先登录市集账号。" } }
    }));
    await page.route("**/api/market/binding", route => route.fulfill({
      json: { bound: false, base_url: "https://market.synthetic.test/" }
    }));
    await page.getByRole("button", { name: "卡片市集", exact: true }).click();
    await picker.getByRole("button", { name: `使用 ${selected.name}`, exact: true }).click();
    await picker.waitFor({ state: "hidden" });
    assert.equal(await application.evaluate(() => globalThis.syntheticBrowserUrls.at(-1).url),
      "https://market.synthetic.test/");
  } finally {
    await application.evaluate(({ dialog }) => {
      globalThis.browserTestModule.launchBrowser = globalThis.originalBrowserLaunch;
      dialog.showOpenDialog = globalThis.originalBrowserDialog;
    });
  }
  assert.equal(errors.length, 0, errors.join("\n"));
  console.log(JSON.stringify({ passed: true, bootMilliseconds, userSession: true, gatewayIsolation: true,
    builtinVideo: true, browserPicker: true, cancelDoesNotLaunch: true, manualBrowserSelection: true,
    screenshots: ["assets/desktop-home.png", "assets/desktop-settings.png"] }));
} finally { await application.close(); }
