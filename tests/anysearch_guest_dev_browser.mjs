import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFile, mkdir } from 'node:fs/promises';
import path from 'node:path';

// Only read local Vite assets. Every API is synthetic; all other network is blocked.
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PW_ROOT + '/playwright-core');
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
const origin = process.env.TEST_FRONTEND || 'http://127.0.0.1:5173';
const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
const checks = [];
const model = { config_id: 'synthetic-guest-config', kind: 'content_model', protocol: 'openai', owner_scope: 'user', endpoint: 'https://model.synthetic.test/v1', model_name: 'synthetic-existing-model', credential_mask: '****', capability_status: {}, config_version: 1, is_active: true };
const search = { ...model, config_id: 'synthetic-search', kind: 'search', protocol: 'anysearch', endpoint: 'https://api.anysearch.com', model_name: '', capability_status: {} };
try {
  const localEnvironment = await readFile(new URL('../frontend/.env.local', import.meta.url), 'utf8');
  assert(/^VITE_DISABLE_AUTH=1\s*$/m.test(localEnvironment), 'This focused check requires existing dev flag 1');
  const response = await browser.newContext();
  try {
    const module = await response.request.get(`${origin}/src/hooks/useSession.ts`);
    assert(module.ok());
    assert(!(await module.text()).includes('debug-user'), 'The served development module must use real session recovery');
  } finally { await response.close(); }
  for (const [name, guest, routeHash, saveFailure, expired = false] of [
    ['dev_flag_guest_can_save_test_reload_and_keep_models', true, '/capabilities', false],
    ['dev_flag_guest_failed_save_clears_key', true, '/capabilities', true],
    ['dev_flag_guest_expired_search_remains_enabled_update_without_probe', true, '/capabilities', false, true],
    ...['/login', '/register', '/verify', '/reset'].map(route => [`dev_flag_${route.slice(1)}_redirects_home`, true, route, false]),
    ['dev_flag_existing_real_session_keeps_search_form', false, '/capabilities', false],
  ]) {
    const context = await browser.newContext({ serviceWorkers: 'block' });
    const page = await context.newPage();
    const requests = [], errors = [], blocked = [], consoleMessages = [];
    let configs = expired ? [model, { ...search, capability_status: { text: { state: 'available', expires_at: '2000-01-01T00:00:00Z' } } }] : [model];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => consoleMessages.push(message.text()));
    page.setDefaultTimeout(10000);
    await page.route('**/*', async routed => {
      const url = new URL(routed.request().url());
      if (url.origin === origin && !url.pathname.startsWith('/api/')) return routed.continue();
      blocked.push(url.origin + url.pathname); return routed.abort();
    });
    await page.route('**/api/**', async routed => {
      const request = routed.request(), url = new URL(request.url()), method = request.method();
      requests.push({ path: url.pathname, method });
      const me = { user_id: guest ? 'synthetic-guest' : 'synthetic-member', email: guest ? '' : 'member@example.test', is_admin: false, status: 'approved', is_guest: guest };
      if (url.pathname === '/api/auth/me') return guest ? json(routed, { error_code: 'AUTH_REQUIRED' }, 401) : json(routed, me);
      if (url.pathname === '/api/auth/guest') return json(routed, me);
      if (url.pathname === '/api/capabilities/configs' && method === 'GET') return json(routed, { data: configs });
      if (url.pathname === '/api/capabilities/configs' && method === 'POST') {
        if (saveFailure) return json(routed, { error_code: 'temporary_failure', message: 'synthetic-private-detail' }, 503);
        configs = [...configs, search]; return json(routed, search, 201);
      }
      if (url.pathname === '/api/capabilities/configs/synthetic-search/test') {
        const capability_status = { text: { state: 'available' } };
        configs = configs.map(config => config.config_id === search.config_id ? { ...config, capability_status } : config);
        return json(routed, { capability_status });
      }
      if (url.pathname === '/api/capabilities/configs/synthetic-search' && method === 'PATCH') {
        configs = configs.map(config => config.config_id === search.config_id ? { ...config, config_version: config.config_version + 1, capability_status: {} } : config);
        return json(routed, configs.find(config => config.config_id === search.config_id));
      }
      if (url.pathname === '/api/connectors' || url.pathname === '/api/materials') return json(routed, { data: [] });
      if (url.pathname === '/api/navigation') return json(routed, {});
      errors.push(`Unexpected synthetic API ${method} ${url.pathname}`); return routed.abort();
    });
    try {
      await page.goto(`${origin}/#${routeHash}`);
      if (routeHash === '/capabilities') {
        const card = page.getByRole('region', { name: 'AnySearch 搜索', exact: true });
        await page.getByText(/synthetic-existing-model/).waitFor();
        if (expired) {
          await card.getByText('搜索：已启用', { exact: true }).waitFor();
          await card.getByText('上次验证已过期', { exact: true }).waitFor();
          assert.equal(requests.filter(request => request.path.endsWith('/test')).length, 0);
        }
        await card.getByLabel(expired ? '新的 API Key' : 'API Key', { exact: true }).fill('synthetic-dev-guest-key');
        await card.getByRole('button', { name: expired ? '更新 Key' : '保存 Key', exact: true }).click();
        await card.getByText(saveFailure ? '保存未完成，请稍后重试。' : 'Key 已保存，学习时将按需搜索。', { exact: true }).waitFor();
        assert.equal(await card.getByLabel(saveFailure ? 'API Key' : '新的 API Key', { exact: true }).inputValue(), '');
        assert.equal(requests.filter(request => request.path.endsWith('/test')).length, 0);
        if (!saveFailure) {
          await card.getByText('搜索：已启用', { exact: true }).waitFor();
          await card.getByText('未测试', { exact: true }).waitFor();
          await page.reload();
          await card.getByText('搜索：已启用', { exact: true }).waitFor();
          await card.getByText('未测试', { exact: true }).waitFor();
          assert.equal(requests.filter(request => request.path.endsWith('/test')).length, 0);
          const input = card.getByLabel('新的 API Key', { exact: true });
          assert.equal(await input.inputValue(), '');
          await input.fill('synthetic-dev-guest-replacement-key');
          await card.getByRole('button', { name: '更新 Key', exact: true }).click();
          await card.getByText('Key 已保存，学习时将按需搜索。', { exact: true }).waitFor();
          assert.equal(await input.inputValue(), '');
          assert.equal(requests.filter(request => request.path.endsWith('/test')).length, 0);
          await card.getByText('未测试', { exact: true }).waitFor();
          await card.getByRole('button', { name: '测试搜索', exact: true }).click();
          await card.getByText('AnySearch 测试通过。', { exact: true }).waitFor();
          assert.equal(requests.filter(request => request.path.endsWith('/test')).length, 1);
          await page.reload();
          await card.getByText('搜索：已启用', { exact: true }).waitFor();
          await card.getByText('最近验证可用', { exact: true }).waitFor();
          assert.equal(requests.filter(request => request.path.endsWith('/test')).length, 1);
          await page.getByText(/synthetic-existing-model/).waitFor();
          if (process.env.TEST_OUTPUT && guest) {
            await mkdir(process.env.TEST_OUTPUT, { recursive: true });
            await card.screenshot({ path: path.join(process.env.TEST_OUTPUT, 'guest-search-no-login.png') });
          }
        }
      } else {
        await page.waitForURL('**/#/home');
        await page.getByRole('button', { name: '设置', exact: true }).waitFor();
        assert.equal(await page.locator('.auth-col').count(), 0);
      }
      assert.equal(await page.getByRole('button', { name: /^(登录|申请加入)$/ }).count(), 0);
      assert.equal(await page.locator('a[href="#/login"], a[href="#/register"]').count(), 0);
      assert(requests.some(request => request.path === '/api/auth/me'));
      assert.equal(requests.some(request => request.path === '/api/auth/guest'), guest);
      assert(!requests.some(request => /\/auth\/(?:login|register|verify|reset)/.test(request.path)));
      const stored = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
      assert(!stored.includes('synthetic-dev-guest-key'));
      assert(!stored.includes('synthetic-dev-guest-replacement-key'));
      assert(!consoleMessages.join('\n').includes('synthetic-dev-guest-key'));
      assert(!consoleMessages.join('\n').includes('synthetic-dev-guest-replacement-key'));
      const text = await page.locator('body').innerText();
      assert(!text.includes('synthetic-dev-guest-key'));
      assert(!text.includes('synthetic-dev-guest-replacement-key'));
      assert(!text.includes('synthetic-private-detail'));
      assert(!text.includes('debug@localhost'));
      assert.deepEqual(errors, []);
      assert.deepEqual(blocked, []);
      checks.push(name);
    } finally { await context.close(); }
  }
  console.log(JSON.stringify({ synthetic: true, devFlag: 1, checks }));
} finally { await browser.close(); }
