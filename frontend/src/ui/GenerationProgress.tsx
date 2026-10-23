import { useEffect, useId, useRef, useState } from "react";
import { api } from "../api";
import "./generationProgress.css";

export type GenerationProcess = {
  task_id: string;
  generation: number;
  task_status: string;
  started_at?: string | null;
  updated_at?: string | null;
  stage?: string;
  elapsed_ms?: number;
  sequence: number;
  steps: { key: string; label: string; status: string; started_at?: string | null; finished_at?: string | null }[];
  search?: { status: string; reason?: string; result_count?: number; sources?: { title: string; url: string; domain: string; used?: boolean }[]; query?: string | null } | null;
  thinking?: { text?: string; available?: boolean; show_summary?: boolean; truncated?: boolean } | null;
};

const ACTIVE = new Set(["queued", "leased", "running"]);
const COMPLETED = new Set(["succeeded", "completed", "ready"]);
const FAILED = new Set(["failed", "expired", "error"]);
export function safeSourceUrl(value: string): string | null {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}
function duration(start?: string | null, finish?: string | null) {
  const began = Date.parse(start || "");
  const ended = Date.parse(finish || "");
  if (!Number.isFinite(began) || !Number.isFinite(ended)) return "";
  const seconds = Math.max(0, Math.floor((ended - began) / 1000));
  return `${Math.floor(seconds / 60).toString().padStart(2, "0")}:${(seconds % 60).toString().padStart(2, "0")}`;
}
function headline(process: GenerationProcess | null, active: boolean) {
  if (!process) return active ? "正在生成规划" : "生成过程";
  if (COMPLETED.has(process.task_status)) return "规划生成完成";
  if (FAILED.has(process.task_status)) return "生成暂未完成";
  if (process.task_status === "cancelled") return "生成已停止";
  const current = process.steps.find(step => step.status === "running" || step.status === "started");
  return current?.label || (process.task_status === "queued" ? "等待开始生成" : active ? "正在处理规划" : "生成过程已保留");
}

