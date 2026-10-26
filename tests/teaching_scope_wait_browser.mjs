import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import { mockBuiltFrontend } from "./frontend_dist_mock.mjs";

const { chromium } = createRequire(import.meta.url)(process.env.PW_ROOT + "/playwright-core");
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
const output = ".backup/teaching-scope-wait-browser";
await mkdir(output, { recursive: true });
const checks = [];
try {
  for (const width of [1280, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 800 }, serviceWorkers: "block" });
    const page = await context.newPage(), errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const served = await mockBuiltFrontend(page);
    let starts = 0;
    const positions = [];
    let workspace = {
      conversation_id: "synthetic-scope-wait", kind: "teaching", course_id: "course-a", lesson_id: "lesson-a",
      lesson_title: "合成考纲课堂", generation: 1, session_state: "active", server_managed: true,
      audio_mode: "tts", tts_authorized: true, steps: [], followups: [], queued_tasks: [],
      authorization_required: false, needs_upgrade: false,
      scope_preparation: { status: "running", error_code: null }, last_checkpoint: null
    };
    const json = (route, body) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    await page.route("**/api/**", route => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === "/api/auth/me") return json(route, { user_id: "synthetic-owner", status: "approved" });
      if (pathname === "/api/teaching/synthetic-scope-wait") return json(route, workspace);
      if (pathname === "/api/courses/course-a/lessons/lesson-a/start") {
        starts++;
        workspace = { ...workspace, scope_preparation: { status: "queued", error_code: null } };
        if(workspace.preparation)workspace.preparation={state:"retrying",action:null};
        return json(route, { conversation_id: workspace.conversation_id, task_id: "existing-first-task" });
      }
      if (pathname.endsWith("/checkpoint")) {
        positions.push(route.request().postDataJSON().position_ms);
        return json(route, { saved: true });
      }
      return json(route, { data: [] });
    });
    await page.goto(served.origin + "/#/lecture/synthetic-scope-wait");
    await page.getByText("正在准备本单元考纲，完成后继续讲解…", { exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "恢复讲解", exact: true }).count(), 0);
    assert.equal(await page.getByRole("button", { name: "重试准备考纲", exact: true }).count(), 0);
    assert.equal(starts, 0);
    checks.push(`pending-scope-never-reports-model-unavailable-${width}`);
    await page.waitForFunction(() => document.querySelector(".lw-simple"));
    const deadline = Date.now() + 10000;
    while (!positions.length && Date.now() < deadline) await page.waitForTimeout(200);
    assert(positions.length > 0);
    assert(positions.every(position => position === 0));
    checks.push(`waiting-without-audio-never-advances-playback-${width}`);
    workspace = { ...workspace, scope_preparation: { status: "failed", error_code: "MODEL_PROVIDER_ERROR" } };
    const retry = page.getByRole("button", { name: "重试准备考纲", exact: true });
    await retry.waitFor();
    await retry.click();
    await page.getByText("正在准备本单元考纲，完成后继续讲解…", { exact: true }).waitFor();
    assert.equal(starts, 1);
    assert.equal(new URL(page.url()).hash, "#/lecture/synthetic-scope-wait");
    checks.push(`scope-retry-retains-current-classroom-${width}`);
    workspace = { ...workspace, scope_preparation: { status: "succeeded", error_code: null } };
    await page.getByText("单元考纲已就绪，正在准备首段讲解…", { exact: true }).waitFor();
    assert(!/讲解配置暂不可用/.test(await page.locator("body").innerText()));
    workspace = { ...workspace, queued_tasks: [{ task_id: "failed-first", kind: "lesson_step_generate",
      step_no: 1, status: "failed", error_code: "OUTPUT_INVALID", required_action: "retry" }] };
    await page.getByRole("button", { name: "重试生成", exact: true }).waitFor();
    assert.equal(await page.getByText("单元考纲已就绪，正在准备首段讲解…", { exact: true }).count(), 0);
    assert.equal(await page.getByRole("button", { name: "重试准备考纲", exact: true }).count(), 0);
    await page.getByText("模型返回的内容不符合要求，可重试生成。", { exact: true }).first().waitFor();
    checks.push(`ready-scope-does-not-hide-first-segment-failure-${width}`);
    workspace={...workspace,scope_preparation:{status:"failed",error_code:"MODEL_PROVIDER_ERROR"},
      preparation:{state:"retrying",action:null}};
    await page.getByText("正在准备本讲内容…",{exact:true}).waitFor();
    assert.equal(await page.getByRole("button",{name:/重试准备考纲|重试生成|稍后再试/}).count(),0);
    assert(!/MODEL_PROVIDER_ERROR|模型返回的内容不符合/.test(await page.locator("body").innerText()));
    checks.push(`background-recovery-hides-internal-failures-${width}`);
    workspace={...workspace,preparation:{state:"paused",action:"retry"}};
    await page.getByText("暂时无法准备本讲内容，课程已保留。",{exact:true}).waitFor();
    await page.getByRole("button",{name:"稍后再试",exact:true}).click();
    await page.getByText("正在准备本讲内容…",{exact:true}).waitFor();
    assert.equal(starts,2);
    checks.push(`paused-has-one-bounded-recovery-action-${width}`);
    workspace={...workspace,preparation:{state:"action_required",action:"settings"}};
    await page.getByText("学习服务连接需要更新，课程已保留。",{exact:true}).waitFor();
    assert.equal(await page.getByRole("link",{name:"前往设置",exact:true}).count(),1);
    assert.equal(await page.getByRole("button",{name:/重试|恢复|稍后再试/}).count(),0);
    checks.push(`configuration-failure-has-one-settings-action-${width}`);
    workspace={...workspace,preparation:{state:"paused",action:null,reason:"storage"}};
    await page.getByText("本机空间不足，内容准备已暂停。释放空间后会自动继续。",{exact:true}).waitFor();
    assert.equal(await page.getByRole("link",{name:"前往设置",exact:true}).count(),0);
    assert.equal(await page.getByRole("button",{name:/重试|恢复|稍后再试/}).count(),0);
    assert.equal(await page.getByText("正在准备语音…",{exact:true}).count(),0);
    checks.push(`storage-pressure-waits-without-retry-or-premature-audio-${width}`);
    workspace={...workspace,preparation:{state:"paused",action:null,reason:"memory"}};
    await page.getByText("本机暂时繁忙，稍后会自动继续准备。",{exact:true}).waitFor();
    workspace={...workspace,preparation:{state:"preparing",action:null}};
    await page.getByText("正在准备本讲内容…",{exact:true}).waitFor();
    checks.push(`resource-recovery-resumes-without-user-action-${width}`);
    workspace={...workspace,preparation:{state:"action_required",action:"settings",reason:"quota"}};
    await page.getByText("学习服务额度不足，请补充后继续，课程已保留。",{exact:true}).waitFor();
    assert.equal(await page.getByRole("link",{name:"前往设置",exact:true}).count(),1);
    checks.push(`quota-failure-shows-one-plain-language-action-${width}`);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: `${output}/scope-ready-${width}.png` });
    assert.deepEqual(errors, []);
    assert.deepEqual(served.unexpected, []);
    await context.close();
  }
  console.log(JSON.stringify({ passed: checks.length, checks, synthetic: true }));
} finally { await browser.close(); }
