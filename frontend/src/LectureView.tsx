import { useCallback, useEffect, useRef, useState } from "react";
import { errText } from "./lib/errors";
import { api,classroomApi } from "./api";
import {useInteractiveClassroom,codeLanguage} from "./hooks/useInteractiveClassroom";
import PdfLesson from "./ui/PdfLesson";
import { teachingFailures } from "./lib/teachingRecovery";
import {useNeighborAudio} from "./hooks/useNeighborAudio";
import { useTeachingBuffer } from "./hooks/useTeachingBuffer";
import {lessonAudioRequired,useLessonAudio} from "./hooks/useLessonAudio";
import {useLectureFollow} from "./hooks/useLectureFollow";
import LectureSplit from "./ui/LectureSplit";
import TeachingBoard from "./ui/TeachingBoard";
import { stageBoard, paragraphs } from "./lib/stageBoard";
import CharacterStage from "./ui/CharacterStage";
import { ArrowLeft, BookOpen, Settings, Play, Square, PanelLeftClose, PanelLeftOpen, ListChecks } from "lucide-react";
import "./classroom.css";
import { audioCharacters,exactAudioAlignment } from "./lib/audioTiming";
import {useFeynmanAudio} from "./hooks/useFeynmanAudio";
import FeynmanDialogue, {FeynmanLearningRecord} from "./ui/FeynmanDialogue";
import {useClassroomPresentation} from "./hooks/useClassroomPresentation";
import {useKnowledgeCoverage} from "./hooks/useKnowledgeCoverage";
import TeachingPointer from "./ui/TeachingPointer";
import {characterScene} from "./lib/characterScene";

/**
 * 连续教学工作区：共享文字／板书呈现时钟、暂停／继续与自动续讲；
 * 检查点保存呈现游标，追问保存一次性恢复点，完成仍由用户确认。
 */

type Step = {
  step_no: number; step_id: string; title: string; text: string; status: string;
  board_scope?: string; stage_no?: number; stage_segment_no?: number; stage_finished?: boolean;
  phase?: string; lesson_finished?: boolean; text_version_no: number; board: any | null; audio_id: string | null; audio_ready: boolean;
  audio_status?:string;audio_error?:string;audio_subtitles?:any[];
  character?:any;
};
type Workspace = {
  conversation_id: string; kind: string; course_id: string | null; lesson_id: string | null;
  generation: number; session_state: string; restore_point: any;
  playback_version?:number;
  lesson_title?: string; planned_steps?: number; lesson_finished?: boolean;
  queued_tasks?: { task_id: string; kind: string; status: string; step_no: number; followup?: boolean; error_code?: string; deadline_at?: string; required_action?: string }[];
  authorization_required?: boolean; authorization_error?: string; required_action?: string;
  needs_upgrade?: boolean;
  scope_preparation?: {status: string; error_code?: string | null} | null;
  tts_authorized?:boolean;
  audio_mode?:"text"|"tts";
  artifact_recovery?:{status?:"missing"|"repairing"|"exhausted";available?:boolean;required?:boolean;error_code?:string;missing_steps?:number[];step_no?:number;attempts?:number;max_attempts?:number}|boolean;
  server_managed?:boolean;
  tts_config?:{config_id:string;config_version:number}|null;
  feynman?:{checkpoints:any[];active:any;completed?:any[]};
  knowledge?:{plan_hash:string;total:number;data:any[]};
  channel_checkpoint?:{visible_chars:number;position_ms:number;audio_id:string;paused:boolean;client_seq:number}|null;
  last_checkpoint: { client_seq: number; step_no: number; position_ms: number; saved_at?: string;
    presentation?: {visible_chars?:number;paused?:boolean;text_complete?:boolean;audio_id?:string} } | null;
  steps: Step[];
  followups: { generation: number; role: string; content: string }[];
  followup_board?: any;
  code_feedback?:{generation:number;action:string;step_no:number;block_id:string;instruction:string}|null;
  answer_audio?: {audio_id:string;subtitles:any[]}|null;
};

