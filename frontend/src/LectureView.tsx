import { useCallback, useEffect, useRef, useState } from "react";
import { errText } from "./lib/errors";
import { api } from "./api";
import { useTeachingBuffer } from "./hooks/useTeachingBuffer";
import {lessonAudioRequired,useLessonAudio} from "./hooks/useLessonAudio";
import LectureSplit from "./ui/LectureSplit";
import TeachingBoard from "./ui/TeachingBoard";
import { stageBoard, paragraphs } from "./lib/stageBoard";

/**
 * 连续教学工作区：共享文字／板书呈现时钟、暂停／继续与自动续讲；
 * 检查点保存呈现游标，追问保存一次性恢复点，完成仍由用户确认。
 */

type Step = {
  step_no: number; step_id: string; title: string; text: string; status: string;
  board_scope?: string; stage_no?: number; stage_segment_no?: number; stage_finished?: boolean;
  phase?: string; lesson_finished?: boolean; text_version_no: number; board: any | null; audio_id: string | null; audio_ready: boolean;
  audio_status?:string;audio_error?:string;audio_subtitles?:any[];
};
type Workspace = {
  conversation_id: string; kind: string; course_id: string | null; lesson_id: string | null;
  generation: number; session_state: string; restore_point: any;
  lesson_title?: string; planned_steps?: number; lesson_finished?: boolean;
  queued_tasks?: { task_id: string; kind: string; status: string; step_no: number; followup?: boolean; error_code?: string; deadline_at?: string; required_action?: string }[];
  authorization_required?: boolean; authorization_error?: string; required_action?: string;
  needs_upgrade?: boolean;
  tts_authorized?:boolean;
  audio_mode?:"text"|"tts";
  artifact_recovery?:{status?:"missing"|"repairing"|"exhausted";available?:boolean;required?:boolean;error_code?:string;missing_steps?:number[];step_no?:number;attempts?:number;max_attempts?:number}|boolean;
  server_managed?:boolean;
  tts_config?:{config_id:string;config_version:number}|null;
  last_checkpoint: { client_seq: number; step_no: number; position_ms: number; saved_at?: string;
    presentation?: {visible_chars?:number;paused?:boolean;text_complete?:boolean;audio_id?:string} } | null;
  steps: Step[];
  followups: { generation: number; role: string; content: string }[];
  followup_board?: any;
};

