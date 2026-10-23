import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import { mockBuiltFrontend } from "./frontend_dist_mock.mjs";

const { chromium } = createRequire(import.meta.url)(process.env.PW_ROOT + "/playwright-core");
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
const output = process.env.TEST_OUTPUT || ".backup/material-questions-browser";
await mkdir(output, { recursive: true });
const checks = [];
try {
  for (const width of [1440, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, serviceWorkers: "block" });
    const page = await context.newPage();
    const served = await mockBuiltFrontend(page);
    const errors = [], calls = [];
    let committed = false;
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      calls.push({ path, method: request.method() });
      const json = body => route.fulfill({ json: body });
      if (path === "/api/auth/me") return json({ user_id: "synthetic-question-owner", status: "approved", is_admin: false });
      if (path === "/api/planning/current") return json({ planning: null });
      if (path === "/api/navigation") return json({});
      if (path === "/api/planning/input-matches") return json({ matches: [] });
      if (path === "/api/capabilities/model-roles") return json({ data: [] });
      const item = { item_id: "si_synthetic", client_id: "synthetic", kind: "document",
        name: "synthetic.pdf", status: committed ? "parsing" : "uploaded", material_id: committed ? "mat_synthetic" : null };
      const destinations = [
        { config_id: "ocr", config_version: 1, endpoint: "https://ocr.example.test", purpose: "document" },
        { config_id: "semantic", config_version: 2, endpoint: "https://semantic.example.test", purpose: "semantic" },
        { config_id: "visual", config_version: 3, endpoint: "https://visual.example.test", purpose: "visual" },
      ];
      if (path === "/api/source-imports" && request.method() === "POST") {
        item.client_id = request.postDataJSON().items[0].client_id;
        return json({ batch_id: "src_synthetic", status: "draft", items: [item] });
      }
      if (path.endsWith("/file")) return json({
        batch_id: "src_synthetic", status: "draft", items: [item],
      });
      if (path.endsWith("/prepare")) return json({
        batch_id: "src_synthetic", status: "prepared", manifest_hash: "synthetic-manifest",
        items: [item], material_ids: [], destinations,
      });
      if (path.endsWith("/commit")) {
        assert.equal(request.postDataJSON().confirmed, true);
        assert.equal(request.postDataJSON().manifest_hash, "synthetic-manifest");
        committed = true;
        return json({ batch_id: "src_synthetic", status: "processing", items: [{ ...item, status: "parsing" }], destinations });
      }
      if (path === "/api/source-imports/src_synthetic") return json({
        batch_id: "src_synthetic", status: "processing", items: [{ ...item, status: "parsing" }], destinations,
      });
      return json({ data: [] });
    });
    try {
      await page.goto(served.origin + "/#/home");
      const editor = page.getByLabel("学习目标", { exact: true });
      await editor.fill("复核合成资料");
      await editor.evaluate(element => {
        const clipboard = new DataTransfer();
        clipboard.items.add(new File(["%PDF-synthetic"], "synthetic.pdf", { type: "application/pdf" }));
        element.dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: clipboard }));
      });
      await page.getByText("synthetic.pdf", { exact: true }).waitFor();
      assert.equal(calls.filter(call => call.path === "/api/source-imports").length, 0);
      await page.getByRole("button", { name: "生成规划草稿", exact: true }).click();
      const consent = page.getByRole("dialog");
      await consent.waitFor();
      for (const hostname of ["ocr.example.test", "semantic.example.test", "visual.example.test"]) {
        await consent.getByText(new RegExp(hostname.replaceAll(".", "\\."))).waitFor();
      }
      assert.equal(committed, false);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
      await page.screenshot({ path: `${output}/destinations-${width}.png`, fullPage: true });
      const check = consent.getByRole("checkbox");
      if (await check.count()) await check.check();
      const buttons = consent.getByRole("button");
      const names = await buttons.allTextContents();
      const confirm = names.find(name => /确认|继续|发送|处理/.test(name));
      assert(confirm, `Missing confirmation action: ${names}`);
      await consent.getByRole("button", { name: confirm, exact: true }).click();
      await page.waitForTimeout(300);
      assert.equal(committed, true);
      assert.deepEqual(errors, []);
      checks.push({ width, destinations: 3, explicit_confirmation: true, no_upload_on_paste: true });
    } catch (error) {
      await writeFile(`${output}/failure-${width}.json`, JSON.stringify({
        calls, errors, text: await page.locator("body").innerText(),
      }, null, 2));
      throw error;
    } finally {
      await context.close();
    }
  }
  await writeFile(`${output}/checks.json`, JSON.stringify({ checks }, null, 2));
  console.log(JSON.stringify({ checks }));
} finally {
  await browser.close();
}
