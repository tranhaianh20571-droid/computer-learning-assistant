import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import { mockBuiltFrontend } from "./frontend_dist_mock.mjs";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PW_ROOT + "/playwright-core");
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
const output = process.env.TEST_OUTPUT || ".backup/document-attachments";
await mkdir(output, { recursive: true });
const config = { config_id: "synthetic-doc-config", config_version: 1, kind: "document_parser",
  protocol: "paddleocr_vl", endpoint: "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs",
  model_name: "PaddleOCR-VL-1.6", owner_scope: "user",
  is_active: true, capability_status: {}, credential_mask: "****" };
const checks = [];

async function scenario(width, test) {
  const context = await browser.newContext({ viewport: { width, height: 1000 }, serviceWorkers: "block" });
  const page = await context.newPage();
  page.setDefaultTimeout(7000);
  const served = await mockBuiltFrontend(page);
  const state = { config: null, uploads: 0, status: "parsing", deleted: 0, planning: null, errors: [], library: 0,
    failUpload: false, long: false, processed: 150, preflights: 0 };
  const attachment = () => ({ material_id: "synthetic-mat", status: state.status, kind: "pdf",
    ...(state.long ? { page_count: 338, processed_pages: state.status === "parsed" ? 338 : state.processed,
      strategy: "batched", batch_count: 7, completed_batches: state.status === "parsed" ? 7 : 3,
      figure_review_pages: Array.from({ length: 104 }, (_, i) => i + 1), complex_table_pages: [12],
      batches: [{ start_page: 1, end_page: 50, status: "completed" },
        { start_page: 151, end_page: 200, status: state.status === "parsed" ? "completed" : "processing" }] } : {}) });
  const json = (route, body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  page.on("pageerror", error => state.errors.push(error.message));
  await page.route("**/api/**", async route => {
    const request = route.request(), url = new URL(request.url()), method = request.method();
    const path = url.pathname;
    if (path === "/api/auth/me") return json(route, { user_id: "synthetic-doc-owner", is_admin: false, status: "approved", display_name: "合成用户" });
    if (path === "/api/planning/current") return json(route, { planning: null });
    if (path.includes("navigation")) return json(route, {});
    if (path === "/api/materials") { state.library++; return json(route, { data: [] }); }
    if (path === "/api/connectors") return json(route, { data: [] });
    if (path === "/api/attachments/config") return json(route, { config: state.config });
    if (path === "/api/attachments/preflight") {
      state.preflights++;
      return json(route, { kind: "pdf", page_count: state.long ? 338 : 1,
        strategy: state.long ? "batched" : "short", size_bytes: state.long ? 323000000 : 1000 });
    }
    if (path === "/api/capabilities/configs" && method === "GET") return json(route, { data: state.config ? [state.config] : [] });
    if (path === "/api/capabilities/configs" && method === "POST") {
      const body = request.postDataJSON();
      assert.equal(body.kind, "document_parser"); assert.equal(body.protocol, "paddleocr_vl");
      assert.equal(body.model_name, "PaddleOCR-VL-1.6");
      assert.equal(body.credentials.api_key, "synthetic-document-key");
      state.config = config;
      return json(route, config, 201);
    }
    if (path === "/api/capabilities/configs/synthetic-doc-config/test") return json(route, { capability_status: { connection: { state: "available" } } });
    if (path === "/api/capabilities/configs/synthetic-doc-config" && method === "DELETE") {
      state.config = null; return json(route, { ...config, is_active: false });
    }
    if (path === "/api/attachments" && method === "POST") {
      state.uploads++;
      assert(request.postDataBuffer().includes(Buffer.from('name="confirmed"\r\n\r\ntrue')));
      if (state.failUpload) return json(route, { error_code: "invalid_file_signature" }, 400);
      return json(route, { ...attachment(), task_id: "synthetic-parse" }, 201);
    }
    if (path === "/api/attachments" && method === "GET") return json(route, { data: [attachment()] });
    if (path === "/api/attachments/synthetic-mat" && method === "GET") return json(route, { ...attachment(), page_count: state.long ? 338 : 1 });
    if (path === "/api/attachments/synthetic-mat/markdown") return route.fulfill({
      status: 200, contentType: "text/markdown", body: "Synthetic source transcription",
      headers: { "Content-Disposition": 'attachment; filename="attachment.md"' } });
    if (path === "/api/attachments/synthetic-mat" && method === "DELETE") { state.deleted++; return json(route, {}); }
    if (path === "/api/attachments/synthetic-mat/retry") { state.status = "parsed"; return json(route, { material_id: "synthetic-mat", status: "parsed", kind: "pdf", page_count: 1 }); }
    if (path.includes("reasoning/describe")) return json(route, { supported_modes: ["default"] });
    if (path.includes("input-matches")) return json(route, { matches: [] });
    if (path === "/api/routes" && method === "POST") return json(route, { route_id: "synthetic-route" }, 201);
    if (path === "/api/planning/tasks" && method === "POST") { state.planning = request.postDataJSON(); return json(route, { task_id: "synthetic-plan" }, 201); }
    if (path.startsWith("/api/planning/") || path.startsWith("/api/courses") || path.startsWith("/api/navigation")) return json(route, { data: [], planning: null });
    return json(route, { data: [] });
  });
  const fit = async () => {
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    assert.deepEqual(state.errors, []);
  };
  const choose = async () => {
    await page.waitForFunction(() => {
      const input = document.querySelector('input[aria-label="选择 PDF 或 Word 资料"]');
      return input && !input.disabled;
    });
    await page.getByLabel("选择 PDF 或 Word 资料").setInputFiles({
      name: "用于验证超长中文文件名仍能正确换行的数据库学习资料.pdf",
      mimeType: "application/pdf", buffer: Buffer.from("%PDF-synthetic"),
    });
  };
  try {
    await page.goto(served.origin + "/#/");
    await page.getByRole("heading", { name: "你今天想弄懂什么？" }).waitFor();
    await test({ page, state, choose, fit, served });
    assert.equal(state.library, 0, "task entry must not read the future library");
    assert.deepEqual(served.unexpected, []);
    await fit();
    checks.push(`${width}: ${test.name}`);
  } finally { await context.close(); }
}

try {
  for (const width of [1440, 390]) {
    await scenario(width, async function settingsAndDirectUpload({ page, state, choose, fit, served }) {
      await choose();
      await page.getByRole("dialog", { name: "设置文档识别" }).waitFor();
      assert.equal(state.uploads, 0);
      await page.getByRole("link", { name: "前往设置" }).click();
      await page.getByRole("heading", { name: "PaddleOCR 文档识别" }).waitFor();
      const section = page.getByRole("region", { name: "PaddleOCR 文档识别" });
      assert.equal(await section.getByRole("link", { name: "获取密钥" }).getAttribute("href"),
        "https://aistudio.baidu.com/paddleocr");
      await section.getByLabel("API Key", { exact: true }).fill("synthetic-document-key");
      await section.getByRole("button", { name: "保存密钥", exact: true }).click();
      await section.getByText("密钥已保存。", { exact: true }).waitFor();
      assert.equal(await section.getByLabel("API Key", { exact: true }).inputValue(), "");
      await section.getByRole("button", { name: "测试 PaddleOCR 连接" }).click();
      await section.getByText("连接验证通过；尚未上传文件。").waitFor();
      await fit();
      await page.screenshot({ path: `${output}/settings-${width}.png`, fullPage: true });
      await page.goto(served.origin + "/#/");
      await page.getByLabel("学习目标", { exact: true }).fill("学习数据库事务");
      await choose();
      const consent = page.getByRole("dialog", { name: "确认识别资料" });
      await consent.waitFor();
      assert.equal(state.uploads, 0);
      assert(await consent.getByRole("button", { name: "上传并识别" }).isDisabled());
      await consent.getByRole("checkbox").check();
      await consent.getByRole("button", { name: "上传并识别" }).click();
      await page.getByText("识别中…", { exact: true }).waitFor();
      assert(await page.getByRole("button", { name: "生成规划草稿", exact: true }).isDisabled());
      state.status = "parsed";
      await page.getByText("已识别 · 1 页", { exact: true }).waitFor({ timeout: 10000 });
      assert(!(await page.getByRole("button", { name: "生成规划草稿", exact: true }).isDisabled()));
      await fit();
      await page.screenshot({ path: `${output}/attached-${width}.png`, fullPage: true });
      await page.reload();
      await page.getByText("已识别", { exact: true }).waitFor();
      await page.getByRole("button", { name: "移除 资料", exact: true }).click();
      assert.equal(state.deleted, 1);
      await choose(); await page.getByRole("checkbox").check();
      await page.getByRole("button", { name: "上传并识别" }).click();
      await page.getByText("已识别", { exact: true }).waitFor();
      await page.getByLabel("学习目标", { exact: true }).fill("学习数据库事务");
      await page.getByRole("button", { name: "生成规划草稿", exact: true }).click();
      await page.waitForFunction(() => location.hash.includes("/planning"));
      assert.deepEqual(state.planning.material_ids, ["synthetic-mat"]);
    });
    await scenario(width, async function failureRetryAndRemoval({ page, state, choose }) {
      state.config = config; state.status = "failed";
      await choose();
      await page.getByRole("checkbox").check();
      await page.getByRole("button", { name: "上传并识别" }).click();
      await page.getByRole("button", { name: /^重试 / }).waitFor();
      await page.getByRole("button", { name: /^重试 / }).click();
      await page.getByRole("checkbox").check();
      await page.getByRole("button", { name: "上传并识别" }).click();
      await page.getByText("已识别 · 1 页", { exact: true }).waitFor();
      await page.getByRole("button", { name: /^移除 / }).click();
      assert.equal(state.deleted, 1);
    });
    await scenario(width, async function rejectedUploadCanBeRetried({ page, state, choose }) {
      state.config = config; state.failUpload = true;
      await choose(); await page.getByRole("checkbox").check();
      await page.getByRole("button", { name: "上传并识别" }).click();
      await page.getByText("文件内容与格式不匹配，或文件已损坏。", { exact: true }).waitFor();
      state.failUpload = false; state.status = "parsed";
      await page.getByRole("button", { name: /^重试 / }).click();
      await page.getByRole("checkbox").check();
      await page.getByRole("button", { name: "上传并识别" }).click();
      await page.getByText("已识别", { exact: true }).waitFor();
      assert.equal(state.uploads, 2);
    });
    await scenario(width, async function longDocumentProgressAndDownload({ page, state, choose, fit }) {
      state.config = config; state.long = true;
      await page.getByLabel("学习目标", { exact: true }).fill("完整学习合成资料");
      await choose();
      const consent = page.getByRole("dialog", { name: "确认识别资料" });
      await consent.getByText("338 页 · 发送至 PaddleOCR · 分批完整识别", { exact: true }).waitFor();
      assert.equal(state.uploads, 0);
      assert.equal(state.preflights, 1);
      await consent.getByRole("checkbox").check();
      await consent.getByRole("button", { name: "上传并识别" }).click();
      await page.getByText("已识别 150 / 338 页", { exact: true }).waitFor();
      assert(await page.getByRole("button", { name: "生成规划草稿", exact: true }).isDisabled());
      await page.getByRole("button", { name: /^用于验证超长中文/ }).click();
      const details = page.getByRole("dialog", { name: "资料识别进度" });
      await details.getByText("分批完整识别 · 3 / 7 批完成", { exact: true }).waitFor();
      await details.getByText(/图片未返回图中文字，图表内知识可能缺失/).waitFor();
      const bounds = await details.boundingBox();
      assert(bounds.y >= 0 && bounds.y + bounds.height <= 1000, "long review list must stay inside viewport");
      await fit();
      await page.screenshot({ path: `${output}/long-progress-${width}.png`, fullPage: true });
      await details.getByRole("button", { name: "关闭", exact: true }).click();
      await page.reload();
      await page.getByText("已识别 150 / 338 页", { exact: true }).waitFor();
      state.status = "parsed";
      await page.getByText("已识别 · 338 页", { exact: true }).waitFor({ timeout: 10000 });
      const download = page.getByRole("link", { name: /下载 .*识别原文/ });
      assert.match(await download.getAttribute("href"), /attachments\/synthetic-mat\/markdown/);
      assert.equal(await download.count(), 1);
      const downloaded = page.waitForEvent("download");
      await download.click();
      assert.equal((await downloaded).suggestedFilename(), "attachment.md");
      await fit();
    });
  }
  console.log(JSON.stringify({ passed: checks.length, checks, screenshots: output }));
} finally { await browser.close(); }