/** Authorized business-content snapshot, never a model invocation or a diagnostics log. */
export default function GenerationProgress({ taskId, ownerId, generation = 0, active, recovery }: {
  taskId: string; ownerId: string; generation?: number; active: boolean;
  recovery?: { count: number; limit: number; pending: boolean; exhausted: boolean };
}) {
  const [process, setProcess] = useState<GenerationProcess | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [allSources, setAllSources] = useState(false);
  const [failure, setFailure] = useState("");
  const [retry, setRetry] = useState(0);
  const [now, setNow] = useState(() => new Date().toISOString());
  const identity = `${ownerId}:${taskId}:${generation}`;
  const loadedIdentity = useRef("");
  const scope = useRef(identity);
  scope.current = identity;
  const contentId = useId();

  const cursor = useRef({ identity: "", generation: 0, sequence: -1 });
  useEffect(() => { setProcess(null); setFailure(""); setExpanded(false); setAllSources(false); }, [identity]);
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let request: AbortController | undefined;
    let latestGeneration = cursor.current.identity === identity ? cursor.current.generation : generation;
    let latestSequence = cursor.current.identity === identity ? cursor.current.sequence : -1;
    async function load() {
      request = new AbortController();
      let continuePolling = active;
      try {
        const response = await api.planningProcess(taskId, request.signal);
        const next = response.process as GenerationProcess | null;
        if (stopped || scope.current !== identity) return;
        if (!next) { setProcess(null); setFailure(""); return; }
        if (next.task_id !== taskId) return;
        if (!Number.isFinite(next.generation) || !Number.isFinite(next.sequence) || !Array.isArray(next.steps)) throw new Error("invalid_process");
        if (next.generation < latestGeneration || (next.generation === latestGeneration && next.sequence < latestSequence)) return;
        latestGeneration = next.generation; latestSequence = next.sequence;
        loadedIdentity.current = identity;
        cursor.current = { identity, generation: next.generation, sequence: next.sequence };
        setProcess(next); setFailure("");
        continuePolling = active && ACTIVE.has(next.task_status);
      } catch (error: unknown) {
        if (stopped || request.signal.aborted || scope.current !== identity) return;
        const status = (error as { status?: number }).status;
        if (status === 401 || status === 403 || status === 404) {
          setProcess(null); continuePolling = false;
          setFailure(status === 401 ? "会话已失效，请刷新页面后恢复进度。" : "当前无法访问这次规划的生成过程。");
        } else setFailure("进度连接暂时中断，后台任务状态待确认。重连只获取进度，不会重复生成。");
      } finally {
        if (!stopped && scope.current === identity && continuePolling) timer = setTimeout(load, 2000);
      }
    }
    void load();
    return () => { stopped = true; request?.abort(); if (timer) clearTimeout(timer); };
  }, [identity, taskId, generation, active, retry]);

  useEffect(() => {
    if (!active || (process && !ACTIVE.has(process.task_status))) return;
    const timer = setInterval(() => setNow(new Date().toISOString()), 1000);
    return () => clearInterval(timer);
  }, [active, process?.task_status]);

  const visible = loadedIdentity.current === identity ? process : null;
  const running = active && (!visible || ACTIVE.has(visible.task_status));
  const elapsed = duration(visible?.started_at, running ? now : visible?.updated_at);
  const sources = (visible?.search?.sources || []).map(source => ({ ...source, safeUrl: safeSourceUrl(source.url) }));
  const shownSources = allSources ? sources : sources.slice(0, 2);

  return <section className="generation-progress" aria-label="规划生成过程">
    <div className="generation-progress-head">
      <span className={`generation-progress-marker${running ? " is-running" : ""}`} aria-hidden="true">{running ? "" : visible && COMPLETED.has(visible.task_status) ? "✓" : "○"}</span>
      <div className="generation-progress-heading"><strong>{headline(visible, active)}</strong><p>{failure ? "进度连接待恢复" : recovery?.exhausted ? "自动重试已达上限，当前草稿与回答已保留。" : recovery?.pending ? "正在恢复生成，当前草稿与回答已保留。" : visible ? running ? "生成在后台继续，可以暂时离开。" : "已保留本次生成过程。" : "正在获取任务的真实进度…"}</p></div>
      {recovery && (recovery.pending || recovery.exhausted || (active && recovery.count > 0)) && <span className="generation-retry" role="status" aria-label={`自动重试 ${recovery.count}/${recovery.limit}`}>自动重试 <b>{recovery.count}/{recovery.limit}</b></span>}
      {elapsed && <span className="generation-progress-time" aria-label={`已用时间 ${elapsed}`}>{elapsed}</span>}
      <button className="generation-progress-fold" aria-expanded={expanded} aria-controls={contentId} onClick={() => setExpanded(value => !value)}>{expanded ? "收起 ⌃" : "展开 ⌄"}</button>
    </div>
    {failure && <div className="generation-progress-error" role="status"><p>{failure}</p><button className="btn btn-sm" onClick={() => setRetry(value => value + 1)}>重新连接进度</button></div>}
    {expanded && <div id={contentId} className="generation-progress-body">
      {visible?.steps.length ? <ol className="generation-progress-steps">{visible.steps.map((step, index) => <li key={`${step.key}:${index}`} className={ACTIVE.has(step.status) || step.status === "started" ? "is-current" : FAILED.has(step.status) ? "is-failed" : ""}>
        <span className="generation-step-dot" aria-hidden="true">{COMPLETED.has(step.status) ? "✓" : FAILED.has(step.status) ? "!" : ""}</span>
        <div className="generation-step-label"><strong>{step.label}</strong><span>{duration(step.started_at, step.finished_at) || (step.status === "running" || step.status === "started" ? "进行中" : step.status === "skipped" ? "已跳过" : FAILED.has(step.status) ? "失败" : step.status === "cancelled" ? "已取消" : COMPLETED.has(step.status) ? "已完成" : "等待中")}</span></div>
      </li>)}</ol> : <p className="generation-progress-muted">{failure ? "恢复连接后显示已记录的阶段。" : "尚无已记录的阶段，等待任务更新。"}</p>}
      {visible?.search && <div className="generation-search"><h3>搜索参考资料</h3>
        {visible.search.status === "succeeded" && !sources.length && (visible.search.result_count || 0) > 0 &&
          <p>搜索已完成，找到 {visible.search.result_count} 条结果，当前没有可展示的来源列表。</p>}
        {visible.search.query && <p className="generation-search-query">{visible.search.query}</p>}
        <p className="generation-progress-muted">{sources.length ? `找到 ${visible.search.result_count ?? sources.length} 条结果，${sources.filter(source => source.used).length} 个来源已加入生成上下文` : visible.search.status === "running" || visible.search.status === "started" ? "正在搜索参考资料…" : visible.search.reason === "no_config" || visible.search.status === "no_config" ? "当前账号尚未配置搜索服务，可在设置的搜索页配置。" : visible.search.status === "skipped" ? "本次未使用联网搜索。" : visible.search.status === "pending" ? "尚未进入联网搜索阶段。" : visible.search.status === "failed" || visible.search.status === "unavailable" || visible.search.status === "timeout" ? "搜索暂不可用，将按任务策略继续；未完成联网核验。" : "本次搜索没有可展示的来源。"}</p>
        <ul className="generation-source-list">{shownSources.map((source, index) => <li key={`${source.url}:${index}`}><span className="generation-source-index">{String(index + 1).padStart(2, "0")}</span><div>{source.safeUrl ? <a href={source.safeUrl} target="_blank" rel="noopener noreferrer">{source.title || source.domain}<span aria-hidden="true"> ↗</span></a> : <span>{source.title || source.domain}</span>}<small>{source.domain}</small></div>{source.used && <span className="generation-source-used">已加入</span>}</li>)}</ul>
        {sources.length > 2 && <button className="generation-source-more" onClick={() => setAllSources(value => !value)}>{allSources ? "收起来源" : `查看另 ${sources.length - 2} 个来源`}</button>}
      </div>}
    </div>}
  </section>;
}
