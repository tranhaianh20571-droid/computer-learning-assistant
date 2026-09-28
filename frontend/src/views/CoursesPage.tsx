import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";

/**
 * 课程列表（v2 对齐原型 /course）：
 * - 过滤 pills（全部/进行中/已完成）+ 搜索；
 * - 封面卡片：色块 + 手绘字母 monogram、进度条百分比、下一讲提示；
 * - 进度口径与课程总览一致（已确认大纲的讲次等权）。
 */

const COVER_TONES = ["tone-a", "tone-b", "tone-c", "tone-d", "tone-e"];

function toneOf(id: string) {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return COVER_TONES[h % COVER_TONES.length];
}

type CourseItem = {
  course_id: string; title: string; scenario: string; outline_confirmed: boolean;
  unit_count: number; lesson_total: number; lesson_started: number;
  next_lesson_id: string | null; next_lesson_title: string | null;
};

export default function CoursesPage({ me, onErr, go }: {
  me: any; onErr: (s: string) => void; onOk: (s: string) => void; go: (p: string) => void;
}) {
  const [items, setItems] = useState<CourseItem[] | null>(null);
  const [filter, setFilter] = useState<"all" | "active" | "done">("all");
  const [q, setQ] = useState("");

  const load = useCallback(async () => {
    try {
      const r = await api.listCourses();
      setItems(r.data || []);
    } catch (e: any) {
      onErr(e?.message || "课程列表加载失败");
      setItems([]);
    }
  }, [onErr]);

  useEffect(() => { if (me) load(); else setItems([]); }, [me, load]);

  const shown = useMemo(() => {
    const list = items || [];
    const kw = q.trim().toLowerCase();
    return list.filter((c) => {
      if (filter === "active" && !(c.outline_confirmed && c.lesson_started < c.lesson_total)) return false;
      if (filter === "done" && !(c.outline_confirmed && c.lesson_total > 0 && c.lesson_started >= c.lesson_total)) return false;
      if (kw && !c.title.toLowerCase().includes(kw)) return false;
      return true;
    });
  }, [items, filter, q]);

  return (
    <div className="page-w">
      <div className="phead">
        <span className="kicker">课程</span>
        <div className="row"><h1 className="h1">我的课程</h1></div>
      </div>

      <div className="courses-head">
        <div className="seg" role="radiogroup" aria-label="课程过滤">
          <button role="radio" aria-checked={filter === "all"} onClick={() => setFilter("all")}>全部</button>
          <button role="radio" aria-checked={filter === "active"} onClick={() => setFilter("active")}>进行中</button>
          <button role="radio" aria-checked={filter === "done"} onClick={() => setFilter("done")}>已完成</button>
        </div>
        <input
          className="inp search-inp"
          type="search"
          placeholder="搜索课程…"
          aria-label="搜索课程"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>

      {items === null && <p className="muted">加载中…</p>}
      {items !== null && shown.length === 0 && (
        <div className="note note-info" role="note">
          <span className="tag">空的</span>
          <div className="tt">还没有课程</div>
          <div>回到「新的学习任务」提交一个学习目标，学班会帮你生成课程。</div>
          <div className="acts"><button className="btn btn-pri btn-sm" onClick={() => go("/home")}>去创建</button></div>
        </div>
      )}

      <div className="course-grid">
        {shown.map((c) => {
          const pct = c.lesson_total > 0 ? Math.min(100, Math.round((c.lesson_started / c.lesson_total) * 100)) : 0;
          const done = c.outline_confirmed && c.lesson_total > 0 && c.lesson_started >= c.lesson_total;
          return (
            <article className={`ccard ${toneOf(c.course_id)}`} key={c.course_id}>
              <button className="ccard-cover" onClick={() => go(`/course/${c.course_id}`)} aria-label={`打开课程 ${c.title}`}>
                <span className="mono" aria-hidden="true">{(c.title || "课").trim().charAt(0).toUpperCase()}</span>
              </button>
              <div className="ccard-body">
                <span className="kicker">{c.scenario === "school" ? "学校课程" : "自学"}{c.unit_count > 0 ? ` · ${c.unit_count} 单元` : ""}</span>
                <h2><a href={`#/course/${c.course_id}`}>{c.title || c.course_id}</a></h2>
                <div className="meta-l">
                  {c.outline_confirmed
                    ? <span className="ver ver-conf">大纲已确认</span>
                    : <span className="ver ver-draft">未确认大纲</span>}
                  {done && <span className="ver ver-conf">已完成</span>}
                </div>
                <div className="bar" role="img" aria-label={`进度 ${pct}%`}>
                  <i style={{ width: `${pct}%` }} />
                  <b className="num">{pct}%</b>
                </div>
              </div>
              <div className="ccard-side">
                {c.next_lesson_title ? (
                  <>
                    <span className="kicker">接下来</span>
                    <p className="next-title od-truncate-2">{c.next_lesson_title}</p>
                  </>
                ) : (
                  <span className="kicker">{c.outline_confirmed ? "暂无待学讲次" : "先确认大纲"}</span>
                )}
                <a className="btn btn-pri" href={`#/course/${c.course_id}`}>进入课程</a>
              </div>
            </article>
          );
        })}
      </div>
    </div>
  );
}
