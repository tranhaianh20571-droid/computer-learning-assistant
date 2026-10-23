import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';

// 只读取本地已运行的 Vite 开发资源（5173）。所有 /api 均由合成夹具拦截，其余网络一律阻断。
// 覆盖：明确模型重新授权、不可恢复错误入口与后台独立自动重试；页面读取不追加生成请求。
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PW_ROOT + '/playwright-core');
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
const origin = process.env.TEST_FRONTEND || 'http://127.0.0.1:5173';
const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
const checks = [];
const output = process.env.TEST_OUTPUT || '.backup/planning-manual-retry-20261021/browser';
await mkdir(output, { recursive: true });
const me = { user_id: 'synthetic-planning-owner', email: 'planning@example.test', is_admin: false, status: 'approved', display_name: '合成用户' };
const baseTask = {
  task_id: 'synthetic-planning', title: '合成规划', target_kind: 'route', target_id: 'synthetic-route',
  generation: 2, task_status: 'failed', wait_state: null, confirmed: false, auto_retry_count: 0,
  auto_retry_limit: 5, auto_retry_pending: false, auto_retry_exhausted: false, structure_status: 'pending',
};

async function scenario(name, test, { planning, resumeResponse, draft, onApi } = {}) {
  const context = await browser.newContext({ serviceWorkers: 'block', reducedMotion: 'reduce', viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const state = { requests: [], errors: [], blocked: [], resumeCalls: 0, current: { ...planning }, draft: draft ? structuredClone(draft) : null };
  page.on('pageerror', error => state.errors.push(error.message));
  page.on('dialog', dialog => { state.errors.push(`Unexpected native ${dialog.type()}`); dialog.dismiss(); });
  await page.route('**/*', async routed => {
    const url = new URL(routed.request().url());
    if (url.origin === origin && !url.pathname.startsWith('/api/')) return routed.continue();
    state.blocked.push(url.origin + url.pathname); return routed.abort();
  });
  await page.route('**/api/**', async routed => {
    const request = routed.request(), url = new URL(request.url()), method = request.method();
    const body = request.postData() ? request.postDataJSON() : null;
    state.requests.push({ path: url.pathname, method, body });
    if (onApi && await onApi({ route: routed, url, method, body, state })) return;
    if (url.pathname === '/api/auth/me') return json(routed, me);
    if (url.pathname === '/api/market/profile') return json(routed, { display_name: me.display_name });
    if (url.pathname === '/api/auth/guest') return json(routed, { ...me, is_guest: false });
    if (url.pathname === '/api/navigation') return json(routed, {});
    if (url.pathname === '/api/materials' || url.pathname === '/api/connectors') return json(routed, { data: [] });
    if (draft && url.pathname === '/api/planning/drafts/synthetic-draft') return json(routed, state.draft);
    if (draft && url.pathname === '/api/planning/drafts/synthetic-draft/scene') return json(routed, { elements: [] });
    if (draft && url.pathname.startsWith('/api/planning/viewstates/')) return json(routed, { offsets: {} });
    if (url.pathname === '/api/planning/current') return json(routed, { planning: state.current });
    if (url.pathname === '/api/planning/tasks/synthetic-planning/process') return json(routed, { process: null });
    if (url.pathname === '/api/planning/tasks/synthetic-planning') return json(routed, state.current);
    if (url.pathname === '/api/planning/tasks/synthetic-planning/resume') {
      state.resumeCalls += 1;
      state.current = resumeResponse ? { ...resumeResponse } : { ...state.current, task_status: 'queued' };
      return json(routed, state.current);
    }
    if (url.pathname === '/api/planning/tasks/synthetic-planning/abandon') return json(routed, { ok: true });
    if (/^\/api\/planning\/(?:route|course)\/[^/]+\/cover(?:\/preflight)?$/.test(url.pathname)) {
      return json(routed, { status: 'suppressed', cover_eligible: false });
    }
    state.errors.push(`Missing synthetic API fixture: ${method} ${url.pathname}`);
    return routed.abort();
  });
  try {
    await page.goto(`${origin}/#/planning`);
    await test({ page, state });
    assert.deepEqual(state.errors, []);
    assert.deepEqual(state.blocked, []);
    checks.push(name);
  } finally { await context.close(); }
}

const dialog = page => page.getByRole('status', { name: '规划需要处理' });
const resumeBodies = state => state.requests.filter(request => request.path.endsWith('/resume')).map(request => request.body);

try {
  await scenario('disclosure_required_offers_current_model_and_never_auto_resumes', async ({ page, state }) => {
    await dialog(page).getByText('生成未完成 · 需要重新准备当前模型后继续生成。', { exact: true }).waitFor();
    await page.getByText('继续生成会使用你当前启用的内容模型。', { exact: true }).waitFor();
    assert.equal(state.resumeCalls, 0, 'DISCLOSURE_REQUIRED 不得自动续试');
    assert.equal(await page.getByRole('button', { name: '重试生成', exact: true }).count(), 0);
    await page.getByRole('button', { name: '使用当前模型继续生成', exact: true }).click();
    await dialog(page).waitFor({ state: 'hidden' });
    assert.equal(state.resumeCalls, 1);
    assert.deepEqual(resumeBodies(state), [{ automatic: false }]);
  }, { planning: { ...baseTask, error_code: 'DISCLOSURE_REQUIRED' } });

  await scenario('disclosure_expired_offers_current_model_resume', async ({ page, state }) => {
    await dialog(page).getByText('生成未完成 · 本次模型使用已过期，可以使用当前模型继续生成。', { exact: true }).waitFor();
    await page.getByRole('button', { name: '使用当前模型继续生成', exact: true }).waitFor();
    assert.equal(state.resumeCalls, 0);
  }, { planning: { ...baseTask, error_code: 'DISCLOSURE_EXPIRED' } });

  for (const [code, title] of [
    ['MODEL_CREDENTIAL_INVALID', '生成未完成 · 模型凭据无效，请在能力设置中更新。'],
    ['DISCLOSURE_REVOKED', '生成未完成 · 本次规划的模型使用已被撤销，请检查模型设置。'],
    ['CONFIG_NOT_FOUND', '生成未完成 · 当前没有可用的内容模型，请检查模型设置。'],
  ]) {
    await scenario(`${code.toLowerCase()}_offers_settings_then_explicit_resume`, async ({ page, state }) => {
      await dialog(page).getByText(title, { exact: true }).waitFor();
      await page.getByText('请先检查并更新模型设置，再继续生成；已撤销的同版本模型使用不会自动恢复。', { exact: true }).waitFor();
      assert.equal(state.resumeCalls, 0, `${code} 不得自动续试`);
      await page.getByRole('button', { name: '检查模型设置', exact: true }).waitFor();
      await page.getByRole('button', { name: '更新设置后继续生成', exact: true }).click();
      await dialog(page).waitFor({ state: 'hidden' });
      assert.equal(state.resumeCalls, 1);
      assert.deepEqual(resumeBodies(state), [{ automatic: false }]);
    }, { planning: { ...baseTask, error_code: code } });
  }

  await scenario('access_denied_offers_refresh_only', async ({ page, state }) => {
    await dialog(page).getByText('生成未完成 · 没有权限执行此操作。', { exact: true }).waitFor();
    await page.getByRole('button', { name: '刷新规划', exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: '重试生成', exact: true }).count(), 0);
    assert.equal(await page.getByRole('button', { name: '使用当前模型继续生成', exact: true }).count(), 0);
    assert.equal(state.resumeCalls, 0);
  }, { planning: { ...baseTask, error_code: 'ACCESS_DENIED' } });

  await scenario('retryable_failure_exhausted_never_resumes_on_poll_or_reentry', async ({ page, state }) => {
    await dialog(page).getByText('生成未完成 · 模型响应超时，可重试。', { exact: true }).waitFor();
    await page.getByText('后台自动重试已达上限（5/5），已保留当前规划。', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: '重试生成', exact: true }).count(), 0);
    await page.waitForResponse(response => new URL(response.url()).pathname === '/api/planning/tasks/synthetic-planning');
    await page.reload();
    await dialog(page).waitFor();
    assert.equal(state.resumeCalls, 0);
    assert.deepEqual(resumeBodies(state), []);
    await page.getByRole('button', { name: '手动重试', exact: true }).click();
    await dialog(page).waitFor({ state: 'hidden' });
    assert.equal(state.resumeCalls, 1);
    assert.deepEqual(resumeBodies(state), [{ automatic: false }]);
    assert.equal(state.current.auto_retry_count, 5);
  }, { planning: { ...baseTask, error_code: 'MODEL_TIMEOUT', auto_retry_count: 5, auto_retry_exhausted: true } });

  await scenario('manual_retry_preserves_task_answers_and_recovers_after_request_failure', async ({ page, state }) => {
    await dialog(page).getByText('生成未完成 · 任务已取消。', { exact: true }).waitFor();
    for (const width of [1280, 390, 320]) {
      await page.setViewportSize({ width, height: 1000 });
      await page.waitForFunction(() => innerWidth > 1023 || document.getElementById('side').getBoundingClientRect().right <= 1);
      const button = dialog(page).getByRole('button', { name: '手动重试', exact: true });
      await button.scrollIntoViewIfNeeded();
      assert(await button.evaluate(element => {
        const rect = element.getBoundingClientRect();
        return rect.left >= 0 && rect.right <= innerWidth;
      }));
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(output, `manual-retry-${width}.png`), fullPage: true });
    }
    const clickTwice = () => dialog(page).getByRole('button', { name: '手动重试', exact: true })
      .evaluate(button => { button.click(); button.click(); });
    await clickTwice();
    await dialog(page).getByText('生成未完成 · 模型服务请求失败，可重试。', { exact: true }).waitFor();
    assert.equal(state.resumeCalls, 1);
    await clickTwice();
    await dialog(page).waitFor({ state: 'hidden' });
    assert.equal(state.resumeCalls, 2);
    assert.deepEqual(resumeBodies(state), [{ automatic: false }, { automatic: false }]);
    assert.equal(state.current.task_id, baseTask.task_id);
    assert.equal(state.current.intake.answers[0].question_id, 'synthetic-question');
    assert.equal(state.current.auto_retry_count, 2);
    assert.equal(state.requests.filter(request => request.method === 'POST' && !request.path.endsWith('/resume')).length, 0);
  }, {
    planning: { ...baseTask, error_code: 'TASK_CANCELLED', auto_retry_count: 2,
      intake: { done: true, answers: [{ question_id: 'synthetic-question', selections: ['a'] }] } },
    onApi: async ({ route, url, state }) => {
      if (!url.pathname.endsWith('/resume')) return false;
      state.resumeCalls++;
      if (state.resumeCalls === 1) await json(route, { detail: { error_code: 'MODEL_PROVIDER_ERROR' } }, 502);
      else {
        state.current = { ...state.current, task_status: 'running', error_code: null, generation: 3 };
        await json(route, state.current);
      }
      return true;
    },
  });

  await scenario('retry_pending_suppresses_failure_dialog_and_never_resumes', async ({ page, state }) => {
    await page.getByLabel('自动重试 2/5', { exact: true }).waitFor();
    await page.waitForResponse(response => new URL(response.url()).pathname === '/api/planning/tasks/synthetic-planning');
    assert.equal(await dialog(page).count(), 0);
    assert.equal(await page.getByRole('button', { name: '手动重试', exact: true }).count(), 0);
    await page.reload();
    await page.getByRole('button', { name: '放弃规划', exact: true }).first().waitFor();
    assert.equal(await dialog(page).count(), 0);
    assert.equal(state.resumeCalls, 0);
  }, { planning: { ...baseTask, error_code: 'MODEL_TIMEOUT', auto_retry_count: 2, auto_retry_pending: true } });

  for (const status of ['leased', 'running']) {
    await scenario(`${status}_restores_without_resume_or_failure_dialog`, async ({ page, state }) => {
      await page.getByRole('button', { name: '放弃规划', exact: true }).first().waitFor();
      await page.waitForResponse(response => new URL(response.url()).pathname === '/api/planning/tasks/synthetic-planning');
      assert.equal(await dialog(page).count(), 0);
      assert.equal(state.resumeCalls, 0);
    }, { planning: { ...baseTask, task_status: status, error_code: 'MODEL_TIMEOUT' } });
  }

  await scenario('first_queued_task_retains_initial_resume', async ({ page, state }) => {
    await page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/resume'));
    assert.equal(state.resumeCalls, 1);
    assert.deepEqual(resumeBodies(state), [{ automatic: true }]);
  }, { planning: { ...baseTask, task_status: 'queued', generation: 0 } });

  await scenario('queued_backend_retry_never_adds_resume', async ({ page, state }) => {
    await page.getByRole('button', { name: '放弃规划', exact: true }).first().waitFor();
    await page.waitForResponse(response => new URL(response.url()).pathname === '/api/planning/tasks/synthetic-planning');
    assert.equal(state.resumeCalls, 0);
  }, { planning: { ...baseTask, task_status: 'queued', auto_retry_count: 3, auto_retry_pending: true } });

  for (const exhausted of [false, true]) {
    await scenario(`partial_structure_${exhausted ? 'exhausted' : 'pending'}_shows_bounded_retry_without_manual_repair`, async ({ page, state }) => {
      const count = exhausted ? 5 : 2;
      await page.getByLabel(`自动重试 ${count}/5`, { exact: true }).waitFor();
      if (exhausted) await page.getByText('自动重试已达上限，当前草稿与回答已保留。', { exact: true }).waitFor();
      assert.equal(await page.getByRole('button', { name: /补齐|重试/ }).count(), 0);
      assert.equal(await page.getByRole('button', { name: '没问题，确认草稿', exact: true }).isDisabled(), true);
      assert.equal(await dialog(page).count(), 0);
      await page.waitForResponse(response => new URL(response.url()).pathname === '/api/planning/tasks/synthetic-planning');
      await page.reload();
      await page.getByLabel(`自动重试 ${count}/5`, { exact: true }).waitFor();
      assert.equal(state.requests.filter(request => request.method === 'POST').length, 0);
      if (!exhausted) {
        state.current = { ...state.current, structure_status: 'ready', auto_retry_pending: false, auto_retry_count: 3 };
        state.draft.revision = 2;
        state.draft.content.stages[0].courses[0].units = [{ title: '合成章节', lessons: [{ title: '合成讲次' }] }];
        await page.waitForFunction(() => [...document.querySelectorAll('button')].some(button => button.textContent === '没问题，确认草稿' && !button.disabled));
        assert.equal(await page.getByLabel(/自动重试 \d\/5/, { exact: true }).count(), 0);
        assert.equal(state.requests.filter(request => request.method === 'POST').length, 0);
      }
    }, {
      planning: { ...baseTask, task_status: 'succeeded', draft_id: 'synthetic-draft', structure_status: 'partial', auto_retry_count: exhausted ? 5 : 2, auto_retry_pending: !exhausted, auto_retry_exhausted: exhausted },
      draft: { draft_id: 'synthetic-draft', draft_kind: 'route', target_id: 'synthetic-route', revision: 1, locked: false, content: { stages: [{ title: '合成阶段', courses: [{ title: '合成待补课程', units: [] }] }] } },
    });
  }

  await scenario('saved_progress_prefers_server_answers_over_local_log', async ({ page }) => {
    await dialog(page).getByText('生成未完成 · 需要重新准备当前模型后继续生成。', { exact: true }).waitFor();
    await page.getByText('已提交的访谈回答已保存，重新进入可恢复这次规划。', { exact: true }).waitFor();
  }, {
    planning: {
      ...baseTask, error_code: 'DISCLOSURE_REQUIRED',
      intake: { answers: [{ question_id: 'goal', question: '合成问题', selections: ['a'], free_text: '合成补充' }], questions: [{ question_id: 'goal', question: '合成问题', options: [{ key: 'a', label: '合成选项' }] }] },
    },
  });

  await scenario('search_error_code_stays_out_of_planning_dialog', async ({ page, state }) => {
    await page.getByRole('button', { name: '放弃规划', exact: true }).first().waitFor();
    assert.equal(await dialog(page).count(), 0);
    assert.equal(state.resumeCalls, 0);
  }, { planning: { ...baseTask, error_code: 'SEARCH_TIMEOUT' } });

  for (const width of [1280, 390]) {
    await scenario(`draft_feedback_and_ai_edit_remain_reachable_${width}`, async ({ page, state }) => {
      await page.setViewportSize({ width, height: 600 });
      const panel = page.getByRole('complementary', { name: '提问与反馈' });
      const edit = panel.getByRole('button', { name: '我想修改讲解结构', exact: true });
      await edit.waitFor();
      assert.equal(await panel.getByText('前置策略要求桥接合成基础', { exact: true }).count(), 0);
      assert.equal(await panel.getByRole('button', { name: '没问题，确认草稿', exact: true }).isDisabled(), true);
      await panel.getByRole('checkbox').check();
      assert.equal(await panel.getByRole('button', { name: '没问题，确认草稿', exact: true }).isEnabled(), true);
      await edit.scrollIntoViewIfNeeded();
      assert(await edit.evaluate(button => {
        const rect = button.getBoundingClientRect();
        const panelRect = button.closest('.plan-qa').getBoundingClientRect();
        return rect.top >= Math.max(0, panelRect.top) && rect.bottom <= Math.min(innerHeight, panelRect.bottom);
      }), 'AI edit button must be inside the scroll viewport');
      await edit.click();
      await page.getByLabel('规划修改要求').fill('保留资料，调整讲解顺序');
      await page.getByRole('button', { name: '发送修改要求', exact: true }).click();
      assert.equal(state.editBody.content, '保留资料，调整讲解顺序');
      assert.equal(state.editBody.revision, 1);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(output, `draft-feedback-${width}.png`), fullPage: true });
    }, {
      planning: { ...baseTask, task_status: 'succeeded', error_code: null, draft_id: 'synthetic-draft',
        structure_status: 'ready', intake: { answers: Array.from({ length: 7 }, (_, index) => ({
          question_id: `question-${index}`, question: '合成访谈问题'.repeat(12),
          free_text: '合成回答'.repeat(30), selections: []
        })) } },
      draft: { draft_id: 'synthetic-draft', draft_kind: 'route', target_id: 'synthetic-route',
        revision: 1, locked: false, content: { stages: [{ title: '合成阶段', courses: [{
          title: '合成课程', units: [{ title: '合成单元', lessons: [{ title: '合成讲次' }] }]
        }] }] }, review: { acknowledgement_required: true, issues: [
          { dimension: 'prerequisites', severity: 'critical', message: '前置策略要求桥接合成基础' },
          ...Array.from({ length: 8 }, () => ({
            dimension: 'coverage', severity: 'critical', message: '合成覆盖反馈'.repeat(16)
          }))
        ] } },
      onApi: async ({ route, url, body, state }) => {
        if (!url.pathname.endsWith('/dialogue')) return false;
        if (body) state.editBody = body;
        await json(route, { turns: [] });
        return true;
      },
    });
  }

  console.log(`planning_authorization_browser: ${checks.length}/${checks.length} passed`);
  for (const name of checks) console.log(`  ok ${name}`);
} finally {
  await browser.close();
}
