import { useCallback, useState } from "react";
import { api } from "../api";
import { useEventStream } from "../hooks/useEventStream";
import { errText } from "../lib/errors";
import Badge from "../ui/Badge";

/**
 * 任务状态页（切片 0 演示页，保留模板化事件流）。
 * 原 App 内联 HomeView 更名迁移；SSE 断线 410 回退 REST 全量状态。
 */
export default function TasksView({ me, onErr, onOk, onNeedLogin }: any) {
  const [taskId, setTaskId] = useState("");
  const [status, setStatus] = useState<any>(null);
  const [events, setEvents] = useState<any[]>([]);
  const { runStream } = useEventStream();

  const loadStatus = useCallback(async (id: string) => {
    try {
      const s = await api.taskStatus(id);
      setStatus(s);
    } catch (e) { onErr(errText(e)); }
  }, [onErr]);

  const openSse = useCallback(async (id: string) => {
    try {
      await runStream(
        (signal) => api.taskSse(id, undefined, signal),
        (frame) => {
          const evt = frame.data;
          setEvents((prev) => [...prev, evt]);
          setStatus((s: any) =>
            s
              ? { ...s, status: evt.status, stage: evt.stage, revision: (s.revision || 0) }
              : { status: evt.status, stage: evt.stage }
          );
        },
        { onExpired: () => loadStatus(id) },
      );
    } catch (e) {
      if ((e as Error).name !== "AbortError") onErr(errText(e));
    }
  }, [loadStatus, onErr, runStream]);

  return (
    <div className="page-w">
      <div className="phead">
        <span className="kicker">任务</span>
        <div className="row"><h1 className="h1">任务状态</h1></div>
        <p className="lede">{me ? (me.is_guest ? "访客模式" : `已登录：${me.email}`) : "未登录。请先登录后再创建任务。"}</p>
      </div>
      {!me && <button className="btn btn-pri" onClick={onNeedLogin}>去登录</button>}
      <section className="card stack">
        <div className="row">
          <button
            className="btn"
            disabled={!me}
            onClick={async () => {
              try {
                const r = await api.createTask({
                  idempotency_key: `web-${Date.now()}`,
                  kind: "generic",
                  subject_id: "demo_subject",
                  prompt_name: "lesson_step_v1",
                  allow_fallback: true,
                });
                setTaskId(r.task_id);
                onOk(r.prompt_binding_id ? `任务已创建 ${r.task_id}` : `任务已创建，但 ${r.prompt_unavailable || "提示词不可用"}`);
                await loadStatus(r.task_id);
                await openSse(r.task_id);
              } catch (e) { onErr(errText(e)); }
            }}
          >
            创建最简任务
          </button>
          <input
            className="inp"
            style={{ maxWidth: 260 }}
            placeholder="task_id"
            value={taskId}
            onChange={(e) => setTaskId(e.target.value)}
            aria-label="任务 ID"
          />
          <button className="btn" disabled={!taskId} onClick={() => loadStatus(taskId)}>刷新状态</button>
          <button className="btn" disabled={!taskId} onClick={() => openSse(taskId)}>订阅 SSE</button>
        </div>
      </section>

      {status && (
        <section className="card stack">
          <div className="row">
            <Badge status={status.status || "queued"} />
            <span className="idtag">stage={status.stage || "-"}</span>
            <span className="idtag">revision={status.revision ?? "-"}</span>
          </div>
          <span className="idtag">task_id={status.task_id || taskId}</span>
        </section>
      )}

      {events.length > 0 && (
        <section className="card stack">
          <h2 className="h3">事件（模板化）</h2>
          <div className="tscroll">
            <table className="table">
              <thead>
                <tr><th>序号</th><th>阶段</th><th>状态</th><th>错误码</th></tr>
              </thead>
              <tbody>
                {events.map((e, i) => (
                  <tr key={e.event_id || i}>
                    <td className="num">{e.sequence}</td>
                    <td>{e.stage}</td>
                    <td><Badge status={e.status} /></td>
                    <td className="idtag">{e.error_code || "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}
