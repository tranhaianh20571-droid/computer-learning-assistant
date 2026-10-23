import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mockBuiltFrontend } from './frontend_dist_mock.mjs';

// PW_ROOT/TEST_BROWSER select already-installed tools. All data and API calls are synthetic.
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PW_ROOT + '/playwright-core');
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
const checks = [];
const fixtureConfig = (id = 'synthetic-search') => ({ config_id: id, owner_scope: 'user', kind: 'search', protocol: 'anysearch', endpoint: 'https://api.anysearch.com', model_name: '', credential_mask: '****', capability_status: {}, config_version: 1, is_active: true });
const me = (id, admin = false) => ({ user_id: id, email: `${id}@example.test`, is_admin: admin, status: 'approved' });
const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const searchCard = page => page.getByRole('region', { name: 'AnySearch 搜索', exact: true });
const noSearchDecision = async page => {
  assert.equal(await page.getByRole('button', { name: /重试搜索|离线继续/ }).count(), 0);
  assert.equal(await page.getByRole('dialog').count(), 0);
  assert.equal(await page.getByRole('alertdialog').count(), 0);
};

async function scenario(name, test, { configs = [], admin = false, guest = false, route = '/capabilities', planning = null, onApi, viewport = { width: 1440, height: 1000 } } = {}) {
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const served = await mockBuiltFrontend(page);
  const state = { owner: 'synthetic-owner-a', configs, requests: [], probeDetail: '', probeFailure: false, saveFailure: false, deleteFailure: false, chatMode: 'done', generation: 0, messages: [], console: [], errors: [] };
  page.on('pageerror', error => state.errors.push(error.message));
  page.on('console', message => state.console.push(message.text()));
  page.on('dialog', dialog => { state.errors.push(`Unexpected native ${dialog.type()}`); dialog.dismiss(); });
  await page.route('**/api/**', async routed => {
    const request = routed.request(), url = new URL(request.url()), method = request.method();
    const body = request.postData() ? request.postDataJSON() : null;
    state.requests.push({ path: url.pathname, url: url.href, method, body });
    if (onApi && await onApi({ route: routed, request, url, method, body, state })) return;
    if (url.pathname === '/api/auth/me') return guest ? json(routed, { detail: { error_code: 'AUTH_REQUIRED' } }, 401) : json(routed, me(state.owner, admin));
    if (url.pathname === '/api/auth/logout') { state.owner = 'synthetic-owner-b'; state.configs = []; return json(routed, {}); }
    if (url.pathname === '/api/auth/guest') return json(routed, { ...me(state.owner), is_guest: guest });
    if (url.pathname === '/api/auth/admin/pending' || url.pathname === '/api/connectors') return json(routed, { data: [] });
    if (url.pathname === '/api/capabilities/configs' && method === 'GET') return json(routed, { data: state.configs });
    if (url.pathname === '/api/capabilities/configs' && method === 'POST') {
      if (state.saveFailure) return json(routed, { detail: { error_code: 'SEARCH_REQUEST_REJECTED', message: 'synthetic-provider-private-detail' } }, 422);
      const saved = fixtureConfig(); state.configs = [...state.configs, saved]; return json(routed, saved, 201);
    }
    if (/\/capabilities\/configs\/[^/]+$/.test(url.pathname) && method === 'PATCH') {
      const id = url.pathname.split('/').at(-1);
      state.configs = state.configs.map(config => config.config_id === id ? { ...config, config_version: config.config_version + 1, capability_status: {} } : config);
      return json(routed, state.configs.find(config => config.config_id === id));
    }
    if (/\/capabilities\/configs\/[^/]+$/.test(url.pathname) && method === 'DELETE') {
      if (state.deleteFailure) return json(routed, { detail: { error_code: 'ERROR', message: 'synthetic-provider-private-detail' } }, 503);
      state.configs = state.configs.filter(config => config.config_id !== url.pathname.split('/').at(-1)); return json(routed, { ok: true });
    }
    if (url.pathname.endsWith('/test')) {
      if (state.probeFailure) return json(routed, { detail: { error_code: 'ERROR', message: 'synthetic-provider-private-detail' } }, 503);
      const status = { text: { state: state.probeDetail ? 'unavailable' : 'available', detail: state.probeDetail } };
      state.configs = state.configs.map(config => ({ ...config, capability_status: status }));
      return json(routed, { capability_status: status });
    }
    if (url.pathname === '/api/chat') return json(routed, { conversation_id: 'synthetic-conversation' }, 201);
    if (url.pathname === '/api/chat/synthetic-conversation') return json(routed, { generation: state.generation, messages: state.messages });
    if (url.pathname.endsWith('/messages/stream')) {
      const gen = ++state.generation;
      state.messages.push({ message_id: `q-${gen}`, generation: gen, role: 'user', content: body.content, status: 'succeeded' });
      if (state.chatMode === 'http_wait') return json(routed, { error_code: 'AWAITING_SEARCH_DECISION' }, 409);
      const event = state.chatMode === 'done' ? 'done' : state.chatMode === 'wait' ? 'wait' : 'error';
      const data = state.chatMode === 'model_error' ? { error_code: 'MODEL_CAPABILITY_UNAVAILABLE' } : state.chatMode === 'search_error' ? { error_code: 'SEARCH_TIMEOUT' } : { external_result_count: gen === 1 ? 2 : 0 };
      if (event === 'done') state.messages.push({ message_id: `a-${gen}`, generation: gen, role: 'assistant', content: '合成回答', status: 'succeeded' });
      const prefix = state.chatMode === 'wait' ? 'event: delta\ndata: {"text":"不应残留的合成片段"}\n\n' : '';
      return routed.fulfill({ contentType: 'text/event-stream', body: `${prefix}event: ${event}\ndata: ${JSON.stringify(data)}\n\n` });
    }
    if (url.pathname === '/api/navigation') return json(routed, {});
    if (url.pathname === '/api/materials') return json(routed, { data: [] });
    if (url.pathname === '/api/planning/current') return json(routed, { planning });
    if (url.pathname.endsWith('/cover')) return json(routed, { status: 'suppressed', cover_eligible: false });
    if (url.pathname === '/api/planning/tasks/synthetic-planning/process') return json(routed, {
      schema_version: 1, task_id: 'synthetic-planning', generation: planning?.generation || 1,
      sequence: 0, task_status: planning?.task_status || 'awaiting', stage: 'intake', elapsed_ms: 0,
      steps: [], search: { status: 'pending', sources: [], result_count: 0 },
      thinking: { show_summary: false, available: false, text: '' },
    });
    if (url.pathname === '/api/planning/tasks/synthetic-planning') return json(routed, planning);
    if (url.pathname === '/api/planning/tasks/synthetic-planning/resume') return json(routed, planning);
    throw new Error(`Missing synthetic API fixture: ${method} ${url.pathname}`);
  });
  try {
    await page.goto(`${served.origin}/#${route}`);
    if (route === '/capabilities') await searchCard(page).getByRole('button', { name: /保存 Key|更新 Key/ }).waitFor();
    await test({ page, state, card: searchCard(page), origin: served.origin });
    assert.deepEqual(state.errors, []);
    assert.deepEqual(served.unexpected, []);
    checks.push(name);
  } finally { await context.close(); }
}

