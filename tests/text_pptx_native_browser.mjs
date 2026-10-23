import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";
const { chromium } = createRequire(import.meta.url)(process.env.PW_ROOT + "/playwright");
const base = process.env.TEST_FRONTEND, backend = process.env.TEST_BACKEND, output = process.env.TEST_OUTPUT;
assert.equal(new URL(backend).hostname, "127.0.0.1");
assert.notEqual(new URL(backend).port, "8000");
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
const checks = [];
try {
  for (const width of [1440, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 } });
    const page = await context.newPage();
    const errors = [], external = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", route => {
      const url = new URL(route.request().url());
      if (["127.0.0.1", "localhost"].includes(url.hostname)) return route.continue();
      external.push(url.origin);
      return route.abort();
    });
    try {
      await page.goto(base + "/#/login");
      await page.getByLabel("邮箱", { exact: true }).fill("synthetic-browser-member@example.com");
      await page.getByLabel("密码", { exact: true }).fill("synthetic-password-12345");
      await page.getByRole("button", { name: "登录", exact: true }).click();
      await page.waitForURL("**/#/home");
      const settings = await context.request.get(backend + "/api/attachments/config");
      assert.equal(settings.status(), 200);
      const configuration = await settings.json();
      assert.equal(configuration.config, null);
      assert.equal(configuration.max_text_bytes, 20 * 1024 * 1024);
      assert.equal(configuration.max_pptx_bytes, 200000000);
      const source = "# Synthetic chapter\n\nWhy does random sampling reduce selection bias?\n";
      await page.getByLabel("选择 PDF 或 Word 资料").setInputFiles([
        { name: "synthetic.txt", mimeType: "text/plain", buffer: Buffer.from(source) },
        { name: "synthetic.md", mimeType: "text/markdown", buffer: Buffer.from(source) },
      ]);
      await page.waitForFunction(() =>
        document.querySelectorAll(".attachment-list li").length === 2 &&
        [...document.querySelectorAll(".attachment-list [role=status]")].every(el => el.textContent === "已读取"));
      const list = await (await context.request.get(backend + "/api/attachments")).json();
      const latest = list.data.filter(item => item.status === "parsed").slice(0, 2);
      assert.equal(latest.length, 2);
      for (const item of latest) {
        assert.equal(item.unit, "document");
        assert.equal(item.local, true);
        assert.equal(item.page_count, 0);
        const download = await context.request.get(backend + `/api/attachments/${item.material_id}/markdown`);
        assert.equal(download.status(), 200);
        assert.equal(await download.text(), source);
      }
      const actor = await (await context.request.get(backend + "/api/auth/me")).json();
      const sourceCheck = spawnSync(process.env.TEST_PYTHON,
        ["tests/local_attachment_native_check.py", actor.user_id, ...latest.map(item => item.material_id)],
        { encoding: "utf8", env: process.env });
      assert.equal(sourceCheck.status, 0, sourceCheck.stderr);
      assert.equal(JSON.parse(sourceCheck.stdout).line_locators, true);
      assert.equal(await page.getByRole("dialog").count(), 0);
      await page.reload();
      await page.getByText("已读取", { exact: true }).first().waitFor();
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
      assert.deepEqual(errors, []);
      assert.deepEqual(external, []);
      await page.screenshot({ path: `${output}/native-local-${width}.png`, fullPage: true });
      checks.push(`native_local_upload_worker_download_reload_${width}`);
    } finally {
      await context.close();
    }
  }
  console.log(JSON.stringify({ checks, real_backend: true, external_calls: 0 }));
} finally {
  await browser.close();
}
