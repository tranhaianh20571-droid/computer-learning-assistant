import UnitAssessment from "./ui/UnitAssessment";
import CourseCover from "./ui/CourseCover";
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { courseLink, routeLink } from "./lib/learningLinks";
import Dialog from "./ui/Dialog";

/**
 * 路线总览与课程总览（设计稿 5/6 节）：
 * - 总览与导图从同一已确认版本读取（同版口径）；
 * - 路线总览：route-sum 概况 + stage/crs 列表；不输出路线整体百分比；
 * - 课程总览：c-info 侧栏（进度 / 单元）+ lec 讲次行（三层状态字形）；
 * - 未确认大纲或讲次未就绪：开始学习禁用并给出 .why 原因（教学启动归切片 4）。
 */

export function RouteOverview({ routeId, versionId, search = "", onErr, go }: {
  routeId: string; versionId?: string; search?: string; onErr: (s: string) => void; go: (p: string) => void;
}) {
  const [overview, setOverview] = useState<any>(null);
  const [detaching, setDetaching] = useState<any>(null);
  const load = useCallback(async () => {
    try {
      setOverview(await api.routeOverview(routeId, versionId));
    } catch (e: any) { onErr(e?.message || "课程合集加载失败"); }
  }, [onErr, routeId, versionId]);
  useEffect(() => { setOverview(null); load(); }, [load]);
  if (!overview) return <p className="muted">加载中…</p>;
  const courses = (overview.stages || []).flatMap((s: any) => s.courses || []);
  const href = (c: any) => `#${courseLink(c.course_id, routeId, c.route_version_id || overview.current_version_id)}`;
  const startOutlineFor = async (courseId: string) => {
    try {
      const task = await api.createPlanningTask({ idempotency_key: `web-${crypto.randomUUID()}`,
        task_kind: "outline_generate", planning_scope: "course", target_course_id: courseId });
      sessionStorage.setItem("slice3.pendingTask", task.task_id);
      sessionStorage.removeItem("slice3.pendingRoute"); go("/planning");
    } catch (e: any) { onErr(e?.message || "创建大纲任务失败"); }
  };
  const highlighted = (title: string) => search.trim() && title.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase());
  return <div className="page-w">
    <nav className="crumb" aria-label="课程层级"><a href="#/courses">我的课程</a><span className="sep">›</span><span>{overview.title}</span></nav>
    <div className="phead"><span className="kicker">课程合集 · {overview.scenario === "school" ? "学校课程" : "自学"}</span>
      <div className="row"><h1 className="h1">{overview.title}</h1>
        {!overview.is_current && <span className="library-historical">历史内容</span>}
      </div><p className="lede">共 {overview.course_count} 门课程，已完成 {overview.course_completed} 门。选择一门课程查看单元和讲次。</p>
      {!overview.is_current && <a className="btn btn-sm" href={`#${routeLink(routeId)}`}>返回当前课程</a>}
    </div>
    {overview.is_current && <div className="route-ink-cover"><CourseCover kind="route" id={routeId} title={overview.title} /></div>}
    {courses.length === 0 && <p className="muted">无当前课程，可查看下方历史课程。</p>}
    {overview.stages.map((stage: any) => stage.courses.length > 0 && <section className="stage" key={stage.stage_id}>
      <div className="stage-h"><span className="k">{stage.course_count} 门课程</span><h2>{stage.title}</h2></div>
      <ol className="crs">{stage.courses.map((c: any) => <li key={c.node_id} className={highlighted(c.title) ? "library-match" : ""}>
        <span className="ord">#{String(c.order_index + 1).padStart(2, "0")}</span>
        <div><h3><a href={href(c)}>{c.title}</a></h3>{c.goal_snapshot && <p className="goal">{c.goal_snapshot}</p>}
          {highlighted(c.title) && <span className="help">搜索匹配</span>}</div>
        <div className="ost">{!c.outline_confirmed && <span className="ol-st ol-pending">待确认大纲</span>}
          <span className="sub">已完成 {c.lesson_completed} / {c.lesson_total} 讲</span></div>
        <div className="next"><a className="btn btn-pri" href={href(c)}>进入课程</a>
          {!c.outline_confirmed && <><button className="btn btn-sm" onClick={() => startOutlineFor(c.course_id)}>生成课程大纲</button>
</>}

        </div>
      </li>)}</ol>
    </section>)}
    {overview.history.length > 0 && <details className="library-history" open={Boolean(search)}><summary>历史课程 · {overview.history.length} 个版本</summary>
      {overview.history.map((h: any) => <section key={h.version_id}><h2>{h.title} · v{h.version_no}</h2>
        <ul>{h.courses.map((c: any) => <li key={c.node_id} className={highlighted(c.title) ? "library-match" : ""}>
          <a href={href(c)}>{c.title}</a> <span className="muted">历史课程</span>
        </li>)}</ul><a className="btn btn-sm" href={`#${routeLink(routeId, h.version_id)}`}>查看此版本</a>
      </section>)}
    </details>}
    {detaching && <Dialog kind="info" title="移出课程合集" onClose={() => setDetaching(null)} actions={<>
      <button className="btn btn-sm" onClick={() => setDetaching(null)}>取消</button>
      <button className="btn btn-pri btn-sm" onClick={async () => {
        try { await api.detachCourse(routeId, detaching.course_id); setDetaching(null); await load(); }
        catch (e: any) { onErr(e?.message || "移出失败"); }
      }}>确认移出</button></>}>
      <p>“{detaching.title}”会保留为独立课程，已有学习记录不变。</p>
    </Dialog>}
  </div>;
}

