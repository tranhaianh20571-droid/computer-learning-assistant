import {useCallback, useEffect, useRef, useState} from "react";
import {ArrowLeft, ArrowRight, ArrowUpRight, Play, RotateCcw, Search, X} from "lucide-react";
import {reviewApi} from "../api";
import {errText} from "../lib/errors";
import CourseCover from "../ui/CourseCover";
import Dialog from "../ui/Dialog";
import QuestionText from "../ui/QuestionText";
import QuestionSource from "../ui/QuestionSource";
import "./review.css";

const labels:Record<string,string> = {overdue:"已逾期",today:"今日到期",week:"未来 7 天",later:"7 天以后"};
const colors:Record<string,string> = {overdue:"#a45040",today:"#946d28",week:"#426454",later:"#becbc0"};
const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai";
const pending = (status:string) => ["queued","leased","running","grading"].includes(status);

export default function ReviewPage({route,go}:{route:string;go:(route:string)=>void}) {
  const [view,setView]=useState<any>(null),[error,setError]=useState(""),[filter,setFilter]=useState("all");
  const [query,setQuery]=useState(""),[busy,setBusy]=useState(false),[quiz,setQuiz]=useState<any>(null);
  const [answer,setAnswer]=useState(""),[rating,setRating]=useState(3),[quizError,setQuizError]=useState("");
  const [points,setPoints]=useState<any>(null);
  const [draftState,setDraftState]=useState("");
  const dialogRef=useRef<HTMLDialogElement>(null);
  const hydratedSession=useRef("");
  const lock=useRef(false), requestKey=useRef(crypto.randomUUID()), generation=useRef(0);
  const params=new URLSearchParams(route.split("?")[1]||"");
  const collection=params.get("collection");
  const sessionId=params.get("session");
  const quizStatus=quiz?.status, quizRevision=quiz?.revision, submittedAnswer=quiz?.answer;
  useEffect(()=>{if(sessionId&&!dialogRef.current?.open)dialogRef.current?.showModal();},[sessionId]);
  const load=useCallback(async()=>{
    try {setView(await reviewApi.overview(timezone));setError("");}
    catch(e){setError(errText(e));}
  },[]);
  useEffect(()=>{queueMicrotask(()=>void load());},[load]);
  useEffect(()=>{
    const refresh=()=>{if(document.visibilityState==="visible")void load();};
    const timer=window.setInterval(refresh,60000);
    window.addEventListener("focus",refresh);
    return ()=>{clearInterval(timer);window.removeEventListener("focus",refresh);};
  },[load]);
  const accept=useCallback((next:any,keepDraft=false)=>{
    hydratedSession.current=next.session_id;
    setQuiz(next);if(!keepDraft){setAnswer(next.answer||next.draft?.answer||"");setRating(next.draft?.rating||3);}
    requestKey.current=crypto.randomUUID();
  },[]);
  useEffect(()=>{
    const gen=++generation.current;
    if(!sessionId){hydratedSession.current="";queueMicrotask(()=>setQuiz(null));return;}
    let cancelled=false;
    let timer:number|undefined;
    const refresh=async()=>{
      try {
        const next=await reviewApi.get(sessionId);
        if(cancelled||generation.current!==gen)return;
        setQuiz(next);
        if(next.answer)setAnswer(next.answer);
        else if(hydratedSession.current!==sessionId){setAnswer(next.draft?.answer||"");setRating(next.draft?.rating||3);}
        hydratedSession.current=sessionId;
        setQuizError("");
        if(pending(next.status))timer=window.setTimeout(refresh,1500);
      } catch(e){if(!cancelled&&generation.current===gen)setQuizError(errText(e));}
    };
    void refresh();
    return ()=>{cancelled=true;clearTimeout(timer);};
  },[sessionId,quizStatus]);
  useEffect(()=>{
    if(!sessionId||quizStatus!=="ready"||submittedAnswer)return;
    let cancelled=false;
    const timer=window.setTimeout(()=>{
      reviewApi.draft(sessionId,{revision:quizRevision,answer,rating})
        .then(()=>{if(!cancelled)setDraftState("草稿已保存");})
        .catch(()=>{if(!cancelled)setDraftState("草稿保存失败，请保持页面打开");});
    },500);
    return ()=>{cancelled=true;clearTimeout(timer);};
  },[sessionId,quizRevision,quizStatus,submittedAnswer,answer,rating]);
  const act=async(action:()=>Promise<any>,keepDraft=false)=>{
    if(lock.current)return;
    lock.current=true;setBusy(true);setQuizError("");
    try{const next=await action();accept(next,keepDraft);void load();}
    catch(e){setQuizError(errText(e));}
    finally{lock.current=false;setBusy(false);}
  };
  const start=async(course:string)=>{
    if(lock.current)return;
    lock.current=true;setBusy(true);setError("");
    try{
      const next=await reviewApi.create(course,requestKey.current,timezone);
      accept(next);go(`/review?session=${encodeURIComponent(next.session_id)}`);
    }catch(e){setError(errText(e));}
    finally{lock.current=false;setBusy(false);}
  };
  const close=()=>{go("/review");void load();setQuizError("");};
  const items=collection ? view?.data.find((c:any)=>c.id===collection)?.courses||[] : view?.data||[];
  const shown=items.filter((c:any)=>(filter==="all"||c.counts[filter]>0)&&
    [c.title,...(c.courses||[]).map((child:any)=>child.title)].join(" ").toLowerCase().includes(query.trim().toLowerCase()));
  const counts=view?.counts||{}, due=(counts.overdue||0)+(counts.today||0);
  let offset=0;
  const courseCard=(c:any)=><article className="review-card" key={c.id}>
    <div className="review-cover"><CourseCover kind={c.cover_owner_kind||c.kind} id={c.cover_owner_id||c.id} title={c.title}/></div>
    <div className="review-card-copy"><p className="review-eyebrow">{c.kind==="route"?`课程合集 · ${c.course_count} 门课程`:`课程 · ${c.unit_count} 单元`}</p>
      <h3>{c.title}</h3>
      <div className="review-counts">{["overdue","today"].map(k=><div key={k}><strong style={{color:colors[k]}}>{c.counts[k]}</strong><span>{labels[k]}</span></div>)}</div>
      <div className="review-card-footer">{c.kind==="route"?<>
        <span>{c.courses.filter((x:any)=>Object.values(x.counts).some(n=>Number(n)>0)).length} 门课程有复习安排</span>
        <button className="btn btn-pri" onClick={()=>{setQuery("");go(`/review?collection=${encodeURIComponent(c.id)}`);}}>查看合集<ArrowRight size={16}/></button>
      </>:<>
        <button className="review-text" onClick={async()=>{try{setPoints({...await reviewApi.points(c.id,timezone),title:c.title});}catch(e){setError(errText(e));}}}>查看知识点<ArrowUpRight size={16}/></button>
        <button className="btn btn-pri" disabled={busy||!((c.counts.overdue||0)+(c.counts.today||0))} onClick={()=>void start(c.id)}>开始复习</button>
      </>}</div>
    </div>
  </article>;
  return <main className="review-page">
    <header className="review-heading"><div><p className="review-eyebrow">学习之后，再想起来</p><h1>我的复习</h1></div>
      <button className="iconbtn" aria-label="刷新复习安排" title="刷新复习安排" onClick={load}><RotateCcw size={19}/></button></header>
    {error&&<div className="note note-err" role="alert"><p>{error}</p><button className="btn" onClick={load}>重试读取</button></div>}
    {!view&&!error?<p role="status">正在读取复习安排…</p>:view&&<>
      <section className="review-overview" aria-label="复习概览">
        <div className="review-ring-block"><div className="review-ring">
          <svg viewBox="0 0 240 240" aria-label="知识点到期时间分布"><circle cx="120" cy="120" r="94" fill="none" stroke="#edf0ec" strokeWidth="20"/>
            {Object.keys(labels).map(k=>{
              const length=view.total?counts[k]/view.total*590.62:0, startOffset=offset;offset+=length;
              return length>0?<circle key={k} cx="120" cy="120" r="94" fill="none" stroke={colors[k]} strokeWidth={filter===k?26:20}
                strokeDasharray={`${Math.max(0,length-4)} ${590.62-Math.max(0,length-4)}`} strokeDashoffset={-startOffset}
                transform="rotate(-90 120 120)"/>:null;
            })}
          </svg><div><span>{filter==="all"?"今日待复习":labels[filter]}</span><strong>{filter==="all"?due:counts[filter]}</strong><small>个知识点</small></div>
        </div><p>已排程 <b>{view.total}</b></p></div>
        <div className="review-rhythm"><h2>复习节奏</h2>
          <div className="review-legend">{Object.keys(labels).map(k=><button key={k} onClick={()=>setFilter(k)} aria-pressed={filter===k}><i style={{background:colors[k]}}/><span>{labels[k]}</span><b>{counts[k]}</b></button>)}</div>
          <div className="review-week-title"><span>未来 7 天新增到期</span><small>不含已逾期</small></div>
          <div className="review-week">{view.week.map((d:any)=><div key={d.date}><span>{d.count}</span><i style={{height:Math.max(3,Math.min(65,d.count/Math.max(1,...view.week.map((x:any)=>x.count))*65))}}/><small>{d.date.slice(5).replace("-","/")}</small></div>)}</div>
        </div>
        <div className="review-today"><p className="review-eyebrow">今日安排</p><h2>{due} 个知识点</h2><p>逾期优先，再复习今日到期</p><p>涉及 {view.due_course_ids.length} 门课程</p>
          <button className="btn btn-pri" disabled={busy||!view.due_course_ids.length} onClick={()=>void start(view.due_course_ids[0])}><Play size={16}/>开始今日复习</button>
          {view.resume_session_id&&<button className="review-text" onClick={()=>go(`/review?session=${encodeURIComponent(view.resume_session_id)}`)}>继续上次复习<ArrowRight size={16}/></button>}
        </div>
      </section>
      <section aria-label="按课程复习">
        <div className="review-library-heading"><h2>按课程复习</h2><label className="review-search"><Search size={17}/><input value={query} onChange={e=>setQuery(e.target.value)} type="search" aria-label="搜索复习课程" placeholder="搜索课程"/></label></div>
        <div className="review-filters" role="group" aria-label="复习时间筛选">
          {[["all","全部课程"],...Object.entries(labels).filter(([k])=>k!=="later")].map(([k,label])=><button key={k} aria-pressed={filter===k} onClick={()=>setFilter(k)}>{label}{k!=="all"&&<small>{counts[k]}</small>}</button>)}
        </div>
        {collection&&<div className="review-collection"><button className="review-text" onClick={()=>{setQuery("");go("/review");}}><ArrowLeft size={17}/>返回全部课程</button><h3>{view.data.find((c:any)=>c.id===collection)?.title||"合集不可用"}</h3></div>}
        {!shown.length?<div className="review-empty"><h3>{view.total?"没有符合条件的复习课程":"还没有复习安排"}</h3><p>{view.total?"可以切换时间范围或清除搜索。":"讲完知识点后，系统会自动安排复习。"}</p>
          <button className="btn" onClick={()=>{setQuery("");setFilter("all");}}>清除筛选</button></div>:<div className="review-grid">{shown.map(courseCard)}</div>}
      </section>
    </>}
    {points&&<Dialog kind="info" title={points.title} onClose={()=>setPoints(null)}>
      <div className="review-points">{points.data.length?points.data.map((p:any)=><div key={p.record_id}><b>{p.title}</b><span>{labels[p.kind]} · {new Date(p.due_at).toLocaleString("zh-CN")}</span></div>):<p>本课程还没有复习安排。</p>}</div>
    </Dialog>}
    {sessionId&&<dialog ref={dialogRef} className="review-quiz" onCancel={e=>{e.preventDefault();close();}} aria-label="课程复习">
      <header><span>{quiz?.title||"课程复习"}</span><button className="iconbtn" aria-label="退出复习" title="退出复习" onClick={close}><X size={20}/></button></header>
      {quizError&&<p role="alert" className="review-error">{quizError}<button className="review-text" onClick={()=>void act(()=>reviewApi.get(sessionId))}>重新读取</button></p>}
      {!quiz?<p role="status">正在读取复习会话…</p>:quiz.status==="finished"?<div className="review-empty"><h2>本轮复习结束</h2>
        <button className="btn btn-pri" onClick={close}>返回复习安排</button>
        {view?.due_course_ids.some((id:string)=>id!==quiz.course_id)&&<button className="btn" onClick={()=>void start(view.due_course_ids.find((id:string)=>id!==quiz.course_id))}>继续下一门课程</button>}
      </div>:<>
        <p className="review-eyebrow">{quiz.position+1} / {quiz.total} · {quiz.point_title}</p>
        {pending(quiz.status)?<div><p role="status">{quiz.status==="grading"?"正在核对回答…":"正在准备本轮复习题…"}</p><button className="review-text" disabled={busy} onClick={()=>void act(async()=>{await reviewApi.cancel(sessionId);return reviewApi.get(sessionId);})}>取消本次处理</button></div>:
        ["failed","expired","cancelled"].includes(quiz.status)?<div role="alert"><p>本次处理未完成，已提交的回答会保留。</p><button className="btn" disabled={busy} onClick={()=>void act(()=>reviewApi.retry(sessionId))}>重试处理</button></div>:quiz.question&&<>
          <QuestionSource source={quiz.question.source}/>
          <h2 className="review-question"><QuestionText text={quiz.question.prompt}/></h2>
          <label className="review-answer-label" htmlFor="review-answer">你的回答</label>
          <textarea id="review-answer" value={answer} maxLength={4000} readOnly={!!quiz.feedback||!!quiz.answer} onChange={e=>{setAnswer(e.target.value);setDraftState("保存中…");}} placeholder="先凭自己的理解回答…"/>
          {!quiz.feedback&&<small role="status">{draftState}</small>}
          {quiz.question.reference&&!quiz.feedback&&<div className="review-feedback"><b>参考答案</b><p><QuestionText text={quiz.question.reference}/></p><small>已查看参考，本题作为受助练习。</small></div>}
          {quiz.feedback?<div className="review-feedback"><b>{quiz.feedback.result==="correct"?"回答正确":quiz.feedback.result==="incorrect"?"需要再巩固":"本次无法确定"}</b><p>{quiz.feedback.feedback}</p>
            <b>参考答案</b><p><QuestionText text={quiz.feedback.reference}/></p><small>{quiz.feedback.scheduled?`下次复习：${new Date(quiz.feedback.due_at).toLocaleString("zh-CN")}`:"本次未更新复习间隔。"}</small>
            <button className="btn btn-pri" disabled={busy} onClick={()=>void act(()=>reviewApi.next(sessionId,quiz.revision))}>下一题<ArrowRight size={16}/></button>
          </div>:<>
            <fieldset className="review-rating"><legend>回忆费力程度</legend>{[[2,"费力想起"],[3,"基本想起"],[4,"轻松想起"]].map(([v,label])=><label key={v}><input type="radio" name="review-rating" value={v} checked={rating===v} onChange={()=>{setRating(Number(v));setDraftState("保存中…");}}/>{label}</label>)}</fieldset>
            <div className="review-quiz-actions"><button className="review-text" disabled={busy||quiz.assisted} onClick={()=>void act(()=>reviewApi.help(sessionId,quiz.revision),true)}>查看参考答案</button>
              <button className="review-text" disabled={busy} onClick={()=>void act(()=>reviewApi.next(sessionId,quiz.revision,true))}>跳过</button>
              <button className="btn btn-pri" disabled={busy||!answer.trim()} onClick={()=>void act(()=>reviewApi.answer(sessionId,{revision:quiz.revision,answer,rating,idempotency_key:requestKey.current}))}>提交回答</button></div>
          </>}
        </>}
      </>}
    </dialog>}
  </main>;
}