export default function LectureView({ conversationId, onErr, onOk, go,reviewStep }: {
  conversationId: string; onErr: (s: string) => void; onOk: (s: string) => void; go: (p: string) => void;
  reviewStep?:number;
}) {
  const [ws, setWs] = useState<Workspace | null>(null);
  const [navCollapsed,setNavCollapsed]=useState(()=>{
    try{return localStorage.getItem("lecture.navCollapsed")==="true";}catch{return false;}
  });
  const toggleNav=()=>{
    const next=!navCollapsed;setNavCollapsed(next);
    try{localStorage.setItem("lecture.navCollapsed",String(next));}catch{/* Keep the control usable without storage. */}
  };
  const [pointingTarget,setPointingTarget]=useState<{x:number;y:number;visible:boolean}|null>(null);
  const handlePointingTarget=useCallback((target:{x:number;y:number;visible:boolean}|null)=>{setPointingTarget(old=>old?.x===target?.x&&old?.y===target?.y&&old?.visible===target?.visible?old:target);},[]);
  const [current, setCurrent] = useState(1);
  const [advanceTo,setAdvanceTo] = useState<number|null>(null);
  const [playing, setPlaying] = useState(true);
  const playingRef = useRef(true);
  const [pane, setPane] = useState<"board" | "text">("board");
  const [followup, setFollowup] = useState("");
  const [busy, setBusy] = useState(false);
  const [quizBusy,setQuizBusy]=useState(false),[quizHelp,setQuizHelp]=useState(false),[quizAutoPlay,setQuizAutoPlay]=useState(false);
  const quizStart=useRef(false),quizRequestKey=useRef("");
  const attemptedTasks = useRef(new Set<string>());
  const [pendingTask, setPendingTask] = useState<string | null>(null);
  const [followupTask, setFollowupTask] = useState<string | null>(null);
  const answerMedia=useRef<HTMLAudioElement|null>(null),playbackEpoch=useRef(0),answerTrack=useRef<any>(null);
  const [answerPlaying,setAnswerPlaying]=useState(false),[answerReady,setAnswerReady]=useState(false);
  const [answerAudible,setAnswerAudible]=useState(false);
  const [answerTextPlaying,setAnswerTextPlaying]=useState(false);
  const answerTextPlayingRef=useRef(false);answerTextPlayingRef.current=answerTextPlaying;
  const answerPlayingRef=useRef(false);answerPlayingRef.current=answerPlaying;
  const presentation=useClassroomPresentation();
  useEffect(()=>{
    const track=ws?.answer_audio;
    if(!track||ws.session_state!=="awaiting_resume"||answerTrack.current?.audioId===track.audio_id)return;
    const reply=ws.followups.find(item=>item.role==="assistant"&&item.generation===ws.generation);
    if(!reply)return;
    if(ws.followup_board?.alignment_version===1&&!exactAudioAlignment(reply.content,track.subtitles||[])){
      answerMedia.current?.pause();answerMedia.current=null;answerTrack.current=null;
      setAnswerReady(false);setAnswerPlaying(false);setAnswerAudible(false);
      onErr("语音字幕无法与讲稿精确对应，请重新准备语音。");return;
    }
    answerMedia.current?.pause();
    const media=new Audio(api.audioUrl(track.audio_id));answerMedia.current=media;
    answerTrack.current={audioId:track.audio_id,generation:ws.generation,subtitles:track.subtitles,text:reply.content};
    setAnswerReady(true);setAnswerPlaying(false);
    if(ws.channel_checkpoint?.audio_id===track.audio_id)media.onloadedmetadata=()=>{media.currentTime=(ws.channel_checkpoint?.position_ms||0)/1000;};
    media.onended=()=>{if(!answerPlayingRef.current)return;bufferRef.current.audioClock(`followup:${ws.generation}`,Array.from(reply.content).length);bufferRef.current.flush();setAnswerPlaying(false);setAnswerReady(false);};
    media.onerror=()=>{bufferRef.current.audioGate(`followup:${ws.generation}`,true);setAnswerPlaying(false);};
  },[ws?.answer_audio?.audio_id,ws?.generation,ws?.session_state]);
  useEffect(()=>()=>{playbackEpoch.current++;answerMedia.current?.pause();answerMedia.current=null;},[conversationId]);
  const [previews, setPreviews] = useState<Record<string, any>>({});
  const subscriptions = useRef(new Map<string, AbortController>());
  const streamRetryAt = useRef(new Map<string, number>());
  const [savedAt, setSavedAt] = useState("");
  const seqRef = useRef(0);
  const posRef = useRef(0);
  const restoredPosition = useRef(false);
  const restoredPlayback=useRef<number|null>(null);
  const latestConversation = useRef(conversationId); latestConversation.current=conversationId;
  useEffect(()=>{restoredPosition.current=false;restoredPlayback.current=null;seqRef.current=0;posRef.current=0;setSavedAt("");
    playingRef.current=true;setPlaying(true);setCurrent(1);setAdvanceTo(null);setWs(null);setPreviews({});
    setPendingTask(null);setBusy(false);setFollowupTask(null);attemptedTasks.current.clear();},[conversationId]);
  const bufferRef=useRef<any>(null);
  const audioStep=ws?.steps.find(step=>step.step_no===current);
  const hasRunnable=Boolean([...(ws?.steps||[]).map(step=>step.board),ws?.followup_board,
    ...Object.values(previews).map(preview=>preview.board)].some(board=>board?.presentation?.blocks?.some((block:any)=>
    block.type==="code"&&codeLanguage(block.language))));
  const runnableConversation=useRef("");
  if(hasRunnable)runnableConversation.current=conversationId;
  const codeEnabled=hasRunnable||runnableConversation.current===conversationId;
  const codeClass=useInteractiveClassroom(conversationId,ws?.playback_version||0,codeEnabled);
  const [boardMode,setBoardMode]=useState<"board"|"pdf">("board");
  const interactionContinue=useRef<()=>Promise<boolean>>(async()=>false);
  const interactionContinuing=useRef(false);
  const audioRequired=lessonAudioRequired(ws,audioStep,current);
  useNeighborAudio(conversationId,current,ws?.steps||[],audioRequired);
  const bufferWithAudio = useTeachingBuffer(conversationId, ws, previews, current, playing, audioRequired,answerPlaying||answerTextPlaying,ws?.playback_version||0);
  bufferRef.current=bufferWithAudio;
  const buffer=bufferWithAudio;
  const pdfFocus=(ws?.session_state==="awaiting_resume"?
    buffer.view(`followup:${ws.generation}`)?.board?.pdf_focus||ws.followup_board?.pdf_focus:null)||
    buffer.view(`step:${current}`)?.board?.pdf_focus||audioStep?.board?.pdf_focus;
  useEffect(()=>{setBoardMode(pdfFocus?"pdf":"board");},[conversationId,current,pdfFocus?.target_id]);
  useEffect(()=>{if(answerTextPlaying&&ws&&buffer.view(`followup:${ws.generation}`)?.remaining===0)setAnswerTextPlaying(false);},[answerTextPlaying,ws,buffer]);
  useEffect(()=>{
    if(!answerPlaying||!answerMedia.current||!answerTrack.current)return;
    let frame=0;
    const epoch=playbackEpoch.current;
    const tick=()=>{
      if(epoch!==playbackEpoch.current)return;
      const track=answerTrack.current;
      if(track&&answerMedia.current){
        const media=answerMedia.current,ready=!media.paused&&!media.seeking&&media.readyState>=2;
        setAnswerAudible(old=>old===ready?old:ready);
        bufferRef.current.audioGate(`followup:${track.generation}`,!ready);
        if(ready)bufferRef.current.audioClock(`followup:${track.generation}`,audioCharacters(track.text,track.subtitles,media.currentTime*1000,ws?.followup_board?.alignment_version===1));
      }
      frame=requestAnimationFrame(tick);
    };
    frame=requestAnimationFrame(tick);return()=>{cancelAnimationFrame(frame);setAnswerAudible(false);};
  },[answerPlaying]);
  const audio=useLessonAudio(conversationId,current,audioStep,playing,bufferRef,posRef,ws?.last_checkpoint,audioRequired);
  const audioRef=useRef(audio);audioRef.current=audio;
  const [paintedStep,setPaintedStep]=useState("");
  const boardPainted=paintedStep===`${conversationId}:${current}`;
  const handleBoardPainted=useCallback(()=>{
    if(ws?.session_state==="active")setPaintedStep(`${conversationId}:${current}`);
  },[conversationId,current,ws?.session_state]);
  const coverage=useKnowledgeCoverage(conversationId,audioStep,playing,buffer,audio,posRef,Boolean(reviewStep)||!ws?.knowledge,boardPainted);
  const audioAttempts=useRef(new Set<string>()),audioRequests=useRef(new Set<string>());
  const [audioFailure,setAudioFailure]=useState("");
  const [connectionWaiting,setConnectionWaiting]=useState(false);
  const [audioRetrying,setAudioRetrying]=useState(false),audioRetryEpoch=useRef(0);
  const textFollow=useLectureFollow(conversationId,playing||answerPlaying||answerTextPlaying,Boolean(ws));
  const {readingHistory}=textFollow;

  const refresh = useCallback(async () => {
    try {
      const w = await api.getWorkspace(conversationId);
      if(latestConversation.current!==conversationId)return;
      if(w.needs_upgrade) { setWs(w); return; }
      if (restoredPlayback.current!==null&&restoredPlayback.current!==(w.playback_version||0)){
        audioRef.current.pause();playingRef.current=false;setPlaying(false);setPreviews({});
        restoredPosition.current=false;posRef.current=0;setAdvanceTo(null);setPaintedStep("");
        for(const controller of subscriptions.current.values())controller.abort();
        subscriptions.current.clear();audioAttempts.current.clear();audioRequests.current.clear();
      }
      restoredPlayback.current=w.playback_version||0;
      if (!restoredPosition.current) {
        restoredPosition.current=true;
        if(reviewStep&&w.steps.some((s:any)=>s.step_no===reviewStep))setCurrent(reviewStep);
        else if(w.last_checkpoint?.step_no>0)setCurrent(w.last_checkpoint.step_no);
        posRef.current=w.last_checkpoint?.position_ms||0;
        const running=!reviewStep&&w.session_state==="active" && !w.last_checkpoint?.presentation?.paused;
        playingRef.current=running;setPlaying(running);bufferRef.current.pause(!running);
        if(w.last_checkpoint?.saved_at)setSavedAt(new Date(w.last_checkpoint.saved_at).toLocaleTimeString("zh-CN",{hour12:false}));
      }
      setWs(w);
      setConnectionWaiting(false);
      seqRef.current = Math.max(seqRef.current, w.last_checkpoint?.client_seq ?? 0);
      if (w.session_state !== "active"||w.feynman?.active) {
        audioRef.current.pause();playingRef.current=false;setPlaying(false);
        if(w.feynman?.active||(!answerPlayingRef.current&&!answerTextPlayingRef.current))bufferRef.current.pause(true);
      }
    } catch (e: any) {
      if(latestConversation.current!==conversationId)return;
      if(!e?.status||e.status>=500)setConnectionWaiting(true);
      else onErr(e?.message || "教学工作区加载失败");
    }
  }, [conversationId, onErr, go]);

  const quiz=ws?.feynman?.active;
  const quizAudio=useFeynmanAudio(conversationId,quiz,quizAutoPlay,refresh);
  const saveAnswerPosition=useCallback(async()=>{
    if(ws?.session_state!=="awaiting_resume")return;
    const cursor=bufferRef.current.cursor(`followup:${ws.generation}`);
    try{await api.savePresentation(conversationId,{generation:ws.generation,client_seq:Date.now(),
      ...{visible_chars:cursor.visible_chars},position_ms:Math.floor((answerMedia.current?.currentTime||0)*1000),
      audio_id:answerTrack.current?.audioId||"",paused:!answerPlayingRef.current});}catch(error:any){
        if(!error?.status||error.status>=500)setConnectionWaiting(true);
        else onErr("回答位置保存失败，可重试。");
      }
  },[conversationId,ws?.generation,ws?.session_state,onErr]);
  const stopAll=useCallback(()=>{
    presentation.stop(bufferRef.current,()=>audioRef.current.pause(),()=>{answerMedia.current?.pause();},()=>quizAudio.pause());
    playingRef.current=false;answerPlayingRef.current=false;
    setPlaying(false);setAnswerPlaying(false);setAnswerTextPlaying(false);setQuizAutoPlay(false);
  },[presentation.stop,quizAudio.pause]);
  useEffect(()=>{if(!answerPlaying&&!answerTextPlaying)return;const timer=setInterval(()=>{void saveAnswerPosition();},1500);return()=>clearInterval(timer);},[answerPlaying,answerTextPlaying,saveAnswerPosition]);
  const quizAction=async(action:string)=>{
    if(!quiz||quizBusy)return;
    setQuizBusy(true);
    try{
      await api.feynmanAction(conversationId,action,{revision:quiz.revision});
      await refresh();
      if(action==="continue"||action==="skip"){
        setQuizHelp(false);quizStart.current=false;audioRef.current.enable();
        const resume=quiz.scope!=="lesson";
        playingRef.current=resume;setPlaying(resume);bufferRef.current.pause(!resume);
        await saveCheckpoint(current,posRef.current);
      }
    }catch(error){onErr(errText(error));}finally{setQuizBusy(false);}
  };
  const answerQuiz=async()=>{
    if(!quiz||quizBusy||quiz.state!=="waiting"||!followup.trim()||!quizAudio.done)return;
    setQuizBusy(true);setQuizAutoPlay(true);
    if(!quizRequestKey.current)quizRequestKey.current=crypto.randomUUID();
    try{
      await api.feynmanAction(conversationId,"answer",{question_id:quiz.question_id,revision:quiz.revision,
        content:followup,idempotency_key:quizRequestKey.current,help:quizHelp});
      setFollowup("");setQuizHelp(false);quizRequestKey.current="";
      await refresh();
    }catch(error){onErr(errText(error));}finally{setQuizBusy(false);}
  };

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    const timer = window.setInterval(refresh, 1500);
    const reconnect = () => {
      attemptedTasks.current.clear();audioAttempts.current.clear();audioRequests.current.clear();
      for(const controller of subscriptions.current.values())controller.abort();
      subscriptions.current.clear();
      streamRetryAt.current.clear();
      void refresh();
      if(playingRef.current)audioRef.current.retry();
    };
    window.addEventListener("online",reconnect);
    return () => {window.clearInterval(timer);window.removeEventListener("online",reconnect);};
  }, [refresh]);

  useEffect(() => {
    const eligible = (ws?.queued_tasks || []).filter(t =>
      t.kind === "lesson_step_generate" && ["queued", "leased", "running"].includes(t.status) &&
      (t.followup || (ws?.session_state === "active" && t.step_no <= current + 1)));
    if (followupTask && !eligible.some(t => t.task_id === followupTask))
      eligible.push({ task_id: followupTask, kind: "lesson_step_generate", status: "running", step_no: 0, followup: true });
    const ids = new Set(eligible.map(t => t.task_id));
    for(const id of streamRetryAt.current.keys())if(!ids.has(id))streamRetryAt.current.delete(id);
    for (const [id, controller] of subscriptions.current) {
      if (!ids.has(id)) { controller.abort(); subscriptions.current.delete(id); }
    }
    setPreviews(old => Object.fromEntries(Object.entries(old).filter(([id]) => ids.has(id))));
    for (const task of eligible) {
      if (subscriptions.current.has(task.task_id)||(streamRetryAt.current.get(task.task_id)||0)>Date.now()) continue;
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
      }).finally(()=>{
        if(subscriptions.current.get(task.task_id)===controller){
          streamRetryAt.current.set(task.task_id,Date.now()+1500);
          subscriptions.current.delete(task.task_id);
        }
      });
    }
  }, [ws, current, refresh, followupTask]);

  useEffect(() => () => {
    for (const controller of subscriptions.current.values()) controller.abort();
    subscriptions.current.clear();
    streamRetryAt.current.clear();
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
      attemptedTasks.current.delete(pendingTask);
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
        audioRequests.current.delete(key);
        if(latestConversation.current===conversationId)setAudioFailure("语音准备失败，可重试或继续文字讲解。");
      });
    }
    for(const task of ws.queued_tasks||[]){
      if(task.kind!=="tts"||task.status!=="queued"||task.step_no<current||task.step_no>current+1||audioAttempts.current.has(task.task_id))continue;
      audioAttempts.current.add(task.task_id);
      api.runTeachingTask(task.task_id).then(()=>refresh()).catch(()=>{
        audioAttempts.current.delete(task.task_id);
        if(latestConversation.current===conversationId)setAudioFailure("语音生成失败，可重试或继续文字讲解。");
      });
    }
  },[ws,playing,current,conversationId,refresh,audio.textOnly]);

  const saveCheckpoint = useCallback(async (stepNo: number, positionMs: number, reset = false) => {
    if(reviewStep)return true;
    seqRef.current += 1;
    try {
      await api.saveCheckpoint(conversationId, {
        client_seq: seqRef.current, step_no: stepNo, position_ms: positionMs,
        board_state:{presentation:{...(reset?{visible_chars:0,text_complete:false}:bufferRef.current.cursor(`step:${stepNo}`)),paused:!playingRef.current,audio_id:reset?null:audioRef.current.sourceId,playback_version:ws?.playback_version||0}},
      });
      setSavedAt(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
      return true;
    } catch (e: any) {
      if(!e?.status||e.status>=500){
        setConnectionWaiting(true);
      } else if (e?.code === "CHECKPOINT_STALE") {
        onErr("位置保存冲突：已按服务端最新位置校准。");
      } else {
        onErr("位置保存失败（本地继续播放）。");
      }
      return false;
    }
  }, [conversationId, onErr,reviewStep,ws?.playback_version]);

  useEffect(()=>{
    if(!playing || advanceTo===null || ws?.conversation_id!==conversationId || ws.session_state!=="active"
        || !ws.steps.some(segment=>segment.step_no===advanceTo))return;
    const target=advanceTo;setAdvanceTo(null);setCurrent(target);posRef.current=0;
    saveCheckpoint(target,0,true);
  },[advanceTo,ws,conversationId,saveCheckpoint,playing]);

  const live=buffer.view(`step:${current}`);
  useEffect(()=>{audioRetryEpoch.current++;setAudioFailure("");setAudioRetrying(false);},[conversationId,current]);
  useEffect(()=>{if(audioStep?.audio_ready)setAudioFailure("");},[audioStep?.audio_ready,audioStep?.audio_id]);
  const displayComplete=Boolean(live?.final && live.remaining===0);
  const finalSegment=Boolean(ws?.steps.find(s=>s.step_no===current)?.lesson_finished);
  const lessonQuizFinished=Boolean(ws?.feynman?.completed?.some((record:any)=>
    record.scope==="lesson"&&record.after_step_no===current&&record.result!=="skipped"));
  const audioComplete=!audioRequired||audio.finished;
  const interactiveBlocks=(audioStep?.board?.presentation?.blocks||[]).filter((block:any)=>
    block.type==="code"&&block.interaction&&codeLanguage(block.language));
  const codeOwnerStep=(block:any)=>ws?.steps.find(step=>step.step_no<=current&&
    step.board_scope===audioStep?.board_scope&&step.board?.presentation?.blocks?.some((item:any)=>
      item.id===block.id&&item.code===block.code))?.step_no||current;
  const pendingCode=interactiveBlocks.find((block:any)=>codeClass.get(codeOwnerStep(block),block).phase!=="done");
  const pendingCell=pendingCode?codeClass.get(codeOwnerStep(pendingCode),pendingCode):null;
  useEffect(()=>{
    if(!pendingCode||!codeClass.ready||!playing||!displayComplete||!audioComplete||reviewStep||
        ws?.session_state!=="active"||readingHistory)return;
    stopAll();setAdvanceTo(null);
    codeClass.update(codeClass.get(codeOwnerStep(pendingCode),pendingCode));
    void codeClass.save();void saveCheckpoint(current,posRef.current);
  },[pendingCode?.id,codeClass.ready,playing,displayComplete,audioComplete,reviewStep,ws?.session_state,readingHistory]);
  const quizBoundary=ws?.feynman?.checkpoints?.find((point:any)=>point.after_step_no===current&&!point.resolved);
  const recorded=useRef(new Set<string>()),[recordError,setRecordError]=useState("");
  const recordPoint=useCallback(async()=>{
    if(reviewStep||!ws?.knowledge?.plan_hash)return true;
    await coverage.flush();
    const boundary=ws.feynman?.checkpoints?.find((p:any)=>p.after_step_no===current);
    if(boundary&&boundary.scope!=="lesson"&&!recorded.current.has(`${conversationId}:${boundary.checkpoint_id}`)){
      try{
        await api.completeKnowledge(conversationId,{checkpoint_id:boundary.checkpoint_id,plan_hash:ws.knowledge.plan_hash});
        recorded.current.add(`${conversationId}:${boundary.checkpoint_id}`);setRecordError("");await refresh();
      }catch{setRecordError("已讲知识点尚未保存。请重试；缺少播放记录时需回放补足。");return false;}
    }
    return true;
  },[conversationId,current,ws?.knowledge?.plan_hash,ws?.feynman?.checkpoints,coverage.flush,refresh,reviewStep]);
  useEffect(()=>{
    if(!playing||!displayComplete||!audioComplete||pendingCode||!quizBoundary||quiz||quizStart.current||readingHistory||Boolean(ws?.knowledge&&!boardPainted))return;
    quizStart.current=true;setQuizAutoPlay(true);setAdvanceTo(null);
    audioRef.current.pause();playingRef.current=false;setPlaying(false);bufferRef.current.pause(true);
    void (async()=>{
      try{if(!await saveCheckpoint(current,posRef.current)){quizStart.current=false;return;}
        try{if(!await recordPoint()){quizStart.current=false;return;}}
        catch{setRecordError("播放记录保存失败，请重试。");quizStart.current=false;return;}
        await api.feynmanAction(conversationId,"start",{checkpoint_id:quizBoundary.checkpoint_id});await refresh();
      }catch(error){onErr(errText(error));quizStart.current=false;}
    })();
  },[playing,displayComplete,audioComplete,pendingCode?.id,quizBoundary?.checkpoint_id,quiz?.session_id,readingHistory,current,conversationId,boardPainted]);
  useEffect(()=>{
    if(!playing||!displayComplete||!audioComplete||pendingCode||finalSegment||readingHistory||quiz||quizBoundary||ws?.session_state!=="active")return;
    const timer=window.setTimeout(()=>{void recordPoint().then(()=>setAdvanceTo(current+1)).catch(()=>setRecordError("播放记录保存失败，请重试。"));},1800);
    return()=>window.clearTimeout(timer);
  },[playing,displayComplete,audioComplete,pendingCode?.id,finalSegment,readingHistory,current,ws?.session_state,quiz,quizBoundary]);

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
    if(ws.feynman?.active)return;
    if(!playing&&await interactionContinue.current())return;
    if(ws.session_state==="awaiting_resume") {
      try {await api.resumeLesson(conversationId);await refresh();}
      catch(e:any){onErr(e.message||"恢复失败");return;}
    } else if(ws.session_state!=="active")return;
    if (playing) {
      stopAll();
      saveCheckpoint(current, posRef.current);
      return;
    }
    audioRef.current.enable();playingRef.current=true;bufferRef.current.pause(false);setPlaying(true);
    textFollow.resume();
    await saveCheckpoint(current,posRef.current);await refresh();
  }, [current, playing, saveCheckpoint, ws, conversationId, refresh, onErr]);

  const sendFollowup = useCallback(async (feedback?:{step_no:number;block_id:string;action:string}) => {
    if ((!feedback&&!followup.trim()) || !ws || busy) return false;
    setBusy(true);
    const permissionEpoch=presentation.epoch.current;
    const epoch=++playbackEpoch.current;answerMedia.current?.pause();setAnswerPlaying(false);setAnswerReady(false);
    setAdvanceTo(null);
    audioRef.current.pause();playingRef.current=false;bufferRef.current.pause(true);setPlaying(false);
    try {
      if(!await saveCheckpoint(current, posRef.current))return;
      if(epoch!==playbackEpoch.current)return;
      const r = feedback?await classroomApi.feedback(conversationId,{...feedback,revision:codeClass.currentRevision()}):await api.sendFollowup(conversationId, {
        content: followup, idempotency_key: `web-fu-${Date.now()}`,
      });
      if(epoch!==playbackEpoch.current){await api.cancelTask(r.task_id);return;}
      setFollowup("");
      if (!r.deduped||feedback&&["queued","failed","expired"].includes(r.status)) {
        setFollowupTask(r.task_id);
        await api.runTeachingTask(r.task_id); // 本地开发直接执行回答
      }
      if(epoch!==playbackEpoch.current)return;
      await refresh();
      if(ws?.tts_authorized){
        try{
          const request=await api.answerAudio(conversationId);
          if(request.task_id){setFollowupTask(request.task_id);let timeout:number|undefined;try{await Promise.race([api.runTeachingTask(request.task_id),new Promise((_,reject)=>{timeout=window.setTimeout(()=>reject(new Error("answer_audio_wait")),12000);})]);}finally{clearTimeout(timeout);}}
          if(epoch!==playbackEpoch.current)return;
          const result=await api.getWorkspace(conversationId);
          const track=result.answer_audio;
          if(track?.audio_id){
            const media=new Audio(api.audioUrl(track.audio_id));answerMedia.current=media;setAnswerReady(true);
            answerTrack.current={audioId:track.audio_id,generation:result.generation,subtitles:track.subtitles,text:result.followups.find((item:any)=>item.generation===result.generation&&item.role==="assistant")?.content||""};
            media.onended=()=>{if(epoch===playbackEpoch.current&&answerPlayingRef.current){bufferRef.current.audioClock(`followup:${result.generation}`,Array.from(answerTrack.current.text).length);bufferRef.current.flush();setAnswerPlaying(false);setAnswerReady(false);}};
            media.onerror=()=>{if(epoch===playbackEpoch.current){setAnswerPlaying(false);setAnswerReady(false);}};
          }
          await refresh();
        }catch{/* Saved answer remains available as clean text. */}
      }
      onOk("回答已保存。");
      if(feedback&&epoch===playbackEpoch.current&&permissionEpoch===presentation.epoch.current){
        if(ws.tts_authorized&&answerMedia.current){
          const media=answerMedia.current;
          try{
            await media.play();
            if(epoch!==playbackEpoch.current||permissionEpoch!==presentation.epoch.current){media.pause();return true;}
            bufferRef.current.pause(false);setAnswerPlaying(true);
          }catch{onErr("点击右下角播放按钮收听代码反馈。");}
        }else{bufferRef.current.pause(false);setAnswerTextPlaying(true);}
      }
      return true;
    } catch (e: any) {
      onErr(e?.message || "追问失败");
      return false;
    } finally {
      if(epoch===playbackEpoch.current){setFollowupTask(null);setBusy(false);}
    }
  }, [conversationId, current, followup, onErr, onOk, refresh, saveCheckpoint, ws, busy]);

  interactionContinue.current=async()=>{
    if(!pendingCode||!pendingCell||!displayComplete||!audioComplete)return false;
    if(interactionContinuing.current)return true;
    interactionContinuing.current=true;
    try{
    if(ws?.session_state==="awaiting_resume"){
      try{await api.resumeLesson(conversationId);await refresh();}catch(error){onErr(errText(error));}
      return true;
    }
    if(codeClass.running){onErr("请等待运行完成，或先停止运行。");return true;}
    if(!codeClass.ready||!pendingCell.runs||pendingCell.status==="idle"){
      onErr("先点击白板代码块的运行按钮，观察结果后再继续。");return true;
    }
    if(pendingCell.phase==="change"&&pendingCell.code===pendingCell.baseline_code){
      onErr("请修改代码中的数据并运行，再继续。");return true;
    }
    const next=pendingCell.phase==="run"?{...pendingCell,phase:"change" as const,
      baseline_code:pendingCell.code,baseline_output:pendingCell.output}:{...pendingCell,phase:"done" as const};
    codeClass.update(next);
    if(!await codeClass.save())return true;
    const success=await sendFollowup({step_no:pendingCell.step_no,block_id:pendingCell.block_id,
      action:pendingCell.phase==="run"?"observe":"compare"});
    if(!success){codeClass.update(pendingCell);await codeClass.save();}
    return true;
    }finally{interactionContinuing.current=false;}
  };

  const recover = async () => {
    if (!ws?.course_id || !ws.lesson_id || busy) return;
    setBusy(true);
    try {
      const result = ws.needs_upgrade || ws.scope_preparation ? await api.startLessonV2(ws.course_id, ws.lesson_id) : await api.recoverLesson(ws.course_id, ws.lesson_id, conversationId);
      if (!result) return;
      if (result.conversation_id !== conversationId) { go(`/lecture/${result.conversation_id}`); return; }
      attemptedTasks.current.clear(); await refresh();
    } catch (error) { onErr(errText(error)); }
    finally { setBusy(false); }
  };

  if (!ws) return <p className="muted" role="status">{connectionWaiting?"连接暂不可用，恢复连接后继续加载课堂。":"加载中…"}</p>;

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
  let boardScene = boardKey.startsWith("followup:") ? ownBoard : stageBoard(ws.steps,ownBoard,current,scope);
  const keepingCode=boardKey.startsWith("followup:")&&interactiveBlocks.length&&ws.code_feedback?.generation===ws.generation;
  if(keepingCode)
    boardScene=stageBoard(ws.steps,audioStep?.board,current,audioStep?.board_scope||scope);
  const codeOrigin=(block:any)=>boardKey.startsWith("followup:")&&!keepingCode
    ?{source_step:0,source_generation:ws.generation}:{source_step:codeOwnerStep(block),source_generation:1};
  if(boardScene?.presentation)boardScene={...boardScene,presentation:{...boardScene.presentation,
    blocks:boardScene.presentation.blocks.map((block:any)=>({...block,...codeOrigin(block)})),
    all_blocks:(boardScene.presentation.all_blocks||boardScene.presentation.blocks).map((block:any)=>({...block,...codeOrigin(block)}))}};
  const originalRefs = boardScene?.original_refs || [];
  const awaiting = ws.session_state === "awaiting_resume";
  const boardPresenting=(playing&&(!audioRequired||audio.state==="playing"))||answerAudible||answerTextPlaying;
  const livePresenting=boardPresenting||quizAudio.state==="playing";
  const controlLabel=livePresenting?"停止讲解":quiz?(quizHelp?"发送求助":"提交回答"):busy?"取消回答":followup.trim()?"发送追问":answerReady?"播放回答":"继续讲解";
  const failures=teachingFailures(ws,current);
  const failedTasks = [...failures.current,...failures.followup];
  const nextFailed=failures.next.length>0;
  const artifactExhausted=typeof ws.artifact_recovery==="object"&&ws.artifact_recovery?.status==="exhausted";
  const artifactRepairing=typeof ws.artifact_recovery==="object"&&ws.artifact_recovery?.status==="repairing";
  const artifactMissing=ws.artifact_recovery===true || Boolean(typeof ws.artifact_recovery==="object"&&(ws.artifact_recovery?.available||ws.artifact_recovery?.required));
  const blocked = ws.authorization_required || ws.required_action === "authorize" || ws.needs_upgrade;
  const scopeWaiting = !!ws.scope_preparation && !blocked;
  const scopeFailed = scopeWaiting && ["failed", "expired", "cancelled", "not_generated"].includes(ws.scope_preparation!.status);
  const generationRetry=failedTasks.some(task=>task.required_action==="retry");
  const recoveryNeeded=!artifactExhausted&&(artifactMissing || generationRetry || failedTasks.length>0);
  const failureCode=failedTasks[0]?.error_code;
  const failureMessage=!failureCode||failureCode==="INTERNAL_ERROR"?"本段准备中断，可重试恢复。":errText({code:failureCode});
  const emptyMessage = scopeWaiting ? scopeFailed
    ? `单元考纲准备未完成。${ws.scope_preparation?.error_code ? errText({code: ws.scope_preparation.error_code}) : "可以重试准备。"}`
    : ws.scope_preparation?.status === "succeeded" ? "单元考纲已就绪，正在准备首段讲解…" : "正在准备本单元考纲，完成后继续讲解…"
    : artifactExhausted ? "讲解补生成已达到重试上限，请检查模型设置。" : artifactRepairing ? "正在重新准备首段讲解…" : artifactMissing ? "首段讲解内容缺失，点击恢复讲解可重新准备。" : blocked ? "讲解配置暂不可用，请检查模型设置或恢复讲解。" : failedTasks.length ? failureMessage : "正在准备首段讲解…";

  return (
    <div className="ink-classroom" data-nav-collapsed={navCollapsed}><aside id="classroom-navigation" className="classroom-nav" aria-label="课堂导航">
      <div className="classroom-nav-top">
      <button className="iconbtn" aria-label="返回课程" title="返回课程" onClick={async()=>{stopAll();await saveCheckpoint(current,posRef.current);await saveAnswerPosition();go(`/course/${ws.course_id}`);}}><ArrowLeft size={20}/></button>
      <button className="iconbtn" aria-label={navCollapsed?"展开课堂侧栏":"收起课堂侧栏"} title={navCollapsed?"展开课堂侧栏":"收起课堂侧栏"} aria-expanded={!navCollapsed} aria-controls="classroom-navigation" onClick={toggleNav}>{navCollapsed?<PanelLeftOpen size={20}/>:<PanelLeftClose size={20}/>}</button>
      </div>
      <h1>{ws.lesson_title||"课堂"}</h1>
      <a className="classroom-people" href={`#/cards?conversation=${encodeURIComponent(conversationId)}`} title="人物导师" onClick={async event=>{
        event.preventDefault();stopAll();await saveCheckpoint(current,posRef.current);go(`/cards?conversation=${encodeURIComponent(conversationId)}`);
      }}><BookOpen size={18}/><span className="classroom-link-label">人物</span></a>
      <div className="classroom-mentor"><span>当前导师</span><strong>{step?.character?.profile?.name||"课堂默认"}</strong></div>
      <a className="classroom-knowledge" href={`#/knowledge/${conversationId}`} title="本节课已讲知识点" aria-label={`本节课 · 已讲知识点${ws.knowledge?` ${ws.knowledge.total}`:""}`} onClick={()=>{stopAll();void saveCheckpoint(current,posRef.current);}}><ListChecks size={18}/><span className="classroom-link-label">本节课 · 已讲知识点{ws.knowledge?` ${ws.knowledge.total}`:""}</span></a>
      <div className="classroom-nav-bottom"><a href="#/capabilities" title="模型设置"><Settings size={18}/><span className="classroom-link-label">设置</span></a>{savedAt&&<time>{savedAt}</time>}</div>
    </aside><div className="lw-simple" data-audio-state={audio.state} data-current-segment={current} data-audio-position={posRef.current}>
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
                {pdfFocus&&<div className="classroom-mode" role="tablist" aria-label="教学视图">
                  <button role="tab" aria-selected={boardMode==="pdf"} onClick={()=>setBoardMode("pdf")}>原文</button>
                  <button role="tab" aria-selected={boardMode==="board"} onClick={()=>setBoardMode("board")}>板书</button>
                </div>}
                {pdfFocus&&<div className="classroom-board-surface" hidden={boardMode!=="pdf"}><PdfLesson conversation={conversationId} focus={pdfFocus}
                  active={boardMode==="pdf"} onTarget={handlePointingTarget} onPainted={handleBoardPainted}/></div>}
                <div className="classroom-board-surface" hidden={boardMode==="pdf"}>
                  <TeachingBoard theme="ink" onTarget={boardMode==="board"?handlePointingTarget:undefined} onPainted={boardMode==="board"?handleBoardPainted:undefined}
                    presenting={boardPresenting&&boardMode==="board"} scene={boardScene}
                    codeContext={hasRunnable?codeClass:undefined} onCodeEdit={()=>{handlePointingTarget(null);if(playing){stopAll();void saveCheckpoint(current,posRef.current);}}}
                    sceneKey={`${conversationId}:${boardKey.startsWith("followup:")&&!interactiveBlocks.length?boardKey:scope}`} />
                </div>
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
          <TeachingPointer target={pointingTarget} active={Boolean(boardScene)}/>
          <CharacterStage character={step?.character} conversationId={conversationId} stepNo={current} playing={livePresenting}
            scene={characterScene({playing:livePresenting,
              waiting:Boolean(quiz&&quiz.state!=="feedback")||busy,understood:quiz?.state==="feedback"&&quiz?.result==="understood",completed:ws.session_state==="completed"})}/>
        </div>

        <div className="tx-col">
          {readingHistory && <div className="stream-controls lecture-follow-control">
            <button className="btn btn-sm" onClick={textFollow.resume}>回到最新</button>
          </div>}
          <div ref={textFollow.viewport} className="stream" role="log" aria-label="讲解文字流" tabIndex={0}
            onScroll={textFollow.onScroll} onWheel={textFollow.onWheel} onTouchStart={textFollow.onTouchStart} onTouchMove={textFollow.onTouchMove} onTouchEnd={textFollow.onTouchEnd}
            onKeyDown={textFollow.onKeyDown} onPointerDown={textFollow.onPointerDown}
            onPointerMove={textFollow.onPointerMove} onPointerUp={textFollow.onPointerUp} onPointerCancel={textFollow.onPointerUp}>
            <div ref={textFollow.content} className="lecture-stream-content">
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
            {ws.feynman?.completed?.map((record:any)=><section key={record.session_id} className="feynman-dialogue" aria-label={record.scope==="lesson"?"已结束的讲末费曼对话":"已结束的知识点问答"}>
              <header><strong>{record.scope==="lesson"?"本讲费曼对话记录":"知识点问答记录"}</strong><span>{record.result==="understood"?"本次解释较完整":record.result==="review"?"需要复习":"未验证"}</span></header>
              {record.history.map((item:any)=><article key={item.id} className={item.role==="user"?"feynman-user":"feynman-teacher"}>
                <small>{item.role==="user"?"我的回答":"导师"}</small><p>{item.text}</p>
              </article>)}
              <FeynmanLearningRecord record={record.learning_record}/>
            </section>)}
            <FeynmanDialogue quiz={quiz} audio={quizAudio} busy={quizBusy} onAction={quizAction}/>
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
          </div>
          <div className="lecture-actions">
            {connectionWaiting&&<p className="help" role="status">连接暂不可用，已保留当前讲解，恢复连接后继续准备。</p>}
            {recordError&&<p role="alert">{recordError}<button className="btn btn-sm" onClick={()=>void recordPoint()}>重试保存</button></p>}
            {scopeFailed && <button className="btn btn-sm" disabled={busy} onClick={recover}>重试准备考纲</button>}
            {!scopeWaiting && (blocked || recoveryNeeded || artifactExhausted) && <div role="status"><p>{emptyMessage}</p>
              {(blocked || recoveryNeeded) && <button className="btn btn-sm" disabled={busy} onClick={recover}>{artifactMissing || blocked ? "恢复讲解" : "重试生成"}</button>}
              <a className="btn btn-sm" href="#/capabilities">模型设置</a>
            </div>}
            {nextFailed&&!blocked&&<p className="help" role="status">下一段暂未准备好，将在可恢复时自动重试；当前讲解可以继续。
              <button className="btn btn-sm" disabled={busy} onClick={recover}>重试准备下一段</button></p>}
            {finalSegment && displayComplete && audioComplete && !quiz && quizBoundary?.scope==="lesson" && <button className="btn btn-pri btn-sm" disabled={busy||awaiting} onClick={async()=>{
              try {if(!await saveCheckpoint(current,posRef.current))return;
                await api.feynmanAction(conversationId,"start",{checkpoint_id:quizBoundary.checkpoint_id});
                setQuizAutoPlay(true);await refresh();
              }catch(error){onErr(errText(error));}
            }}>开始本讲费曼对话</button>}
            {finalSegment && ((displayComplete && audioComplete)||lessonQuizFinished) && !quiz && !quizBoundary && <button className="btn btn-pri btn-sm" disabled={busy||awaiting} onClick={async()=>{
              try {if(!await recordPoint())return;await api.completeLesson(conversationId);go(`/knowledge/${conversationId}`);}
              catch(e:any){onErr(e.message||"完成确认失败");}
            }}>确认本讲已学完</button>}
          </div>
          {audioRequired&&!audio.textOnly&&<div className="lecture-audio-hint" role="status">
            {audio.state==="blocked"?<button className="btn btn-sm" onClick={audio.enable}>点击开启声音</button>:
              audio.state==="failed"||audioFailure||["failed","invalid"].includes(audioStep?.audio_status||"")?<>
                <span>{audio.alignmentFailed?"语音字幕无法与讲稿精确对应，请重新准备语音。":audioStep?.audio_error==="subtitle_missing"?"语音字幕缺失，暂时无法同步播放。":audioFailure||"语音暂时不可用。"}</span>
                <button className="btn btn-sm" disabled={audioRetrying} onClick={async()=>{
                  const epoch=audioRetryEpoch.current;
                  setAudioRetrying(true);setAudioFailure("");audio.retry();
                  try{
                    if(!audioStep?.audio_ready){
                      const request=await api.ensureLessonAudio(conversationId,current);
                      if(request.task_id){
                        audioAttempts.current.add(request.task_id);
                        await api.runTeachingTask(request.task_id);
                      }
                    }
                    if(epoch===audioRetryEpoch.current)await refresh();
                  }catch{
                    if(epoch===audioRetryEpoch.current)setAudioFailure("语音重试失败。");
                  }finally{
                    if(epoch===audioRetryEpoch.current)setAudioRetrying(false);
                  }
                }}>重试语音</button>
              </>:!audioStep?.audio_ready?<span>正在准备语音…</span>:null}
          </div>}
          <div className="composer2" style={{ padding: "12px 20px 18px", boxShadow: "0 -8px 20px rgba(20,30,45,.06)" }}>
            {pendingCode&&!playing&&ws.session_state==="active"&&displayComplete&&<div className="code-interaction-prompt" role="status">
              <p>{pendingCell?.phase==="change"?((ws.code_feedback?.step_no===pendingCell.step_no&&
                ws.code_feedback?.block_id===pendingCell.block_id&&ws.code_feedback.instruction)||pendingCode.change_instruction||"修改代码中的一个数据，运行后观察结果变化。"):
                (pendingCode.run_instruction||"点击白板代码块的运行按钮，观察结果。")}</p>
              <span>观察好了，点击右下角的继续按钮。</span>
            </div>}
            {hasRunnable&&codeClass.environment&&<div className="code-environment" role="status">{codeClass.environment}</div>}
            {hasRunnable&&codeClass.error&&<div className="code-state-error" role="alert">{codeClass.error}
              <button type="button" onClick={()=>codeClass.ready?void codeClass.save():codeClass.reload()}>{codeClass.ready?"重试保存":"重新加载"}</button></div>}
            <div className="box">
              {quiz&&<div className="feynman-input-mode"><span>{quizHelp?"向导师求助":"回答本题"}</span>
                <button type="button" disabled={quizBusy||quiz.state!=="waiting"} onClick={()=>setQuizHelp(value=>!value)}>{quizHelp?"返回作答":"先问一下"}</button></div>}
              <label className="sr" htmlFor="fu">{quiz?(quizHelp?"向导师求助":"回答本题"):"课程追问"}</label>
              <textarea
                className="ta"
                id="fu"
                placeholder={quiz?(quizHelp?"说说你不理解的地方…":quiz.scope==="lesson"?"用自己的话解释本讲的核心概念与机制…":"用自己的话解释当前知识点…"):"针对当前讲次追问，比如：这个例子换个场景还成立吗？"}
                value={followup}
                maxLength={quiz?3000:1000}
                onChange={(e) => {setFollowup(e.target.value);quizRequestKey.current="";}}
                onKeyDown={e=>{
                  if(e.key==="Enter"&&!e.shiftKey&&!e.nativeEvent.isComposing){
                    e.preventDefault();if(quiz){void answerQuiz();}else if(!busy&&followup.trim())sendFollowup();
                  }
                }}
              />
              <button className="lecture-play-control" aria-label={playing?"停止讲解":controlLabel}
                title={playing?"停止讲解":controlLabel}
                onClick={async()=>{
                  if(playing||answerPlaying||answerTextPlaying||quizAudio.state==="playing"){stopAll();await saveCheckpoint(current,posRef.current);await saveAnswerPosition();return;}
                  if(quiz){await answerQuiz();return;}
                  if(busy){playbackEpoch.current++;setBusy(false);answerMedia.current?.pause();if(followupTask){try{await api.cancelTask(followupTask);await refresh();}catch(error){onErr(errText(error));}}setFollowupTask(null);return;}
                  if(answerPlaying){answerMedia.current?.pause();setAnswerPlaying(false);return;}
                  if(playing){await togglePlay();return;}
                  if(followup.trim()){await sendFollowup();return;}
                  if(answerReady&&answerMedia.current){const media=answerMedia.current,epoch=presentation.epoch.current;try{if(media.currentTime===0&&answerTrack.current){bufferRef.current.restoreCursor(`followup:${answerTrack.current.generation}`,0);}await media.play();if(epoch!==presentation.epoch.current){media.pause();return;}bufferRef.current.pause(false);setAnswerPlaying(true);}catch{onErr("请再次点击三角按钮开启回答语音。");}return;}
                  if(awaiting&&!ws.tts_authorized&&buffer.view(`followup:${ws.generation}`)?.remaining){bufferRef.current.pause(false);setAnswerTextPlaying(true);return;}
                  await togglePlay();
                }}
                disabled={quizAudio.state==="playing"||playing||answerPlaying||answerTextPlaying?false:quiz?quizBusy||quiz.state!=="waiting"||!followup.trim()||!quizAudio.done:!busy&&!playing&&!followup.trim()&&ws.session_state!=="active"&&!awaiting}>
                {playing||busy||answerPlaying||answerTextPlaying||quizAudio.state==="playing"?<Square size={16} fill="currentColor"/>:<Play size={18} fill="currentColor"/>}
              </button>
            </div>
          </div>
        </div>
      </LectureSplit>
    </div></div>
  );
}
