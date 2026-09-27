import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import ReadonlyCanvas from "./excalidraw/ReadonlyCanvas";

/**
 * 课程规划工作台（设计稿 mm-clean / verbar / tree）：
 * - 版本栏：基线版本 / 编辑中草稿（虚线斜纹 + 自动保存状态机）/ 确认与范围操作；
 * - 左侧画布：只读 Excalidraw（已确认版本场景）；右侧层级列表承载业务编辑；
 * - 停止编辑 1 秒自动保存，服务端成功后才更新已保存 revision，失败可见可重试；
 * - 等待态（范围澄清 / 搜索决策）以 note + radio-row 呈现。
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

export default function PlanningView({ onErr, onOk, needLogin, go }: {
  onErr: (s: string) => void; onOk: (s: string) => void; needLogin: boolean; go: (p: string) => void;
}) {
  const [title, setTitle] = useState("");
  const [scope, setScope] = useState("auto");
  const [routeId, setRouteId] = useState("");
  const [taskId, setTaskId] = useState("");
  const [task, setTask] = useState<any>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [localContent, setLocalContent] = useState<any>(null);
  const [saveState, setSaveState] = useState("saved");
  const [scene, setScene] = useState<any>(null);
  const [runError, setRunError] = useState("");
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
    try {
      const body = await api.runPlanningTask(id);
      if (body.draft?.draft_id) {
        const d = await api.getDraft(body.draft.draft_id);
        setDraft(d);
        setLocalContent(d.content);
        setSaveState(d.saved_state);
      }
      await refreshTask(id);
      if (body.wait_state === "awaiting_scope_clarification") onOk("规划范围存在歧义，请回答一个问题。");
      else if (body.wait_state === "awaiting_search_decision") onOk("联网搜索失败，请选择重试或离线继续。");
      else if (body.task_status === "succeeded") onOk("候选已生成，请在草稿中查看并确认。");
      else if (body.task_status === "failed") setRunError(body.error_code || "生成失败");
    } catch (e: any) {
      setRunError(e?.code || "任务执行失败");
    }
  }, [onOk, refreshTask]);

  // 从首页跳转而来：自动执行待处理任务
  useEffect(() => {
    if (bootRef.current) return;
    bootRef.current = true;
    const pending = sessionStorage.getItem("slice3.pendingTask");
    const pendingRoute = sessionStorage.getItem("slice3.pendingRoute");
    if (pendingRoute) setRouteId(pendingRoute);
    if (pending) {
      sessionStorage.removeItem("slice3.pendingTask");
      setTaskId(pending);
      runTask(pending);
    }
  }, [runTask]);

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
        planning_scope: scope,
        target_route_id: rid,
      });
      setTaskId(body.task_id);
      await runTask(body.task_id);
    } catch (e: any) {
      onErr(e?.message || "创建任务失败");
    }
  }, [needLogin, onErr, routeId, runTask, scope, title]);

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
    }
  }, [onErr, onOk, refreshTask, taskId]);

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
          <span className="seg" role="radiogroup" aria-label="规划范围">
            <button role="radio" aria-checked={scope === "auto"} onClick={() => setScope("auto")}>自动判断</button>
            <button role="radio" aria-checked={scope === "course"} onClick={() => setScope("course")}>单门课程</button>
            <button role="radio" aria-checked={scope === "route"} onClick={() => setScope("route")}>多课程路线</button>
          </span>
          <button className="btn btn-pri" onClick={createAndRun} disabled={needLogin}>生成路线候选</button>
          {taskId && <span className="idtag">task_id={taskId}</span>}
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

      {routeId && (
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
            <span className="sp" />
            {draft && <span className="idtag">draft_id={draft.draft_id}</span>}
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
