import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

/**
 * 讲次教学工作区（切片 4 T16，设计稿 lw-simple 布局）：
 * - 工具栏：退出、当前讲次、保存状态；板书列 + 文字列；窄屏切换；
 * - 播放控制器：单一 HTMLAudioElement；暂停本地立即并冻结板书时钟；
 *   无音频/无字幕 → 无音频手动模式（明确显示）；
 * - 检查点：播放期间每 5 秒 + 步骤切换 + 追问前（client_seq 单调递增）；
 * - 追问：暂停预生成 + 一次性恢复点；回答后 awaiting_resume，点继续才恢复。
 */

type Step = {
  step_no: number; step_id: string; title: string; text: string; status: string;
  text_version_no: number; board: any | null; audio_id: string | null; audio_ready: boolean;
};
type Workspace = {
  conversation_id: string; kind: string; course_id: string | null; lesson_id: string | null;
  generation: number; session_state: string; restore_point: any;
  last_checkpoint: { client_seq: number; step_no: number; position_ms: number } | null;
  steps: Step[];
  followups: { generation: number; role: string; content: string }[];
};

export default function LectureView({ conversationId, onErr, onOk, go }: {
  conversationId: string; onErr: (s: string) => void; onOk: (s: string) => void; go: (p: string) => void;
}) {
  const [ws, setWs] = useState<Workspace | null>(null);
  const [current, setCurrent] = useState(1);
  const [playing, setPlaying] = useState(false);
  const [noAudio, setNoAudio] = useState(false);
  const [pane, setPane] = useState<"board" | "text">("board");
  const [followup, setFollowup] = useState("");
  const [busy, setBusy] = useState(false);
  const [pendingTask, setPendingTask] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState("");
  const seqRef = useRef(0);
  const posRef = useRef(0);

  const refresh = useCallback(async () => {
    try {
      const w = await api.getWorkspace(conversationId);
      setWs(w);
      seqRef.current = Math.max(seqRef.current, w.last_checkpoint?.client_seq ?? 0);
      if (w.session_state === "awaiting_resume") setPlaying(false);
    } catch (e: any) {
      onErr(e?.message || "教学工作区加载失败");
    }
  }, [conversationId, onErr]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // 唤起待执行任务（启动后的首段/追问回答由服务端 worker 池或本入口执行）
  useEffect(() => {
    const t = sessionStorage.getItem("slice4.pendingTask");
    if (t) {
      sessionStorage.removeItem("slice4.pendingTask");
      setPendingTask(t);
    }
  }, []);

  const runPending = useCallback(async () => {
    if (!pendingTask) return;
    setBusy(true);
    try {
      await api.runTeachingTask(pendingTask);
      await refresh();
      onOk("本段已生成。");
    } catch (e: any) {
      onErr(e?.message || "生成失败");
    } finally {
      setBusy(false);
      setPendingTask(null);
    }
  }, [onErr, onOk, pendingTask, refresh]);

  const saveCheckpoint = useCallback(async (stepNo: number, positionMs: number) => {
    seqRef.current += 1;
    try {
      await api.saveCheckpoint(conversationId, {
        client_seq: seqRef.current, step_no: stepNo, position_ms: positionMs,
      });
      setSavedAt(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
    } catch (e: any) {
      if (e?.code === "CHECKPOINT_STALE") {
        onErr("位置保存冲突：已按服务端最新位置校准。");
      } else {
        onErr("位置保存失败（本地继续播放）。");
      }
    }
  }, [conversationId, onErr]);

  // 播放期间每 5 秒保存一次
  useEffect(() => {
    if (!playing || !ws) return;
    const timer = window.setInterval(() => {
      saveCheckpoint(current, posRef.current);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [playing, current, saveCheckpoint, ws]);

  const gotoStep = useCallback(async (n: number) => {
    if (!ws) return;
    const target = ws.steps.find((s) => s.step_no === n);
    if (!target) return;
    if (playing) {
      // 切换步骤先暂停（本地立即）并保存位置
      setPlaying(false);
      await saveCheckpoint(current, posRef.current);
    }
    setCurrent(n);
    posRef.current = 0;
  }, [current, playing, saveCheckpoint, ws]);

  const togglePlay = useCallback(async () => {
    if (!ws) return;
    if (playing) {
      setPlaying(false); // 本地立即暂停；异步持久化
      saveCheckpoint(current, posRef.current);
      return;
    }
    const step = ws.steps.find((s) => s.step_no === current);
    if (step && !step.audio_ready) {
      setNoAudio(true); // 无音频手动模式（明确显示）
      setPlaying(true);
      return;
    }
    setPlaying(true);
    setNoAudio(false);
  }, [current, playing, saveCheckpoint, ws]);

  const sendFollowup = useCallback(async () => {
    if (!followup.trim() || !ws) return;
    setPlaying(false);
    await saveCheckpoint(current, posRef.current); // 追问前保存位置
    setBusy(true);
    try {
      const r = await api.sendFollowup(conversationId, {
        content: followup, idempotency_key: `web-fu-${Date.now()}`,
      });
      setFollowup("");
      if (!r.deduped) {
        await api.runTeachingTask(r.task_id); // 本地开发直接执行回答
      }
      await refresh();
      onOk("回答完成；原课程已暂停，点击继续课程恢复。");
    } catch (e: any) {
      onErr(e?.message || "追问失败");
    } finally {
      setBusy(false);
    }
  }, [conversationId, current, followup, onErr, onOk, refresh, saveCheckpoint, ws]);

  if (!ws) return <p className="muted">加载中…</p>;

  const step = ws.steps.find((s) => s.step_no === current)
    || ws.steps.find((s) => s.step_no === Math.min(...(ws.steps.map((x) => x.step_no) || [1])))
    || ws.steps[0];
  const boardScene = step?.board;
  const originalRefs = boardScene?.original_refs || [];
  const awaiting = ws.session_state === "awaiting_resume";
  const nextStepNo = current + 1;
  const hasNext = ws.steps.some((s) => s.step_no === nextStepNo);

  return (
    <div className="lw-simple">
      <div className="lecture-toolbar">
        <button className="lecture-exit" onClick={() => go(`/course/${ws.course_id}`)}>
          ← <span>退出讲次</span>
        </button>
        <div className="lecture-current">
          <b className="serif">{step?.title || "讲次教学"}</b>
          <span>第 {current} 步 · 共 {ws.steps.length || "…"} 步</span>
        </div>
        {ws.steps.length > 0 && (
          <span className="idtag" aria-label="讲次进度">{Math.round((current / ws.steps.length) * 100)}%</span>
        )}
        <span className="lecture-zoom">{playing ? "播放中" : "已暂停"}</span>
        <span className="lecture-spacer" />
        {savedAt && <span className="help">位置已保存 {savedAt}</span>}
        <div className="row" style={{ gap: 8 }}>
          <button className="btn btn-sm" onClick={togglePlay} disabled={busy}>
            {playing ? "暂停" : noAudio ? "手动推进" : "播放"}
          </button>
          <button className="btn btn-sm" onClick={() => gotoStep(current - 1)} disabled={current <= 1}>‹ 上一步</button>
          <button
            className="btn btn-sm"
            onClick={async () => {
              await saveCheckpoint(current, posRef.current);
              if (hasNext) await gotoStep(nextStepNo);
              else onOk("已是最后一段；后续步骤在播放中预生成。");
            }}
          >
            下一步 ›
          </button>
          {pendingTask && <button className="btn btn-sm btn-pri" onClick={runPending} disabled={busy}>生成下一段</button>}
        </div>
      </div>

      {noAudio && (
        <div style={{ padding: "10px 22px 0" }}>
          <div className="note note-warn" role="status">
            <span className="tag">无音频</span>
            <div className="tt">本段没有可播放音频</div>
            <div>音频未配置、未生成或缺字幕时，课程由你手动推进；文字与板书照常可读。</div>
          </div>
        </div>
      )}
      {awaiting && (
        <div style={{ padding: "10px 22px 0" }}>
          <div className="note note-accent" role="status">
            <span className="tag">待恢复</span>
            <div className="tt">追问已回答 · 原课程暂停在恢复点</div>
            <div>恢复点：第 {ws.restore_point?.step_no ?? 0} 步 · {ws.restore_point?.position_ms ?? 0}ms（连续追问沿用最初恢复点）。</div>
            <div className="acts">
              <button className="btn btn-sm btn-pri" onClick={async () => {
                await api.resumeLesson(conversationId);
                await refresh();
                onOk("已恢复；点击播放继续。");
              }}>继续课程 · 不自动播放</button>
            </div>
          </div>
        </div>
      )}

      <div className="lw-body" data-pane={pane}>
        <div className="bd-col">
          <div className="bd-tabs" role="tablist" aria-label="板书与文字切换">
            <button role="tab" aria-selected={pane === "board"} onClick={() => setPane("board")}>板书</button>
            <button role="tab" aria-selected={pane === "text"} onClick={() => setPane("text")}>文字</button>
          </div>
          <div className="board">
            <div className="board-inner">
              {boardScene ? (
                <>
                  <div className="canvas-readonly-wrap">
                    {/* 板书场景由服务端结构化数据受控转换；播放控制器按 reveal_ms 呈现 */}
                    <ReadonlyBoard elements={boardScene.elements} />
                  </div>
                  {originalRefs.length > 0 && (
                    <div className="od-stack" style={{ marginTop: 12 }}>
                      <span className="kicker">原图（讲解依据，必须呈现）</span>
                      {originalRefs.map((r: any) => (
                        <img
                          key={r.figure_id}
                          className="od-media"
                          src={api.figureUrl(r.material_id, r.figure_id)}
                          alt={r.caption || "结构图原图"}
                          style={{ maxWidth: 480, borderRadius: 8, border: "1px solid var(--rule)" }}
                        />
                      ))}
                    </div>
                  )}
                </>
              ) : (
                <p className="muted">本段板书生成中或未生成；文字先行可读。</p>
              )}
            </div>
          </div>
        </div>

        <div className="tx-col">
          <div className="stream" role="log" aria-label="讲解文字流">
            {[...ws.steps].sort((a, b) => a.step_no - b.step_no).map((s) => (
              <section
                key={s.step_id}
                className={`tx-step${s.step_no === current ? " cur" : ""}`}
                id={`tx-${s.step_no}`}
                onClick={() => gotoStep(s.step_no)}
                style={{ cursor: "pointer" }}
              >
                <div className="h" style={{ display: "flex", gap: 12, alignItems: "baseline", flexWrap: "wrap" }}>
                  <b>第 {s.step_no} 步 · {s.title}</b>
                  <span className={`badge ${s.status === "ready" ? "succeeded" : "running"}`}>
                    {s.status === "ready" ? "文字就绪" : "生成中"}
                  </span>
                  {s.audio_ready && <span className="badge queued">音频就绪</span>}
                </div>
                <p style={{ maxWidth: "64ch", lineHeight: 1.75 }}>{s.text}</p>
              </section>
            ))}
            {ws.followups?.map((f, i) => (
              <section key={`${f.generation}-${i}`} className={`tx-step ${f.role === "user" ? "me" : "cur"}`}>
                <div className="h" style={{ display: "flex", gap: 12, alignItems: "baseline" }}>
                  <b>{f.role === "user" ? "我的追问" : "学伴回答"} · 第 {f.generation} 轮</b>
                </div>
                <p style={{ maxWidth: "64ch", lineHeight: 1.75 }}>{f.content}</p>
              </section>
            ))}
            {ws.steps.length === 0 && <p className="muted">首段生成中；文字一好就能读。</p>}
          </div>
          <div className="composer2" style={{ padding: "12px 20px 18px", boxShadow: "0 -8px 20px rgba(20,30,45,.06)" }}>
            <div className="box">
              <label className="sr" htmlFor="fu">课程追问</label>
              <textarea
                className="ta"
                id="fu"
                placeholder="针对当前讲次追问，比如：这一步的例子换个场景还成立吗？"
                value={followup}
                maxLength={1000}
                onChange={(e) => setFollowup(e.target.value)}
              />
              <button className="btn btn-pri" onClick={sendFollowup} disabled={busy || !followup.trim()}>追问</button>
            </div>
            <div className="hint">
              <span>输入不会打断讲解；发送后才会暂停并创建恢复点，回答后点「继续课程 · 不自动播放」。</span>
              <span><kbd>位置</kbd> 每 5 秒自动保存</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/** 板书只读画布（独立懒加载 Excalidraw；加载失败降级为文字讲解） */
function ReadonlyBoard({ elements }: { elements: any[] }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [mod, ReactMod, ReactDOMMod] = await Promise.all([
          import("@excalidraw/excalidraw"),
          import("react"),
          import("react-dom/client"),
          import("@excalidraw/excalidraw/index.css"),
        ]);
        if (cancelled || !ref.current) return;
        const restored = mod.restore(
          { elements, appState: { viewBackgroundColor: "#fbfcfd" }, files: undefined },
          null, null, { repairBindings: false, refreshDimensions: true },
        );
        const root = ReactDOMMod.createRoot(ref.current);
        root.render(
          ReactMod.createElement(mod.Excalidraw, {
            initialData: restored,
            viewModeEnabled: true,
            gridModeEnabled: false,
          }),
        );
      } catch {
        if (!cancelled) setErr("板书画布加载失败；文字讲解不受影响。");
      }
    })();
    return () => { cancelled = true; };
  }, [elements]);
  if (err) return <div className="notice">{err}</div>;
  return <div ref={ref} style={{ height: 480 }} />;
}
