import { useCallback, useMemo, useRef, useState } from "react";
import { api } from "./api";
import ReadonlyCanvas from "./excalidraw/ReadonlyCanvas";

/**
 * 切片 3 课程规划页：
 * - 四入口任务流（route_generate / outline_generate），范围选择与澄清/搜索等待决策；
 * - 草稿层级列表（业务排序/重命名走草稿 PUT，画布坐标不是顺序来源）；
 * - 停止编辑 1 秒后自动保存；服务端成功后才更新已保存 revision；失败可见可重试；
 * - 确认（幂等键）→ 不可变版本；导图读取已确认版本场景（与总览同版）。
 */

const WAIT_TEXT: Record<string, string> = {
  awaiting_scope_clarification: "规划范围存在歧义，请选择围绕一门课程还是一条路线。",
  awaiting_search_decision: "联网搜索失败：可重试，或明确选择离线继续（不会伪造联网依据）。",
};

type Draft = {
  draft_id: string;
  draft_kind: string;
  target_id: string;
  revision: number;
  content: any;
  saved_state: string;
};

type TaskInfo = {
  task_id: string;
  status?: string;
  wait_state?: string | null;
  clarification_question?: string | null;
  resolved_scope?: string | null;
};

export default function PlanningView({ onErr, onOk, needLogin }: { onErr: (s: string) => void; onOk: (s: string) => void; needLogin: boolean }) {
  const [title, setTitle] = useState("");
  const [scope, setScope] = useState("auto");
  const [taskKind, setTaskKind] = useState("route_generate");
  const [routeId, setRouteId] = useState("");
  const [taskId, setTaskId] = useState("");
  const [task, setTask] = useState<TaskInfo | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [localContent, setLocalContent] = useState<any>(null);
  const [saveState, setSaveState] = useState<string>("");
  const [scene, setScene] = useState<any>(null);
  const [outlineTitle, setOutlineTitle] = useState("");
  const saveTimer = useRef<number | null>(null);
  const dirtyRef = useRef(false);

  const isRoute = taskKind === "route_generate";

  const refreshTask = useCallback(async (id: string) => {
    try {
      const t = await api.getPlanningTask(id);
      setTask(t);
    } catch { /* 任务查询失败保持当前状态 */ }
  }, []);

  const loadScene = useCallback(async (rid: string) => {
    if (!rid) return;
    try {
      const s = await api.routeScene(rid);
      setScene(s);
    } catch {
      setScene(null); // 未确认版本：无场景
    }
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
      if (body.wait_state) onOk(WAIT_TEXT[body.wait_state] || "任务等待用户决策。");
      else if (body.task_status === "succeeded") onOk("候选已生成，请在草稿中查看并确认。");
      else if (body.task_status === "failed") onErr(`生成失败：${body.error_code || "未知错误"}`);
    } catch (e: any) {
      onErr(e?.code ? `${e.code}` : "任务执行失败");
    }
  }, [onErr, onOk, refreshTask]);

  const createAndRun = useCallback(async () => {
    if (needLogin) return onErr("请先登录。");
    try {
      let rid = routeId;
      if (!rid) {
        const r = await api.createRoute(title || "未命名路线", "self_study");
        rid = r.route_id;
        setRouteId(rid);
      }
      if (taskKind === "outline_generate") {
        // 大纲任务需要课程壳：由已确认路线的第一门课程承接（简化入口，完整流程在总览页）
        const overview = await api.routeOverview(rid);
        const course = overview.stages?.[0]?.courses?.[0];
        if (!course) return onErr("请先生成并确认路线，再展开课程大纲。");
        const body = await api.createPlanningTask({
          idempotency_key: `web-${Date.now()}`,
          task_kind: taskKind,
          planning_scope: "course",
          target_course_id: course.course_id,
        });
        setTaskId(body.task_id);
        await runTask(body.task_id);
        return;
      }
      const body = await api.createPlanningTask({
        idempotency_key: `web-${Date.now()}`,
        task_kind: taskKind,
        planning_scope: scope,
        target_route_id: rid,
      });
      setTaskId(body.task_id);
      await runTask(body.task_id);
    } catch (e: any) {
      onErr(e?.message || "创建任务失败");
    }
  }, [needLogin, onErr, onOk, routeId, runTask, scope, taskKind, title]);

  // 1 秒自动保存（停止编辑后）
  const scheduleSave = useCallback((next: any) => {
    setLocalContent(next);
    dirtyRef.current = true;
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
    <div className="stack">
      <div className="card stack">
        <h1>课程规划</h1>
        <p className="lede">
          选择规划类型与范围；生成候选 → 草稿确认 → 不可变版本。导图与总览始终读取已确认版本。
        </p>
        <div className="row">
          <label>
            规划类型{" "}
            <select value={taskKind} onChange={(e) => setTaskKind(e.target.value)} aria-label="规划类型">
              <option value="route_generate">学习路线</option>
              <option value="outline_generate">课程大纲（需已确认路线）</option>
            </select>
          </label>
          <label>
            范围{" "}
            <select value={scope} onChange={(e) => setScope(e.target.value)} aria-label="规划范围" disabled={!isRoute}>
              <option value="auto">自动判断（歧义时询问）</option>
              <option value="course">单门课程</option>
              <option value="route">完整路线</option>
            </select>
          </label>
        </div>
        {isRoute ? (
          <div className="field">
            <label>路线标题（可留空自动创建）</label>
            <input value={title} onChange={(e) => setTitle(e.target.value)} aria-label="路线标题" />
          </div>
        ) : (
          <div className="field">
            <label>课程大纲标题（用于确认路线后展开）</label>
            <input value={outlineTitle} onChange={(e) => setOutlineTitle(e.target.value)} aria-label="课程大纲标题" />
          </div>
        )}
        <div className="row">
          <button onClick={createAndRun} disabled={needLogin}>生成候选</button>
          {taskId && <span className="mono">task_id={taskId}</span>}
        </div>
        {waitState === "awaiting_scope_clarification" && (
          <div className="notice" role="status">
            {task?.clarification_question || WAIT_TEXT[waitState]}
            <div className="row">
              <button onClick={() => decision({ resolved_scope: "course" })}>围绕一门课程</button>
              <button onClick={() => decision({ resolved_scope: "route" })}>完整学习路线</button>
            </div>
          </div>
        )}
        {waitState === "awaiting_search_decision" && (
          <div className="notice" role="status">
            {WAIT_TEXT[waitState]}
            <div className="row">
              <button onClick={() => decision({ decision: "retry" })}>重试搜索</button>
              <button className="secondary" onClick={() => decision({ decision: "offline_continue" })}>离线继续</button>
            </div>
          </div>
        )}
      </div>

      {routeId && (
        <div className="card stack">
          <h2>导图（已确认版本）</h2>
          {scene ? (
            <ReadonlyCanvas elements={scene.elements} />
          ) : (
            <p className="lede">尚无已确认版本；确认草稿后此处展示导图（与总览同版）。</p>
          )}
          <div className="row">
            <span className="mono">route_id={routeId}</span>
            <a href={`#/route/${routeId}`}>查看路线总览 →</a>
          </div>
        </div>
      )}

      {draft && localContent && (
        <DraftEditor
          draft={draft}
          content={localContent}
          saveState={saveState}
          onChange={scheduleSave}
          onConfirm={confirmDraft}
          onScopeSwitch={setScopeAndNewDraft}
          onRetrySave={() => scheduleSave(localContent)}
        />
      )}
    </div>
  );
}

