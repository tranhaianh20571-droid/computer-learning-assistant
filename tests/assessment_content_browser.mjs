import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import { mockBuiltFrontend } from "./frontend_dist_mock.mjs";

const { chromium } = createRequire(import.meta.url)(process.env.PW_ROOT + "/playwright-core");
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
const output = ".backup/assessment-v2/browser";
await mkdir(output, { recursive: true });
const checks = [];
const questions = Array.from({ length: 48 }, (_, i) => ({
  id: `q${i}`, kind: "short", prompt: `计算合成数据 ${i + 1} 与 2 的乘积。`, options: [], points: 2,
  lesson_ids: [`l${Math.floor(i / 12)}`], primary_lesson_id: `l${Math.floor(i / 12)}`,
  rounding: { decimals: 2, notation: "decimal" },
  figure: i === 0 ? { kind: "generated_chart", caption: "合成数据图",
    chart_spec: { chart_type: "line", x_label: "输入", y_label: "观测值", labels: ["1", "2", "3"],
      series: [{ label: "观测", values: [-2, 0, 4] }] } }
    : i === 1 ? { kind: "data_table", table: { columns: ["输入", "输出"], rows: [["1", "2"], ["2", "4"]] } }
      : i === 2 ? { kind: "material_figure", source: { material_id: "synthetic", page_no: 1, block_no: 0 } }
        : { kind: "none" },
}));
try {
  for (const width of [1440, 390, 320]) {
    const page = await browser.newPage({ viewport: { width, height: 960 }, serviceWorkers: "block" });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const served = await mockBuiltFrontend(page);
    let submitted;
    await page.route("**/api/**", async route => {
      const path = new URL(route.request().url()).pathname;
      if (path.includes("/images/")) {
        return route.fulfill({ contentType: "image/png", body: Buffer.from(
          "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3XcAAAAASUVORK5CYII=", "base64") });
      }
      let body = { data: [] };
      if (path === "/api/auth/me") body = { user_id: "synthetic", status: "approved" };
      else if (path.endsWith("/overview")) body = { title: "合成计算课程", outline_confirmed: true,
        units: [{ unit_id: "unit", title: "合成计算单元", lessons: Array.from({ length: 4 }, (_, i) => ({
          lesson_id: `l${i}`, title: `合成第 ${i + 1} 讲`, learning_state: "user_completed" })) }], progress: {} };
      else if (path.endsWith("/assessment")) body = { assessment_id: "quiz", status: "succeeded", eligible: true,
        assessment_version: 2, questions };
      else if (path === "/api/assessments/quiz/attempts") {
        submitted = route.request().postDataJSON();
        body = { attempt_id: "attempt", task_id: "grade" };
      } else if (path === "/api/assessment-attempts/attempt") body = { status: "succeeded",
        feedback: { earned: 96, maximum: 96, advice: "合成评分",
          grades: questions.map(q => ({ question_id: q.id, earned: 2, maximum: 2, goal: "合成乘法",
            reason: "合成正确", explanation: "合成解析", correct_answer: ["合成答案"], weaknesses: [] })) } };
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    });
    await page.goto(served.origin + "/#/course/course");
    await page.getByRole("button", { name: "查看单元测验", exact: true }).click();
    await page.locator(".assessment-chart svg").waitFor();
    assert.equal(await page.locator(".number").count(), 48);
    assert.equal(await page.getByText("合成乘法", { exact: true }).count(), 0);
    assert.equal(await page.getByText("参考答案", { exact: true }).count(), 0);
    await page.screenshot({ path: `${output}/chart-${width}.png`, fullPage: true });
    await page.locator("textarea").fill("2.00");
    await page.getByRole("button", { name: "下一题", exact: true }).click();
    assert.equal(await page.locator(".assessment-table td").count(), 4);
    await page.locator("textarea").fill("4.00");
    await page.getByRole("button", { name: "下一题", exact: true }).click();
    await page.locator(".assessment-figure img").waitFor();
    await page.waitForFunction(() => document.querySelector(".assessment-figure img")?.naturalWidth > 0);
    const metrics = await page.locator(".assessment-screen").evaluate(el => ({ width: el.clientWidth, scroll: el.scrollWidth }));
    assert(metrics.scroll <= width + 1, JSON.stringify(metrics));
    if (width === 1440) {
      for (let i = 2; i < 48; i++) {
        await page.locator("textarea").fill(String((i + 1) * 2));
        if (i < 47) await page.getByRole("button", { name: "下一题", exact: true }).click();
      }
      await page.getByRole("button", { name: "检查并提交", exact: true }).click();
      await page.getByRole("button", { name: "提交测验", exact: true }).last().click();
      await page.getByRole("heading", { name: "本次得分：96 / 96" }).waitFor();
      assert.equal(Object.keys(submitted.answers).length, 48);
      assert.equal(await page.locator(".result-knowledge").getByText("合成乘法", { exact: true }).count(), 1);
    }
    assert.deepEqual(errors, []);
    checks.push({ width, questions: 48, charts: true, tables: true, originalImage: true, noOverflow: true });
    await page.close();
  }
  console.log(JSON.stringify({ checks, submitted48Answers: true, synthetic: true }));
} finally {
  await browser.close();
}
