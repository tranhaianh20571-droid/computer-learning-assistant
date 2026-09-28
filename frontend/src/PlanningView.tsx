import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import ReadonlyCanvas from "./excalidraw/ReadonlyCanvas";

/**
 * 课程规划工作台（v2 三态：访谈 → 生成 → 草稿）：
 * - intake：生成前访谈（选项卡问答，范围并入第一轮，上限 4 轮），画布此阶段不渲染；
 * - generating：任务运行横幅（长任务，请勿刷新）；内部 ID（task_id/draft_id）不外显；
 * - draft：版本栏 + 只读画布 + 层级列表（1 秒自动保存，服务端确认后才更新 revision）；
 * - 等待态（范围澄清[跳过范围题时的回落] / 搜索决策）以 note 呈现。
 */

const SAVE_TEXT: Record<string, string> = {
  saved: "草稿已保存 · 服务端已确认",
  saving: "正在保存草稿…",
  failed: "草稿未保存 · 服务端未确认",
};

type Draft = {
  draft_id: string;
  draft_kind: string;
  target_id: string;
  revision: number;
  content: any;
  saved_state: string;
};

function SaveBadge({ state, onRetry }: { state: string; onRetry: () => void }) {
  if (state === "failed") {
    return (
      <span className="save is-failed" role="alert">
        <span className="dot" />草稿未保存 · 服务端未确认
        <button className="retry" onClick={onRetry}>重试</button>
      </span>
    );
  }
  if (state === "saving") return <span className="save is-saving"><span className="dot" />{SAVE_TEXT.saving}</span>;
  return <span className="save"><span className="dot" />{SAVE_TEXT.saved}</span>;
}

