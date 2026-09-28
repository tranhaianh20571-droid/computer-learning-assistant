import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import ReadonlyCanvas from "./excalidraw/ReadonlyCanvas";

/**
 * 路线总览与课程总览（设计稿 5/6 节）：
 * - 总览与导图从同一已确认版本读取（同版口径）；
 * - 路线总览：route-sum 概况 + stage/crs 列表；不输出路线整体百分比；
 * - 课程总览：c-info 侧栏（大纲版本 ver / 进度 / 单元）+ lec 讲次行（三层状态字形）；
 * - 未确认大纲或讲次未就绪：开始学习禁用并给出 .why 原因（教学启动归切片 4）。
 */

const PREP_LABEL: Record<string, { text: string; cls: string }> = {
  not_generated: { text: "未生成", cls: "" },
  generating: { text: "生成中", cls: "is-making" },
  ready: { text: "已就绪", cls: "is-ready" },
  failed: { text: "失败", cls: "is-fail" },
};

function PrepState({ status, extra }: { status: string; extra?: string }) {
  const meta = PREP_LABEL[status] || PREP_LABEL.not_generated;
  return (
    <span className={`st st-prep ${meta.cls}`}>
      <i className="g" aria-hidden="true" />
      <span className="k">讲解</span>
      {extra || meta.text}
    </span>
  );
}

