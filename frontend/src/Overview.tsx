import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import ReadonlyCanvas from "./excalidraw/ReadonlyCanvas";

/**
 * 切片 3 T07：路线总览与课程总览。
 * - 与导图同版（同一已确认版本读取）；准备状态标签按切片 0 视觉规则；
 * - 不输出路线整体百分比；课程进度按当前已确认大纲讲次等权（本切片全为未开始）；
 * - 未确认大纲或讲次未就绪：「开始学习」禁用并显示原因（教学启动行为归切片 4）。
 */

const PREP_LABEL: Record<string, { text: string; cls: string }> = {
  not_generated: { text: "未生成", cls: "queued" },
  generating: { text: "生成中", cls: "running" },
  ready: { text: "已就绪", cls: "succeeded" },
  failed: { text: "失败", cls: "failed" },
};

function PrepBadge({ status }: { status: string }) {
  const meta = PREP_LABEL[status] || PREP_LABEL.not_generated;
  return <span className={`badge ${meta.cls}`}>{meta.text}</span>;
}

export function RouteOverview({ routeId, onErr }: { routeId: string; onErr: (s: string) => void }) {
  const [overview, setOverview] = useState<any>(null);
  const [scene, setScene] = useState<any>(null);

  const load = useCallback(async () => {
    try {
      setOverview(await api.routeOverview(routeId));
      try {
        setScene(await api.routeScene(routeId));
      } catch {
        setScene(null);
      }
      api.putNavigation({ route_id: routeId }).catch(() => {});
    } catch (e: any) {
      onErr(e?.message || "路线总览加载失败");
    }
  }, [onErr, routeId]);

  useEffect(() => {
    load();
  }, [load]);

  if (!overview) return <div className="card stack"><p className="lede">加载中…</p></div>;

  return (
    <div className="stack">
      <div className="card stack">
        <h1>路线总览</h1>
        <p className="lede">
          版本 v{overview.version_no ?? "-"}（已确认）· 阶段 {overview.stages?.length ?? 0} 个。
          学习顺序以层级与先修关系为准。
        </p>
        {overview.stages?.map((s: any) => (
          <div key={s.stage_id} className="stack" style={{ borderLeft: "3px solid var(--border)", paddingLeft: "0.75rem" }}>
            <h3>{s.title}</h3>
            <ul style={{ margin: 0, paddingLeft: "1.2rem" }}>
              {s.courses?.map((c: any) => (
                <li key={c.course_id}>
                  <a href={`#/course/${c.course_id}`}>{c.title || c.course_id}</a>{" "}
                  <span className={`badge ${c.outline_confirmed ? "succeeded" : "queued"}`}>
                    {c.outline_confirmed ? "大纲已确认" : "未确认大纲"}
                  </span>
                  <span className="help"> 已开始 {c.progress?.lesson_started ?? 0}/{c.progress?.lesson_total ?? 0} 讲</span>
                </li>
              ))}
            </ul>
          </div>
        ))}
        <div className="row mono">route_id={routeId}</div>
      </div>
      <div className="card stack">
        <h2>路线导图（与总览同版）</h2>
        {scene ? <ReadonlyCanvas elements={scene.elements} /> : <p className="lede">尚无已确认路线版本。</p>}
      </div>
    </div>
  );
}

export function CourseOverview({ courseId, onErr, onOk }: { courseId: string; onErr: (s: string) => void; onOk: (s: string) => void }) {
  const [overview, setOverview] = useState<any>(null);

  const load = useCallback(async () => {
    try {
      setOverview(await api.courseOverview(courseId));
      api.putNavigation({ course_id: courseId }).catch(() => {});
    } catch (e: any) {
      onErr(e?.message || "课程总览加载失败");
    }
  }, [courseId, onErr]);

  useEffect(() => {
    load();
  }, [load]);

  if (!overview) return <div className="card stack"><p className="lede">加载中…</p></div>;

  const start = async (lessonId: string, startable: boolean, reason: string) => {
    if (!startable) {
      onErr(reason);
      return;
    }
    try {
      const r = await api.startLesson(courseId, lessonId);
      // 切片 3 只交付门槛校验；首段教学生成由切片 4 提供
      onOk(r.reason === "teaching_generation_in_slice4"
        ? "大纲已确认；教学生成将在后续版本开放。"
        : "已请求开始学习。");
    } catch (e: any) {
      onErr(e?.code === "LESSON_UNLOCK_REQUIRED" ? "未确认大纲，暂不能开始学习。" : e?.message || "操作失败");
    }
  };

  return (
    <div className="stack">
      <div className="card stack">
        <h1>课程总览</h1>
        <p className="lede">
          版本 v{overview.version_no ?? "-"} ·{" "}
          <span className={`badge ${overview.outline_confirmed ? "succeeded" : "failed"}`}>
            {overview.outline_confirmed ? "大纲已确认" : "未确认大纲（禁止启动教学）"}
          </span>
          {" "}· 已开始 {overview.progress?.lesson_started ?? 0}/{overview.progress?.lesson_total ?? 0} 讲
        </p>
        {overview.source_route_version_id && (
          <p className="help">本课程由路线展开生成；安排过先修 ≠ 已掌握，请按先修顺序学习。</p>
        )}
        {overview.units?.map((u: any) => (
          <div key={u.unit_id} className="stack" style={{ borderLeft: "3px solid var(--border)", paddingLeft: "0.75rem" }}>
            <h3>{u.title}</h3>
            <ul style={{ margin: 0, paddingLeft: "1.2rem" }}>
              {u.lessons?.map((l: any) => (
                <li key={l.lesson_id}>
                  <span className="mono">第 {l.order_index + 1} 讲</span>{" "}
                  {l.goal || l.lesson_id} <PrepBadge status={l.status_preparation} />
                  {l.prereq_hint && (
                    <span className="badge running" title="安排过先修 ≠ 已掌握">有先修</span>
                  )}
                  <button
                    className="secondary"
                    style={{ marginLeft: "0.5rem" }}
                    disabled={!overview.teaching_unlocked || !l.startable}
                    aria-disabled={!overview.teaching_unlocked || !l.startable}
                    title={!overview.teaching_unlocked
                      ? "未确认大纲，禁止启动教学"
                      : !l.startable ? "讲次未就绪（未生成/生成中/失败）" : "开始学习"}
                    onClick={() => start(
                      l.lesson_id, overview.teaching_unlocked && l.startable,
                      overview.teaching_unlocked ? "该讲次尚未就绪。" : "未确认大纲，禁止启动教学。",
                    )}
                  >
                    开始学习
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ))}
        <div className="row mono">course_id={courseId}</div>
      </div>
      <div className="card stack">
        <h2>大纲导图（与总览同版）</h2>
        <SceneLoader courseId={courseId} />
      </div>
    </div>
  );
}

function SceneLoader({ courseId }: { courseId: string }) {
  const [scene, setScene] = useState<any>(null);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    (async () => {
      try {
        setScene(await api.courseScene(courseId));
      } catch {
        setScene(null);
      }
      setLoaded(true);
    })();
  }, [courseId]);
  if (!loaded) return <p className="lede">加载中…</p>;
  return scene ? <ReadonlyCanvas elements={scene.elements} /> : <p className="lede">尚无已确认大纲版本。</p>;
}