try {
  const guestModel = { ...fixtureConfig('synthetic-guest-model'), kind: 'content_model', protocol: 'openai', model_name: 'synthetic-existing-guest-model' };
  await scenario('guest_search_create_update_probe_disable_without_login_keeps_models', async ({ page, state, card }) => {
    await page.getByText(/synthetic-existing-guest-model/).waitFor();
    assert.equal(await card.getByRole('link').count(), 0);
    assert(state.requests.some(request => request.path === '/api/auth/me'));
    assert(state.requests.some(request => request.path === '/api/auth/guest'));
    await card.getByLabel('API Key', { exact: true }).fill('synthetic-guest-key');
    await card.getByRole('button', { name: '保存 Key', exact: true }).click();
    await card.getByText('Key 已保存，学习时将按需搜索。', { exact: true }).waitFor();
    assert.equal(state.requests.filter(request => request.path.endsWith('/test')).length, 0);
    await card.getByText('搜索：已启用', { exact: true }).waitFor();
    await card.getByText('未测试', { exact: true }).waitFor();
    const input = card.getByLabel('新的 API Key', { exact: true });
    assert.equal(await input.inputValue(), '');
    await input.fill('synthetic-guest-replacement');
    await card.getByRole('button', { name: '更新 Key', exact: true }).click();
    await card.getByText('Key 已保存，学习时将按需搜索。', { exact: true }).waitFor();
    assert.equal(state.requests.filter(request => request.path.endsWith('/test')).length, 0);
    await card.getByRole('button', { name: '测试搜索', exact: true }).click();
    await card.getByText('AnySearch 测试通过。', { exact: true }).waitFor();
    assert.equal(state.requests.filter(request => request.path.endsWith('/test')).length, 1);
    assert.equal(await input.inputValue(), '');
    await card.getByRole('button', { name: '停用搜索', exact: true }).click();
    await card.getByText('搜索已停用。学习将使用现有资料继续。', { exact: true }).waitFor();
    await page.getByText(/synthetic-existing-guest-model/).waitFor();
    assert(!state.requests.some(request => /auth\/(?:login|register)/.test(request.path)));
  }, { guest: true, configs: [guestModel] });
  for (const [label, status, body, expected] of [
    ['forbidden', 403, { detail: { error_code: 'access_denied', message: 'synthetic-provider-private-detail' } }, '当前会话无权管理此搜索配置，请刷新后重试。'],
    ['expired_session', 401, { detail: { error_code: 'auth_required' } }, '会话已失效，请刷新页面后重试。'],
    ['conflict', 409, { detail: { error_code: 'config_conflict' } }, '配置已发生变化，请刷新后再更新或测试。'],
    ['general_422_fields', 422, { detail: { error_code: 'invalid_field', message: 'synthetic-provider-private-detail' } }, '配置格式不正确，请检查 API Key 的格式后重试。'],
  ]) {
    await scenario(`save_${label}_clears_key_safe_message_never_probes`, async ({ page, state, card }) => {
      await card.getByLabel('API Key', { exact: true }).fill('synthetic-rejected-key');
      await card.getByRole('button', { name: '保存 Key', exact: true }).click();
      await card.getByText(expected, { exact: true }).waitFor();
      assert.equal(await card.getByLabel('API Key', { exact: true }).inputValue(), '');
      assert.equal(state.requests.filter(request => request.path.endsWith('/test')).length, 0);
      const stored = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
      assert(!stored.includes('synthetic-rejected-key'));
      assert(!state.console.join('\n').includes('synthetic-rejected-key'));
      assert(!state.console.join('\n').includes('synthetic-provider-private-detail'));
      assert(!(await page.locator('body').innerText()).includes('synthetic-provider-private-detail'));
      assert(!(await page.locator('body').innerText()).includes('synthetic-rejected-key'));
    }, { guest: true, onApi: async ({ route, url, method }) => {
      if (url.pathname !== '/api/capabilities/configs' || method !== 'POST') return false;
      await json(route, body, status); return true;
    } });
  }
  await scenario('personal_card_fixed_payload_clear_key_no_storage', async ({ page, state, card }) => {
    const input = card.getByLabel('API Key', { exact: true });
    assert.equal(await input.getAttribute('type'), 'password');
    assert.equal(await input.getAttribute('autocomplete'), 'off');
    assert.equal(await card.locator('select').count(), 0);
    assert.equal(await page.getByRole('heading', { name: '管理员云能力' }).count(), 0);
    for (const mask of ['****', 'masked-****', '••••', ' with-space ']) {
      await input.fill(mask); assert(await card.getByRole('button', { name: '保存 Key', exact: true }).isDisabled());
    }
    await input.fill('synthetic-personal-key');
    await card.getByRole('button', { name: '保存 Key', exact: true }).click();
    await card.getByText('Key 已保存，学习时将按需搜索。', { exact: true }).waitFor();
    assert.equal(await card.getByLabel('新的 API Key', { exact: true }).inputValue(), '');
    const created = state.requests.find(request => request.method === 'POST' && request.path === '/api/capabilities/configs');
    assert.deepEqual(created.body, { kind: 'search', protocol: 'anysearch', endpoint: 'https://api.anysearch.com', model_name: '', credentials: { api_key: 'synthetic-personal-key' }, owner_scope: 'user' });
    assert.equal(state.requests.filter(request => request.path.endsWith('/test')).length, 0);
    assert(state.requests.every(request => !request.url.includes('synthetic-personal-key')));
    const stored = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
    assert(!stored.includes('synthetic-personal-key'));
    assert(!state.console.join('\n').includes('synthetic-personal-key'));
    assert(!(await page.locator('body').innerText()).includes('synthetic-personal-key'));
    assert.equal(await page.getByRole('dialog').count(), 0);
    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  });
  await scenario('update_and_test_omit_masks_and_metadata', async ({ state, card }) => {
    const input = card.getByLabel('新的 API Key', { exact: true });
    assert.equal(await input.inputValue(), '');
    assert(await card.getByRole('button', { name: '更新 Key', exact: true }).isDisabled());
    await card.getByRole('button', { name: '测试搜索', exact: true }).click();
    await card.getByText('AnySearch 测试通过。', { exact: true }).waitFor();
    assert.equal(state.requests.find(request => request.path.endsWith('/test')).body, null);
    assert.equal(state.requests.filter(request => request.path.endsWith('/test')).length, 1);
    await input.fill('synthetic-replacement-key');
    await card.getByRole('button', { name: '更新 Key', exact: true }).click();
    await card.getByText('Key 已保存，学习时将按需搜索。', { exact: true }).waitFor();
    assert.deepEqual(state.requests.find(request => request.method === 'PATCH').body, { credentials: { api_key: 'synthetic-replacement-key' } });
    assert.equal(state.requests.filter(request => request.path.endsWith('/test')).length, 1);
    await card.getByText('未测试', { exact: true }).waitFor();
    assert.equal(await input.inputValue(), '');
  }, { configs: [fixtureConfig()] });
  for (const [name, capability_status, label] of [
    ['untested', {}, '未测试'],
    ['expired', { text: { state: 'available', expires_at: '2000-01-01T00:00:00Z' } }, '上次验证已过期'],
  ]) {
    await scenario(`${name}_config_remains_enabled_no_automatic_probe`, async ({ page, state, card }) => {
      await card.getByText('搜索：已启用', { exact: true }).waitFor();
      await card.getByText(label, { exact: true }).waitFor();
      assert.equal(state.requests.filter(request => request.path.endsWith('/test')).length, 0);
      const input = card.getByLabel('新的 API Key', { exact: true });
      await input.fill(`synthetic-${name}-replacement`);
      assert.equal(await card.getByRole('button', { name: '更新 Key', exact: true }).isEnabled(), true);
      await card.getByRole('button', { name: '更新 Key', exact: true }).click();
      await card.getByText('Key 已保存，学习时将按需搜索。', { exact: true }).waitFor();
      await card.getByText('未测试', { exact: true }).waitFor();
      assert.equal(state.requests.filter(request => request.path.endsWith('/test')).length, 0);
      assert.equal(await input.inputValue(), '');
      await card.getByRole('button', { name: '测试搜索', exact: true }).click();
      await card.getByText('AnySearch 测试通过。', { exact: true }).waitFor();
      assert.equal(state.requests.filter(request => request.path.endsWith('/test')).length, 1);
      await card.getByText('最近验证可用', { exact: true }).waitFor();
      await noSearchDecision(page);
    }, { configs: [{ ...fixtureConfig(), capability_status }] });
  }
  await scenario('probe_failure_retains_saved_config_and_sanitizes_detail', async ({ page, state, card }) => {
    state.probeDetail = 'synthetic-provider-private-detail';
    await card.getByLabel('API Key', { exact: true }).fill('synthetic-failure-key');
    await card.getByRole('button', { name: '保存 Key', exact: true }).click();
    await card.getByText('Key 已保存，学习时将按需搜索。', { exact: true }).waitFor();
    assert.equal(state.requests.filter(request => request.path.endsWith('/test')).length, 0);
    assert.equal(await card.getByRole('button', { name: '测试搜索', exact: true }).count(), 1);
    await card.getByRole('button', { name: '测试搜索', exact: true }).click();
    await card.getByText('上次验证未确认可用，可稍后测试；学习时仍会按需尝试搜索。', { exact: true }).first().waitFor();
    assert.equal(state.requests.filter(request => request.path.endsWith('/test')).length, 1);
    assert(!(await page.locator('body').innerText()).includes('synthetic-provider-private-detail'));
    state.probeDetail = 'SEARCH_CREDENTIAL_INVALID';
    await card.getByRole('button', { name: '测试搜索', exact: true }).click();
    await card.getByText('API Key 无效，请更新后测试。', { exact: true }).first().waitFor();
    state.probeFailure = true;
    await card.getByRole('button', { name: '测试搜索', exact: true }).click();
    await card.getByText('测试未完成，可稍后重试；学习时仍会按需尝试搜索。', { exact: true }).waitFor();
    await noSearchDecision(page);
  });
  await scenario('failed_save_clears_plaintext_and_never_tests', async ({ page, state, card }) => {
    state.saveFailure = true;
    await card.getByLabel('API Key', { exact: true }).fill('synthetic-rejected-key');
    await card.getByRole('button', { name: '保存 Key', exact: true }).click();
    await card.getByText('搜索服务未接受请求，请稍后测试。', { exact: true }).waitFor();
    assert.equal(await card.getByLabel('API Key', { exact: true }).inputValue(), '');
    assert.equal(state.requests.filter(request => request.path.endsWith('/test')).length, 0);
    assert(!(await page.locator('body').innerText()).includes('synthetic-provider-private-detail'));
  });
  await scenario('disable_failure_then_success_no_confirmation', async ({ page, state, card }) => {
    state.deleteFailure = true;
    await card.getByRole('button', { name: '停用搜索', exact: true }).click();
    await card.getByText('停用未完成，请稍后重试。', { exact: true }).waitFor();
    assert.equal(state.configs.length, 1);
    state.deleteFailure = false;
    await card.getByRole('button', { name: '停用搜索', exact: true }).click();
    await card.getByText('搜索已停用。学习将使用现有资料继续。', { exact: true }).waitFor();
    assert.equal(state.configs.length, 0);
    assert.equal(await card.getByRole('button', { name: '保存 Key', exact: true }).count(), 1);
    await noSearchDecision(page);
  }, { configs: [fixtureConfig()] });
  await scenario('admin_search_stays_personal_ocr_form_has_no_search_kind', async ({ page, card }) => {
    const adminCard = page.locator('.card').filter({ has: page.getByRole('heading', { name: '管理员云能力', exact: true }) });
    await adminCard.waitFor();
    assert.equal(await adminCard.locator('select').count(), 0);
    assert.equal(await page.locator('option[value="search"]').count(), 0);
    assert.equal(await card.getByLabel('API Key', { exact: true }).count(), 1);
  }, { admin: true });
  await scenario('account_switch_clears_unsubmitted_key_and_old_config', async ({ page, card }) => {
    await card.getByLabel('新的 API Key', { exact: true }).fill('synthetic-unsaved-owner-a-key');
    await page.getByRole('button', { name: '退出登录', exact: true }).click();
    await page.waitForURL(process.env.TEST_AUTH_DISABLED === '0' ? '**/#/login' : '**/#/home');
    await page.evaluate(() => { location.hash = '/capabilities'; });
    const next = searchCard(page);
    await next.getByText('尚未启用搜索。你仍可使用现有资料学习。', { exact: true }).waitFor();
    assert.equal(await next.getByLabel('API Key', { exact: true }).inputValue(), '');
    assert.equal(await next.getByRole('button', { name: '停用搜索', exact: true }).count(), 0);
  }, { configs: [fixtureConfig('synthetic-owner-a-config')] });
  const gate = deferred(), arrived = deferred();
  await scenario('late_save_after_account_switch_does_not_test_or_restore_old_config', async ({ page, state, card }) => {
    await card.getByLabel('API Key', { exact: true }).fill('synthetic-pending-owner-a-key');
    await card.getByRole('button', { name: '保存 Key', exact: true }).click();
    await arrived.promise;
    assert.equal(await card.getByLabel('API Key', { exact: true }).inputValue(), '');
    await page.getByRole('button', { name: '退出登录', exact: true }).click();
    await page.waitForURL(process.env.TEST_AUTH_DISABLED === '0' ? '**/#/login' : '**/#/home');
    await page.evaluate(() => { location.hash = '/capabilities'; });
    await searchCard(page).getByText('尚未启用搜索。你仍可使用现有资料学习。', { exact: true }).waitFor();
    gate.resolve();
    await page.waitForResponse(response => response.url().endsWith('/api/capabilities/configs') && response.request().method() === 'POST');
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(await searchCard(page).getByLabel('API Key', { exact: true }).inputValue(), '');
    assert.equal(state.requests.filter(request => request.path.endsWith('/test')).length, 0);
    assert.equal(await searchCard(page).getByRole('button', { name: '停用搜索', exact: true }).count(), 0);
  }, { onApi: async ({ route, url, method }) => {
    if (url.pathname !== '/api/capabilities/configs' || method !== 'POST') return false;
    arrived.resolve(); await gate.promise; await json(route, fixtureConfig('synthetic-old-owner-result'), 201); return true;
  } });
  for (const mode of ['done', 'http_wait', 'wait', 'search_error', 'model_error']) {
    await scenario(`chat_${mode}_normal_payload_and_recovery`, async ({ page, state }) => {
      state.chatMode = mode;
      await page.getByLabel('继续提问', { exact: true }).fill('合成学习问题');
      await page.getByRole('button', { name: '发送', exact: true }).click();
      if (mode === 'model_error') await page.getByText('未配置可用模型', { exact: true }).waitFor();
      else if (mode === 'done') await page.getByText('合成回答', { exact: true }).waitFor();
      else await page.getByText('这一轮回答尚未完成', { exact: true }).waitFor();
      const request = state.requests.find(request => request.path.endsWith('/messages/stream'));
      assert(request); assert(!Object.hasOwn(request.body, 'use_search'));
      assert.equal(await page.locator('input[type="checkbox"]').count(), 0);
      assert.equal(state.requests.filter(request => request.path.endsWith('/resume')).length, 0);
      if (mode === 'model_error') {
        await page.getByText('未配置可用模型', { exact: true }).waitFor();
      } else if (mode === 'done') {
        await page.getByText('合成回答', { exact: true }).waitFor();
        await page.getByText('本次回答参考了', { exact: false }).waitFor();
        await page.getByLabel('继续提问', { exact: true }).fill('第二个合成问题');
        await page.getByRole('button', { name: '发送', exact: true }).click();
        await page.waitForFunction(() => document.querySelectorAll('.turn.ai .body p').length === 2);
        assert.equal(await page.getByText('本次回答参考了', { exact: false }).count(), 0);
        await noSearchDecision(page);
      } else {
        await page.getByText('这一轮回答尚未完成', { exact: true }).waitFor();
        await noSearchDecision(page);
        assert.equal(await page.getByText('不应残留的合成片段', { exact: true }).count(), 0);
      }
    }, { route: '/chat' });
  }
  const planningBase = { task_id: 'synthetic-planning', title: '合成规划', target_id: 'synthetic-route', generation: 1, task_status: 'awaiting', wait_state: null };
  await scenario('planning_legacy_search_wait_has_no_search_decision_ui', async ({ page }) => {
    await page.getByText('合成规划', { exact: true }).waitFor();
    await noSearchDecision(page);
  }, { route: '/planning', planning: { ...planningBase, wait_state: 'awaiting_search_decision' } });
  await scenario('planning_scope_clarification_preserved', async ({ page }) => {
    await page.getByRole('button', { name: '继续了解学习要求', exact: true }).waitFor();
    await noSearchDecision(page);
  }, { route: '/planning', planning: { ...planningBase, wait_state: 'awaiting_scope_clarification', scope_clarification: { reason: '合成范围需要选择' } } });
  await scenario('planning_nonsearch_failure_recovery_preserved', async ({ page }) => {
    await page.getByRole('alertdialog').waitFor();
    await page.getByRole('button', { name: '检查模型设置', exact: true }).waitFor();
    await page.getByRole('button', { name: '更新设置后继续生成', exact: true }).waitFor();
  }, { route: '/planning', planning: { ...planningBase, task_status: 'failed', wait_state: 'model_recovery', error_code: 'MODEL_CAPABILITY_UNAVAILABLE' } });
  await scenario('mobile_personal_search_layout_and_key_update', async ({ page, state, card }) => {
    const input = card.getByLabel('新的 API Key', { exact: true });
    await input.fill('synthetic-mobile-private-key');
    await card.getByRole('button', { name: '更新 Key', exact: true }).click();
    await card.getByText('Key 已保存，学习时将按需搜索。', { exact: true }).waitFor();
    await page.waitForFunction(() => document.querySelector('input[type="password"]')?.value === '');
    assert.equal(state.requests.filter(request => request.method === 'PATCH').length, 1);
    assert.equal(state.requests.filter(request => request.path.endsWith('/test')).length, 0);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    const bounds = await input.boundingBox();
    assert(bounds && bounds.x >= 0 && bounds.x + bounds.width <= 390);
    await noSearchDecision(page);
  }, { configs: [fixtureConfig()], viewport: { width: 390, height: 844 } });
  console.log(JSON.stringify({ synthetic: true, transport: 'built-assets-intercepted-no-server', checks }));
} catch (error) {
  console.log(JSON.stringify({ synthetic: true, completedChecks: checks }));
  throw error;
} finally { await browser.close(); }