export default function LectureView({ conversationId, onErr, onOk, go }: {
  conversationId: string; onErr: (s: string) => void; onOk: (s: string) => void; go: (p: string) => void;
}) {
  const [ws, setWs] = useState<Workspace | null>(null);
  const [current, setCurrent] = useState(1);
  const [advanceTo,setAdvanceTo] = useState<number|null>(null);
  const [playing, setPlaying] = useState(true);
  const playingRef = useRef(true);
  const [pane, setPane] = useState<"board" | "text">("board");
  const [followup, setFollowup] = useState("");
  const [busy, setBusy] = useState(false);
  const attemptedTasks = useRef(new Set<string>());
  const [pendingTask, setPendingTask] = useState<string | null>(null);
  const [followupTask, setFollowupTask] = useState<string | null>(null);
  const [previews, setPreviews] = useState<Record<string, any>>({});
  const subscriptions = useRef(new Map<string, AbortController>());
  const [savedAt, setSavedAt] = useState("");
  const seqRef = useRef(0);
  const posRef = useRef(0);
  const restoredPosition = useRef(false);
  const latestConversation = useRef(conversationId); latestConversation.current=conversationId;
  useEffect(()=>{restoredPosition.current=false;seqRef.current=0;posRef.current=0;setSavedAt("");
    playingRef.current=true;setPlaying(true);setCurrent(1);setAdvanceTo(null);setWs(null);setPreviews({});
    setPendingTask(null);setBusy(false);setFollowupTask(null);attemptedTasks.current.clear();},[conversationId]);
  const bufferRef=useRef<any>(null);
  const audioStep=ws?.steps.find(step=>step.step_no===current);
  const audioRequired=lessonAudioRequired(ws,audioStep,current);
  const bufferWithAudio = useTeachingBuffer(conversationId, ws, previews, current, playing, audioRequired);
  bufferRef.current=bufferWithAudio;
  const buffer=bufferWithAudio;
  const audio=useLessonAudio(conversationId,current,audioStep,playing,bufferRef,posRef,ws?.last_checkpoint,audioRequired);
  const audioRef=useRef(audio);audioRef.current=audio;
  const audioAttempts=useRef(new Set<string>()),audioRequests=useRef(new Set<string>());
  const [audioFailure,setAudioFailure]=useState("");
  const textScroller = useRef<HTMLDivElement>(null);
  const textScrollFrame = useRef<number | null>(null);
  const followLatest = useRef(true);
  const [readingHistory,setReadingHistory] = useState(false);
  useEffect(() => {
    const node = textScroller.current;
    if (!(playing || followupTask) || !followLatest.current || !node) return;
    if (textScrollFrame.current !== null) cancelAnimationFrame(textScrollFrame.current);
    textScrollFrame.current = requestAnimationFrame(() => {
      textScrollFrame.current = null;
      if (followLatest.current && textScroller.current) textScroller.current.scrollTop = textScroller.current.scrollHeight;
    });
    return () => {
      if (textScrollFrame.current !== null) {
        cancelAnimationFrame(textScrollFrame.current);
        textScrollFrame.current = null;
      }
    };
  }, [playing, followupTask, previews, ws?.steps.length, ws?.followups?.length, current]);

  const refresh = useCallback(async () => {
    try {
      const w = await api.getWorkspace(conversationId);
      if(latestConversation.current!==conversationId)return;
      if(w.needs_upgrade) { setWs(w); return; }
      if (!restoredPosition.current) {
        restoredPosition.current=true;
        if(w.last_checkpoint?.step_no>0)setCurrent(w.last_checkpoint.step_no);
        posRef.current=w.last_checkpoint?.position_ms||0;
        const running=w.session_state==="active" && !w.last_checkpoint?.presentation?.paused;
        playingRef.current=running;setPlaying(running);bufferRef.current.pause(!running);
        if(w.last_checkpoint?.saved_at)setSavedAt(new Date(w.last_checkpoint.saved_at).toLocaleTimeString("zh-CN",{hour12:false}));
      }
      setWs(w);
      seqRef.current = Math.max(seqRef.current, w.last_checkpoint?.client_seq ?? 0);
      if (w.session_state !== "active") {playingRef.current=false;setPlaying(false);bufferRef.current.pause(true);}
    } catch (e: any) {
      onErr(e?.message || "教学工作区加载失败");
    }
  }, [conversationId, onErr, go]);



  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    const timer = window.setInterval(refresh, 1500);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    const eligible = (ws?.queued_tasks || []).filter(t =>
      t.kind === "lesson_step_generate" && ["queued", "leased", "running"].includes(t.status) &&
      (t.followup || (ws?.session_state === "active" && t.step_no <= current + 1)));
    if (followupTask && !eligible.some(t => t.task_id === followupTask))
      eligible.push({ task_id: followupTask, kind: "lesson_step_generate", status: "running", step_no: 0, followup: true });
    const ids = new Set(eligible.map(t => t.task_id));
    for (const [id, controller] of subscriptions.current) {
      if (!ids.has(id)) { controller.abort(); subscriptions.current.delete(id); }
    }
    setPreviews(old => Object.fromEntries(Object.entries(old).filter(([id]) => ids.has(id))));
    for (const task of eligible) {
      if (subscriptions.current.has(task.task_id)) continue;
      const controller = new AbortController();
      subscriptions.current.set(task.task_id, controller);
      api.streamTeachingContent(task.task_id, event => {
        if (controller.signal.aborted) return;
        if (event.event === "snapshot") setPreviews(old => ({ ...old, [task.task_id]: event }));
        else if (event.event === "status" || event.event === "error") {
          setPreviews(old => { const next = { ...old }; delete next[task.task_id]; return next; });
        }
        if (event.event === "done" || event.event === "error") refresh();
      }, controller.signal).catch(() => {
        // The ordinary workspace poll still delivers validated final content.
        if (!controller.signal.aborted) setPreviews(old => {
          const next = { ...old }; delete next[task.task_id]; return next;
        });
      });
    }
  }, [ws, current, refresh, followupTask]);

  useEffect(() => () => {
    for (const controller of subscriptions.current.values()) controller.abort();
    subscriptions.current.clear();
  }, [conversationId]);

  // 唤起待执行任务（启动后的首段/追问回答由服务端 worker 池或本入口执行）
  useEffect(() => {
    const t = sessionStorage.getItem("slice4.pendingTask");
    if (t) {
      sessionStorage.removeItem("slice4.pendingTask");
      setPendingTask(t);
    }
  }, [conversationId]);

  const runPending = useCallback(async () => {
    if(!ws || ws.authorization_required || ws.needs_upgrade)return;
    if(ws.server_managed&&!ws.queued_tasks?.find(task=>task.task_id===pendingTask)?.followup){setPendingTask(null);return;}
    if (!pendingTask || attemptedTasks.current.has(pendingTask)) return;
    attemptedTasks.current.add(pendingTask);
    setBusy(true);
    try {
      const result = await api.runTeachingTask(pendingTask);
      if(result.status==='preparing')attemptedTasks.current.delete(pendingTask);
      await refresh();
      if (result.status === "failed") onErr("本段生成未完成，可重试。");
    } catch (e: any) {
      onErr(e?.message || "生成失败");
    } finally {
      if(latestConversation.current===conversationId){setBusy(false);setPendingTask(null);}
    }
  }, [onErr, onOk, pendingTask, refresh, conversationId,ws]);

  useEffect(() => {
    if (pendingTask && !busy) runPending();
  }, [pendingTask, busy, runPending]);
  useEffect(() => {
    if (busy || pendingTask || !ws||ws.server_managed||ws.authorization_required||ws.needs_upgrade) return;
    const next = ws?.queued_tasks?.find(t => t.kind!=="tts"&&t.status === "queued" && (t.followup || (playing && ws.session_state === "active" && t.step_no <= current + 1)) && !attemptedTasks.current.has(t.task_id));
    if (next) setPendingTask(next.task_id);
  }, [ws, current, busy, pendingTask, playing]);

  useEffect(()=>{
    if(!playing||!ws||ws.session_state!=="active"||ws.server_managed||ws.authorization_required||ws.needs_upgrade||ws.tts_authorized===false||ws.audio_mode==="text")return;
    if(ws.tts_config&&(ws.tts_authorized===true||ws.audio_mode==="tts")&&!audio.textOnly)for(const segment of ws.steps.filter(s=>s.step_no>=current&&s.step_no<=current+1&&!s.audio_ready)){
      const key=`${conversationId}:${segment.step_no}:${segment.text_version_no}:${ws.tts_config.config_id}:${ws.tts_config.config_version}`;
      if(audioRequests.current.has(key))continue;
      audioRequests.current.add(key);
      api.ensureLessonAudio(conversationId,segment.step_no).then(()=>refresh()).catch(()=>{
        if(latestConversation.current===conversationId)setAudioFailure("语音准备失败，可重试或继续文字讲解。");
      });
    }
    for(const task of ws.queued_tasks||[]){
      if(task.kind!=="tts"||task.status!=="queued"||task.step_no<current||task.step_no>current+1||audioAttempts.current.has(task.task_id))continue;
      audioAttempts.current.add(task.task_id);
      api.runTeachingTask(task.task_id).then(()=>refresh()).catch(()=>{
        if(latestConversation.current===conversationId)setAudioFailure("语音生成失败，可重试或继续文字讲解。");
      });
    }
  },[ws,playing,current,conversationId,refresh,audio.textOnly]);

  const saveCheckpoint = useCallback(async (stepNo: number, positionMs: number, reset = false) => {
    seqRef.current += 1;
    try {
      await api.saveCheckpoint(conversationId, {
        client_seq: seqRef.current, step_no: stepNo, position_ms: positionMs,
        board_state:{presentation:{...(reset?{visible_chars:0,text_complete:false}:bufferRef.current.cursor(`step:${stepNo}`)),paused:!playingRef.current,audio_id:reset?null:audioRef.current.sourceId}},
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

  useEffect(()=>{
    if(!playing || advanceTo===null || ws?.conversation_id!==conversationId || ws.session_state!=="active"
        || !ws.steps.some(segment=>segment.step_no===advanceTo))return;
    const target=advanceTo;setAdvanceTo(null);setCurrent(target);posRef.current=0;
    saveCheckpoint(target,0,true);
  },[advanceTo,ws,conversationId,saveCheckpoint,playing]);

  const live=buffer.view(`step:${current}`);
  useEffect(()=>setAudioFailure(""),[conversationId,current]);
  const displayComplete=Boolean(live?.final && live.remaining===0);
  const finalSegment=Boolean(ws?.steps.find(s=>s.step_no===current)?.lesson_finished);
  const audioComplete=!audioRequired||audio.finished;
  useEffect(()=>{
    if(!playing||!displayComplete||!audioComplete||finalSegment||readingHistory||ws?.session_state!=="active")return;
    const timer=window.setTimeout(()=>setAdvanceTo(current+1),1800);
    return()=>window.clearTimeout(timer);
  },[playing,displayComplete,audioComplete,finalSegment,readingHistory,current,ws?.session_state]);

  // 播放期间每 5 秒保存一次
  useEffect(() => {
    if (!playing || ws?.conversation_id!==conversationId || ws.session_state!=="active") return;
    const timer = window.setInterval(() => {
      if(!audioRef.current.sourceId)posRef.current+=5000;
      saveCheckpoint(current, posRef.current);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [playing, current, saveCheckpoint, ws?.conversation_id, ws?.session_state, conversationId]);

  const gotoStep = useCallback(async (n: number) => {
    if (!ws) return;
    const target = ws.steps.find((s) => s.step_no === n);
    if (!target) return;
    setAdvanceTo(null);
    if (playing) {
      // 切换步骤先暂停（本地立即）并保存位置
      audioRef.current.pause();playingRef.current=false;bufferRef.current.pause(true);setPlaying(false);
      await saveCheckpoint(current, posRef.current);
    }
    setCurrent(n);
    posRef.current = 0;
    await saveCheckpoint(n, 0);
    await refresh();
  }, [current, playing, saveCheckpoint, ws, refresh]);

  const togglePlay = useCallback(async () => {
    if (!ws) return;
    if(ws.session_state==="awaiting_resume") {
      try {await api.resumeLesson(conversationId);await refresh();}
      catch(e:any){onErr(e.message||"恢复失败");return;}
    } else if(ws.session_state!=="active")return;
    if (playing) {
      audioRef.current.pause();playingRef.current=false;bufferRef.current.pause(true);setPlaying(false);
      saveCheckpoint(current, posRef.current);
      return;
    }
    audioRef.current.enable();playingRef.current=true;bufferRef.current.pause(false);setPlaying(true);
    followLatest.current=true;setReadingHistory(false);
    await saveCheckpoint(current,posRef.current);await refresh();
  }, [current, playing, saveCheckpoint, ws, conversationId, refresh, onErr]);

  const sendFollowup = useCallback(async () => {
    if (!followup.trim() || !ws) return;
    setAdvanceTo(null);
    audioRef.current.pause();playingRef.current=false;bufferRef.current.pause(true);setPlaying(false);
    await saveCheckpoint(current, posRef.current); // 追问前保存位置
    setBusy(true);
    try {
      const r = await api.sendFollowup(conversationId, {
        content: followup, idempotency_key: `web-fu-${Date.now()}`,
      });
      setFollowup("");
      if (!r.deduped) {
        setFollowupTask(r.task_id);
        await api.runTeachingTask(r.task_id); // 本地开发直接执行回答
      }
      await refresh();
      onOk("回答完成；点击三角按钮继续课程。");
    } catch (e: any) {
      onErr(e?.message || "追问失败");
    } finally {
      setFollowupTask(null);
      setBusy(false);
    }
  }, [conversationId, current, followup, onErr, onOk, refresh, saveCheckpoint, ws]);

  const recover = async () => {
    if (!ws?.course_id || !ws.lesson_id || busy) return;
    setBusy(true);
    try {
      const result = ws.needs_upgrade ? await api.startLessonV2(ws.course_id, ws.lesson_id) : await api.recoverLesson(ws.course_id, ws.lesson_id, conversationId);
      if (!result) return;
      if (result.conversation_id !== conversationId) { go(`/lecture/${result.conversation_id}`); return; }
      attemptedTasks.current.clear(); await refresh();
    } catch (error) { onErr(errText(error)); }
    finally { setBusy(false); }
  };

  if (!ws) return <p className="muted">加载中…</p>;

  const step = ws.steps.find((s) => s.step_no === current)
    || ws.steps.find((s) => s.step_no === Math.min(...(ws.steps.map((x) => x.step_no) || [1])))
    || ws.steps[0];
  const drafts = Object.entries(previews).filter(([, draft]) => !ws.steps.some(s => s.step_no === draft.step_no));
  const visibleDrafts = drafts.filter(([,draft]) => draft.followup || draft.step_no===current);
  const currentDraft = visibleDrafts.find(([, draft]) => draft.followup)?.[1] || visibleDrafts.find(([, draft]) => draft.step_no === current)?.[1];
  const boardKey = currentDraft?.followup ? `followup:${currentDraft.conversation_generation}`
    : ws.session_state === "awaiting_resume" ? `followup:${ws.generation}` : `step:${current}`;
  const shown = buffer.view(boardKey);
  const rawBoard=currentDraft?.followup?currentDraft.board:ws.session_state==="awaiting_resume"?ws.followup_board:step?.board;
  const ownBoard=shown ? shown.board : rawBoard?.presentation ? {...rawBoard,presentation:{...rawBoard.presentation,
    all_blocks:rawBoard.presentation.blocks,blocks:[]}} : null;
  const scope=currentDraft?.board_scope||shown?.board?.board_scope||step?.board_scope||`segment:${current}`;
  const boardScene = boardKey.startsWith("followup:") ? ownBoard : stageBoard(ws.steps,ownBoard,current,scope);
  const originalRefs = boardScene?.original_refs || [];
  const awaiting = ws.session_state === "awaiting_resume";
  const failedTasks = (ws.queued_tasks || []).filter(task => task.kind !== "tts" && ["failed", "expired", "cancelled"].includes(task.status));
  const artifactExhausted=typeof ws.artifact_recovery==="object"&&ws.artifact_recovery?.status==="exhausted";
  const artifactRepairing=typeof ws.artifact_recovery==="object"&&ws.artifact_recovery?.status==="repairing";
  const artifactMissing=ws.artifact_recovery===true || Boolean(typeof ws.artifact_recovery==="object"&&(ws.artifact_recovery?.available||ws.artifact_recovery?.required));
  const blocked = ws.authorization_required || ws.required_action === "authorize" || ws.needs_upgrade;
  const recoveryNeeded=!artifactExhausted&&(artifactMissing || ws.required_action==="retry" || failedTasks.length>0);
  const emptyMessage = artifactExhausted ? "讲解补生成已达到重试上限，请检查模型设置。" : artifactRepairing ? "正在重新准备首段讲解…" : artifactMissing ? "首段讲解内容缺失，点击恢复讲解可重新准备。" : blocked ? "讲解配置暂不可用，请检查模型设置或恢复讲解。" : failedTasks.length ? errText({ code: failedTasks[0].error_code || (failedTasks[0].status === "expired" ? "TASK_EXPIRED" : "INTERNAL_ERROR") }) : "正在准备首段讲解…";

  return (
    <div className="lw-simple" data-audio-state={audio.state} data-current-segment={current} data-audio-position={posRef.current}>
      <div className="lecture-minibar">
        <button className="lecture-back" aria-label="返回课程" title="返回课程" onClick={async()=>{
          audioRef.current.pause();playingRef.current=false;bufferRef.current.pause(true);setPlaying(false);
          await saveCheckpoint(current,posRef.current);go(`/course/${ws.course_id}`);
        }}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7" /></svg></button>
        {savedAt && <time className="lecture-save-time" aria-label="位置保存时间">{savedAt}</time>}
      </div>

      <div className="bd-tabs" role="tablist" aria-label="板书与文字切换">
            <button role="tab" aria-selected={pane === "board"} onClick={() => setPane("board")}>板书</button>
            <button role="tab" aria-selected={pane === "text"} onClick={() => setPane("text")}>文字</button>
          </div>
      <LectureSplit pane={pane}>
        <div className="bd-col">
          <div className="board">
            <div className="board-inner">
              <div className="canvas-readonly-wrap">
                <TeachingBoard presenting={playing || Boolean(followupTask)} scene={boardScene} sceneKey={`${conversationId}:${boardKey.startsWith("followup:")?boardKey:scope}`} />
              </div>
              {boardScene ? (
                <>
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
                <p className="muted">{audioRequired && !audio.textOnly ? "正在等待语音与板书同步…" : "本段板书生成中或未生成；文字可继续阅读。"}</p>
              )}
            </div>
          </div>
        </div>

        <div className="tx-col">
          {readingHistory && <div className="stream-controls">
            <button className="btn btn-sm" onClick={()=>{
              followLatest.current=true;setReadingHistory(false);
              if(textScroller.current)textScroller.current.scrollTop=textScroller.current.scrollHeight;
            }}>回到最新</button>
          </div>}
          <div ref={textScroller} className="stream" role="log" aria-label="讲解文字流" onScroll={event=>{
            const node=event.currentTarget;
            followLatest.current=node.scrollHeight-node.scrollTop-node.clientHeight<50;
            setReadingHistory(!followLatest.current);
          }}>
            {[...ws.steps].filter(s=>s.step_no<=current).sort((a,b)=>a.step_no-b.step_no).map(s=>{
              const visible=buffer.view(`step:${s.step_no}`);
              return <section key={s.step_id} className={`lecture-segment${s.step_no===current?" current":""}`} id={`tx-${s.step_no}`}
                data-phase={s.phase||"default"}
                data-segment-no={s.step_no} data-stage-no={s.stage_no} onClick={()=>{if(s.step_no!==current)gotoStep(s.step_no);}}>
                {paragraphs(visible?.text||"",s.board?.beats).map((text,index)=><p key={index}>{text}</p>)}
              </section>;
            })}
            {ws.followups?.map((reply,index)=><section key={`${reply.generation}-${index}`}
              className={`lecture-segment${reply.role==="user"?" question":""}`} data-phase={reply.role==="user"?"question":"answer"}>
              {reply.role==="user" && <span className="help">我的追问</span>}
              {paragraphs(reply.role==="assistant"?(buffer.view(`followup:${reply.generation}`)?.text||""):reply.content,
                reply.role==="assistant" && reply.generation===ws.generation ? ws.followup_board?.beats : [])
                .map((text,i)=><p key={i}>{text}</p>)}
            </section>)}
            {visibleDrafts.map(([id,draft])=>{
              const visible=buffer.view(draft.followup?`followup:${draft.conversation_generation}`:`step:${draft.step_no}`);
              return <section key={id} className="lecture-segment current" data-teaching-preview="true"
                data-phase={draft.followup?"answer":step?.phase||"default"}
                data-segment-no={draft.step_no} data-stage-no={draft.stage_no}>
                {paragraphs(visible?.text||"",draft.beats).map((text,i)=><p key={i}>{text}</p>)}
              </section>;
            })}
            {ws.steps.length === 0 && !visibleDrafts.some(([, draft]) => draft.text) && <p className="muted" role="status">{emptyMessage}</p>}
          </div>
          <div className="lecture-actions">
            {(blocked || recoveryNeeded || artifactExhausted) && <div role="status"><p>{emptyMessage}</p>
              {failedTasks.map(task => <p className="help" key={task.task_id}>{task.error_code || task.status}{task.deadline_at ? ` · 截止 ${new Date(task.deadline_at).toLocaleString("zh-CN")}` : ""}</p>)}
              {(blocked || recoveryNeeded) && <button className="btn btn-sm" disabled={busy} onClick={recover}>{artifactMissing || blocked ? "恢复讲解" : "重试生成"}</button>}
              <a className="btn btn-sm" href="#/capabilities">模型设置</a>
            </div>}
            {finalSegment && displayComplete && audioComplete && <button className="btn btn-pri btn-sm" disabled={busy||awaiting} onClick={async()=>{
              try {await api.completeLesson(conversationId);go(`/course/${ws.course_id}`);}
              catch(e:any){onErr(e.message||"完成确认失败");}
            }}>确认本讲已学完</button>}
          </div>
          {audioRequired&&!audio.textOnly&&<div className="lecture-audio-hint" role="status">
            {audio.state==="blocked"?<button className="btn btn-sm" onClick={audio.enable}>点击开启声音</button>:
              audio.state==="failed"||audioFailure||["failed","invalid"].includes(audioStep?.audio_status||"")?<>
                <span>{audioStep?.audio_error==="subtitle_missing"?"语音字幕缺失，暂时无法同步播放。":audioFailure||"语音暂时不可用。"}</span>
                <button className="btn btn-sm" onClick={()=>{
                  audioRequests.current.clear();setAudioFailure("");
                  for(const task of ws.queued_tasks||[])if(task.kind==="tts"&&task.step_no===current){
                    audioAttempts.current.add(task.task_id);
                    api.runTeachingTask(task.task_id).then(refresh).catch(()=>setAudioFailure("语音重试失败。"));
                  }
                  refresh();
                }}>重试语音</button>
              </>:!audioStep?.audio_ready?<span>正在准备语音…</span>:null}
            {(audio.state==="blocked"||audio.state==="failed"||audioFailure||!audioStep?.audio_ready)&&
              <button className="btn btn-sm" onClick={audio.skip}>继续文字讲解</button>}
          </div>}
          <div className="composer2" style={{ padding: "12px 20px 18px", boxShadow: "0 -8px 20px rgba(20,30,45,.06)" }}>
            <div className="box">
              <label className="sr" htmlFor="fu">课程追问</label>
              <textarea
                className="ta"
                id="fu"
                placeholder="针对当前讲次追问，比如：这个例子换个场景还成立吗？"
                value={followup}
                maxLength={1000}
                onChange={(e) => setFollowup(e.target.value)}
                onKeyDown={e=>{
                  if(e.key==="Enter"&&!e.shiftKey&&!e.nativeEvent.isComposing){
                    e.preventDefault();if(!busy&&followup.trim())sendFollowup();
                  }
                }}
              />
              <button className="lecture-play-control" aria-label={followup.trim()?"发送追问":playing?"暂停讲解":"继续讲解"}
                title={followup.trim()?"发送追问":playing?"暂停讲解":"继续讲解"} aria-pressed={followup.trim()?undefined:!playing}
                onClick={followup.trim()?sendFollowup:togglePlay}
                disabled={followup.trim()?busy:ws.session_state!=="active"&&!awaiting}>
                <svg viewBox="0 0 24 24" aria-hidden="true">{followup.trim()?<path d="M12 4l7 7-1.5 1.5-4.5-4.5V20h-2V8l-4.5 4.5L5 11z"/>:playing?<rect x="7" y="7" width="10" height="10" rx="1"/>:<path d="M9 5l11 7-11 7z"/>}</svg>
              </button>
            </div>
          </div>
        </div>
      </LectureSplit>
    </div>
  );
}