/** 访谈问卷：选项卡网格（单选/多选）+ 跳过 + 自由补充 */
function IntakeForm({ intake, onDecide, busy }: {
  intake: any; onDecide: (answers: any[]) => void; busy: boolean;
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
    const answers = questions
      .map((q) => ({
        question_id: q.question_id,
        selections: sel[q.question_id] || [],
        free_text: (freetext[q.question_id] || "").trim() || undefined,
      }))
      .filter((a) => a.selections.length > 0 || a.free_text);
    onDecide(answers);
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

export default function PlanningView({ onErr, onOk, needLogin, go }: {
  onErr: (s: string) => void; onOk: (s: string) => void; needLogin: boolean; go: (p: string) => void;
}) {
  const [title, setTitle] = useState("");
  const [routeId, setRouteId] = useState("");
  const [taskId, setTaskId] = useState("");
  const [task, setTask] = useState<any>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [localContent, setLocalContent] = useState<any>(null);
  const [saveState, setSaveState] = useState("saved");
  const [scene, setScene] = useState<any>(null);
  const [runError, setRunError] = useState("");
  const [running, setRunning] = useState(false);
  const saveTimer = useRef<number | null>(null);
  const dirtyRef = useRef(false);
  const bootRef = useRef(false);

  const refreshTask = useCallback(async (id: string) => {
    try { setTask(await api.getPlanningTask(id)); } catch { /* 保持当前状态 */ }
  }, []);

  const loadScene = useCallback(async (rid: string) => {
    if (!rid) return;
    try { setScene(await api.routeScene(rid)); } catch { setScene(null); }
  }, []);

  const runTask = useCallback(async (id: string) => {
    localStorage.setItem("slice3.lastPlanningTask", id);
    setRunning(true);
    try {
      const body = await api.runPlanningTask(id);
      if (body.draft?.draft_id) {
        const d = await api.getDraft(body.draft.draft_id);
        setDraft(d);
        setLocalContent(d.content);
        setSaveState(d.saved_state);
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
            if (t.draft_id) {
              const d = await api.getDraft(t.draft_id);
              setDraft(d);
              setLocalContent(d.content);
              setSaveState(d.saved_state);
              try { setScene(await api.routeScene(t.target_id)); } catch { setScene(null); }
            }
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

  // 1 秒自动保存（停止编辑后）
  const scheduleSave = useCallback((next: any) => {
    setLocalContent(next);
    dirtyRef.current = true;
    setSaveState("saving");
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(async () => {
      if (!draft || !dirtyRef.current) return;
      try {
        const saved = await api.saveDraft(draft.draft_id, {
          revision: draft.revision, content: next, saved_state: "saved",
        });
        dirtyRef.current = false;
        setDraft(saved);
        setSaveState("saved");
      } catch (e: any) {
        setSaveState("failed");
        if (e?.code === "DRAFT_REVISION_MISMATCH") {
          try {
            const latest = await api.getDraft(draft.draft_id);
            setDraft(latest);
            setLocalContent(latest.content);
            setSaveState(latest.saved_state);
            dirtyRef.current = false;
            onErr("草稿已在其他窗口更新，已加载服务端最新版本。");
          } catch { /* 保持失败可见 */ }
        } else {
          onErr(`自动保存失败（${e?.code || "错误"}），修改仍在本地，可重试。`);
        }
      }
    }, 1000);
  }, [draft, onErr]);

  const confirmDraft = useCallback(async () => {
    if (!draft) return;
    try {
      const r = await api.confirmDraft(draft.draft_id, {
        revision: draft.revision, idempotency_key: `web-confirm-${Date.now()}`,
      });
      onOk(r.deduped ? "该草稿此前已确认。" : `已确认并创建不可变版本 v${r.version_no}。`);
      setDraft(null);
      setLocalContent(null);
      setScene(null);
      await loadScene(draft.target_id);
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
        setLocalContent(d.content);
        setSaveState(d.saved_state);
      }
      await refreshTask(taskId);
      onOk("已按你的选择继续。");
    } catch (e: any) {
      onErr(`决策失败：${e?.code || "错误"}`);
    } finally {
      setRunning(false);
    }
  }, [onErr, onOk, refreshTask, taskId]);

  const submitIntake = useCallback((answers: any[]) => {
    onOk("");
    decision({ kind: "intake", answers });
  }, [decision]);

  const setScopeAndNewDraft = useCallback(async (next: string) => {
    if (!draft) return;
    try {
      const r = await api.switchDraftScope(draft.draft_id, next);
      setDraft(r.draft);
      setLocalContent(r.draft.content);
      onOk("已切换范围，输入已保留到新草稿。");
    } catch (e: any) {
      onErr(`范围切换失败：${e?.code || "错误"}`);
    }
  }, [draft, onErr, onOk]);

  const waitState = task?.wait_state;
  const intake = task?.intake;
  const showIntake = waitState === "awaiting_intake" && intake?.questions?.length > 0;
  const hasBoard = Boolean(draft || scene);

  return (
    <div className="mm-clean">
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
        {runError && (
          <div className="note note-err" role="alert">
            <span className="tag">失败</span>
            <div className="tt">生成失败 · {runError}</div>
            <div>不无限重试；可调整输入后重新生成。</div>
          </div>
        )}
      </div>

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

      {routeId && hasBoard && (
        <>
          <div className="verbar">
            <div className="cell">
              <span className="lab">基线版本</span>
              <div className="line">
                {scene
                  ? <><span className="ver ver-conf">已确认 v1</span><span className="muted">路线导图与总览同版</span></>
                  : <span className="muted">尚无已确认版本；确认草稿后此处展示导图。</span>}
              </div>
            </div>
            <div className={`cell ${draft ? "editing" : ""}`}>
              <span className="lab">{draft ? "编辑中 · 草稿" : "草稿"}</span>
              <div className="line">
                {draft ? (
                  <>
                    <span className="ver ver-draft">草稿 rev {draft.revision}</span>
                    <SaveBadge state={saveState} onRetry={() => scheduleSave(localContent)} />
                  </>
                ) : (
                  <span className="muted">生成候选后，在这里编辑并确认。</span>
                )}
              </div>
            </div>
            <div className="acts">
              {draft && (
                <>
                  <button className="btn btn-pri" onClick={confirmDraft}>确认草稿</button>
                  <button className="btn btn-draft" onClick={() => setScopeAndNewDraft(draft.draft_kind === "route" ? "course" : "route")}>
                    切换为{draft.draft_kind === "route" ? "课程大纲" : "路线"}
                  </button>
                </>
              )}
            </div>
          </div>

          <div className="mm-tools">
            <span className="hint">画布位置只影响视角；业务排序与重命名在右侧层级列表进行（写草稿，不直接改已确认版本）。</span>
          </div>

          <div className="mm-body">
            <div className="mm-main">
              <div className="stage-wrap">
                {scene ? (
                  <div className="canvas-readonly-wrap">
                    <ReadonlyCanvas elements={scene.elements} height={520} />
                  </div>
                ) : (
                  <div className="canvas-readonly-wrap" style={{ display: "grid", placeItems: "center", color: "var(--ink3)" }}>
                    确认草稿后，这里展示与总览同版的路线导图。
                  </div>
                )}
              </div>
            </div>
            <aside className="mm-side" aria-label="草稿层级列表">
              <div className="tabs" role="tablist">
                <button role="tab" aria-selected={true}>层级列表</button>
              </div>
              <div className="side-scroll">
                {draft && localContent ? (
                  <DraftTree draft={draft} content={localContent} onChange={scheduleSave} />
                ) : (
                  <p className="muted">尚无草稿。生成候选后，这里会显示阶段、课程（或单元、讲次）的层级列表，可重命名与调整顺序。</p>
                )}
              </div>
            </aside>
          </div>
        </>
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

function DraftTree({ draft, content, onChange }: {
  draft: Draft; content: any; onChange: (next: any) => void;
}) {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const isRoute = draft.draft_kind === "route";
  const groups: { id: string; title: string; items: { id: string; title: string }[] }[] = isRoute
    ? (content.stages || []).map((s: any, i: number) => ({
        id: `s${i}`, title: s.title,
        items: (s.courses || []).map((c: any, j: number) => ({ id: `s${i}c${j}`, title: c.title })),
      }))
    : (content.units || []).map((u: any, i: number) => ({
        id: `u${i}`, title: u.title,
        items: (u.lessons || []).map((l: any, j: number) => ({ id: `u${i}l${j}`, title: l.title })),
      }));

  const move = (list: any[], from: number, to: number) => {
    if (to < 0 || to >= list.length) return list;
    const next = [...list];
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    return next;
  };
  const editGroupTitle = (gi: number, title: string) => {
    const next = structuredClone(content);
    if (isRoute) next.stages[gi].title = title; else next.units[gi].title = title;
    onChange(next);
  };
  const editItemTitle = (gi: number, ii: number, title: string) => {
    const next = structuredClone(content);
    if (isRoute) next.stages[gi].courses[ii].title = title; else next.units[gi].lessons[ii].title = title;
    onChange(next);
  };
  const moveItem = (gi: number, ii: number, dir: -1 | 1) => {
    const next = structuredClone(content);
    if (isRoute) next.stages[gi].courses = move(next.stages[gi].courses, ii, ii + dir);
    else next.units[gi].lessons = move(next.units[gi].lessons, ii, ii + dir);
    onChange(next);
  };
  const moveGroup = (gi: number, dir: -1 | 1) => {
    const next = structuredClone(content);
    if (isRoute) next.stages = move(next.stages, gi, gi + dir);
    else next.units = move(next.units, gi, gi + dir);
    onChange(next);
  };

  return (
    <ol className="tree" aria-label="草稿层级列表">
      {groups.map((g, gi) => (
        <li key={g.id}>
          <div className="ti" style={{ ["--lvl" as any]: 0 }}>
            <button
              className="tw"
              aria-expanded={!!open[g.id]}
              aria-label={`${open[g.id] ? "收起" : "展开"} ${g.title}`}
              onClick={() => setOpen((o) => ({ ...o, [g.id]: !o[g.id] }))}
            >
              {open[g.id] ? "▾" : "▸"}
            </button>
            <input
              className="ren"
              value={g.title}
              aria-label={`${isRoute ? "阶段" : "单元"}标题`}
              onChange={(e) => editGroupTitle(gi, e.target.value)}
              style={{ width: "100%" }}
            />
            <span className="mv">
              <button onClick={() => moveGroup(gi, -1)} aria-label="上移">↑</button>
              <button onClick={() => moveGroup(gi, 1)} aria-label="下移">↓</button>
            </span>
          </div>
          {open[g.id] && (
            <ul>
              {g.items.map((item, ii) => (
                <li key={item.id}>
                  <div className="ti lv1" style={{ ["--lvl" as any]: 1 }}>
                    <span className="ord">{ii + 1}</span>
                    <input
                      className="ren"
                      value={item.title}
                      aria-label={`${isRoute ? "课程" : "讲次"}标题`}
                      onChange={(e) => editItemTitle(gi, ii, e.target.value)}
                      style={{ width: "100%" }}
                    />
                    <span className="mv">
                      <button onClick={() => moveItem(gi, ii, -1)} aria-label="上移条目">↑</button>
                      <button onClick={() => moveItem(gi, ii, 1)} aria-label="下移条目">↓</button>
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </li>
      ))}
    </ol>
  );
}
