/**
 * Paid, explicit channel benchmark. No network on import or --self-test.
 * Original responses, prompts and credentials are never written to artifacts.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import {
  ROOT, loadCases, LOCAL_CASES, requestJSON, visibleResult, seededShuffle,
  referenceMetrics, summarizeReferences, percentile, usageSummary,
} from './search_comparison_live.mjs';
import { localityMetric } from './search_deepseek_live.mjs';

export const CHANNELS = {
  spectra: 'https://spectrarouter.com/v1',
  deepkey: 'https://deepkey.top/v1',
};
export const MODELS = ['grok-4.6', 'grok-4.7'];
export const INSTRUCTION = "Use web search. Give a concise factual answer in the question's language (at most 400 words), with clickable primary-source citations. Correct false premises and explicitly identify uncertainty. Do not use X search or code execution.";

export function errorClass(response) {
  const body = response.json || {};
  const text = JSON.stringify(body);
  if (response.error) return response.error;
  if ([401, 402, 403].includes(response.status) || body.code === 403 || body.code === 402) {
    if (/billing|insufficient|credit|balance|余额|额度/i.test(text)) return 'BILLING_OR_QUOTA';
    return response.status === 401 ? 'AUTHENTICATION_BLOCKED' : 'ACCESS_BLOCKED';
  }
  if (response.status === 429) return 'RATE_LIMIT';
  if (response.status !== 200) return `HTTP_${response.status}`;
  return null;
}

export function decode(response, channel, model, effort) {
  const parsed = visibleResult('grok', response);
  parsed.sources = parsed.sources.filter(source => {
    try {
      const url = new URL(source.url);
      return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password;
    } catch { return false; }
  });
  const searchProven = parsed.ok && parsed.sources.length > 0;
  const modelConfirmed = [model, `${model}-build`].includes(response.json?.model);
  const effortConfirmed = response.json?.reasoning?.effort === effort;
  const error = errorClass(response)
    || (!searchProven ? (parsed.error || 'SEARCH_NOT_PROVEN')
      : !modelConfirmed ? 'MODEL_ALIAS_UNRECOGNIZED'
        : !effortConfirmed ? 'EFFORT_NOT_CONFIRMED' : null);
  return {
    ...parsed, channel, model, provider: `${channel}:${model}`,
    effort, searchProven, modelConfirmed, effortConfirmed,
    ok: searchProven && modelConfirmed && effortConfirmed,
    error,
  };
}

export function project(row) {
  const allowedModels = MODELS.flatMap(model => [model, `${model}-build`]);
  const calls = (row.json?.output || []).filter(item => item.type === 'web_search_call');
  const numericUsage = Object.fromEntries(Object.entries(usageSummary(row.usage))
    .map(([key, value]) => [key, typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null]));
  return {
    channel: row.channel, model: row.model, provider: row.provider,
    phase: row.phase, round: row.round, case_id: row.task.id,
    domain: row.task.domain, language: row.task.language,
    requested_effort: row.effort,
    returned_effort: ['low', 'medium', 'high', 'xhigh'].includes(row.json?.reasoning?.effort) ? row.json.reasoning.effort : null,
    returned_model: allowedModels.includes(row.json?.model) ? row.json.model : null,
    wire_attempted: row.wireAttempted !== false, http_status: row.status,
    error_code: row.error, search_proven: row.searchProven,
    model_confirmed: row.modelConfirmed, effort_confirmed: row.effortConfirmed,
    success: row.ok, duration_ms: row.ms, within_20s: row.ok && row.ms <= 20000,
    utc: row.utc, source_count: row.sources.length, tool_calls: row.toolCalls,
    completed_tool_calls: calls.filter(item => item.status === 'completed').length,
    tool_actions: Object.fromEntries(['search', 'open_page', 'find_in_page'].map(type =>
      [type, calls.filter(item => item.action?.type === type).length])),
    requested_tool_cap: 3,
    returned_tool_cap: Number.isInteger(row.json?.max_tool_calls) && row.json.max_tool_calls >= 0 ? row.json.max_tool_calls : null,
    answer_chars: row.answer.length,
    citation_count: (row.json?.output || []).filter(item => item.type === 'message')
      .flatMap(item => item.content || []).flatMap(item => item.annotations || []).filter(item => item.url).length,
    usage: numericUsage, references: referenceMetrics(row),
  };
}

export class ChannelExperiment {
  constructor({ keys, blocked = [], concurrency = 2, timeout = 120000 }) {
    this.keys = keys;
    this.blocked = new Set(blocked);
    this.concurrency = concurrency;
    this.timeout = timeout;
    this.rows = [];
    this.phase = 'idle';
    this.done = 0;
    this.total = 0;
    this.active = 0;
  }

  async one(task, channel, model, effort) {
    if (this.blocked.has(channel)) {
      return decode({ status: 0, ms: 0, error: 'CHANNEL_BLOCKED_NO_REQUEST', wireAttempted: false }, channel, model, effort);
    }
    const response = await requestJSON(`${CHANNELS[channel]}/responses`, this.keys[channel], {
      model, input: `${task.query}\n${INSTRUCTION}`,
      tools: [{ type: 'web_search' }], max_tool_calls: 3,
      max_output_tokens: 3000, reasoning: { effort }, store: false,
    }, this.timeout);
    const decoded = decode(response, channel, model, effort);
    if (['BILLING_OR_QUOTA', 'AUTHENTICATION_BLOCKED', 'ACCESS_BLOCKED'].includes(decoded.error)) {
      this.blocked.add(channel);
    }
    return decoded;
  }

  async runPhase(tasks, { phase, effort = 'high', rounds = 1, roundStart = 1, channels = ['deepkey'] }) {
    this.phase = phase;
    this.done = 0;
    const queue = [];
    for (let round = roundStart; round < roundStart + rounds; round++) {
      for (const task of seededShuffle(tasks, 20261017 + round)) queue.push({ task, round });
    }
    const arms = channels.flatMap(channel => MODELS.map(model => ({ channel, model })));
    this.total = queue.length * arms.length;
    let next = 0;
    await Promise.all(Array.from({ length: this.concurrency }, async () => {
      while (next < queue.length) {
        const { task, round } = queue[next++];
        await Promise.all(seededShuffle(arms, round * 1000 + next).map(async ({ channel, model }) => {
          this.active++;
          const utc = new Date().toISOString();
          const result = await this.one(task, channel, model, effort);
          this.rows.push({ ...result, task, round, phase, utc });
          this.done++;
          this.active--;
        }));
      }
    }));
  }

  async runAll(tasks, channels) {
    const sample = tasks.filter(task => /01$/.test(task.semantic));
    await this.runPhase(tasks, { phase: 'main_high', channels });
    if (channels.every(channel => this.blocked.has(channel))) return;
    await this.runPhase(sample, { phase: 'repeat_high', channels, rounds: 2, roundStart: 2 });
    await this.runPhase(sample, { phase: 'sample_low', channels, effort: 'low' });
    await this.runPhase(LOCAL_CASES, { phase: 'locality_high', channels });
    this.phase = 'complete';
  }

  progress() {
    return {
      phase: this.phase, done: this.done, total: this.total,
      active: this.active, blocked: [...this.blocked],
      groups: Object.entries(CHANNELS).flatMap(([channel]) => MODELS.map(model => {
        const rows = this.rows.filter(row => row.channel === channel && row.model === model);
        return { channel, model, recorded: rows.length, sent: rows.filter(row => row.wireAttempted !== false).length,
          success: rows.filter(row => row.ok).length };
      })),
    };
  }
}

export function aggregate(rows) {
  const latency = rows.filter(row => row.wireAttempted !== false
    && !['BILLING_OR_QUOTA', 'AUTHENTICATION_BLOCKED', 'ACCESS_BLOCKED'].includes(row.error));
  const successful = latency.filter(row => row.ok);
  const usage = field => rows.reduce((sum, row) => sum + (Number(field(row.usage)) || 0), 0);
  return {
    n: rows.length, actual_requests: rows.filter(row => row.wireAttempted !== false).length,
    success: rows.filter(row => row.ok).length,
    search_proven: rows.filter(row => row.searchProven).length,
    within_20s: rows.filter(row => row.ok && row.ms <= 20000).length,
    latency_n: latency.length,
    p50_ms: percentile(latency.map(row => row.ms), 0.5),
    p95_ms: percentile(latency.map(row => row.ms), 0.95),
    success_latency_n: successful.length,
    success_p50_ms: percentile(successful.map(row => row.ms), 0.5),
    success_p95_ms: percentile(successful.map(row => row.ms), 0.95),
    references: summarizeReferences(rows),
    tool_calls: rows.reduce((sum, row) => sum + (Number(row.toolCalls) || 0), 0),
    over_requested_tool_cap: rows.filter(row => row.toolCalls > 3).length,
    errors: Object.fromEntries([...new Set(rows.filter(row => !row.ok).map(row => row.error))]
      .map(error => [error, rows.filter(row => row.error === error).length])),
    input_tokens: usage(item => item.input_tokens),
    output_tokens: usage(item => item.output_tokens),
    cached_tokens: usage(item => item.input_tokens_details?.cached_tokens),
    cost_in_usd_ticks_reported: usage(item => item.cost_in_usd_ticks),
  };
}

export function summarize(experiment) {
  const phaseGroups = (phase, selectors = {}) => Object.keys(CHANNELS).flatMap(channel => MODELS.map(model => ({
    channel, model, ...selectors,
    ...aggregate(experiment.rows.filter(row => row.phase === phase && row.channel === channel && row.model === model
      && Object.entries(selectors).every(([key, value]) => row.task[key] === value))),
  })));
  const sampleIDs = new Set(experiment.rows.filter(row => row.phase === 'sample_low').map(row => row.task.id));
  return {
    utc: new Date().toISOString(), seed: 20261017, concurrency_questions: experiment.concurrency,
    blocked_channels: [...experiment.blocked],
    main: phaseGroups('main_high'),
    strata: ['paper', 'development', 'agent', 'learning'].flatMap(domain => ['zh', 'en']
      .flatMap(language => phaseGroups('main_high', { domain, language }))),
    phases: ['main_high', 'repeat_high', 'sample_low', 'locality_high'].map(phase => ({ phase, groups: phaseGroups(phase) })),
    effort_sample: Object.keys(CHANNELS).flatMap(channel => MODELS.map(model => ({
      channel, model,
      high_first: aggregate(experiment.rows.filter(row => row.phase === 'main_high' && sampleIDs.has(row.task.id)
        && row.channel === channel && row.model === model)),
      high_repeated: aggregate(experiment.rows.filter(row => ['main_high', 'repeat_high'].includes(row.phase)
        && sampleIDs.has(row.task.id) && row.channel === channel && row.model === model)),
      low: aggregate(experiment.rows.filter(row => row.phase === 'sample_low' && row.channel === channel && row.model === model)),
    }))),
    locality: experiment.rows.filter(row => row.phase === 'locality_high').map(row => ({
      ...project(row),
      preset_site_or_paper_match_count: localityMetric({
        ...row, sources: row.sources.slice(0, 10).map(item => ({ ...item, summary: '', snippet: '', content: '' })),
      }).preset_site_or_paper_match_count,
    })),
  };
}

async function selfTest() {
  const tasks = await loadCases();
  assert.equal(tasks.length, 64);
  const fake = {
    status: 200, ms: 10, json: {
      status: 'completed', model: 'grok-4.6-build', reasoning: { effort: 'high' },
      output: [
        { type: 'web_search_call', status: 'completed', action: { type: 'search', sources: [{ url: 'https://example.org/' }] } },
        { type: 'message', content: [{ text: 'synthetic answer', annotations: [] }] },
      ],
    },
  };
  const result = decode(fake, 'deepkey', 'grok-4.6', 'high');
  assert.ok(result.ok && result.searchProven);
  assert.equal(decode(fake, 'deepkey', 'grok-4.6', 'low').error, 'EFFORT_NOT_CONFIRMED');
  assert.equal(decode({ status: 200, json: { status: 'completed', output: [] } }, 'deepkey', 'grok-4.6', 'high').ok, false);
  assert.equal(errorClass({ status: 403, json: { code: 403, message: 'synthetic insufficient quota' } }), 'BILLING_OR_QUOTA');
  const experiment = new ChannelExperiment({ keys: {}, blocked: ['spectra'] });
  const skipped = await experiment.one(tasks[0], 'spectra', 'grok-4.6', 'high');
  assert.equal(skipped.wireAttempted, false);
  const row = { ...result, task: tasks[0], phase: 'test', round: 1 };
  assert.ok(!JSON.stringify(project(row)).includes('synthetic answer'));
  assert.ok(!JSON.stringify(project(row)).includes('example.org'));
  assert.equal(project(row).tool_actions.search, 1);
  const blocked = { ...row, ok: false, error: 'BILLING_OR_QUOTA', ms: 1 };
  assert.equal(aggregate([row, blocked]).latency_n, 1);
  assert.equal(aggregate([row, blocked]).n, 2);
  const fastFailure = { ...row, ok: false, error: 'SEARCH_NOT_PROVEN', ms: 1 };
  assert.equal(aggregate([row, fastFailure]).p50_ms, 1);
  assert.equal(aggregate([row, fastFailure]).success_p50_ms, 10);
  const syntheticSensitive = {
    ...row, json: { ...row.json, model: 'unrecognized-synthetic-private-value' },
    usage: { input_tokens: { private: 'synthetic-private-value' } },
  };
  assert.ok(!JSON.stringify(project(syntheticSensitive)).includes('synthetic-private-value'));
  const schedule = new ChannelExperiment({ keys: {} });
  schedule.one = async (_task, channel, model, effort) => decode({
    ...fake, json: { ...fake.json, model: `${model}-build`, reasoning: { effort } },
  }, channel, model, effort);
  await schedule.runAll(tasks, ['deepkey']);
  assert.equal(schedule.rows.length, 192);
  assert.equal(schedule.rows.filter(item => item.phase === 'main_high').length, 128);
  assert.equal(schedule.rows.filter(item => item.phase === 'repeat_high').length, 32);
  assert.equal(schedule.rows.filter(item => item.phase === 'sample_low').length, 16);
  assert.equal(schedule.rows.filter(item => item.phase === 'locality_high').length, 16);
  const local = new ChannelExperiment({ keys: {} });
  const localRow = {
    ...row, task: LOCAL_CASES[0], phase: 'locality_high',
    sources: [...Array.from({ length: 10 }, () => ({ url: 'https://example.org/' })), { url: 'https://jos.org.cn/' }],
  };
  local.rows = [localRow];
  assert.equal(summarize(local).locality[0].preset_site_or_paper_match_count, 0);
  localRow.sources.unshift({ url: 'https://jos.org.cn/' });
  assert.equal(summarize(local).locality[0].preset_site_or_paper_match_count, 1);
  console.log('PASS: identity, search proof, billing circuit, metadata projection, denied latency, 192-request schedule, locality top-10 cap');
}

async function liveCLI() {
  const keys = { spectra: process.env.GROK_SPECTRAROUTER_API_KEY, deepkey: process.env.DEEPKEY_API_KEY };
  const configured = Object.keys(CHANNELS).filter(channel => keys[channel]);
  if (!configured.length) throw new Error('Set GROK_SPECTRAROUTER_API_KEY and/or DEEPKEY_API_KEY in the process environment.');
  const tasks = await loadCases();
  const experiment = new ChannelExperiment({ keys });
  const probeTask = tasks.find(task => task.id === 'P01-en');
  const probes = [];
  for (const channel of configured) {
    for (const model of MODELS) {
      if (experiment.blocked.has(channel)) break;
      const utc = new Date().toISOString();
      probes.push({ ...(await experiment.one(probeTask, channel, model, 'high')),
        task: probeTask, phase: 'preflight_high', round: 1, utc });
    }
  }
  const eligible = configured.filter(channel => !experiment.blocked.has(channel)
    && MODELS.every(model => probes.some(row => row.channel === channel && row.model === model && row.ok)));
  const interval = setInterval(() => console.log(JSON.stringify(experiment.progress())), 30000);
  try { if (eligible.length) await experiment.runAll(tasks, eligible); }
  finally { clearInterval(interval); }
  const directory = path.join(ROOT, '文档/测试归档/search-comparison/grok-channels/runs',
    new Date().toISOString().replace(/[:.]/g, '-'));
  await fs.mkdir(directory, { recursive: true });
  await fs.writeFile(path.join(directory, 'search-metrics.json'), JSON.stringify(experiment.rows.map(project), null, 2) + '\n');
  await fs.writeFile(path.join(directory, 'preflight-metrics.json'), JSON.stringify(probes.map(project), null, 2) + '\n');
  await fs.writeFile(path.join(directory, 'summary.json'), JSON.stringify(summarize(experiment), null, 2) + '\n');
  console.log(experiment.blocked.size ? 'PARTIAL CHANNEL AVAILABILITY: see metadata.' : 'COMPLETED: see metadata.');
  if (eligible.length !== configured.length || experiment.blocked.size) process.exitCode = 2;
}

const IS_ENTRY = typeof process !== 'undefined' && process.argv[1]
  && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (IS_ENTRY && process.argv.includes('--self-test')) await selfTest();
if (IS_ENTRY && process.argv.includes('--live')) await liveCLI();
