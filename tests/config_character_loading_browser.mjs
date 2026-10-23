import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdir } from 'node:fs/promises';
import { mockBuiltFrontend } from './frontend_dist_mock.mjs';

const { chromium } = createRequire(import.meta.url)(process.env.PW_ROOT + '/playwright-core');
const browser = await chromium.launch({ headless: true, executablePath: process.env.TEST_BROWSER });
const checks = [];
const output = '.backup/config-character-loading';
await mkdir(output, { recursive: true });
const config = { config_id: 'synthetic-config', owner_scope: 'user', kind: 'content_model',
  protocol: 'openai', endpoint: 'https://model.synthetic.test/v1', model_name: 'synthetic-model',
  credential_mask: '****', capability_status: {}, config_version: 1, is_active: true };
const profile = { name: 'Synthetic tutor', identity: 'Tutor', background: '', personality: '',
  appearance: '', tags: [], voice_id: '', voice_config_id: '', media_ids: [], base_asset_id: '' };
const card = { card_id: 'synthetic-card', revision: 1, published_revision: 1, editable: true,
  collected: true, profile, assets: [] };
const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body) });

async function scenario(name, hash, test) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: 'block' });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  await page.clock.install();
  const served = await mockBuiltFrontend(page);
  const errors = [], failed = [];
  const state = { configGets: 0, hanging: true };
  const held = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('requestfailed', request => failed.push(new URL(request.url()).pathname));
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') return json(route, { user_id: 'synthetic-owner', is_admin: false, status: 'approved' });
    if (path === '/api/capabilities/configs') {
      state.configGets++;
      if (state.hanging) {
        await new Promise(resolve => held.push(resolve));
        return json(route, { data: [config] }).catch(() => {});
      }
      return json(route, { data: [config] });
    }
    if (path === '/api/cards' || path === '/api/card-market') return json(route, { data: [card] });
    if (path === '/api/mentor-preference') return json(route, { version: 0, card_id: null, revision: null });
    if (path === '/api/character-voices') return json(route, { data: [] });
    if (path === '/api/character-expression') return json(route, { data: [], recommended_voice_id: null,
      voice_gender: 'unknown', match_status: 'no_compatible_voice', speaking_style: '表达沉稳，逐步拆解。' });
    if (path === '/api/cards/synthetic-card') return json(route, card);
    if (path.endsWith('/media-jobs') || path === '/api/connectors') return json(route, { data: [] });
    throw new Error(`Unexpected synthetic request: ${path}`);
  });
  try {
    await page.goto(served.origin + '/#/' + hash);
    await test({ page, state, failed, served });
    assert.deepEqual(errors, []);
    assert.deepEqual(served.unexpected, []);
    checks.push(name);
  } finally {
    for (const release of held) release();
    await context.close();
  }
}

try {
  await scenario('settings_timeout_and_retry', 'capabilities', async ({ page, state }) => {
    await page.getByText('正在加载能力配置…', { exact: true }).waitFor();
    await page.clock.fastForward(10001);
    await page.getByText('加载能力配置失败，请重试。', { exact: true }).waitFor();
    assert.equal(await page.getByText('正在加载能力配置…', { exact: true }).count(), 0);
    state.hanging = false;
    await page.getByRole('button', { name: '重试加载配置', exact: true }).click();
    await page.getByText(/synthetic-model/).waitFor();
    await page.screenshot({ path: output + '/settings-recovered.png', fullPage: true });
  });

  await scenario('leaving_settings_cancels_request_and_lists_do_not_need_configs', 'capabilities',
    async ({ page, state, failed }) => {
      await page.getByText('正在加载能力配置…', { exact: true }).waitFor();
      await page.getByRole('button', { name: '人物导师', exact: true }).click();
      await page.getByRole('button', { name: '查看 Synthetic tutor', exact: true }).waitFor();
      await page.waitForFunction(() => !document.body.innerText.includes('正在加载人物…'));
      assert.equal(state.configGets, 1);
      await page.getByRole('button', { name: '卡片市集', exact: true }).click();
      await page.getByRole('button', { name: '查看 Synthetic tutor', exact: true }).waitFor();
      assert.equal(state.configGets, 1);
      assert(failed.includes('/api/capabilities/configs'));
      await page.screenshot({ path: output + '/market-independent.png' });
    });

  await scenario('editor_survives_config_timeout_and_retries_locally', 'card/synthetic-card',
    async ({ page, state }) => {
      await page.getByLabel('名字', { exact: true }).waitFor();
      await page.getByText('正在加载人物服务配置…', { exact: true }).waitFor();
      assert.equal(await page.getByText('正在加载人物…', { exact: true }).count(), 0);
      await page.clock.fastForward(10001);
      const configError = page.getByRole('alert').filter({ hasText: '加载人物服务配置失败，请重试。' });
      await configError.waitFor();
      assert.equal(await page.getByLabel('名字', { exact: true }).inputValue(), profile.name);
      state.hanging = false;
      await page.getByRole('button', { name: '重试加载服务配置', exact: true }).click();
      await configError.waitFor({ state: 'hidden' });
      await page.getByText('正在加载人物服务配置…', { exact: true }).waitFor({ state: 'hidden' });
      assert.equal(state.configGets, 2);
    });
  console.log(JSON.stringify({ synthetic: true, checks }));
} finally {
  await browser.close();
}
