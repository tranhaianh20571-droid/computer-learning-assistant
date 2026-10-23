import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import { mockBuiltFrontend } from "./frontend_dist_mock.mjs";

const { chromium } = createRequire(import.meta.url)(process.env.PW_ROOT + "/playwright-core");
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
const output = process.env.TEST_OUTPUT || ".backup/workspace-revision";
await mkdir(output, { recursive: true });
const failures = [], checks = [];
const materialId = "mat_synthetic";
const mentor = {
  card_id: "card_synthetic",
  revision: 1,
  editable: true,
  collected: false,
  published_revision: 1,
  profile: { name: "合成导师", identity: "循序渐进的学习伙伴", tags: ["清楚自然", "先讲直觉"], base_asset_id: "" },
  assets: [],
};
const library = [
  { id: "route_synthetic", kind: "route", title: "合成学习路线", scenario: "self_study", course_count: 2, course_completed: 0, lesson_total: 12, lesson_completed: 0, completed: false, state: "in_progress" },
  { id: "course_synthetic", kind: "course", title: "合成基础课程", scenario: "self_study", unit_count: 3, lesson_total: 8, lesson_completed: 2, completed: false, state: "in_progress" },
];

try {
  for (const width of [1440, 768, 390, 320]) {
    const context = await browser.newContext({ viewport: { width, height: 960 }, reducedMotion: "reduce" });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    page.on("pageerror", error => failures.push(`${width}: ${error.message}`));
    const served = await mockBuiltFrontend(page);
    let adminSidebar = false;
    await page.route("**/api/**", async route => {
      const request = route.request();
      const { pathname } = new URL(request.url());
      const json = body => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
      if (pathname === "/api/auth/me") return json({ user_id: "synthetic-owner", email: "reader@example.test", status: "approved", is_admin: adminSidebar });
      if (pathname === "/api/navigation") return json({});
      if (pathname === "/api/planning/current") return json({ planning: null });
      if (pathname === "/api/planning/library") return json({ data: library });
      if (pathname === "/api/planning/library/prewarm") return json({ ok: true });
      if (pathname === "/api/mentor-preference") return json({ version: 1, card_id: null, revision: null });
      if (pathname === "/api/cards" || pathname === "/api/card-market") return json({ data: [mentor] });
      if (pathname === "/api/capabilities/model-roles") return json({ data: ["planning", "teaching", "dialogue"].map(role => ({
        role, revision: 0, config_id: null, configuration: null,
      })) });
      if (pathname === "/api/materials") return json({ data: [{
        material_id: materialId, kind: "pdf", size_bytes: 98304, status: "parsed", created_at: "2026-10-19T00:00:00Z",
      }] });
      if (pathname === `/api/materials/${materialId}`) return json({ material_id: materialId, kind: "pdf", page_count: 2 });
      if (pathname.startsWith(`/api/materials/${materialId}/pages/`)) {
        const pageNo = Number(pathname.split("/").at(-1));
        return json({ page_id: `page_${pageNo}`, material_id: materialId, page_no: pageNo, status: "success", regions: [] });
      }
      if (pathname === "/api/materials/search") return json({ data: [] });
      if (pathname === "/api/capabilities/configs") return json({ data: [] });
      return json({});
    });

    await page.goto(served.origin + "/#/home");
    await page.locator(".revision-app").waitFor();
    const rail = page.getByRole("navigation", { name: "主导航" });
    await rail.getByRole("button", { name: "我的资料" }).waitFor();
    assert.equal(await page.locator(".revision-topic-hint").count(), 1);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `home overflow at ${width}`);
    await page.mouse.move(Math.max(4, width - 8), 5);
    await page.screenshot({ path: `${output}/home-${width}.png`, fullPage: true });

    const openNavigation = async () => {
      if (width <= 1023) await page.getByRole("button", { name: "打开导航" }).click();
    };
    await openNavigation();
    await rail.getByRole("button", { name: "我的资料" }).click();
    await page.getByRole("heading", { name: "我的资料", exact: true }).waitFor();
    assert.equal(await page.locator(".knowledge-material-select").count(), 1,
      `synthetic material missing: ${await page.locator("main").innerText()}`);
    await page.waitForFunction(() => document.querySelectorAll(".knowledge-material-select").length === 1);
    assert.equal(await page.locator(".knowledge-layout").evaluate(element => getComputedStyle(element).display),
      width <= 700 ? "block" : "grid", `knowledge layout at ${width}`);
    await page.getByRole("button", { name: /检索当前资料/ }).waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `knowledge overflow at ${width}`);
    await page.mouse.move(Math.max(4, width - 8), 5);
    await page.screenshot({ path: `${output}/knowledge-${width}.png`, fullPage: true });

    await openNavigation();
    await rail.getByRole("button", { name: "课程" }).click();
    await page.getByRole("heading", { name: "我的课程" }).waitFor();
    await page.getByText("合成基础课程", { exact: true }).waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `courses overflow at ${width}`);
    await page.mouse.move(Math.max(4, width - 8), 5);
    await page.screenshot({ path: `${output}/courses-${width}.png`, fullPage: true });

    await openNavigation();
    await rail.getByRole("button", { name: "人物导师" }).click();
    await page.getByRole("heading", { name: "我的人物", exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: /收录人物|取消收录/ }).count(), 0);
    assert.equal(await page.getByRole("link", { name: "卡片市集" }).count(), 0);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `characters overflow at ${width}`);
    await page.mouse.move(Math.max(4, width - 8), 5);
    await page.screenshot({ path: `${output}/characters-${width}.png`, fullPage: true });

    await openNavigation();
    await rail.getByRole("button", { name: "卡片市集" }).click();
    await page.getByRole("heading", { name: "卡片市集", exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "新人物", exact: true }).count(), 0);
    assert.equal(await page.getByRole("button", { name: "收录人物", exact: true }).count(), 1);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `market overflow at ${width}`);
    await page.mouse.move(Math.max(4, width - 8), 5);
    await page.screenshot({ path: `${output}/market-${width}.png`, fullPage: true });

    if (width === 1440 || width === 390) {
      const shortHeight = width === 1440 ? 480 : 420;
      await page.setViewportSize({ width, height: shortHeight });
      adminSidebar = true;
      await page.goto(served.origin + "/#/home");
      await page.reload();
      await page.locator(".revision-app").waitFor();
      if (width <= 1023) await page.getByRole("button", { name: "打开导航" }).click();
      await page.locator("#side").getByRole("button", { name: "注册审批" }).waitFor();
      const footer = await page.locator("#side .side-foot").evaluate(element => {
        const rect = element.getBoundingClientRect();
        return { top: rect.top, bottom: rect.bottom, viewport: innerHeight };
      });
      assert.ok(footer.top >= 0 && footer.bottom <= footer.viewport + 1,
        `sidebar footer clipped at ${width}x${shortHeight}: ${JSON.stringify(footer)}`);
      assert.ok(await page.locator("#side .revision-avatar").evaluate(element =>
        element.getBoundingClientRect().bottom <= innerHeight + 1),
      `account avatar clipped at ${width}x${shortHeight}`);
      assert.ok(await page.getByRole("button", { name: "退出登录" }).evaluate(element =>
        element.getBoundingClientRect().bottom <= innerHeight + 1),
      `logout action clipped at ${width}x${shortHeight}`);
      await page.screenshot({ path: `${output}/home-short-height-${width}.png`, fullPage: true });
      adminSidebar = false;
    }

    if (width === 1440 || width === 390) {
      await page.goto(served.origin + "/#/capabilities");
      await page.locator(".revision-settings").waitFor();
      if (width <= 600) {
        const settingsRail = page.locator(".settings-app-nav.side");
        assert.notEqual(await settingsRail.evaluate(element => getComputedStyle(element).display), "none");
        assert.equal(await settingsRail.evaluate(element => Math.round(element.getBoundingClientRect().width)), 64);
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `settings overflow at ${width}`);
      await page.mouse.move(Math.max(4, width - 8), 5);
      await page.screenshot({ path: `${output}/settings-${width}.png`, fullPage: true });
    }

    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `horizontal overflow at ${width}`);
    assert.deepEqual(served.unexpected, []);
    context.close();
    checks.push(width);
  }

  const hintContext = await browser.newContext({ viewport: { width: 1280, height: 900 }, reducedMotion: "no-preference" });
  const hintPage = await hintContext.newPage();
  await mockBuiltFrontend(hintPage);
  await hintPage.route("**/api/**", route => {
    const pathname = new URL(route.request().url()).pathname;
    const value = pathname === "/api/auth/me"
      ? { user_id: "synthetic-owner", email: "reader@example.test", status: "approved", is_admin: false }
      : pathname === "/api/planning/current" ? { planning: null } : {};
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(value) });
  });
  await hintPage.goto("https://frontend.synthetic.test/#/home");
  const initialHint = await hintPage.locator(".revision-topic-hint").innerText();
  await hintPage.waitForFunction(initial => document.querySelector(".revision-topic-hint")?.textContent !== initial, initialHint, { timeout: 7000 });
  await hintPage.locator("#goal").fill("合成输入");
  assert.equal(await hintPage.locator(".revision-topic-hint").count(), 0, "typing hides the rotating prompt");
  await hintPage.locator("#goal").fill("");
  assert.equal(await hintPage.locator(".revision-topic-hint").count(), 1, "clearing restores the prompt");
  await hintContext.close();

  const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
  const page = await context.newPage();
  const served = await mockBuiltFrontend(page);
  await page.route("**/api/**", route => route.fulfill({ contentType: "application/json", body: JSON.stringify({ planning: null, data: [] }) }));
  await page.goto(served.origin + "/#/planning");
  assert.equal(await page.locator(".revision-app").count(), 0, "planning keeps the original layout scope");
  assert.equal(await page.locator(".mm-clean").count(), 1);
  await page.screenshot({ path: `${output}/planning-preserved.png`, fullPage: true });
  await page.goto(served.origin + "/#/lecture/synthetic-lesson");
  assert.equal(await page.locator(".revision-app").count(), 0, "lecture keeps the original full-screen layout scope");
  await context.close();

  assert.deepEqual(failures, []);
  console.log(JSON.stringify({ result: "passed", widths: checks, providers: "synthetic-only", untouched: ["planning", "lecture"] }));
} finally {
  await browser.close();
}
