import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Dialog from "../ui/Dialog";
import { api } from "../api";
import CourseCover from "../ui/CourseCover";
import { routeLink } from "../lib/learningLinks";

const COVER_TONES = ["tone-a", "tone-b", "tone-c", "tone-d", "tone-e"];
function toneOf(id: string) {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return COVER_TONES[h % COVER_TONES.length];
}
function preference(key: string, fallback: string) {
  try { return sessionStorage.getItem(key) || fallback; } catch { return fallback; }
}

export default function CoursesPage({ me, onErr, go }: {
  me: any; onErr: (s: string) => void; onOk: (s: string) => void; go: (p: string) => void;
}) {
  const [items, setItems] = useState<any[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [deleting, setDeleting] = useState<any>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const deleteLock = useRef(false);
  const [filter, setFilter] = useState(() => preference("library.filter", "all"));
  const [q, setQ] = useState(() => preference("library.search", ""));
  const load = useCallback(async () => {
    setFailed(false);
    try { setItems((await api.listLibrary()).data || []);api.prewarmLibrary().catch(()=>{}); }
    catch (e: any) { onErr(e?.message || "课程列表加载失败"); setFailed(true); }
  }, [onErr]);
  useEffect(() => { if (me) load(); else setItems([]); }, [me, load]);
  useEffect(() => {
    try { sessionStorage.setItem("library.filter", filter); sessionStorage.setItem("library.search", q); } catch { /* optional preference */ }
  }, [filter, q]);
  const shown = useMemo(() => (items || []).filter((item) => {
    if (filter === "active" && item.completed) return false;
    if (filter === "done" && !item.completed) return false;
    const kw = q.trim().toLocaleLowerCase();
    return !kw || [item.title, ...(item.search_titles || []), ...(item.history_titles || [])]
      .some((title) => title.toLocaleLowerCase().includes(kw));
  }), [items, filter, q]);

  return <div className="page-w">
    <div className="phead"><span className="kicker">课程</span><div className="row"><h1 className="h1">我的课程</h1></div></div>
    <div className="courses-head">
      <div className="seg" role="radiogroup" aria-label="课程过滤">
        {[["all", "全部"], ["active", "进行中"], ["done", "已完成"]].map(([key, label]) =>
          <button key={key} role="radio" aria-checked={filter === key} onClick={() => setFilter(key)}>{label}</button>)}
      </div>
      <input className="inp search-inp" type="search" placeholder="搜索课程…" aria-label="搜索课程" value={q} onChange={(e) => setQ(e.target.value)} />
    </div>
    {items === null && !failed && <p className="muted">加载中…</p>}
    {failed && <div className="note note-err"><div>课程加载失败，请重试。</div><button className="btn" onClick={load}>重新加载</button></div>}
    {items && shown.length === 0 && !failed && <div className="note note-info" role="note">
      <span className="tag">{items.length ? "无匹配" : "空的"}</span>
      <div className="tt">{items.length ? "没有符合条件的课程" : "还没有课程"}</div>
      <div className="acts">{items.length
        ? <button className="btn btn-sm" onClick={() => { setQ(""); setFilter("all"); }}>清除筛选</button>
        : <button className="btn btn-pri btn-sm" onClick={() => go("/home")}>去创建</button>}</div>
    </div>}
    <div className="course-grid">{shown.map((c) => {
      const grouped = c.kind === "route";
      const pct = c.lesson_total ? Math.min(100, Math.round(c.lesson_completed / c.lesson_total * 100)) : 0;
      const base = grouped ? routeLink(c.id) : `/course/${c.id}`;
      const path = grouped && q.trim() ? `${base}?q=${encodeURIComponent(q.trim())}` : base;
      const matches = q.trim() ? (c.search_titles || []).filter((title: string) => title.toLocaleLowerCase().includes(q.trim().toLocaleLowerCase())) : [];
      const historyMatches = q.trim() ? (c.history_titles || []).filter((title: string) => title.toLocaleLowerCase().includes(q.trim().toLocaleLowerCase())) : [];
      return <article className={`ccard ${toneOf(c.id)}`} key={`${c.kind}:${c.id}`}>
        <div className="ccard-cover"><CourseCover kind={c.kind} id={c.id} title={c.title} onOpen={() => go(path)} /></div>
        <div className="ccard-body">
          <span className="kicker">{c.scenario === "school" ? "学校课程" : "自学"} · {grouped ? `${c.course_count} 门课程` : `${c.unit_count} 单元`}</span>
          <h2><a href={`#${path}`}>{c.title}</a></h2>
          <div className="meta-l"><span className={`library-state is-${c.completed ? "done" : c.state === "in_progress" ? "active" : c.state === "preparing" ? "preparing" : "idle"}`}>
            <i className="library-state-mark" aria-hidden="true" />
            {c.completed ? "已完成" : c.state === "in_progress" ? "学习中" : c.state === "preparing" ? "待确认大纲" : "未开始"}
          </span>{grouped && <span>共 {c.course_count} 门，已完成 {c.course_completed} 门</span>}</div>
          {matches.length > 0 && <p className="help">匹配子课程：{matches.join("、")}</p>}
          {historyMatches.length > 0 && <p className="help">匹配历史课程：{historyMatches.join("、")}</p>}
          {grouped ? <p className="help">{c.course_count ? "进入后选择一门课程继续学习。" : "无当前课程，可查看历史课程。"}</p>
            : <div className="bar" role="img" aria-label={`完成进度 ${pct}%`}><i style={{ width: `${pct}%` }} /><b className="num">{pct}%</b></div>}
        </div>
        <div className="ccard-side"><span className="kicker">{grouped ? "课程合集" : "接下来"}</span>
          {!grouped && <p className="next-title od-truncate-2">{c.next_lesson_title || (c.completed ? "所有讲次已完成" : "先确认课程大纲")}</p>}
          <a className="btn btn-pri" href={`#${path}`}>进入课程</a>
          <button className="library-delete" onClick={() => { setDeleteError(""); setDeleting(c); }}>{grouped ? "删除合集" : "删除课程"}</button>
        </div>
      </article>;
    })}</div>
    {deleting && <Dialog kind="info" title={deleting.kind === "route" ? "删除课程合集" : "删除课程"}
      onClose={() => { if (!deleteLock.current) setDeleting(null); }} actions={<>
        <button className="btn" disabled={deleteBusy} onClick={() => setDeleting(null)}>取消</button>
        <button className="btn btn-danger" disabled={deleteBusy} onClick={async () => {
          if (deleteLock.current) return;
          deleteLock.current = true; setDeleteBusy(true); setDeleteError("");
          try {
            if (deleting.kind === "route") await api.deleteRoute(deleting.id);
            else await api.deleteCourse(deleting.id);
            setItems(prev => prev?.filter(item => !(item.kind === deleting.kind && item.id === deleting.id)) || []);
            setDeleting(null); await load();
          } catch (e: any) { setDeleteError(e?.message || "删除失败，请重试。"); }
          finally { deleteLock.current = false; setDeleteBusy(false); }
        }}>{deleteBusy ? "正在删除…" : "确认删除"}</button>
      </>}>
      <p>确定删除“{deleting.title}”？</p>
      <p>{deleting.kind === "route" ? "仅删除合集编排，其中的课程和已有学习记录会保留。" : "将删除这门课程的大纲、讲次和相关学习产物，并从所属合集中移除。此操作无法撤销。"}</p>
      {deleteError && <p className="library-delete-error" role="alert">{deleteError}</p>}
    </Dialog>}
  </div>;
}