export function CourseOverview({ courseId, ownerUserId, context = "", onErr, onOk, go }: {
  courseId: string; ownerUserId?: string; context?: string; onErr: (s: string) => void; onOk: (s: string) => void; go: (p: string) => void;
}) {
  const [overview, setOverview] = useState<any>(null);
  const [starting,setStarting]=useState(false);
  const [entryIssue,setEntryIssue]=useState<"settings"|"retry"|null>(null);

  const load = useCallback(async () => {
    try {
      setOverview(await api.courseOverview(courseId, context));
      api.putNavigation({ course_id: courseId }).catch(() => {});
    } catch (e: any) {
      onErr(e?.message || "课程总览加载失败");
    }
  }, [courseId, context, onErr]);

  useEffect(() => { load(); }, [load]);

  if (!overview) return <p className="muted">加载中…</p>;

  const start = async (lessonId: string, startable: boolean, reason: string) => {
    if (!startable) { onErr(reason); return; }
    if(starting)return;
    setEntryIssue(null);
    setStarting(true);
    try {
      const r = await api.startLessonV2(courseId, lessonId);
      if (!r) return;
      if (r.conversation_id) {
        if(r.task_id)sessionStorage.setItem("slice4.pendingTask", r.task_id);
        else sessionStorage.removeItem("slice4.pendingTask");
        onOk(r.recovered ? "已恢复讲解，进入课堂。" : "开始学习。");
        go(`/lecture/${r.conversation_id}`);
      } else {
        onOk("已请求开始学习。");
      }
    } catch (e: any) {
      if(e?.code==="LESSON_UNLOCK_REQUIRED")onErr("未确认大纲，暂不能开始学习。");
      else setEntryIssue(/CONFIG|CREDENTIAL|DISCLOSURE|CAPABILITY|QUOTA/i.test(e?.code||"")?"settings":"retry");
    }finally{setStarting(false);}
  };

  const allLessons = (overview.units || []).flatMap((u: any) => u.lessons || []);
  const lessonTotal = overview.progress?.lesson_total ?? allLessons.length;
  const lessonStarted = overview.progress?.lesson_completed ?? 0;
  const pct = lessonTotal > 0 ? Math.min(100, Math.round((lessonStarted / lessonTotal) * 100)) : 0;
  const next = allLessons.find(
    (l: any) => l.learning_state !== "user_completed" && l.startable && overview.teaching_unlocked,
  );
  const nextUnit = next && (overview.units || []).find((u: any) => u.lessons?.some((l: any) => l.lesson_id === next.lesson_id));
  const title = overview.title || courseId;

  return (
    <div className="ccourse">
      <aside className="c-info">
        <button className="btn btn-sm" onClick={() => go(overview.context ? routeLink(overview.context.route_id, overview.context.is_current ? undefined : overview.context.route_version_id) : "/courses")}>← {overview.context ? "返回课程合集" : "返回我的课程"}</button>
        <nav className="library-breadcrumb" aria-label="课程层级"><a href="#/courses">我的课程</a>{overview.context && <> › <a href={`#${routeLink(overview.context.route_id, overview.context.is_current ? undefined : overview.context.route_version_id)}`}>{overview.context.title}</a></>} › {overview.title}</nav>
        {!overview.context && overview.contexts?.length > 0 && <details><summary>所属课程合集</summary>{overview.contexts.map((c: any) => <p key={c.route_version_id}><a href={`#${courseLink(courseId, c.route_id, c.route_version_id)}`}>{c.title} · {c.is_current ? "当前" : "历史"}</a></p>)}</details>}
        {(overview.cover_eligible ?? !overview.contexts?.length) && <div className={`ccover ${["tone-a", "tone-b", "tone-c", "tone-d", "tone-e"][courseId.length % 5]}`}>
          <CourseCover kind="course" id={courseId} title={title} />
        </div>}
        <span className="kicker">课程 · {overview.scenario === "school" ? "学校课程" : "自学"}</span>
        <h1 className="serif" style={{ fontSize: 26, lineHeight: 1.4, margin: 0 }}>{title}</h1>
        {overview.historical_outline && <p className="library-historical">正在查看历史课程内容</p>}
        {!overview.outline_confirmed && <p className="help">大纲尚未确认，确认后才能开始学习。</p>}
        {overview.outline_confirmed && (
          <div className="blk c-prog">
            <span className="k">课程进度</span>
            <div className="bigpct">{pct}% <small>已完成 {lessonStarted}/{lessonTotal} 讲</small></div>
            <div className="bar" role="img" aria-label={`进度 ${pct}%`}><i style={{ width: `${pct}%` }} /></div>
            <span className="help">只统计当前已确认大纲的讲次等权进度。</span>
          </div>
        )}
        {overview.source_route_version_id && (
          <div className="blk">
            <span className="k">课程来源</span>
            <span className="help">由学习路线展开生成；安排过先修 ≠ 已掌握，请按先修顺序学习。</span>
          </div>
        )}

      </aside>

      <section>
        {!overview.outline_confirmed && (
          <div className="note note-draft" role="note" style={{ marginBottom: 24 }}>
            <span className="tag">草稿</span>
            <div className="tt">这份大纲还没确认</div>
            <div>确认之后即可开始学习，其余内容会在后台准备。</div>
          </div>
        )}

        {overview.outline_confirmed && (
          <div
            className="action-card"
            style={{ marginBottom: 18, cursor: "pointer" }}
            role="button"
            tabIndex={0}
            onClick={() => go("/materials")}
            onKeyDown={(e) => { if (e.key === "Enter") go("/materials"); }}
          >
            <span className="plus" aria-hidden="true">＋</span>
            <div style={{ flex: 1 }}>
              <b>上传资料，扩展这门课程</b>
              <p className="help" style={{ margin: "2px 0 0" }}>上传新的 PDF，或直接描述你想添加、修改的内容。</p>
            </div>
            <span className="btn">去准备资料</span>
          </div>
        )}

        {overview.outline_confirmed && (
          <div className="row" style={{ gap: 14, marginBottom: 22, flexWrap: "wrap" }}>
            <span className="unitdots" aria-label={`共 ${(overview.units || []).length} 个单元`}>
              {(overview.units || []).map((u: any) => {
                const ls = u.lessons || [];
                const allDone = ls.length > 0 && ls.every((l: any) => l.learning_state === "user_completed");
                return <i key={u.unit_id} className={allDone ? "on" : ""} title={u.title} />;
              })}
            </span>
            {next ? (
              <button className="nextpill" disabled={starting} onClick={() => start(next.lesson_id, true, "")}>
                <span className="kicker">下一步</span>
                <b>{next.title || "下一讲"}</b>
                <span aria-hidden="true">›</span>
              </button>
            ) : (
              <span className="ver ver-conf">全部讲次已完成 🎉</span>
            )}
            {nextUnit && <span className="help">{nextUnit.title} · 共 {lessonTotal} 讲</span>}
          </div>
        )}

        {entryIssue&&<p role="status" className="help">{entryIssue==="settings"
          ?"学习服务连接需要更新，课程已保留。":"暂时无法进入课堂，课程已保留，请稍后再试。"}
          {entryIssue==="settings"&&<a href="#/capabilities">前往设置</a>}</p>}
        {(overview.units || []).map((u: any, ui: number) => (
          <div key={u.unit_id} id={`unit-${u.unit_id}`} style={{ marginBottom: 28 }}>
            <div className="unit-h">
              <span className="k">第 {ui + 1} 单元，共 {(overview.units || []).length} 单元 · {u.lesson_count} 讲</span>
              <h2 className="h2 serif">{u.title}</h2>
            </div>
            <ol className="lecs">
              {u.lessons?.map((l: any) => {
                const startable = overview.teaching_unlocked && l.startable;
                const completed=l.learning_state==="user_completed";
                const why = !overview.teaching_unlocked
                  ? "未确认大纲，不能学习"
                  : !l.startable
                    ? (l.status_preparation === "failed" ? "上次生成失败，可以重试" : "点击后开始生成讲解")
                    : completed?"回看课堂录像":"进入课堂继续学习";
                return (
                  <li className={`lec${overview.teaching_unlocked ? "" : " locked"}`} key={l.lesson_id}>
                    <span className="no">{String(l.order_index + 1).padStart(2, "0")}</span>
                    <div>
                      <h3>{l.title || l.goal || l.lesson_id}</h3>
                      {l.prereq_arranged && <p className="desc">有先修讲次；安排过先修 ≠ 已掌握。</p>}
                      <div className="states">
                        <span className={`st st-learn ${l.learning_state === "user_completed" ? "is-done" : l.learning_state === "in_progress" ? "is-doing" : ""}`}>
                          <i className="g" aria-hidden="true" />
                          <span className="k">学习</span>
                          {l.learning_state === "user_completed" ? "已完成" : l.learning_state === "in_progress" ? "进行中" : "未开始"}
                        </span>
                      </div>
                    </div>
                    <div className="go">
                      <button
                        className={`btn${startable ? " btn-pri" : ""}`}
                        disabled={!startable}
                        onClick={() => completed?go(`/lesson-review/${courseId}/${l.lesson_id}`):
                          start(l.lesson_id, startable,overview.teaching_unlocked ? "该讲次尚未就绪。" : "未确认大纲，禁止启动教学。")}
                      >
                        {overview.teaching_unlocked ? (l.startable ? completed?"复习":"开始学习" : "未就绪") : "未确认，不能学习"}
                      </button>
                      <span className="why">{why}</span>
                    </div>
                  </li>
                );
              })}
            </ol>
            {overview.outline_confirmed && !overview.historical_outline && <UnitAssessment key={`${ownerUserId}:${courseId}:${u.unit_id}`} ownerUserId={ownerUserId} courseTitle={overview.title} unitIndex={ui + 1} lessons={u.lessons || []} courseId={courseId} unitId={u.unit_id} unitTitle={u.title}
              onReview={() => document.getElementById(`unit-${u.unit_id}`)?.scrollIntoView({ behavior: "smooth" })} />}
          </div>
        ))}
      </section>
    </div>
  );
}
