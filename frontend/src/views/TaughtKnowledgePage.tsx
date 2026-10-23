import {useCallback,useEffect,useState} from "react";
import {ArrowLeft,ArrowUpRight,Play,Pencil,Check,X} from "lucide-react";
import {api} from "../api";
import {errText} from "../lib/errors";
import "./taughtKnowledge.css";

export default function TaughtKnowledgePage({conversationId,go,onErr}:{conversationId:string;go:(p:string)=>void;onErr:(s:string)=>void}){
  const [view,setView]=useState<any>(null),[error,setError]=useState(""),[loading,setLoading]=useState(true);
  const [editing,setEditing]=useState(false),[title,setTitle]=useState(""),[saving,setSaving]=useState(false);
  const load=useCallback(async()=>{
    setError("");
    try{const result=await api.knowledge(conversationId);setView(result);setTitle(result.lesson_title);}
    catch(e){setError(errText(e));}finally{setLoading(false);}
  },[conversationId]);
  useEffect(()=>{setView(null);setLoading(true);void load();},[load]);
  const rename=async()=>{
    if(!title.trim()||saving)return;
    setSaving(true);
    try{const result=await api.renameLesson(conversationId,title,view.lesson_title);setView({...view,lesson_title:result.lesson_title});setEditing(false);}
    catch(e){onErr(errText(e));}finally{setSaving(false);}
  };
  const labels:Record<string,string>={passed:"已通过",pending:"待回答",review:"待巩固",skipped:"已跳过"};
  return <main className="taught-page">
    <header><button className="iconbtn" aria-label="返回课堂" title="返回课堂" onClick={()=>go(`/lecture/${conversationId}`)}><ArrowLeft size={20}/></button>
      <div><p>本节课 / 已讲知识点</p>{editing?<form onSubmit={e=>{e.preventDefault();void rename();}}>
        <input aria-label="本节课名称" autoFocus value={title} maxLength={200} onChange={e=>setTitle(e.target.value)}
          onBlur={e=>{if(!e.currentTarget.form?.contains(e.relatedTarget as Node))void rename();}}
          onKeyDown={e=>{if(e.key==="Escape"){setEditing(false);setTitle(view.lesson_title);}}}/>
        <button className="iconbtn" aria-label="保存讲次名称" title="保存讲次名称" disabled={saving}><Check size={18}/></button>
        <button className="iconbtn" type="button" aria-label="取消重命名" title="取消重命名" onClick={()=>{setEditing(false);setTitle(view.lesson_title);}}><X size={18}/></button>
      </form>:<div className="taught-title"><h1>{view?.lesson_title||"本节课"}</h1>{view&&<button className="iconbtn" aria-label="重命名本节课" title="重命名本节课" onClick={()=>setEditing(true)}><Pencil size={17}/></button>}</div>}</div>
      <button className="btn btn-pri" onClick={()=>go(`/lecture/${conversationId}`)}><Play size={16}/>继续学习</button>
    </header>
    {loading?<p role="status">正在读取已讲知识点…</p>:error?<div role="alert"><p>{error}</p><button className="btn" onClick={load}>重试读取</button></div>:view&&<>
      <p className="taught-count">已讲完 <strong>{view.total}</strong> 个知识点</p>
      {!view.data.length?<section className="taught-empty"><h2>还没有已讲知识点</h2><p>讲完第一个知识点后，会记录在这里。</p></section>:
        <div className="taught-list">{view.data.map((record:any,index:number)=><article key={record.record_id}>
          <span className="taught-number">{String(index+1).padStart(2,"0")}</span><div><h2>{record.title}</h2>{record.summary&&<p>{record.summary}</p>}
          <small>讲完于 {new Date(record.completed_at).toLocaleString("zh-CN")}</small>
          <details><summary>回顾来源</summary><p>本知识点包含 {record.sources.length} 段讲解。</p>
            <button className="btn btn-sm" onClick={()=>go(`/lecture/${record.conversation_id}?review=${record.sources[0]?.step_no||record.after_step_no}`)}>暂停回看讲解与板书</button></details></div>
          <span className={`taught-status ${record.assessment}`}>问答 · {labels[record.assessment]||"待回答"}</span>
          <button className="iconbtn" aria-label={`回看${record.title}`} title={`回看${record.title}`} onClick={()=>go(`/lecture/${record.conversation_id}?review=${record.sources[0]?.step_no||record.after_step_no}`)}><ArrowUpRight size={19}/></button>
        </article>)}</div>}
      {view.next_after&&<button className="btn" onClick={async()=>{try{const next=await api.knowledge(conversationId,view.next_after);setView({...next,data:[...view.data,...next.data]});}catch(e){onErr(errText(e));}}}>加载更多</button>}
    </>}
  </main>;
}
