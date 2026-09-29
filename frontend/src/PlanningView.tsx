import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import ReadonlyCanvas from "./excalidraw/ReadonlyCanvas";

/**
 * 课程规划工作台（v2 三态：访谈 → 生成 → 草稿）：
 * - intake：生成前访谈（选项卡问答，范围并入第一轮，上限 4 轮），画布此阶段不渲染；
 * - generating：任务运行横幅（长任务，请勿刷新）；内部 ID（task_id/draft_id）不外显；
 * - draft：左栏提问与反馈 + 只读画布（草稿生成后直接预览导图，不必先确认）；
 * - 等待态（范围澄清[跳过范围题时的回落] / 搜索决策）以 note 呈现。
 */

type Draft = {
  draft_id: string;
  draft_kind: string;
  target_id: string;
  revision: number;
  content: any;
  saved_state: string;
};

/** 访谈问答记录条目（导图步骤左栏只读回看） */
type QaEntry = { q: string; labels: string[]; free?: string; extra?: string };

/** 清洗记录条目（localStorage/服务端数据可能缺字段，缺 labels 会让渲染崩溃） */
function normalizeQaEntry(e: any): QaEntry | null {
  if (!e || typeof e !== "object") return null;
  const labels = Array.isArray(e.labels) ? e.labels.filter((x: any) => typeof x === "string" && x) : [];
  const free = typeof e.free === "string" && e.free ? e.free : undefined;
  const extra = typeof e.extra === "string" && e.extra ? e.extra : undefined;
  const q = typeof e.q === "string" && e.q ? e.q : "";
  if (!q && !labels.length && !free && !extra) return null;
  return { q: q || "访谈反馈", labels, free, extra };
}

/** 访谈问卷：选项卡网格（单选/多选）+ 跳过 + 自由补充 */
function IntakeForm({ intake, onDecide, busy }: {
  intake: any; onDecide: (answers: any[], log: QaEntry[]) => void; busy: boolean;
}) {
  const [sel, setSel] = useState<Record<string, string[]>>({});
  const [freetext, setFreetext] = useState<Record<string, string>>({});
  const questions: any[] = intake.questions || [];

  const toggle = (qid: string, key: string, kind: string) => {
    setSel((prev) => {
      const cur = prev[qid] || [];
      if (kind === "multi") {
        return { ...prev, [qid]: cur.includes(key) ? cur.filter((x) => x !== key) : [...cur, key] };
      }
      return { ...prev, [qid]: [key] };
    });
  };

  const submit = () => {
    const answers: any[] = [];
    const log: QaEntry[] = [];
    questions.forEach((q) => {
      const selections = sel[q.question_id] || [];
      const free = (freetext[q.question_id] || "").trim() || undefined;
      if (selections.length === 0 && !free) return;
      answers.push({ question_id: q.question_id, selections, free_text: free });
      log.push({
        q: q.question,
        labels: selections.map((k: string) => (q.options.find((o: any) => o.key === k)?.label) || k),
        free,
      });
    });
    onDecide(answers, log);
  };

  const answered = questions.filter((q) => (sel[q.question_id] || []).length > 0 || freetext[q.question_id]).length;

  return (
    <div className="intake-wrap">
      <div className="intake-head">
        <span className="kicker">开始前，先了解你</span>
        <span className="help">第 {intake.round} 轮 · 至多 {intake.max_rounds} 轮 · 可跳过不确定的问题</span>
      </div>
      {questions.map((q) => (
        <fieldset className="q-card" key={q.question_id}>
          <legend>
            {q.question}
            {q.kind === "multi" && <span className="badge queued" style={{ marginLeft: 8 }}>多选</span>}
          </legend>
          <div className="opt-grid">
            {q.options.map((o: any) => {
              const on = (sel[q.question_id] || []).includes(o.key);
              return (
                <button
                  key={o.key}
                  type="button"
                  className={`opt-card${on ? " on" : ""}`}
                  aria-pressed={on}
                  onClick={() => toggle(q.question_id, o.key, q.kind)}
                >
                  <b>{o.label}</b>
                  {o.detail && <span>{o.detail}</span>}
                </button>
              );
            })}
            <button
              type="button"
              className={`opt-card other${freetext[q.question_id] ? " on" : ""}`}
              aria-pressed={!!freetext[q.question_id]}
              onClick={() => setFreetext((p) => ({ ...p, [q.question_id]: p[q.question_id] ?? "" }))}
            >
              都不太对？写个自己的
            </button>
          </div>
          {freetext[q.question_id] !== undefined && (
            <input
              className="inp"
              style={{ marginTop: 10, maxWidth: 520 }}
              placeholder="用自己的话补充…"
              value={freetext[q.question_id]}
              aria-label={`补充 ${q.question}`}
              onChange={(e) => setFreetext((p) => ({ ...p, [q.question_id]: e.target.value }))}
            />
          )}
          {q.skippable && (
            <button
              type="button"
              className="linkbtn"
              style={{ marginTop: 8 }}
              onClick={() => setSel((p) => ({ ...p, [q.question_id]: [] }))}
            >
              跳过这题
            </button>
          )}
        </fieldset>
      ))}
      <div className="row" style={{ marginTop: 18, gap: 12 }}>
        <button className="btn btn-pri" onClick={submit} disabled={busy || answered === 0}>
          {busy ? <><span className="spin" />提交中…</> : "提交并生成"}
        </button>
        <span className="help">已答 {answered}/{questions.length} 题；跳过的题按默认方式处理。</span>
      </div>
    </div>
  );
}