export function RouteOverview({ routeId, onErr, go }: {
  routeId: string; onErr: (s: string) => void; go: (p: string) => void;
}) {
  const [overview, setOverview] = useState<any>(null);
  const [scene, setScene] = useState<any>(null);
  const [showCanvas, setShowCanvas] = useState(false);

  const load = useCallback(async () => {
    try {
      setOverview(await api.routeOverview(routeId));
      try { setScene(await api.routeScene(routeId)); } catch { setScene(null); }
      api.putNavigation({ route_id: routeId }).catch(() => {});
    } catch (e: any) {
      onErr(e?.message || "路线总览加载失败");
    }
  }, [onErr, routeId]);

  useEffect(() => { load(); }, [load]);

  if (!overview) return <p className="muted">加载中…</p>;

  const readyCount = overview.stages?.filter((s: any) => s.courses?.some((c: any) => c.outline_confirmed)).length || 0;
  const pendingCount = (overview.stages || []).reduce(
    (acc: number, s: any) => acc + s.courses?.filter((c: any) => !c.outline_confirmed).length, 0);

  const startOutlineFor = async (courseId: string) => {
    try {
      const task = await api.createPlanningTask({
        idempotency_key: `web-${Date.now()}`,
        task_kind: "outline_generate",
        planning_scope: "course",
        target_course_id: courseId,
      });
      sessionStorage.setItem("slice3.pendingTask", task.task_id);
      sessionStorage.removeItem("slice3.pendingRoute");
      go("/planning");
    } catch (e: any) {
      onErr(e?.message || "创建大纲任务失败");
    }
  };

  return (
    <div className="page-w">
      <div className="phead">
        <span className="kicker">学习路线 · {overview.scenario === "school" ? "学校课程" : "自学"}</span>
        <div className="row">
          <h1 className="h1">学习路线</h1>
          {overview.has_version
            ? <span className="ver ver-conf">已确认 v{overview.version_no}</span>
            : <span className="ver ver-hist">尚无已确认版本</span>}
          {scene && (
            <button className="btn btn-sm" onClick={() => setShowCanvas((v) => !v)}>
              {showCanvas ? "收起导图" : "查看导图（与总览同版）"}
            </button>
          )}
        </div>
        <p className="lede">学习顺序以层级与先修关系为准；画布位置不会改变业务顺序。</p>
      </div>

      <div className="route-sum" aria-label="路线概况">
        <a href="#c-ready"><span className="n">{readyCount} 门</span><span className="l">大纲已确认 · 可以展开学习</span></a>
        <a href="#c-act"><span className="n">{pendingCount} 门</span><span className="l">还没有大纲</span></a>
      </div>

      {showCanvas && scene && (
        <div className="canvas-readonly-wrap" style={{ marginBottom: 24 }}>
          <ReadonlyCanvas elements={scene.elements} height={460} />
        </div>
      )}

      {(overview.stages || []).map((s: any) => (
        <section className="stage" aria-labelledby={`st-${s.stage_id}`} key={s.stage_id}>
          <div className="stage-h">
            <span className="k">阶段 {String(s.order_index + 1).padStart(2, "0")} · {s.course_count} 门课程</span>
            <h2 id={`st-${s.stage_id}`}>{s.title}</h2>
          </div>
          <ol className="crs">
            {s.courses?.map((c: any) => (
              <li key={c.course_id} id={c.outline_confirmed ? "c-ready" : "c-act"}>
                <span className="ord">#{String(c.order_index + 1).padStart(2, "0")}</span>
                <div>
                  <h3><a href={`#/course/${c.course_id}`}>{c.title || c.course_id}</a></h3>
                  {c.goal_snapshot && <p className="goal">{c.goal_snapshot}</p>}
                  <div className="meta-l">
                    <span>course_id={c.course_id.slice(0, 12)}</span>
                  </div>
                </div>
                <div className="ost">
                  {c.outline_confirmed
                    ? <span className="ol-st ol-ready">大纲已确认</span>
                    : <span className="ol-st ol-pending">未确认大纲</span>}
                  {c.outline_confirmed && (
                    <span className="sub">已开始 <b className="num">{c.progress?.lesson_started ?? 0} / {c.progress?.lesson_total ?? 0}</b> 讲</span>
                  )}
                </div>
                <div className="next">
                  {c.outline_confirmed ? (
                    <>
                      <a className="btn btn-pri" href={`#/course/${c.course_id}`}>进入课程</a>
                      <span className="why">大纲已确认，可以查看讲次与准备状态。</span>
                    </>
                  ) : (
                    <>
                      <button className="btn btn-draft" onClick={() => startOutlineFor(c.course_id)}>生成课程大纲</button>
                      <span className="why">确认大纲后才能开始学习；先修不等于已掌握。</span>
                    </>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </section>
      ))}
    </div>
  );
}

export function CourseOverview({ courseId, onErr, onOk, go }: {
  courseId: string; onErr: (s: string) => void; onOk: (s: string) => void; go: (p: string) => void;
}) {
  const [overview, setOverview] = useState<any>(null);

  const load = useCallback(async () => {
    try {
      setOverview(await api.courseOverview(courseId));
      api.putNavigation({ course_id: courseId }).catch(() => {});
    } catch (e: any) {
      onErr(e?.message || "课程总览加载失败");
    }
  }, [courseId, onErr]);

  useEffect(() => { load(); }, [load]);

  if (!overview) return <p className="muted">加载中…</p>;

  const start = async (lessonId: string, startable: boolean, reason: string) => {
    if (!startable) { onErr(reason); return; }
    try {
      const r = await api.startLessonV2(courseId, lessonId);
      // 计时起点 = 本次点击；首段生成任务已创建
      if (r.conversation_id) {
        sessionStorage.setItem("slice4.pendingTask", r.task_id);
        onOk("首段生成中，进入讲次工作区。");
        go(`/lecture/${r.conversation_id}`);
      } else {
        onOk("已请求开始学习。");
      }
    } catch (e: any) {
      onErr(e?.code === "LESSON_UNLOCK_REQUIRED" ? "未确认大纲，暂不能开始学习。" : e?.message || "操作失败");
    }
  };

  const allLessons = (overview.units || []).flatMap((u: any) => u.lessons || []);
  const lessonTotal = overview.progress?.lesson_total ?? allLessons.length;
  const lessonStarted = overview.progress?.lesson_started ?? 0;
  const pct = lessonTotal > 0 ? Math.min(100, Math.round((lessonStarted / lessonTotal) * 100)) : 0;
  const next = allLessons.find(
    (l: any) => l.learning_state !== "user_completed" && l.startable && overview.teaching_unlocked,
  );
  const nextUnit = next && (overview.units || []).find((u: any) => u.lessons?.some((l: any) => l.lesson_id === next.lesson_id));
  const title = overview.title || courseId;

  return (
    <div className="ccourse">
      <aside className="c-info">
        <button className="btn btn-sm" onClick={() => go("/courses")}>← 返回课程</button>
        <div className={`ccover ${["tone-a", "tone-b", "tone-c", "tone-d", "tone-e"][courseId.length % 5]}`}>
          <span className="monogram" aria-hidden="true">{title.trim().charAt(0).toUpperCase()}</span>
        </div>
        <span className="kicker">课程 · {overview.scenario === "school" ? "学校课程" : "自学"}</span>
        <h1 className="serif" style={{ fontSize: 26, lineHeight: 1.4, margin: 0 }}>{title}</h1>
        <div className="blk">
          <span className="k">大纲版本</span>
          {overview.outline_confirmed ? (
            <span className="ver ver-conf">已确认 v{overview.version_no}</span>
          ) : (
            <>
              <span className="ver ver-draft">草稿 · 从未确认</span>
              <span className="help">确认之前，这门课只能编辑大纲，不能开始学习。</span>
            </>
          )}
        </div>
        {overview.outline_confirmed && (
          <div className="blk c-prog">
            <span className="k">课程进度</span>
            <div className="bigpct">{pct}% <small>已开始 {lessonStarted}/{lessonTotal} 讲</small></div>
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
        <div className="blk">
          <button className="btn btn-sm" onClick={() => go("/planning")}>去调整规划</button>
          <span className="help">调整大纲会产生或编辑草稿，不会覆盖已确认的内容。</span>
        </div>
      </aside>

      <section>
        {!overview.outline_confirmed && (
          <div className="note note-draft" role="note" style={{ marginBottom: 24 }}>
            <span className="tag">草稿</span>
            <div className="tt">这份大纲还没确认</div>
            <div>确认之后才能开始学习；确认不会启动整课的讲解生成或语音合成。</div>
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
              <button className="nextpill" onClick={() => start(next.lesson_id, true, "")}>
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

        {(overview.units || []).map((u: any, ui: number) => (
          <div key={u.unit_id} id={`unit-${u.unit_id}`} style={{ marginBottom: 28 }}>
            <div className="unit-h">
              <span className="k">第 {ui + 1} 单元，共 {(overview.units || []).length} 单元 · {u.lesson_count} 讲</span>
              <h2 className="h2 serif">{u.title}</h2>
            </div>
            <ol className="lecs">
              {u.lessons?.map((l: any) => {
                const startable = overview.teaching_unlocked && l.startable;
                const why = !overview.teaching_unlocked
                  ? "未确认大纲，不能学习"
                  : !l.startable
                    ? (l.status_preparation === "failed" ? "上次生成失败，可以重试" : "讲解还没生成好")
                    : "进入后从第 1 步开始生成";
                return (
                  <li className={`lec${overview.teaching_unlocked ? "" : " locked"}`} key={l.lesson_id}>
                    <span className="no">{String(l.order_index + 1).padStart(2, "0")}</span>
                    <div>
                      <h3>{l.title || l.goal || l.lesson_id}</h3>
                      {l.prereq_arranged && <p className="desc">有先修讲次；安排过先修 ≠ 已掌握。</p>}
                      <div className="states">
                        <PrepState status={l.status_preparation} />
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
                        onClick={() => start(l.lesson_id, startable,
                          overview.teaching_unlocked ? "该讲次尚未就绪。" : "未确认大纲，禁止启动教学。")}
                      >
                        {overview.teaching_unlocked ? (l.startable ? "开始学习" : "未就绪") : "未确认，不能学习"}
                      </button>
                      <span className="why">{why}</span>
                    </div>
                  </li>
                );
              })}
            </ol>
          </div>
        ))}
      </section>
    </div>
  );
}
