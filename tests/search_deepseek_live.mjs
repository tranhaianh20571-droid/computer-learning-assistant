/**
 * Explicit paid search benchmark; original responses and credentials stay in memory.
 * No requests are sent by import or by --self-test.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import {
  ROOT, loadCases, LOCAL_CASES, requestJSON, seededShuffle, percentile,
  referenceMetrics, referenceIdentity, summarizeReferences,
} from './search_comparison_live.mjs';

const DEEPSEEK_URL = 'https://api.deepseek.com/v1/tools/search';
const ANYSEARCH_URL = 'https://api.anysearch.com/v1/search';
export const SHORT_QUERIES = {
  'A07-en': 'How should a long-running agent handle cancellation, expired leases and stale writes? Cite primary framework or database docs.',
  'L01-en': 'Find an open linear algebra course: prerequisites, matrix/eigenvalue units and exercises for a learner with basic calculus.',
  'L03-en': 'Find data-structures materials for a Python beginner. Propose a four-week sequence grounded in syllabus and exercises.',
  'L07-en': "Use authoritative teaching sources to correct 'p-value is the probability the null is true'; give a numerical example.",
  'L08-en': 'Can a learner with high-school algebra start graduate convex optimization? Verify prerequisites and propose a bridge plan.',
};

export async function casesForDeepSeek() {
  return (await loadCases()).map(task => ({
    ...task, query: SHORT_QUERIES[task.id] || task.query,
    query_variant: SHORT_QUERIES[task.id] ? 'shortened_v1' : 'original',
  }));
}

export function localityCases() {
  const shorter = {
    'XP02-en': "Find Zhao et al.'s A Survey of Large Language Models: original year, authors, arXiv ID and free full text.",
    'XD02-en': 'Using MDN, show how AbortController cancels fetch; distinguish HTTP errors from network failures.',
    'XA02-en': 'Find Anthropic client tool-use docs: who executes client tools and how is tool_result returned in conversation?',
    'XL02-en': 'Find MIT OCW 6.006: prerequisites, data-structure/algorithm units, problem sets and free solutions.',
  };
  return LOCAL_CASES.map(task => ({
    ...task, query: shorter[task.id] || task.query,
    query_variant: shorter[task.id] ? 'locality_shortened_v1' : 'original',
  }));
}

export function localityMetric(row) {
  const domains = {
    'XP01-zh': ['jos.org.cn'],
    'XD01-zh': ['developers.weixin.qq.com'],
    'XD02-en': ['developer.mozilla.org'],
    'XA01-zh': ['help.aliyun.com'],
    'XA02-en': ['docs.anthropic.com', 'platform.claude.com'],
    'XL01-zh': ['icourse163.org', 'xuetangx.com', 'smartedu.cn'],
    'XL02-en': ['ocw.mit.edu'],
  }[row.task.id] || [];
  const matches = row.sources.filter(item => {
    if (row.task.id === 'XP02-en') return referenceIdentity(item.url) === 'arxiv:2303.18223';
    try {
      const host = new URL(item.url).hostname;
      return domains.some(domain => host === domain || host.endsWith('.' + domain));
    } catch { return false; }
  }).length;
  return { ...project(row), preset_site_or_paper_match_count: matches };
}

export function parseSearch(provider, response) {
  const body = response.json || {};
  let values;
  let shape;
  if (provider === 'deepseek') {
    if (Array.isArray(body.results)) {
      values = body.results;
      shape = 'flat';
    }
  } else if (body.code === 0 && Array.isArray(body.data?.results)) {
    values = body.data.results;
    shape = 'anysearch_nested';
  }
  const sources = (values || []).filter(item => {
    try {
      const url = new URL(item.url);
      return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password;
    } catch { return false; }
  }).slice(0, 10).map(item => ({
    url: item.url, title: typeof item.title === 'string' ? item.title : '',
    summary: typeof item.summary === 'string' ? item.summary : '',
    snippet: typeof item.snippet === 'string' ? item.snippet : '',
    content: typeof item.content === 'string' ? item.content : '',
  }));
  const ok = response.status === 200 && Boolean(shape) && sources.length > 0;
  let error = response.error || null;
  if (!ok && !error) {
    error = [401, 402, 403].includes(response.status) ? 'ACCOUNT_BLOCKED'
      : response.status === 429 ? 'RATE_LIMIT'
        : response.status !== 200 ? `HTTP_${response.status}`
          : shape ? 'EMPTY_RESULTS' : 'INVALID_SCHEMA';
  }
  return { ...response, ok, error, sources, response_shape: shape || null };
}

function textStats(sources, key) {
  const lengths = sources.map(item => item[key].length);
  return {
    nonempty: lengths.filter(n => n > 0).length,
    total_chars: lengths.reduce((sum, n) => sum + n, 0),
    median_chars: percentile(lengths, 0.5),
  };
}

export function project(row) {
  return {
    provider: row.provider, case_id: row.task.id, domain: row.task.domain,
    language: row.task.language, query_variant: row.task.query_variant,
    query_chars: [...row.task.query].length, phase: row.phase, round: row.round,
    utc: row.utc, duration_ms: row.ms, http_status: row.status,
    success: row.ok, error_code: row.error, wire_attempted: row.wireAttempted !== false,
    response_shape: row.response_shape,
    source_count: row.sources.length, within_20s: row.ok && row.ms <= 20000,
    summary: textStats(row.sources, 'summary'), snippet: textStats(row.sources, 'snippet'),
    content: textStats(row.sources, 'content'),
    references: referenceMetrics(row),
  };
}

export class DeepSeekComparison {
  constructor({ deepseekKey, anysearchKey, concurrency = 4, timeout = 120000 }) {
    this.keys = { deepseek: deepseekKey, anysearch: anysearchKey };
    this.concurrency = concurrency;
    this.timeout = timeout;
    this.blocked = new Set();
    this.rows = [];
    this.done = 0;
    this.total = 0;
    this.active = 0;
    this.pacer = Promise.resolve();
    this.nextDeepSeekStart = 0;
  }

  async paceDeepSeek() {
    const slot = this.pacer.then(async () => {
      const delay = Math.max(0, this.nextDeepSeekStart - Date.now());
      if (delay) await new Promise(resolve => setTimeout(resolve, delay));
      this.nextDeepSeekStart = Date.now() + 220;
    });
    this.pacer = slot;
    await slot;
  }

  async one(task, provider, { needContent = false } = {}) {
    const skipped = () => parseSearch(provider, {
      status: 0, ms: 0, error: 'ACCOUNT_BLOCKED_NO_REQUEST', wireAttempted: false,
    });
    if (this.blocked.has(provider)) return skipped();
    if (provider === 'deepseek') await this.paceDeepSeek();
    if (this.blocked.has(provider)) return skipped();
    const payload = provider === 'deepseek'
      ? { query: task.query, count: 10, need_content: needContent }
      : { query: task.query, max_results: 10, format: 'json', language: task.language === 'zh' ? 'zh-CN' : 'en' };
    const response = await requestJSON(
      provider === 'deepseek' ? DEEPSEEK_URL : ANYSEARCH_URL, this.keys[provider], payload, this.timeout,
    );
    if ([401, 402, 403].includes(response.status)) this.blocked.add(provider);
    return parseSearch(provider, response);
  }

  async run(tasks, rounds = 3, phase = 'formal') {
    const queue = [];
    for (let round = 1; round <= rounds; round++) {
      for (const task of seededShuffle(tasks, 20261017 + round)) queue.push({ task, round });
    }
    this.total = queue.length * 2;
    this.done = 0;
    let index = 0;
    await Promise.all(Array.from({ length: this.concurrency }, async () => {
      while (index < queue.length) {
        const { task, round } = queue[index++];
        const providers = seededShuffle(['deepseek', 'anysearch'], 20261017 + index);
        await Promise.all(providers.map(async provider => {
          this.active++;
          const utc = new Date().toISOString();
          const response = await this.one(task, provider);
          this.rows.push({ ...response, task, provider, phase, round, utc });
          this.done++;
          this.active--;
        }));
      }
    }));
  }

  async contentCheck(tasks) {
    const selected = tasks.filter(task => /01$/.test(task.semantic));
    const rows = [];
    for (const task of selected) {
      if (this.blocked.has('deepseek')) break;
      const utc = new Date().toISOString();
      const response = await this.one(task, 'deepseek', { needContent: true });
      rows.push({ ...response, task, provider: 'deepseek', phase: 'content_true', round: 1, utc });
    }
    return rows;
  }

  progress() {
    return {
      done: this.done, planned: this.total, active: this.active,
      blocked: [...this.blocked],
      groups: ['deepseek', 'anysearch'].map(provider => {
        const rows = this.rows.filter(row => row.provider === provider);
        return { provider, n: rows.length, success: rows.filter(row => row.ok).length };
      }),
    };
  }
}

export function aggregate(rows) {
  return {
    n: rows.length, success: rows.filter(row => row.ok).length,
    within_20s: rows.filter(row => row.ok && row.ms <= 20000).length,
    p50_ms: percentile(rows.map(row => row.ms), 0.5),
    p95_ms: percentile(rows.map(row => row.ms), 0.95),
    references: summarizeReferences(rows),
    errors: Object.fromEntries([...new Set(rows.filter(row => !row.ok).map(row => row.error))]
      .map(error => [error, rows.filter(row => row.error === error).length])),
    total_sources: rows.reduce((n, row) => n + row.sources.length, 0),
    summaries_available: rows.reduce((n, row) => n + row.sources.filter(source => source.summary.length > 0).length, 0),
    snippets_available: rows.reduce((n, row) => n + row.sources.filter(source => source.snippet.length > 0).length, 0),
    contents_available: rows.reduce((n, row) => n + row.sources.filter(source => source.content.length > 0).length, 0),
  };
}

export function summarize(run, contentRows) {
  const formal = run.rows.filter(row => row.phase === 'formal');
  const groups = selectors => ['deepseek', 'anysearch'].map(provider => ({
    provider, ...selectors,
    ...aggregate(formal.filter(row => row.provider === provider
      && Object.entries(selectors).every(([key, value]) => row.task[key] === value))),
  }));
  return {
    status: run.blocked.size ? 'blocked_partial' : 'completed_search_benchmark',
    utc: new Date().toISOString(), planned_formal_requests: 384, seed: 20261017,
    concurrency_pairs: run.concurrency, deepseek_min_start_interval_ms: 220,
    shortened_cases: Object.keys(SHORT_QUERIES),
    main: groups({}),
    rounds: [1, 2, 3].map(round => ({
      round, groups: ['deepseek', 'anysearch'].map(provider => ({
        provider, ...aggregate(formal.filter(row => row.round === round && row.provider === provider)),
      })),
    })),
    strata: ['paper', 'development', 'agent', 'learning']
      .flatMap(domain => ['zh', 'en'].flatMap(language => groups({ domain, language }))),
    content_check: aggregate(contentRows),
    content_check_rows: contentRows.map(project),
    locality_rows: run.rows.filter(row => row.phase === 'locality').map(localityMetric),
    reference_hit_difference_ci: sourceDifferenceCI(formal),
    stability: ['deepseek', 'anysearch'].map(provider => {
      const tasks = new Map();
      for (const row of formal.filter(item => item.provider === provider && item.task.gold.urls.length)) {
        if (!tasks.has(row.task.id)) tasks.set(row.task.id, []);
        tasks.get(row.task.id).push(referenceMetrics(row).matched_references > 0);
      }
      return {
        provider, tasks: tasks.size,
        hit_in_any_round: [...tasks.values()].filter(values => values.some(Boolean)).length,
        hit_in_every_round: [...tasks.values()].filter(values => values.length === 3 && values.every(Boolean)).length,
      };
    }),
  };
}

export function sourceDifferenceCI(rows, draws = 4000) {
  if (rows.some(row => ['ACCOUNT_BLOCKED', 'ACCOUNT_BLOCKED_NO_REQUEST'].includes(row.error))) return null;
  const clusters = new Map();
  for (const row of rows.filter(item => item.task.gold.urls.length)) {
    if (!clusters.has(row.task.semantic)) clusters.set(row.task.semantic, { deepseek: [], anysearch: [] });
    clusters.get(row.task.semantic)[row.provider].push(Number(referenceMetrics(row).matched_references > 0));
  }
  const differences = [...clusters.values()].filter(item => item.deepseek.length === 6 && item.anysearch.length === 6)
    .map(item => (item.deepseek.reduce((a, b) => a + b, 0) - item.anysearch.reduce((a, b) => a + b, 0)) / 6);
  if (!differences.length) return null;
  let seed = 20261017;
  const bootstrap = [];
  for (let draw = 0; draw < draws; draw++) {
    let sum = 0;
    for (let index = 0; index < differences.length; index++) {
      seed = (1664525 * seed + 1013904223) >>> 0;
      sum += differences[Math.floor(seed / 4294967296 * differences.length)];
    }
    bootstrap.push(sum / differences.length);
  }
  return {
    metric: 'deepseek_minus_anysearch_preset_source_hit',
    semantic_clusters: differences.length, draws,
    difference: differences.reduce((a, b) => a + b, 0) / differences.length,
    low: Math.abs(percentile(bootstrap, 0.025)) < 1e-12 ? 0 : percentile(bootstrap, 0.025),
    high: Math.abs(percentile(bootstrap, 0.975)) < 1e-12 ? 0 : percentile(bootstrap, 0.975),
  };
}

async function selfTest() {
  const tasks = await casesForDeepSeek();
  assert.equal(tasks.length, 64);
  assert.equal(tasks.filter(task => task.query_variant === 'shortened_v1').length, 5);
  assert.ok(tasks.every(task => [...task.query].length <= 128));
  assert.ok(localityCases().every(task => [...task.query].length <= 128));
  const fake = { title: 'Synthetic source', url: 'https://example.org/source', summary: 'synthetic', snippet: '', content: '' };
  const flat = parseSearch('deepseek', { status: 200, ms: 1, json: { results: [fake] } });
  const nested = parseSearch('deepseek', { status: 200, ms: 1, json: { code: 0, data: { biz_data: { results: [fake] } } } });
  assert.ok(flat.ok);
  assert.equal(nested.ok, false);
  assert.ok(parseSearch('anysearch', { status: 200, json: { code: 0, data: { results: [fake] } } }).ok);
  assert.equal(parseSearch('deepseek', { status: 200, json: { message: 'success' } }).ok, false);
  const run = new DeepSeekComparison({ deepseekKey: 'synthetic', anysearchKey: 'synthetic' });
  run.blocked.add('deepseek');
  assert.equal((await run.one(tasks[0], 'deepseek')).wireAttempted, false);
  const projected = project({ ...flat, task: tasks[0], provider: 'deepseek', phase: 'test', round: 1 });
  assert.ok(!JSON.stringify(projected).includes('example.org'));
  assert.ok(!JSON.stringify(projected).includes('Synthetic source'));
  assert.equal(aggregate([{ ...flat, task: tasks[0] }, { ...flat, task: tasks[0], ok: false, ms: 120000, error: 'DEADLINE' }]).n, 2);
  assert.equal(sourceDifferenceCI([]), null);
  console.log('PASS: 64 bilingual cases, 128-char limit, provider schemas, circuit breaker, body-free projection, failure denominator');
}

async function liveCLI() {
  if (!process.env.DEEPSEEK_API_KEY || !process.env.ANYSEARCH_API_KEY) {
    throw new Error('Set DEEPSEEK_API_KEY and ANYSEARCH_API_KEY in the process environment.');
  }
  const run = new DeepSeekComparison({
    deepseekKey: process.env.DEEPSEEK_API_KEY, anysearchKey: process.env.ANYSEARCH_API_KEY,
  });
  const tasks = await casesForDeepSeek();
  const interval = setInterval(() => console.log(JSON.stringify(run.progress())), 30000);
  let content;
  try {
    await run.run(tasks);
    content = await run.contentCheck(tasks);
    await run.run(localityCases(), 1, 'locality');
  } finally { clearInterval(interval); }
  const directory = path.join(ROOT, '文档/测试归档/search-comparison/deepseek/runs', new Date().toISOString().replace(/[:.]/g, '-'));
  await fs.mkdir(directory, { recursive: true });
  await fs.writeFile(path.join(directory, 'search-metrics.json'), JSON.stringify(run.rows.map(project), null, 2) + '\n');
  await fs.writeFile(path.join(directory, 'summary.json'), JSON.stringify(summarize(run, content), null, 2) + '\n');
  console.log(run.blocked.size ? 'BLOCKED: partial metadata saved.' : 'COMPLETED: metadata and reference-hit statistics saved.');
  if (run.blocked.size) process.exitCode = 2;
}

const IS_ENTRY = typeof process !== 'undefined' && process.argv[1]
  && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (IS_ENTRY && process.argv.includes('--self-test')) await selfTest();
if (IS_ENTRY && process.argv.includes('--live')) await liveCLI();