export default function PlanningView({ onErr, onOk, needLogin, go, onMapMode }: {
  onErr: (s: string) => void; onOk: (s: string) => void; needLogin: boolean; go: (p: string) => void;
  /** 进入导图步骤时通知外壳：自动隐藏侧栏 + 顶栏转极简（离开该步骤自动恢复） */
  onMapMode?: (on: boolean) => void;
}) {
  const [title, setTitle] = useState("");
  const [routeId, setRouteId] = useState("");
  const [taskId, setTaskId] = useState("");
  const [task, setTask] = useState<any>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [scene, setScene] = useState<any>(null);
  const [draftScene, setDraftScene] = useState<any>(null);
  const [runError, setRunError] = useState("");
  const [running, setRunning] = useState(false);
  const [qaLog, setQaLog] = useState<QaEntry[]>([]);
  const bootRef = useRef(false);

  // 访谈问答记录：按任务持久化（刷新后导图步骤左栏仍可回看）；读取时清洗，坏数据不再导致白屏
  useEffect(() => {
    if (!taskId) { setQaLog([]); return; }
    try {
      const raw = localStorage.getItem(`slice3.intakeLog.${taskId}`);
      const parsed: any[] = raw ? JSON.parse(raw) : [];
      setQaLog((Array.isArray(parsed) ? parsed : []).map(normalizeQaEntry).filter(Boolean) as QaEntry[]);
    } catch { setQaLog([]); }
  }, [taskId]);

  useEffect(() => {
    if (!taskId || qaLog.length === 0) return;
    try { localStorage.setItem(`slice3.intakeLog.${taskId}`, JSON.stringify(qaLog)); } catch { /* 隐私模式忽略 */ }
  }, [qaLog, taskId]);

  const refreshTask = useCallback(async (id: string) => {
    try { setTask(await api.getPlanningTask(id)); } catch { /* 保持当前状态 */ }
  }, []);

  const loadScene = useCallback(async (rid: string, kind?: string) => {
    if (!rid) return;
    try {
      setScene(kind === "course" ? await api.courseScene(rid) : await api.routeScene(rid));
    } catch { setScene(null); }
  }, []);

  const runTask = useCallback(async (id: string) => {
    localStorage.setItem("slice3.lastPlanningTask", id);
    setRunning(true);
    try {
      const body = await api.runPlanningTask(id);
      if (body.draft?.draft_id) {
        const d = await api.getDraft(body.draft.draft_id);
        setDraft(d);
      }
      await refreshTask(id);
      if (body.wait_state === "awaiting_intake") onOk("先回答几个问题，帮我们把规划调得更准。");
      else if (body.wait_state === "awaiting_scope_clarification") onOk("规划范围存在歧义，请回答一个问题。");
      else if (body.wait_state === "awaiting_search_decision") onOk("联网搜索失败，请选择重试或离线继续。");
      else if (body.task_status === "succeeded") onOk("候选已生成，请在草稿中查看并确认。");
      else if (body.task_status === "failed") setRunError(body.error_code || "生成失败");
    } catch (e: any) {
      setRunError(e?.code || "任务执行失败");
    } finally {
      setRunning(false);
    }
  }, [onOk, refreshTask]);

  // 从首页跳转而来：自动执行待处理任务；另恢复最近一次规划任务（刷新后仍可见访谈/草稿状态）
  useEffect(() => {
    if (bootRef.current) return;
    bootRef.current = true;
    const pending = sessionStorage.getItem("slice3.pendingTask");
    const pendingRoute = sessionStorage.getItem("slice3.pendingRoute");
    if (pendingRoute) setRouteId(pendingRoute);
    const last = localStorage.getItem("slice3.lastPlanningTask");
    if (last) {
      setTaskId(last);
      (async () => {
        try {
          const t = await api.getPlanningTask(last);
          setTask(t);
          if (t.target_id) {
            setRouteId(t.target_id);
            // 确认过的草稿不再当作“编辑中”复活；已确认版本的导图始终加载
            const confirmed = t.draft_id && localStorage.getItem(`slice3.confirmedDraft.${t.draft_id}`) === "1";
            if (t.draft_id && !confirmed) {
              try {
                const d = await api.getDraft(t.draft_id);
                setDraft(d);
              } catch { /* 草稿不可用则只展示已确认版本 */ }
            }
            try {
              setScene(t.target_kind === "course" ? await api.courseScene(t.target_id) : await api.routeScene(t.target_id));
            } catch { setScene(null); }
          }
        } catch { /* 最近任务不可用则忽略 */ }
      })();
    }
    if (pending) {
      sessionStorage.removeItem("slice3.pendingTask");
      setTaskId(pending);
      runTask(pending);
    }
  }, [runTask, refreshTask]);

  // 草稿预览：无已确认版本时直接用草稿内容渲染导图（生成即看图，不必先确认）
  useEffect(() => {
    if (!draft || scene) { setDraftScene(null); return; }
    let cancelled = false;
    api.draftScene(draft.draft_id).then(
      (s: any) => { if (!cancelled) setDraftScene(s); },
      () => { if (!cancelled) setDraftScene(null); },
    );
    return () => { cancelled = true; };
  }, [draft, scene]);

  const createAndRun = useCallback(async () => {
    if (needLogin) return onErr("请先登录。");
    try {
      let rid = routeId;
      if (!rid) {
        const r = await api.createRoute(title || "未命名路线", "self_study");
        rid = r.route_id;
        setRouteId(rid);
      }
      const body = await api.createPlanningTask({
        idempotency_key: `web-${Date.now()}`,
        task_kind: "route_generate",
        planning_scope: "auto", // 范围由访谈第一轮决定（用户作答落 resolved_scope）
        target_route_id: rid,
      });
      setTaskId(body.task_id);
      await runTask(body.task_id);
    } catch (e: any) {
      onErr(e?.message || "创建任务失败");
    }
  }, [needLogin, onErr, routeId, runTask, title]);

  const confirmDraft = useCallback(async () => {
    if (!draft) return;
    try {
      await api.confirmDraft(draft.draft_id, {
        revision: draft.revision, idempotency_key: `web-confirm-${Date.now()}`,
      });
      // 确认成功不弹横幅（导图切到已确认态即反馈）；标记该草稿已确认，重进页面不复活编辑态
      try { localStorage.setItem(`slice3.confirmedDraft.${draft.draft_id}`, "1"); } catch { /* 隐私模式忽略 */ }
      onOk("");
      setDraft(null);
      setScene(null);
      setDraftScene(null);
      await loadScene(draft.target_id, draft.draft_kind);
    } catch (e: any) {
      onErr(`确认失败：${e?.code || e?.message || "错误"}`);
    }
  }, [draft, loadScene, onErr, onOk]);

  const decision = useCallback(async (payload: Record<string, unknown>) => {
    if (!taskId) return;
    setRunning(true);
    try {
      const body = await api.decidePlanningTask(taskId, payload);
      if (body.draft?.draft_id) {
        const d = await api.getDraft(body.draft.draft_id);
        setDraft(d);
      }
      await refreshTask(taskId);
      if (body.task_status === "failed") {
        // 生成失败要可见可重试，而不是无声地停在当前页面
        setRunError(body.error_code || "生成失败");
        onOk("");
        return;
      }
      onOk("已按你的选择继续。");
    } catch (e: any) {
      setRunError(e?.code || "决策失败");
      onErr(`决策失败：${e?.code || "错误"}`);
    } finally {
      setRunning(false);
    }
  }, [onErr, onOk, refreshTask, taskId]);

  const submitIntake = useCallback((answers: any[], log: QaEntry[]) => {
    onOk("");
    setQaLog((prev) => [...prev, ...log].slice(-40));
    decision({ kind: "intake", answers });
  }, [decision, onOk]);

  const setScopeAndNewDraft = useCallback(async (next: string) => {
    if (!draft) return;
    try {
      const r = await api.switchDraftScope(draft.draft_id, next);
      setDraft(r.draft);
      onOk("已切换范围，输入已保留到新草稿。");
    } catch (e: any) {
      onErr(`范围切换失败：${e?.code || "错误"}`);
    }
  }, [draft, onErr, onOk]);

  const waitState = task?.wait_state;
  const intake = task?.intake;
  const showIntake = waitState === "awaiting_intake" && intake?.questions?.length > 0;
  const hasBoard = Boolean(draft || scene);
  const activeScene = scene || draftScene;
  // 导图步骤 = 蓝图/草稿已就绪且不在访谈中
  const mapStep = Boolean(routeId && hasBoard && !showIntake);

  // 导图步骤请求极简外壳（隐藏侧栏、顶栏只留菜单按钮）；离开该步骤（含路由离开）自动恢复
  useEffect(() => {
    if (!mapStep || !onMapMode) return;
    onMapMode(true);
    return () => onMapMode(false);
  }, [mapStep, onMapMode]);

  // 提问与反馈（只读）：优先用本地问答记录；跨浏览器恢复时按服务端 intake 尽力回显；统一清洗防渲染崩溃
  const rawTranscript: any[] = qaLog.length > 0 ? qaLog : (intake?.answers || []).map((a: any) => {
    const q = (intake?.questions || []).find((x: any) => x.question_id === a.question_id);
    const labels = (Array.isArray(a.selections) ? a.selections : [])
      .map((k: string) => (q?.options || []).find((o: any) => o.key === k)?.label)
      .filter(Boolean);
    return {
      q: q?.question || "访谈反馈",
      labels,
      extra: !q && Array.isArray(a.selections) && a.selections.length ? `已选择 ${a.selections.length} 项` : undefined,
      free: a.free_text || undefined,
    };
  });
  const transcript: QaEntry[] = rawTranscript.map(normalizeQaEntry).filter(Boolean) as QaEntry[];

  return (
    <div className="mm-clean">
      {!mapStep && (
        <div className="mm-clean-head">
          <span className="kicker">课程规划 · {routeId ? "路线" : "新任务"}</span>
          <h1>{title || "课程规划"}</h1>
          <div className="row" style={{ flexWrap: "wrap", gap: "var(--s3)", alignItems: "center" }}>
            <input
              className="inp"
              style={{ maxWidth: 320 }}
              placeholder="路线标题（可留空自动创建）"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              aria-label="路线标题"
            />
            <button className="btn btn-pri" onClick={createAndRun} disabled={needLogin || running}>生成路线候选</button>
            {routeId && <button className="btn btn-sm" onClick={() => go(`/route/${routeId}`)}>查看路线总览 →</button>}
          </div>
        </div>
      )}

      {runError && (
        <div className="note note-err" role="alert" style={{ margin: "12px 32px" }}>
          <span className="tag">失败</span>
          <div className="tt">生成失败 · {runError}</div>
          <div>不无限重试；访谈回答已保留，可直接重试生成（大 JSON 生成通常需要 1–2 分钟）。</div>
          <div className="acts">
            <button
              className="btn btn-pri btn-sm"
              disabled={running || !taskId}
              onClick={() => { setRunError(""); runTask(taskId); }}
            >
              重试生成
            </button>
            <button className="btn btn-sm" onClick={() => go("/home")}>调整目标</button>
          </div>
        </div>
      )}

      {running && (
        <div className="note note-accent" role="status" style={{ margin: "0 32px 12px" }}>
          <span className="tag"><span className="spin" style={{ marginRight: 6 }} />生成中</span>
          <div className="tt">正在按你的回答生成规划…</div>
          <div>长任务，请勿刷新或离开页面（刷新或离开将停止生成）。</div>
        </div>
      )}

      {showIntake && (
        <div style={{ padding: "4px 32px 12px" }}>
          <IntakeForm intake={intake} onDecide={submitIntake} busy={running} />
        </div>
      )}

      {waitState === "awaiting_scope_clarification" && (
        <div style={{ padding: "12px 32px" }}>
          <div className="clarify" role="group" aria-label="范围澄清">
            <span className="kicker" style={{ color: "var(--accent)" }}>只追问一句</span>
            <p className="q">{task?.clarification_question || "这个规划是围绕一门课程展开，还是一条跨阶段的学习路线？"}</p>
            <div className="radio-list">
              <label className="radio-row">
                <input type="radio" name="scope-clar" onChange={() => decision({ resolved_scope: "course" })} />
                <span><b>围绕一门课程</b><span className="help" style={{ display: "block" }}>→ 单门课程：一份大纲、若干讲次</span></span>
              </label>
              <label className="radio-row">
                <input type="radio" name="scope-clar" onChange={() => decision({ resolved_scope: "route" })} />
                <span><b>一条完整学习路线</b><span className="help" style={{ display: "block" }}>→ 多课程路线：阶段与先修关系</span></span>
              </label>
            </div>
          </div>
        </div>
      )}
      {waitState === "awaiting_search_decision" && (
        <div style={{ padding: "12px 32px" }}>
          <div className="note note-warn" role="alert">
            <span className="tag">等待</span>
            <div className="tt">联网搜索失败</div>
            <div>可以重试搜索，或明确选择离线继续（不会伪造联网依据）。</div>
            <div className="acts">
              <button className="btn btn-sm" onClick={() => decision({ decision: "retry" })}>重试搜索</button>
              <button className="btn btn-sm btn-ink" onClick={() => decision({ decision: "offline_continue" })}>离线继续</button>
            </div>
          </div>
        </div>
      )}

      {routeId && hasBoard && !showIntake && (
        <div className="plan-body">
          <aside className="plan-qa" aria-label="提问与反馈">
            <div className="plan-qa-log">
              <span className="kicker">提问与反馈 · 只读</span>
              {transcript.length > 0 ? transcript.map((e, i) => (
                <div className="qa-item" key={i}>
                  <p className="qa-q">{e.q}</p>
                  {e.labels.length > 0 && <p className="qa-a">{e.labels.join(" · ")}</p>}
                  {e.extra && <p className="qa-a">{e.extra}</p>}
                  {e.free && <p className="qa-free">{e.free}</p>}
                </div>
              )) : (
                <p className="help">生成前访谈的提问与回答会记录在这里，仅供回看。</p>
              )}
            </div>
            <div className="plan-qa-foot">
              <span className="kicker">反馈</span>
              {draft ? (
                <>
                  <button className="btn btn-pri" onClick={confirmDraft} disabled={running}>没问题，确认草稿</button>
                  <button
                    className="btn btn-draft"
                    onClick={() => setScopeAndNewDraft(draft.draft_kind === "route" ? "course" : "route")}
                    disabled={running}
                  >
                    我想修改讲解结构
                  </button>
                </>
              ) : (
                <>
                  <p className="help">已确认为不可变版本，导图与总览同版。</p>
                  <button
                    className="btn btn-pri"
                    onClick={() => go(task?.target_kind === "course" ? `/course/${routeId}` : `/route/${routeId}`)}
                  >
                    {task?.target_kind === "course" ? "查看课程总览 →" : "查看路线总览 →"}
                  </button>
                  <p className="help">如需调整，请回到「新的学习任务」生成新草稿。</p>
                </>
              )}
            </div>
          </aside>

          <div className="mm-main">
            <div className="stage-wrap">
              {activeScene ? (
                <div className="canvas-readonly-wrap">
                  <ReadonlyCanvas elements={activeScene.elements} height="full" />
                </div>
              ) : (
                <div className="canvas-readonly-wrap" style={{ display: "grid", placeItems: "center", color: "var(--ink3)" }}>
                  确认草稿后，这里展示与总览同版的路线导图。
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {routeId && !hasBoard && !running && !showIntake && waitState !== "awaiting_scope_clarification" && waitState !== "awaiting_search_decision" && !runError && (
        <div className="note note-info" style={{ margin: "8px 32px" }} role="note">
          <span className="tag">空的</span>
          <div className="tt">还没有规划内容</div>
          <div>回到「新的学习任务」写下目标并回答几个小问题，学伴会先访谈、再生成，画布只在草稿生成后出现。</div>
          <div className="acts"><button className="btn btn-pri btn-sm" onClick={() => go("/home")}>去创建</button></div>
        </div>
      )}
    </div>
  );
}