function DraftEditor({
  draft, content, saveState, onChange, onConfirm, onScopeSwitch, onRetrySave,
}: {
  draft: Draft;
  content: any;
  saveState: string;
  onChange: (next: any) => void;
  onConfirm: () => void;
  onScopeSwitch: (scope: string) => void;
  onRetrySave: () => void;
}) {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const isRoute = draft.draft_kind === "route";
  const groups: { id: string; title: string; items: { id: string; title: string; goal?: string }[] }[] = useMemo(() => {
    if (isRoute) {
      return (content.stages || []).map((s: any, i: number) => ({
        id: `s${i}`, title: s.title, items: (s.courses || []).map((c: any, j: number) => ({ id: `s${i}c${j}`, title: c.title, goal: c.goal })),
      }));
    }
    return (content.units || []).map((u: any, i: number) => ({
      id: `u${i}`, title: u.title, items: (u.lessons || []).map((l: any, j: number) => ({ id: `u${i}l${j}`, title: l.title, goal: l.goal })),
    }));
  }, [content, isRoute]);

  const move = (list: any[], from: number, to: number) => {
    if (to < 0 || to >= list.length) return list;
    const next = [...list];
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    return next;
  };

  const editGroupTitle = (gi: number, title: string) => {
    const next = structuredClone(content);
    if (isRoute) next.stages[gi].title = title;
    else next.units[gi].title = title;
    onChange(next);
  };
  const editItemTitle = (gi: number, ii: number, title: string) => {
    const next = structuredClone(content);
    if (isRoute) next.stages[gi].courses[ii].title = title;
    else next.units[gi].lessons[ii].title = title;
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
    <div className="card stack">
      <div className="row">
        <h2>草稿（{isRoute ? "路线" : "课程大纲"}）</h2>
        <span className={`badge ${saveState === "saved" ? "succeeded" : saveState === "failed" ? "failed" : "running"}`}>
          {saveState === "saved" ? "已保存" : saveState === "failed" ? "保存失败" : "保存中"}
        </span>
        {saveState === "failed" && <button className="secondary" onClick={onRetrySave}>重试保存</button>}
        <span className="mono">revision={draft.revision}</span>
      </div>
      <p className="lede">业务排序与重命名在此进行（写草稿）；画布位置不会改变学习顺序。</p>
      {groups.map((g, gi) => (
        <div key={g.id} className="stack" style={{ borderLeft: "3px solid var(--border)", paddingLeft: "0.75rem" }}>
          <div className="row">
            <button
              className="secondary"
              aria-expanded={!!open[g.id]}
              onClick={() => setOpen((o) => ({ ...o, [g.id]: !o[g.id] }))}
            >
              {open[g.id] ? "收起" : "展开"}
            </button>
            <input
              value={g.title}
              aria-label={`${isRoute ? "阶段" : "单元"}标题`}
              onChange={(e) => editGroupTitle(gi, e.target.value)}
              style={{ flex: 1, minWidth: 200 }}
            />
            <button className="secondary" onClick={() => moveGroup(gi, -1)} aria-label="上移">↑</button>
            <button className="secondary" onClick={() => moveGroup(gi, 1)} aria-label="下移">↓</button>
          </div>
          {open[g.id] && g.items.map((item, ii) => (
            <div key={item.id} className="row">
              <input
                value={item.title}
                aria-label={`${isRoute ? "课程" : "讲次"}标题`}
                onChange={(e) => editItemTitle(gi, ii, e.target.value)}
                style={{ flex: 1, minWidth: 180 }}
              />
              <button className="secondary" onClick={() => moveItem(gi, ii, -1)} aria-label="上移条目">↑</button>
              <button className="secondary" onClick={() => moveItem(gi, ii, 1)} aria-label="下移条目">↓</button>
            </div>
          ))}
        </div>
      ))}
      <div className="row">
        <button onClick={onConfirm}>确认草稿（创建不可变版本）</button>
        <button className="secondary" onClick={() => onScopeSwitch(isRoute ? "course" : "route")}>
          切换为{isRoute ? "课程大纲" : "路线"}（保留输入）
        </button>
      </div>
    </div>
  );
}
