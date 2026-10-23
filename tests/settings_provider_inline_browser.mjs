import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { mockBuiltFrontend } from "./frontend_dist_mock.mjs";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PW_ROOT + "/playwright-core");
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const served = await mockBuiltFrontend(page);
const output = fileURLToPath(new URL("../.backup/settings-provider-inline", import.meta.url));
await mkdir(output, { recursive: true });
const checks = [], errors = [], unexpected = [], writes = [], catalogs = [], probes = [];
let failSave = false, failTest = false;
const item = (kind, protocol, extra = {}) => ({
  config_id: "synthetic-" + kind, kind, protocol, owner_scope: kind === "ocr" ? "admin" : "user",
  endpoint: `https://${kind.replaceAll("_", "-")}.example.test/v1`, model_name: kind + "-model", credential_mask: "****",
  config_version: 1, is_active: true, capability_status: {}, ...extra,
});
let configs = [item("image_generation", "openai_image"), item("tts", "minimax"),
  item("ocr", "paddleocr"), item("search", "anysearch")];
const json = (route, body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
page.on("pageerror", error => errors.push(error.message));
await page.route("**/api/**", async route => {
  const request = route.request(), url = new URL(request.url()), method = request.method(), p = url.pathname;
  if (p === "/api/auth/me") return json(route, { user_id: "synthetic-owner", is_admin: true, status: "approved" });
  if (p === "/api/capabilities/model-roles") return json(route, { data: ["planning", "teaching", "dialogue", "research"].map(role =>
    ({ role, revision: 0, config_id: null, configuration: null })) });
  if (["/api/connectors", "/api/auth/admin/pending"].includes(p)) return json(route, { data: [] });
  if (p === "/api/capabilities/configs" && method === "GET") return json(route, { data: configs });
  if (p === "/api/capabilities/reasoning/describe") return json(route, {
    schema_version: 1, rules_version: "synthetic", source: "unknown", supported_modes: ["default"],
    effort_values: [], budget: null, summary_supported: false, control_kind: "unknown",
  });
  if (p === "/api/capabilities/models") { catalogs.push(request.postDataJSON()); return json(route, { models: ["synthetic-catalog-model"] }); }
  if (p === "/api/capabilities/tts/voices") return json(route, { data: [
    { voice_id: "synthetic-voice", name: "合成音色", category: "system_voice", description: "" },
  ] });
  const match = p.match(/^\/api\/capabilities\/configs\/([^/]+)(\/test)?$/);
  if (match?.[2]) {
    probes.push(match[1]);
    return failTest ? json(route, { detail: { error_code: "ERROR" } }, 503)
      : json(route, { capability_status: { text: { state: "available" } } });
  }
  if ((p === "/api/capabilities/configs" && method === "POST") || (match && method === "PATCH")) {
    const body = request.postDataJSON();
    writes.push({ method, id: match?.[1], body });
    if (failSave) return json(route, { detail: { error_code: "ERROR", message: "synthetic-hidden-error" } }, 500);
    const old = configs.find(value => value.config_id === match?.[1]);
    const saved = old ? { ...old, ...body, config_version: old.config_version + 1 }
      : item(body.kind, body.protocol, { ...body, config_id: "synthetic-created-" + writes.length });
    delete saved.credentials;
    configs = old ? configs.map(value => value.config_id === old.config_id ? saved : value) : [...configs, saved];
    return json(route, saved);
  }
  if (match && method === "DELETE") { configs = configs.filter(value => value.config_id !== match[1]); return json(route, { ok: true }); }
  unexpected.push(method + " " + p);
  return json(route, { detail: { error_code: "ERROR" } }, 404);
});
const region = title => page.getByRole("region", { name: title + "配置", exact: true });
const media = async () => { await page.goto(served.origin + "/#/capabilities?section=media"); await region("课程封面图片模型").waitFor(); };
try {
  await media();
  let image = region("课程封面图片模型");
  assert.equal(await image.getByLabel("模型", { exact: true }).inputValue(), "image_generation-model");
  assert.equal(await page.getByRole("heading", { name: "当前配置" }).count(), 0);
  assert.equal(await page.getByRole("button", { name: "编辑", exact: true }).count(), 0);
  await image.getByLabel("模型", { exact: true }).fill("inline-updated");
  await image.getByRole("button", { name: "保存", exact: true }).click();
  await page.waitForFunction(() => document.querySelector("#provider-image_generation-key")?.placeholder.includes("留空"));
  assert.equal(writes.at(-1).method, "PATCH");
  assert.equal(writes.at(-1).id, "synthetic-image_generation");
  assert(!("credentials" in writes.at(-1).body));
  await page.reload(); await region("课程封面图片模型").waitFor();
  assert.equal(await region("课程封面图片模型").getByLabel("模型", { exact: true }).inputValue(), "inline-updated");
  checks.push("saved fields stay in their original form and persist across reload");

  image = region("课程封面图片模型");
  failSave = true;
  await image.getByLabel("模型", { exact: true }).fill("failed-draft");
  await image.getByLabel("API Key", { exact: true }).fill("synthetic-replacement");
  await image.getByRole("button", { name: "显示输入的密钥" }).click();
  assert.equal(await image.getByLabel("API Key", { exact: true }).getAttribute("type"), "text");
  await image.getByRole("button", { name: "保存", exact: true }).click();
  await image.getByRole("alert").waitFor();
  assert.equal(await image.getByLabel("模型", { exact: true }).inputValue(), "failed-draft");
  assert.equal(await image.getByLabel("API Key", { exact: true }).inputValue(), "synthetic-replacement");
  assert(!(await page.locator("body").innerText()).includes("synthetic-hidden-error"));
  failSave = false;
  await image.getByRole("button", { name: "测试课程封面图片模型" }).click();
  await image.getByText("文本", { exact: true }).waitFor();
  assert.equal(writes.at(-1).body.credentials.api_key, "synthetic-replacement");
  assert.equal(probes.at(-1), "synthetic-image_generation");
  assert.equal(await image.getByLabel("API Key", { exact: true }).inputValue(), "");
  checks.push("failed drafts survive; testing saves current values first and clears accepted secrets");

  await image.getByRole("button", { name: "获取可用模型：模型" }).click();
  await image.getByRole("button", { name: "synthetic-catalog-model", exact: true }).click();
  assert.equal(catalogs.at(-1).config_id, "synthetic-image_generation");
  assert(!catalogs.at(-1).api_key);
  checks.push("model catalog reuses the saved configuration");

  let tts = region("个人语音");
  await tts.getByLabel("模型", { exact: true }).fill("speech-updated");
  await tts.getByRole("button", { name: "保存", exact: true }).click();
  await page.waitForFunction(() => document.querySelector("#provider-tts-key")?.placeholder.includes("留空"));
  assert.equal(writes.at(-1).id, "synthetic-tts");
  assert(!("credentials" in writes.at(-1).body));
  await tts.getByLabel("API Key", { exact: true }).fill("synthetic-tts-replacement");
  assert(await tts.getByRole("button", { name: "保存", exact: true }).isDisabled());
  await tts.getByRole("button", { name: "获取可用音色" }).click();
  await tts.getByLabel("音色", { exact: true }).selectOption("synthetic-voice");
  await tts.getByRole("button", { name: "保存", exact: true }).click();
  await page.waitForFunction(() => document.querySelector("#provider-tts-key")?.placeholder.includes("留空"));
  assert.deepEqual(writes.at(-1).body.credentials, { api_key: "synthetic-tts-replacement", voice_id: "synthetic-voice" });
  checks.push("TTS blank retains stored voice and key; rotation requires a queried voice");

  const video = region("人物视频模型");
  await video.getByLabel("协议", { exact: true }).selectOption("seedance");
  await video.getByLabel("模型", { exact: true }).fill("synthetic-video");
  await video.getByLabel("API Key", { exact: true }).fill("synthetic-video-credential");
  failTest = true;
  await video.getByRole("button", { name: "测试人物视频模型" }).click();
  await page.waitForFunction(() => document.querySelector("#provider-video_generation-key")?.placeholder.includes("留空"));
  await video.getByRole("alert").waitFor();
  assert((await video.getByRole("alert").innerText()).includes("配置已保存"));
  assert.equal(writes.at(-1).method, "POST");
  const beforeRetry = writes.length;
  failTest = false;
  await video.getByRole("button", { name: "测试人物视频模型" }).click();
  await video.getByText("文本", { exact: true }).waitFor();
  assert.equal(writes.length, beforeRetry);
  checks.push("new video stays in place after save even when optional probe fails; retry avoids duplicates");
  await video.getByLabel("协议", { exact: true }).selectOption("minimax_video");
  assert(await video.getByRole("button", { name: "保存", exact: true }).isDisabled());
  await video.getByLabel("API Key", { exact: true }).fill("synthetic-other-video-credential");
  await video.getByRole("button", { name: "保存", exact: true }).click();
  await page.waitForFunction(() => document.querySelector("#provider-video_generation-key")?.placeholder.includes("留空"));
  assert.equal(writes.at(-1).method, "POST");
  assert.equal(writes.at(-1).body.protocol, "minimax_video");
  assert.equal(writes.at(-1).body.credentials.api_key, "synthetic-other-video-credential");
  checks.push("video protocol changes require their own key and create a new configuration");

  for (const width of [1440, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.evaluate(() => document.querySelector(".settings-scroll").scrollTop = 0);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    const overflow = await page.locator(".provider-connection input,.provider-connection select").evaluateAll(elements =>
      elements.some(element => element.getBoundingClientRect().right > innerWidth));
    assert(!overflow, "provider field overflow at " + width);
    await page.screenshot({ path: path.join(output, `media-${width}.png`), fullPage: true });
  }
  checks.push("four responsive widths keep provider fields inside the viewport");

  await page.goto(served.origin + "/#/capabilities?section=admin");
  const ocr = region("管理员云 OCR");
  await ocr.waitFor();
  await ocr.getByLabel("模型", { exact: true }).fill("ocr-inline-updated");
  await ocr.getByRole("button", { name: "保存", exact: true }).click();
  await page.waitForFunction(() => document.querySelector("#provider-ocr-key")?.placeholder.includes("留空"));
  assert.equal(writes.at(-1).id, "synthetic-ocr");
  assert(!("credentials" in writes.at(-1).body));
  checks.push("administrator OCR uses the same inline save behavior");

  await media();
  await region("课程封面图片模型").getByRole("button", { name: "删除", exact: true }).click();
  await page.getByRole("button", { name: "确认删除", exact: true }).click();
  await page.waitForFunction(() => document.querySelector("#provider-image_generation-key")?.placeholder === "填写 API Key");
  assert(!configs.some(value => value.config_id === "synthetic-image_generation"));
  checks.push("existing deactivation confirmation remains available in place");
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  assert.equal(await page.getByRole("button", { name: "测试搜索" }).getAttribute("title"), "测试");
  assert.equal(await page.getByText("搜索：已启用",{exact:true}).count(),0);
  assert.equal(await page.getByText("上次验证已过期",{exact:true}).count(),0);
  const searchSection=page.getByRole("heading",{name:"AnySearch 搜索",exact:true}).locator("..");
  assert.equal(await searchSection.locator('input[type="url"]').count(),0);
  checks.push("search uses a small test icon and no separate saved-summary page");
  assert.deepEqual(errors, []); assert.deepEqual(unexpected, []); assert.deepEqual(served.unexpected, []);
  await writeFile(path.join(output, "checks.json"), JSON.stringify({ checks, errors, unexpected }, null, 2));
  console.log(JSON.stringify({ checks: checks.length, details: checks, errors }, null, 2));
} finally { await browser.close(); }
