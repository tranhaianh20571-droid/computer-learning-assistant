import { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { api } from "./api";
import { usePlanningAnswerDraft } from "./hooks/usePlanningAnswerDraft";
import MindMapCanvas, { type NodeChange, type NodeOffsets } from "./excalidraw/MindMapCanvas";
import { applyMindMapChanges } from "./lib/mindmapEditing";
import Dialog from "./ui/Dialog";
import PlanningDialogue from "./ui/PlanningDialogue";
import GenerationProgress from "./ui/GenerationProgress";
import { planningMatchLink } from "./lib/learningLinks";
import { errText } from "./lib/errors";

const MODEL_PREPARATION_ERRORS = new Set(["DISCLOSURE_REQUIRED", "DISCLOSURE_EXPIRED"]);
const MODEL_SETTINGS_ERRORS = new Set([
  "DISCLOSURE_REVOKED", "CONFIG_NOT_FOUND", "MODEL_CREDENTIAL_INVALID", "CREDENTIAL_INVALID",
  "MODEL_CAPABILITY_UNAVAILABLE", "CAPABILITY_UNAVAILABLE", "CONNECTOR_OFFLINE", "CONNECTOR_REVOKED",
]);
const REFRESH_ERRORS = new Set(["ACCESS_DENIED", "SESSION_INVALID", "USER_NOT_FOUND", "TASK_NOT_FOUND", "STATE_INVALID"]);

function planningFailure(error: { code?: unknown; status?: number }) {
  const code = typeof error?.code === "string" ? error.code.toUpperCase()
    : error?.status === 401 ? "SESSION_INVALID" : error?.status === 403 ? "ACCESS_DENIED" : "";
  const text: Record<string, string> = {
    DISCLOSURE_REQUIRED: "需要重新准备当前模型后继续生成。",
    DISCLOSURE_EXPIRED: "本次模型使用已过期，可以使用当前模型继续生成。",
    DISCLOSURE_REVOKED: "本次规划的模型使用已被撤销，请检查模型设置。",
    CONFIG_NOT_FOUND: "当前没有可用的内容模型，请检查模型设置。",
    SESSION_INVALID: "当前会话已失效，请刷新页面后继续。",
  };
  return { code, text: text[code] || errText({ code, message: "生成未完成，请稍后重试。" }) };
}

/**
 * 课程规划工作台（v2 三态：访谈 → 生成 → 草稿）：
 * - intake：四至六道模型问题加一道时间预算必答题，完整历史由服务端保存；
 * - generating：任务运行横幅（长任务，请勿刷新）；内部 ID（task_id/draft_id）不外显；
 * - draft：左栏提问与反馈 + 只读画布（草稿生成后直接预览导图，不必先确认）；
 * - 范围澄清保留原有访谈回落；搜索由后台自动执行和降级，不要求用户决策。
 */

type Draft = {
  draft_id: string;
  draft_kind: string;
  target_id: string;
  revision: number;
  content: any;
  coverage?: any;
  review?: any;
  saved_state: string;
  locked?: boolean;
};

/** 访谈问答记录条目（导图步骤左栏只读回看） */
type QaEntry = { q: string; labels: string[]; free?: string; extra?: string };

/** 清洗记录条目（localStorage/服务端数据可能缺字段，缺 labels 会让渲染崩溃） */
function normalizeQaEntry(e: any): QaEntry | null {
  if (!e || typeof e !== "object") return null;
  const labels = Array.isArray(e.labels) ? e.labels.filter((x: any) => typeof x === "string" && x) : [];
  const free = typeof e.free === "string" && e.free ? e.free : undefined;
  const extra = typeof e.extra === "string" && e.extra ? e.extra : undefined;
  const q = typeof e.q === "string" && e.q ? e.q : "";
  if (!q && !labels.length && !free && !extra) return null;
  return { q: q || "访谈反馈", labels, free, extra };
}

/** 访谈问卷：选项卡网格（单选/多选）+ 跳过 + 自由补充 */
function TimeBudgetChoice({label,unit,values,value,min,max,step,onChange}:{
  label:string;unit:string;values:number[];value:any;min:number;max:number;step:number|string;onChange:(value:number|"")=>void;
}) {
  const [custom,setCustom]=useState(value!==undefined&&value!==""&&!values.includes(value));
  return <div className="stack" role="group" aria-label={label}>
    <strong>{label}</strong>
    <div className="opt-grid">
      {values.map(number=><button key={number} type="button" className={`opt-card${!custom&&value===number?" on":""}`}
        aria-pressed={!custom&&value===number} onClick={()=>{setCustom(false);onChange(number);}}><b>{number}{unit}</b></button>)}
      <button type="button" className={`opt-card other${custom?" on":""}`} aria-pressed={custom}
        onClick={()=>{setCustom(!custom);onChange("");}}>都不太符合？自己补充</button>
    </div>
    {custom&&<input className="inp" type="number" required min={min} max={max} step={step}
      aria-label={label} value={value??""} onChange={event=>onChange(event.target.value===""?"":Number(event.target.value))}/>}
  </div>;
}

function IntakeForm({ intake, onDecide, busy, taskId, ownerId, answerDraft, answerRevision }: {
  intake: any; onDecide: (answers: any[], log: QaEntry[]) => void; busy: boolean;
  taskId: string; ownerId: string; answerDraft: any[]; answerRevision: number;
}) {
  const saved = usePlanningAnswerDraft(taskId, ownerId, answerRevision, answerDraft);
  const questions: any[] = (intake.questions || []).filter((question:any)=>intake.schema_version===3||question.question_id!=='scope');
  const sel: Record<string,string[]> = Object.fromEntries(saved.answers.map(a=>[a.question_id,a.selections||[]]));
  const freetext: Record<string,string> = Object.fromEntries(saved.answers.filter(a=>a.editing_free||a.free_text).map(a=>[a.question_id,a.free_text||'']));
  const skipped: Record<string,boolean> = Object.fromEntries(saved.answers.map(a=>[a.question_id,!!a.skipped]));
  const [submitError,setSubmitError]=useState('');
  const timeAnswer = (qid: string) => saved.answers.find(a=>a.question_id===qid)?.time_budget || {};
  const validTime = (value: any) => Number.isInteger(value.weeks) && value.weeks >= 1 && value.weeks <= 104
    && typeof value.hours_per_week === "number" && Number.isFinite(value.hours_per_week)
    && value.hours_per_week >= 0.5 && value.hours_per_week <= 80;
  const change = (qid: string, patch: any) => {
    setSubmitError("");
    const old = saved.answers.find(a=>a.question_id===qid) || {question_id:qid,selections:[],free_text:''};
    saved.update([...saved.answers.filter(a=>a.question_id!==qid), {...old,...patch}]);
  };
  const toggle = (qid: string, key: string, kind: string) => {
    const values=sel[qid]||[];
    change(qid,{skipped:false,
      selections:values.includes(key)?values.filter(x=>x!==key):kind==='multi'?[...values,key]:[key],
      ...(kind === "single" ? { editing_free:false, free_text:"" } : {})});
  };

  const submit = async () => {
    if (questions.some(q=>q.kind==="time_budget"&&!validTime(timeAnswer(q.question_id)))) {
      setSubmitError("请填写学习周期（1～104周）和每周投入（0.5～80小时）。");
      return;
    }
    setSubmitError("");
    try { await saved.beforeSubmit(); } catch (e: any) { setSubmitError(e.message || "保存失败"); return; }
    const answers: any[] = [];
    const log: QaEntry[] = [];
    questions.forEach((q) => {
      if (q.kind === "time_budget") {
        const budget=timeAnswer(q.question_id);
        answers.push({question_id:q.question_id,selections:[],skipped:false,time_budget:budget});
        log.push({q:q.question,labels:[`${budget.weeks}周，每周${budget.hours_per_week}小时`]});
        return;
      }
      const selections = sel[q.question_id] || [];
      const free = (freetext[q.question_id] || "").trim() || undefined;
      const skip=skipped[q.question_id]||selections.length===0&&!free;
      answers.push({ question_id: q.question_id, selections:skip?[]:selections, free_text:skip?undefined:free,skipped:skip });
      log.push({
        q: q.question,
        labels: selections.map((k: string) => (q.options.find((o: any) => o.key === k)?.label) || k),
        free,
        extra:skip?'已跳过':undefined,
      });
    });
    onDecide(answers, log);
  };

  const answered = questions.filter((q) => q.kind==="time_budget" ? validTime(timeAnswer(q.question_id))
    : (sel[q.question_id] || []).length > 0 || freetext[q.question_id]).length;

  return (
    <div className="intake-wrap">
      <div className="intake-head">
        <span className="kicker">开始前，先了解你</span>
        <span className="help">已展示 {intake.shown_count || questions.length} 个问题 · 共 {intake.min_questions || 4}～{intake.max_questions || 6} 个 · {questions.some(q=>q.kind==="time_budget") ? "时间安排必答，其余可跳过" : "可跳过不确定的问题"}</span>
      </div>
      {questions.map((q) => (
        <fieldset className="q-card" key={q.question_id}>
          <legend>
            {q.question}
            {q.kind === "multi" && <span className="badge queued" style={{ marginLeft: 8 }}>多选</span>}
            {q.kind === "time_budget" && <span className="badge queued" style={{ marginLeft: 8 }}>必答</span>}
          </legend>
          {q.kind === "time_budget" ? <div className="stack">
            <TimeBudgetChoice label="学习周期（周）" unit="周" values={[4,8,16,24]}
              value={timeAnswer(q.question_id).weeks} min={1} max={104} step={1}
              onChange={weeks=>change(q.question_id,{skipped:false,time_budget:{...timeAnswer(q.question_id),weeks}})}/>
            <TimeBudgetChoice label="每周投入（小时）" unit="小时" values={[2,5,10,15]}
              value={timeAnswer(q.question_id).hours_per_week} min={0.5} max={80} step="any"
              onChange={hours_per_week=>change(q.question_id,{skipped:false,time_budget:{...timeAnswer(q.question_id),hours_per_week}})}/>
          </div> : <><div className="opt-grid">
            {q.options.map((o: any) => {
              const on = (sel[q.question_id] || []).includes(o.key);
              return (
                <button
                  key={o.key}
                  type="button"
                  className={`opt-card${on ? " on" : ""}`}
                  aria-pressed={on}
                  onClick={() => toggle(q.question_id, o.key, q.kind)}
                >
                  <b>{o.label}</b>
                  {o.detail && <span>{o.detail}</span>}
                </button>
              );
            })}
            <button
              type="button"
              className={`opt-card other${freetext[q.question_id] !== undefined ? " on" : ""}`}
              aria-pressed={freetext[q.question_id] !== undefined}
              onClick={() => change(q.question_id,{skipped:false,
                editing_free:freetext[q.question_id] === undefined,
                free_text:"",
                ...(q.kind === "single" ? { selections:[] } : {})})}
            >
              都不太符合？自己补充
            </button>
          </div>
          {freetext[q.question_id] !== undefined && (
            <input
              className="inp"
              style={{ marginTop: 10, maxWidth: 520 }}
              placeholder="用自己的话补充…"
              value={freetext[q.question_id]}
              maxLength={1000}
              aria-label={`补充 ${q.question}`}
              onChange={(e) => change(q.question_id,{free_text:e.target.value,skipped:false,editing_free:true})}
            />
          )}</>}
          {q.skippable && (
            <button
              type="button"
              className="linkbtn"
              style={{ marginTop: 8 }}
              onClick={() => change(q.question_id,{selections:[],free_text:"",editing_free:false,skipped:true})}
            >
              {skipped[q.question_id]?'已跳过':'跳过这题'}
            </button>
          )}
        </fieldset>
      ))}
      <p role="status" className="help">{submitError || saved.status}</p>
      <div className="row" style={{ marginTop: 18, gap: 12 }}>
        <button className="btn btn-pri" onClick={submit} disabled={busy}>
          {busy ? <><span className="spin" />提交中…</> : "提交并继续"}
        </button>
        <span className="help">已答 {answered}/{questions.length} 题；跳过的题按默认方式处理。</span>
      </div>
    </div>
  );
}

/** Keep required acknowledgement reachable without the optional quality summary. */
function PlanningAcknowledgement({ review, accepted, onAccept }: {
  review: any; accepted: boolean; onAccept: (value: boolean) => void;
}) {
  if (!review?.acknowledgement_required) return null;
  const issues: any[] = Array.isArray(review.issues)
    ? review.issues.filter((issue: any) => issue.dimension !== "prerequisites") : [];
  return (
    <div>
      {issues.length > 0 && (
        <ul className="help" style={{ margin: "6px 0", paddingLeft: 18 }}>
          {issues.slice(0, 8).map((issue, index) => (
            <li key={`${issue.dimension || "issue"}-${index}`}>
              {issue.severity === "critical" ? "【关键】" : ""}{issue.message}
            </li>
          ))}
        </ul>
      )}
        <label className="help" style={{ display: "flex", gap: 8, alignItems: "flex-start", marginTop: 8 }}>
          <input type="checkbox" checked={accepted} onChange={(event) => onAccept(event.target.checked)} />
          {issues.length ? "我已了解并接受以上缺口，仍然使用这份规划" : "我确认使用这份规划"}
        </label>
    </div>
  );
}

export default function PlanningView({ onErr, onOk, needLogin, go, onMapMode, ownerId }: {
  ownerId: string;
  onErr: (s: string) => void; onOk: (s: string) => void; needLogin: boolean; go: (p: string) => void;
  /** 进入导图步骤时通知外壳：自动隐藏侧栏 + 顶栏转极简（离开该步骤自动恢复） */
  onMapMode?: (on: boolean) => void;
}) {
  const [title, setTitle] = useState("");
  const [routeId, setRouteId] = useState("");
  const [taskId, setTaskId] = useState("");
  const [task, setTask] = useState<any>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [mapOffsets, setMapOffsets] = useState<NodeOffsets>({});
  const [mapEditing, setMapEditing] = useState(false);
  const [dialogueOpen, setDialogueOpen] = useState(false);
  const [dialogueBusy, setDialogueBusy] = useState(false);
  const [acceptedIssues, setAcceptedIssues] = useState(false);
  const [selectedPlanNode, setSelectedPlanNode] = useState("");
  const [candidatePreview, setCandidatePreview] = useState<any>(null);
  const [scene, setScene] = useState<any>(null);
  const [draftScene, setDraftScene] = useState<any>(null);
  const [runError, setRunError] = useState("");
  const [runErrorCode, setRunErrorCode] = useState("");
  const [running, setRunning] = useState(false);
  const [qaLog, setQaLog] = useState<QaEntry[]>([]);
  const alive = useRef(true);
  const selectedTask = useRef("");
  const [restoring, setRestoring] = useState(true);
  const [abandoning, setAbandoning] = useState(false);
  const errorSeen = useRef("");
  const loadedDraft = useRef("");
  // ADR-037: resume may fail before the worker ever runs (for example no active content model), which
  // leaves the task "queued" forever. Hold that client-side failure so the 2s poll cannot erase it.
  const heldError = useRef<{ taskId: string; code: string; text: string } | null>(null);
  const resumeLock = useRef(false);
  const resumeVersion = useRef(0);
  useEffect(() => { alive.current=true; return () => { alive.current=false; }; }, []);

  const clearPlanning = useCallback(() => {
    selectedTask.current="";
    setTaskId(""); setTask(null); setDraft(null); setScene(null); setDraftScene(null);
    setRouteId(""); setTitle(""); setRunError(""); setRunErrorCode(""); setRunning(false); setQaLog([]);
    sessionStorage.removeItem("slice3.pendingTask"); sessionStorage.removeItem("slice3.pendingRoute");
    sessionStorage.removeItem("slice3.editDraft"); localStorage.removeItem("slice3.lastPlanningTask");
    loadedDraft.current=""; errorSeen.current=""; heldError.current=null;
  }, []);

  const hydrate = useCallback(async (latest: any) => {
    if (!alive.current || selectedTask.current !== latest.task_id) return;
    if (latest.abandoned || latest.task_status === "cancelled") { clearPlanning(); return; }
    setTask(latest); setTaskId(latest.task_id); setRouteId(latest.target_id || ""); setTitle(latest.title || "");
    const failure = planningFailure({ code: latest.error_code });
    const errorKey=`${latest.task_id}:${latest.generation}:${failure.code}`;
    const held = heldError.current;
    const active = ["queued","leased","running"].includes(latest.task_status) || latest.auto_retry_pending === true;
    if (active && !held) {
      setRunning(true);
      errorSeen.current=""; setRunError(""); setRunErrorCode("");
    } else if (["failed","expired"].includes(latest.task_status) && !failure.code.startsWith("SEARCH_")) {
      heldError.current=null;
      setRunning(false);
      setRunErrorCode(failure.code);
      if(errorSeen.current!==errorKey) {errorSeen.current=errorKey;setRunError(failure.text);}
    } else if (held && held.taskId===latest.task_id) {
      // The task is still queued because the last resume attempt failed before the worker ran:
      // keep that actionable error instead of letting the poll erase it.
      setRunning(false);
      setRunErrorCode(held.code);
      setRunError(held.text);
    } else {
      setRunning(["queued","leased","running"].includes(latest.task_status));
      errorSeen.current=""; setRunError(""); setRunErrorCode("");
    }
    if (latest.draft_id) {
      const d=await api.getDraft(latest.draft_id);
      if (!alive.current || selectedTask.current !== latest.task_id) return;
      const draftKey=`${d.draft_id}:${d.revision}:${d.locked}`;
      if(loadedDraft.current!==draftKey) {loadedDraft.current=draftKey;setDraft(d.locked?null:d);}
    }
    if (latest.confirmed && latest.target_id) {
      const result=latest.target_kind==='course'?await api.courseScene(latest.target_id):await api.routeScene(latest.target_id);
      if(alive.current&&selectedTask.current===latest.task_id)setScene(result);
    }
  }, [clearPlanning]);

  const refreshTask = useCallback(async (id: string) => {
    if (resumeLock.current) return;
    const version = resumeVersion.current;
    try {
      const latest = await api.getPlanningTask(id);
      if (!resumeLock.current && version === resumeVersion.current) await hydrate(latest);
    }
    catch (e: any) {
      if(!resumeLock.current&&version===resumeVersion.current&&alive.current&&selectedTask.current===id&&e.code==='TASK_CANCELLED')clearPlanning();
    }
  }, [hydrate,clearPlanning]);

  const loadScene = useCallback(async (rid: string, kind?: string) => {
    if (!rid) return;
    try {
      setScene((kind === "course" || kind === "course_outline") ? await api.courseScene(rid) : await api.routeScene(rid));
    } catch { setScene(null); }
  }, []);

  const runTask = useCallback(async (id: string, automatic = false) => {
    if (resumeLock.current) return;
    resumeLock.current = true;
    resumeVersion.current++;
    selectedTask.current=id; setTaskId(id); setRunError(""); setRunErrorCode(""); setRunning(true);
    heldError.current=null;
    sessionStorage.removeItem("slice3.editDraft");
    try { await hydrate(await api.resumePlanning(id,automatic)); }
    catch (e: any) {
      if(alive.current&&selectedTask.current===id) {
        const failure = planningFailure(e);
        if(failure.code==='TASK_CANCELLED')clearPlanning();
        else {
          setRunErrorCode(failure.code);
          const text = failure.code.startsWith("SEARCH_") ? "" : failure.text;
          if(text)heldError.current={taskId:id,code:failure.code,text};
          setRunError(text); setRunning(false);
        }
      }
    } finally { resumeLock.current = false; }
  }, [hydrate,clearPlanning]);

  useEffect(() => {
    if(needLogin)return;
    let cancelled=false;
    (async()=>{
      try {
        const pending=sessionStorage.getItem("slice3.pendingTask");
        const editableId=sessionStorage.getItem("slice3.editDraft");
        if(editableId&&!pending){
          const editable=await api.getDraft(editableId);
          if(cancelled)return;
          setRouteId(editable.target_id);setTask({target_kind:editable.draft_kind==='route'?'route':'course',target_id:editable.target_id,confirmed:editable.locked});
          if(editable.locked)await loadScene(editable.target_id,editable.draft_kind);else setDraft(editable);
          return;
        }
        let current;
        try { current = pending ? await api.getPlanningTask(pending) : (await api.currentPlanning()).planning; }
        catch(e:any) {
          if(pending&&[403,404].includes(e.status)) {sessionStorage.removeItem("slice3.pendingTask");current=(await api.currentPlanning()).planning;}
          else throw e;
        }
        if(cancelled)return;
        if(!current||current.abandoned){clearPlanning();return;}
        selectedTask.current=current.task_id;
        await hydrate(current);
        if(cancelled)return;
        sessionStorage.removeItem("slice3.pendingTask");
        // 后台独立接管失败重试与缺失结构；恢复页面只启动尚未执行的排队任务。
        if(!current.confirmed&&!current.wait_state&&current.task_status==="queued"&&!current.auto_retry_pending) {
          await runTask(current.task_id,true);
        }
      } catch(e:any) {
        if(!cancelled){
          const failure = planningFailure(e);
          if(failure.code==='TASK_CANCELLED')clearPlanning();
          else if(!failure.code.startsWith("SEARCH_")){setRunErrorCode(failure.code);setRunError(failure.text);}
        }
      } finally {if(!cancelled)setRestoring(false);}
    })();
    return()=>{cancelled=true;};
  }, [needLogin,hydrate,runTask,loadScene,clearPlanning]);

  useEffect(()=>{
    if(!taskId||restoring)return;
    let cancelled=false;
    let timer: ReturnType<typeof setTimeout>;
    const poll=async()=>{
      await refreshTask(taskId);
      if(!cancelled)timer=setTimeout(poll,2000);
    };
    timer=setTimeout(poll,1000);
    return()=>{cancelled=true;clearTimeout(timer);};
  },[taskId,restoring,refreshTask]);

  const abandon = useCallback(async()=>{
    if(!taskId||abandoning)return;
    setAbandoning(true);
    try {
      const result=await api.abandonPlanning(taskId);
      for(const id of result.task_ids||[taskId]) {
        localStorage.removeItem(`planning.answers.${ownerId}.${id}`);
        localStorage.removeItem(`slice3.intakeLog.${id}`);
      }
      clearPlanning();onOk("已放弃这次规划。");go("/home");
    } catch(e){onErr(errText(e));}
    finally{if(alive.current)setAbandoning(false);}
  },[taskId,abandoning,ownerId,clearPlanning,onOk,onErr,go]);

  // 草稿预览：无已确认版本时直接用草稿内容渲染导图（生成即看图，不必先确认）
  useEffect(() => {
    if (!draft) { setDraftScene(null); return; }
    let cancelled = false;
    api.draftScene(draft.draft_id).then(
      (s: any) => { if (!cancelled) setDraftScene(s); },
      () => { if (!cancelled) setDraftScene(null); },
    );
    return () => { cancelled = true; };
  }, [draft]);

  const createAndRun = useCallback(async () => {
    if (needLogin) return onErr("请先登录。");
    try {
      let rid = routeId;
      if (!rid) {
        const r = await api.createRoute(title || "未命名路线", "self_study");
        rid = r.route_id;
        setRouteId(rid);
      }
      const body = await api.createPlanningTask({
        idempotency_key: `web-${Date.now()}`,
        task_kind: "route_generate",
        planning_scope: "auto", // 范围由AI结合访谈内部判断
        target_route_id: rid,
      });
      setTaskId(body.task_id);
      await runTask(body.task_id);
    } catch (e: any) {
      onErr(e?.message || "创建任务失败");
    }
  }, [needLogin, onErr, routeId, runTask, title]);

  const [duplicate, setDuplicate] = useState<any>(null);
  const [confirming, setConfirming] = useState(false);
  const confirmDraft = useCallback(async (allowDuplicate = false) => {
    if (!draft || confirming) return;
    setConfirming(true);
    try {
      const check = allowDuplicate ? duplicate : await api.draftDuplicates(draft.draft_id, draft.revision);
      if (!allowDuplicate && check.matches?.length && !check.same_target) { setDuplicate(check); return; }
      const confirmed = await api.confirmDraft(draft.draft_id, {
        revision: draft.revision, idempotency_key: `web-confirm-${draft.draft_id}-${draft.revision}`,
        allow_duplicate: allowDuplicate, duplicate_fingerprint: allowDuplicate ? check?.fingerprint : undefined,
        acknowledge_issues: acceptedIssues,
      });
      await api.putViewState(draft.draft_kind === "route" ? "route" : "course",
        `${draft.target_id}:${confirmed.version_id}`, { offsets: mapOffsets });
      // 确认成功不弹横幅（导图切到已确认态即反馈）；标记该草稿已确认，重进页面不复活编辑态
      try { localStorage.setItem(`slice3.confirmedDraft.${draft.draft_id}`, "1"); } catch { /* 隐私模式忽略 */ }
      onOk("");
      setDuplicate(null);
      setDraft(null);
      setScene(null);
      setDraftScene(null);
      setAcceptedIssues(false);
      await loadScene(draft.target_id, draft.draft_kind);
    } catch (e: any) {
      if (e?.code === "PLANNING_DUPLICATE") setDuplicate(e.detail);
      else if (e?.code === "PLANNING_REVIEW_REQUIRED") {
        setDuplicate(null);
        setAcceptedIssues(false);
        onErr("规划体检存在关键缺口，请先勾选确认后再使用。");
      } else { setDuplicate(null); onErr(`确认失败：${e?.message || "错误"}`); }
    } finally { setConfirming(false); }
  }, [draft, duplicate, confirming, loadScene, onErr, onOk, mapOffsets, acceptedIssues]);

  const decision = useCallback(async (payload: Record<string, unknown>) => {
    if(!taskId)return;
    setRunning(true);setRunError("");setRunErrorCode("");
    try {await hydrate(await api.decidePlanningTask(taskId,{...payload,background:true}));}
    catch(e:any){if(alive.current&&selectedTask.current===taskId){const failure=planningFailure(e);setRunErrorCode(failure.code);setRunError(failure.text);setRunning(false);}}
  },[taskId,hydrate]);

  const submitIntake = useCallback((answers: any[], log: QaEntry[]) => {
    onOk("");
    setQaLog((prev) => [...prev, ...log].slice(-40));
    decision({ kind: "intake", answers });
  }, [decision, onOk]);

  const adoptedChange = useCallback(async (editable: Draft) => {
    setDraft(editable);
    setDraftScene(await api.draftScene(editable.draft_id));
    setCandidatePreview(null);
    setSelectedPlanNode("");
    setMapOffsets({});
  }, []);
  useEffect(() => {
    setDialogueOpen(false); setCandidatePreview(null); setAcceptedIssues(false);
  }, [draft?.draft_id]);

  const waitState = task?.wait_state;
  const intake = task?.intake;
  const legacyInterview=waitState==='awaiting_intake'&&intake?.schema_version!==3&&!(intake?.answers||[]).length;
  const upgradedInterviews=useRef(new Set<string>());
  useEffect(()=>{
    if(taskId&&legacyInterview&&!running&&!upgradedInterviews.current.has(taskId)){
      upgradedInterviews.current.add(taskId);decision({kind:'intake',answers:[]});
    }
  },[taskId,legacyInterview,running,decision]);
  const showIntake = waitState === "awaiting_intake" && intake?.questions?.length > 0&&!legacyInterview;
  const hasBoard = Boolean(draft || scene);
  const activeScene = candidatePreview?.scene || (draft ? (draftScene || scene) : scene);
  const editingDraftRef = useRef<Draft | null>(draft);
  const mapSavingRef = useRef(false);
  useEffect(() => { editingDraftRef.current = draft; }, [draft]);
  useEffect(() => {
    if (mapSavingRef.current) return;
    const kind = draft ? "draft" : task?.target_kind === "course" ? "course" : "route";
    const key = draft ? `${draft.draft_id}:${draft.revision}` : scene?.version_id ? `${routeId}:${scene.version_id}` : "";
    if (!key) return;
    let cancelled = false;
    setMapOffsets({});
    api.getViewState(kind, key).then(value => {
      if (!cancelled) setMapOffsets(value.canvas?.offsets || {});
    }).catch(() => { /* default layout remains usable */ });
    return () => { cancelled = true; };
  }, [draft?.draft_id, draft?.revision, scene?.version_id, routeId, task?.target_kind]);
  const beginMapEdit = async () => {
    if (draft) return;
    const kind = task?.target_kind === "course" ? "course" : "route";
    const editable = await api.editPlanningDraft(kind, routeId);
    const preview = await api.draftScene(editable.draft_id);
    const key = `${editable.draft_id}:${editable.revision}`;
    const layout = await api.getViewState("draft", key);
    if (!layout.canvas?.offsets && editable.base_version_id === scene?.version_id) {
      await api.putViewState("draft", key, { offsets: mapOffsets });
    }
    try { localStorage.removeItem(`slice3.confirmedDraft.${editable.draft_id}`); } catch { /* optional marker */ }
    editingDraftRef.current = editable;
    setDraft(editable); setDraftScene(preview);
  };
  const saveMapEdit = async (changes: NodeChange[], offsets: NodeOffsets) => {
    let current = editingDraftRef.current;
    if (!current) throw new Error("未找到可编辑草稿，请重新进入编辑。");
    mapSavingRef.current = true;
    try {
      const content = applyMindMapChanges(current.draft_kind, current.content, changes);
      if (JSON.stringify(content) !== JSON.stringify(current.content)) {
        current = await api.saveDraft(current.draft_id, { revision: current.revision, content });
        editingDraftRef.current = current;
        setDraft(current);
        setDraftScene(await api.draftScene(current!.draft_id));
      }
      await api.putViewState("draft", `${current!.draft_id}:${current!.revision}`, { offsets });
      setMapOffsets(offsets);
    } catch (error: any) {
      throw new Error(error.code === "DRAFT_REVISION_MISMATCH" ? "草稿已在其他页面更新，修改已保留，请刷新后核对。" : error.message || "保存失败，修改已保留。");
    } finally { mapSavingRef.current = false; }
  };
  const courses: any[] = draft?.content?.stages?.flatMap((s: any) => s.courses) || [];
  const missing = courses.filter(c => !c.units?.length);
  const active = running || task?.auto_retry_pending === true;
  const structureIncomplete = task?.structure_status === "partial" || missing.length > 0;
  const autoRetryLimit = Math.min(5, Math.max(1, Number(task?.auto_retry_limit) || 5));
  const autoRetryCount = Math.min(autoRetryLimit, Math.max(0, Number(task?.auto_retry_count) || 0));
  const autoRetryHelp = task?.auto_retry_exhausted
    ? `后台自动重试已达上限（${autoRetryCount}/${autoRetryLimit}），已保留当前规划。`
    : active
      ? `后台正在继续生成，自动重试最多 ${autoRetryLimit} 次；可以暂时离开。`
      : "结构尚未完成，已保留当前规划。";
  // 导图步骤 = 蓝图/草稿已就绪且不在访谈中
  const mapStep = Boolean(routeId && hasBoard && !showIntake);
  const qualityReview = draft?.review && Object.keys(draft.review).length ? draft.review : (task?.review || {});
  const acknowledgementRequired = Boolean(qualityReview?.acknowledgement_required);

  // 导图步骤请求极简外壳（隐藏侧栏、顶栏只留菜单按钮）；离开该步骤（含路由离开）自动恢复
  useEffect(() => {
    if (!mapStep || !onMapMode) return;
    onMapMode(true);
    return () => onMapMode(false);
  }, [mapStep, onMapMode]);

  // 服务端完整历史是恢复真源；本地记录仅兼容旧任务。
  const rawTranscript: any[] = intake?.answers?.length ? intake.answers.map((a: any) => {
    const q = (intake?.history || intake?.questions || []).find((x: any) => x.question_id === a.question_id);
    const labels = a.labels || (Array.isArray(a.selections) ? a.selections : [])
      .map((k: string) => (q?.options || []).find((o: any) => o.key === k)?.label)
      .filter(Boolean);
    return {
      q: a.question || q?.question || "访谈反馈",
      labels,
      extra: a.status==='skipped'?'已跳过':!q && Array.isArray(a.selections) && a.selections.length ? `已选择 ${a.selections.length} 项` : undefined,
      free: a.free_text || undefined,
    };
  }) : qaLog;
  const transcript: QaEntry[] = rawTranscript.map(normalizeQaEntry).filter(Boolean) as QaEntry[];
  const needsModelPreparation = MODEL_PREPARATION_ERRORS.has(runErrorCode);
  const needsModelSettings = MODEL_SETTINGS_ERRORS.has(runErrorCode);
  const needsRefresh = REFRESH_ERRORS.has(runErrorCode);
  const savedProgress = intake?.answers?.length
    ? "已提交的访谈回答已保存，重新进入可恢复这次规划。"
    : task?.answer_draft?.length
      ? "回答草稿已保存，尚未提交；重新进入可继续填写。"
      : task?.draft_id
        ? "规划草稿已保存，重新进入可继续查看。"
        : taskId ? "这次规划已保留，重新进入可恢复进度。" : "请刷新页面，核对上次规划的状态。";
  const recoveryHelp = needsModelPreparation
    ? "继续生成会使用你当前启用的内容模型。"
    : needsModelSettings
      ? "请先检查并更新模型设置，再继续生成；已撤销的同版本模型使用不会自动恢复。"
      : needsRefresh
        ? "请刷新核对会话与任务状态，再继续操作。"
        : task?.auto_retry_exhausted
          ? autoRetryHelp
          : "后台负责可恢复失败的自动重试，最多 5 次；已保留这次规划。";
  const progress = taskId && !task?.confirmed ? <GenerationProgress key={`${ownerId}:${taskId}`}
    taskId={taskId} ownerId={ownerId} generation={task?.generation || 0} active={active}
    recovery={{ count: autoRetryCount, limit: autoRetryLimit, pending: task?.auto_retry_pending === true, exhausted: task?.auto_retry_exhausted === true }} /> : null;

  return (
    <div className="mm-clean">
      {restoring&&<p role="status">正在恢复上次规划…</p>}
      {taskId&&!task?.confirmed&&<div className="row" style={{padding:"12px 32px",justifyContent:"flex-end"}}>
        <button className="btn btn-sm" onClick={()=>go('/home')}>暂时离开</button>
        <button className="btn btn-sm" disabled={abandoning} onClick={abandon}>{abandoning?'正在放弃…':'放弃规划'}</button>
      </div>}
      {duplicate && <Dialog kind="info" title="已有相同课程" onClose={() => { if (!confirming) setDuplicate(null); }} actions={<>
        <button className="btn btn-sm" disabled={confirming} onClick={() => setDuplicate(null)}>返回修改</button>
        <button className="btn btn-sm" disabled={confirming} onClick={() => confirmDraft(true)}>仍然创建</button>
        <button className="btn btn-pri btn-sm" disabled={confirming} onClick={() => { go(planningMatchLink(duplicate.matches[0])); }}>查看已有课程</button>
      </>}>
        <p>已有名称和内容相同的课程。可以直接进入已有课程，保留原学习记录。</p>
        <ul>{duplicate.matches.map((m: any) => <li key={m.id}><a href={`#${planningMatchLink(m)}`}>{m.title}</a> · {m.course_count} 门 · {m.completed ? "已完成" : m.state === "in_progress" ? "学习中" : "未开始"}</li>)}</ul>
      </Dialog>}
      {!mapStep && (
        <div className="mm-clean-head">
          <span className="kicker">课程规划 · {routeId ? "路线" : "新任务"}</span>
          {!taskId && <h1>{title || "课程规划"}</h1>}
          {!taskId && <div className="row" style={{ flexWrap: "wrap", gap: "var(--s3)", alignItems: "center" }}>
            <input
              className="inp"
              style={{ maxWidth: 320 }}
              placeholder="路线标题（可留空自动创建）"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              aria-label="路线标题"
            />
            <button className="btn btn-pri" onClick={createAndRun} disabled={needLogin || running || restoring || !!taskId}>生成路线候选</button>
            {routeId && <button className="btn btn-sm" onClick={() => go(`/route/${routeId}`)}>查看路线总览 →</button>}
          </div>}
          <p className="help">生成会使用你当前启用的内容模型。</p>
        </div>
      )}

      {runError && !active && (
        <section className="planning-recovery" aria-label="规划需要处理" role="status">
          <div><strong>{runError.startsWith("生成未完成") ? runError : `生成未完成 · ${runError}`}</strong>
            <p>{savedProgress}</p><p className="help">{recoveryHelp}</p></div>
          <div className="planning-recovery-actions">
              {needsRefresh ? (
                <button className="btn btn-pri btn-sm" onClick={() => window.location.reload()}>刷新规划</button>
              ) : needsModelSettings ? (
                <>
                  <button className="btn btn-pri btn-sm" onClick={() => go("/capabilities")}>检查模型设置</button>
                  <button className="btn btn-sm" disabled={running || !taskId} onClick={() => { void runTask(taskId, false); }}>
                    更新设置后继续生成
                  </button>
                </>
              ) : needsModelPreparation ? (
                <button className="btn btn-pri btn-sm" disabled={active || !taskId} onClick={() => { void runTask(taskId, false); }}>
                  使用当前模型继续生成
                </button>
              ) : (
                <button type="button" className="btn btn-pri btn-sm" disabled={abandoning || restoring || !taskId}
                  onClick={() => { void runTask(taskId, false); }}>
                  <RefreshCw size={16} aria-hidden="true" />手动重试
                </button>
              )}
              <button className="btn btn-sm" onClick={() => go("/home")}>暂时离开</button>
              <button className="btn btn-sm" disabled={abandoning || !taskId} onClick={abandon}>放弃规划</button>
          </div>
        </section>
      )}

      {hasBoard && !showIntake && progress}
      {taskId && !hasBoard && !showIntake && !task?.confirmed && <div className="planning-intake-layout">
        <div className="planning-pending-progress">{progress}</div>
      </div>}

      {showIntake && (
        <div className="planning-intake-layout">
          <div className="planning-intake-questions">
          <IntakeForm key={taskId+":"+(intake.questions||[]).map((question:any)=>question.question_id).join(':')} intake={intake} onDecide={submitIntake} busy={running} taskId={taskId} ownerId={ownerId} answerDraft={task?.answer_draft||[]} answerRevision={task?.answer_draft_revision||0} />
          </div>
        </div>
      )}

      {waitState === "awaiting_scope_clarification" && <div style={{padding:"12px 32px"}}>
        <button className="btn" disabled={running} onClick={()=>decision({kind:"intake",answers:[]})}>继续了解学习要求</button>
      </div>}

      {routeId && hasBoard && !showIntake && (
        <div className="plan-body">
          <aside className="plan-qa" aria-label="提问与反馈">
            <div className={`plan-qa-log${dialogueOpen ? " dialogue-open" : ""}`}>
              {dialogueOpen && draft && <PlanningDialogue draft={draft} selectedNode={selectedPlanNode}
                onAdopt={adoptedChange} onPreview={setCandidatePreview} onBusy={setDialogueBusy} onErr={onErr}
                onClose={() => { setDialogueOpen(false); setCandidatePreview(null); }} />}
              <details open={!dialogueOpen}><summary className="kicker">生成前访谈记录</summary>
              {transcript.length > 0 ? transcript.map((e, i) => (
                <div className="qa-item" key={i}>
                  <p className="qa-q">{e.q}</p>
                  {e.labels.length > 0 && <p className="qa-a">{e.labels.join(" · ")}</p>}
                  {e.extra && <p className="qa-a">{e.extra}</p>}
                  {e.free && <p className="qa-free">{e.free}</p>}
                </div>
              )) : (
                <p className="help">生成前访谈的提问与回答会记录在这里，仅供回看。</p>
              )}
              </details>
            </div>
            <div className="plan-qa-foot">
              <span className="kicker">反馈</span>
              {draft ? (
                <>
                  <PlanningAcknowledgement review={qualityReview}
                    accepted={acceptedIssues} onAccept={setAcceptedIssues} />
                  {courses.length > 0 && <p role="status">课程结构已完成 {courses.length - missing.length}/{courses.length} 门</p>}
                  {structureIncomplete && <p className="help">课程结构仍在补齐，已保留当前导图。</p>}
                  <button className="btn btn-pri" onClick={() => confirmDraft()} disabled={active || confirming || mapEditing || dialogueBusy || !!candidatePreview || structureIncomplete || (acknowledgementRequired && !acceptedIssues)}>没问题，确认草稿</button>
                  <button
                    className="btn btn-draft"
                    onClick={() => setDialogueOpen(true)}
                    disabled={running || mapEditing || dialogueBusy}
                  >
                    我想修改讲解结构
                  </button>
                </>
              ) : (
                <>
                  <p className="help">已确认为不可变版本，导图与总览同版。</p>
                  <button
                    className="btn btn-pri"
                    onClick={() => go(task?.target_kind === "course" ? `/course/${routeId}` : `/route/${routeId}`)}
                  >
                    {task?.target_kind === "course" ? "查看课程总览 →" : "查看路线总览 →"}
                  </button>
                  <p className="help">课程已确认，规划结构已锁定。</p>
                </>
              )}
            </div>
          </aside>

          <div className="mm-main">
            <div className="stage-wrap" style={{ gridTemplateRows: "minmax(0,1fr)" }}>
              {activeScene ? (
                <div className="canvas-readonly-wrap">
                  {candidatePreview && <div className="candidate-preview-bar" role="status">修改候选预览 · {candidatePreview.stale ? "基于旧草稿" : "采用后才写入草稿"}<button className="btn btn-sm" onClick={() => setCandidatePreview(null)}>回看当前草稿</button></div>}
                  <MindMapCanvas elements={activeScene.elements} height="full" offsets={mapOffsets}
                    onBeginEdit={beginMapEdit} onSave={saveMapEdit} onModeChange={setMapEditing}
                    onSelectNode={setSelectedPlanNode} readOnly={!draft || !!draft.locked || !!candidatePreview || dialogueBusy} ariaLabel="课程结构图：课程、单元、讲次" />
                </div>
              ) : (
                <div className="canvas-readonly-wrap" style={{ display: "grid", placeItems: "center", color: "var(--ink3)" }}>
                  正在加载课程结构图…
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {routeId && !hasBoard && !active && !showIntake && waitState !== "awaiting_scope_clarification" && waitState !== "awaiting_search_decision" && !runError && (
        <div className="note note-info" style={{ margin: "8px 32px" }} role="note">
          <span className="tag">空的</span>
          <div className="tt">还没有规划内容</div>
          <div>回到「新的学习任务」写下目标并回答几个小问题，学伴会先访谈、再生成，画布只在草稿生成后出现。</div>
          <div className="acts"><button className="btn btn-pri btn-sm" onClick={() => go("/home")}>去创建</button></div>
        </div>
      )}
    </div>
  );
}
