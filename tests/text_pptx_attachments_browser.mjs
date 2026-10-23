import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import { mockBuiltFrontend } from "./frontend_dist_mock.mjs";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PW_ROOT + "/playwright-core");
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
const output = process.env.TEST_OUTPUT || ".backup/text-pptx-browser";
await mkdir(output, { recursive: true });
const config = { config_id: "synthetic-parser", config_version: 1,
  endpoint: "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs" };
const checks = [];
const files = {
  txt: { name: "synthetic.txt", mimeType: "text/plain", buffer: Buffer.from("Synthetic local text") },
  md: { name: "synthetic.md", mimeType: "text/markdown", buffer: Buffer.from("# Synthetic\n\nLocal source") },
  pptx: { name: "synthetic.pptx", mimeType: "application/octet-stream", buffer: Buffer.from("Synthetic PPTX") },
};
async function scenario(width, name, run) {
  const context = await browser.newContext({ viewport: { width, height: 1000 }, serviceWorkers: "block" });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const served = await mockBuiltFrontend(page);
  const state = { config: null, uploads: [], items: [], errors: [], preflights: 0 };
  const json = (route, body, status = 200) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  page.on("pageerror", error => state.errors.push(error.message));
  await page.route("**/api/**", async route => {
    const request = route.request(), path = new URL(request.url()).pathname, method = request.method();
    if (path === "/api/auth/me") return json(route,
      { user_id: "synthetic-local-owner", status: "approved", is_admin: false, display_name: "合成用户" });
    if (path === "/api/attachments/config") return json(route, { config: state.config,
      max_text_bytes: 20 * 1024 * 1024, max_pptx_bytes: 200000000, max_pptx_slides: 300 });
    if (path === "/api/attachments/preflight" || path === "/api/attachments" && method === "POST") {
      const body = request.postDataBuffer().toString();
      const kind = body.includes('filename="synthetic.pptx"') ? "pptx" :
        body.includes('filename="synthetic.md"') ? "md" : "txt";
      const local = kind !== "pptx";
      if (path.endsWith("/preflight")) {
        state.preflights++;
        return json(route, { kind, local, requires_ocr: !local, page_count: local ? 0 : 17,
          unit: local ? "document" : "slide", strategy: local ? "local" : "direct" });
      }
      assert.equal(body.includes('name="config_id"'), !local);
      assert.equal(body.includes('name="confirmed"'), !local);
      if (!local) assert(state.config);
      state.uploads.push(kind);
      const item = { material_id: "synthetic-" + kind, task_id: "synthetic-task-" + kind, kind,
        local, unit: local ? "document" : "slide", status: "parsed", page_count: local ? 0 : 17,
        processed_pages: local ? 0 : 17, strategy: local ? "local" : "batched",
        batch_count: local ? 0 : 2, completed_batches: local ? 0 : 2,
        batches: local ? [] : [{ start_page: 1, end_page: 8, status: "completed" },
          { start_page: 9, end_page: 17, status: "completed" }] };
      state.items.push(item);
      return json(route, item, 201);
    }
    if (path === "/api/attachments") return json(route, { data: state.items });
    const item = state.items.find(value => path === "/api/attachments/" + value.material_id);
    if (item && method === "GET") return json(route, item);
    if (item && method === "DELETE") {
      state.items = state.items.filter(value => value !== item);
      return json(route, {});
    }
    return json(route, { data: [], planning: null, matches: [] });
  });
  try {
    await page.goto(served.origin + "/#/");
    await page.getByRole("heading", { name: "你今天想弄懂什么？" }).waitFor();
    await run(page, state);
    assert.deepEqual(state.errors, []);
    assert.deepEqual(served.unexpected, []);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await page.screenshot({ path: `${output}/${name}-${width}.png`, fullPage: true });
    checks.push(`${width}: ${name}`);
  } finally {
    await context.close();
  }
}
try {
  for (const width of [1440, 390]) {
    await scenario(width, "local-without-ocr", async (page, state) => {
      await page.getByLabel("选择 PDF 或 Word 资料").setInputFiles([files.txt, files.md]);
      await page.getByText("已读取", { exact: true }).first().waitFor();
      await page.waitForFunction(() => document.querySelectorAll(".attachment-list li").length === 2);
      assert.deepEqual(state.uploads, ["txt", "md"]);
      assert.equal(await page.getByRole("dialog").count(), 0);
      assert.equal(await page.getByText(/第 \d+ 页/).count(), 0);
      assert.equal(await page.getByRole("link", { name: /下载 synthetic.txt/ }).getAttribute("download"), "attachment.txt");
      await page.reload();
      await page.getByText("已读取", { exact: true }).first().waitFor();
    });
    await scenario(width, "mixed-missing-config", async (page, state) => {
      await page.getByLabel("选择 PDF 或 Word 资料").setInputFiles([files.txt, files.pptx]);
      await page.getByRole("dialog", { name: "设置文档识别" }).waitFor();
      assert.deepEqual(state.uploads, ["txt"]);
      await page.getByRole("button", { name: "取消", exact: true }).click();
      await page.getByText("已读取", { exact: true }).waitFor();
      await page.getByText("等待配置文档识别服务，文本资料已单独处理。", { exact: true }).waitFor();
      state.config = config;
      await page.getByRole("button", { name: "重试 synthetic.pptx", exact: true }).click();
      const consent = page.getByRole("dialog", { name: "确认识别资料" });
      await consent.waitFor();
      assert.deepEqual(state.uploads, ["txt"]);
      await consent.getByRole("checkbox").check();
      await consent.getByRole("button", { name: "上传并识别", exact: true }).click();
      await page.getByText("已识别 · 17 张幻灯片", { exact: true }).waitFor();
      await page.getByRole("button", { name: "synthetic.pptx", exact: true }).click();
      await page.getByText("第 9–17 张幻灯片", { exact: false }).waitFor();
    });
    await scenario(width, "mixed-authorized", async (page, state) => {
      state.config = config;
      await page.getByLabel("选择 PDF 或 Word 资料").setInputFiles([files.md, files.pptx]);
      const consent = page.getByRole("dialog", { name: "确认识别资料" });
      await consent.waitFor();
      await consent.getByText("本地读取，不发送 OCR ·", { exact: false }).waitFor();
      assert.equal(state.uploads.length, 0);
      assert(await consent.getByRole("button", { name: "上传并识别", exact: true }).isDisabled());
      await consent.getByRole("checkbox").check();
      await consent.getByRole("button", { name: "上传并识别", exact: true }).click();
      await page.getByText("已识别 · 17 张幻灯片", { exact: true }).waitFor();
      await page.getByText("已读取", { exact: true }).waitFor();
      assert.deepEqual(state.uploads, ["md", "pptx"]);
    });
  }
  console.log(JSON.stringify({ passed: checks }, null, 2));
} finally {
  await browser.close();
}
