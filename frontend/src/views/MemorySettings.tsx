import {useEffect, useRef, useState} from "react";
import {Check, Eye, EyeOff, LockKeyhole, RefreshCw} from "lucide-react";
import {api} from "../api";
import InfoHint from "../ui/InfoHint";
import ModelPicker from "../ui/ModelPicker";
import {ReasoningSelect,type ReasoningSettings} from "../ui/ReasoningControls";

type Provider = {endpoint:string; model:string; api_key?:string; credential_mask?:string;
  reasoning_settings?:ReasoningSettings};
type Kind = "content" | "embedding" | "rerank";
type Engine = {revision:number; status:string; content:Provider|null; embedding:Provider|null;
  rerank:Provider|null; dimensions:number|null; target_dimensions?:number;
  embedding_status?:"pending"|"detected"|"failed"|"unconfigured";
  memory_dimensions?:number; memory_compatible?:boolean|null;
  rerank_protocol:"rerank"|"jev";
  memory_migration_required?:boolean; migrations?:{status:string;completed:number;total:number}[]};
const empty = ():Provider => ({endpoint:"",model:""});
const labels:Record<Kind,string> = {content:"记忆内容模型",embedding:"Embedding 模型",rerank:"重排模型"};
const hints:Record<Kind,string> = {
  content:"负责理解资料、提炼记忆并生成回答；重建记忆索引前需要先配置它。只影响你自己的记忆，不会读取其他账号的资料。",
  embedding:"把资料和问题转换成向量，用于语义检索。系统默认使用 1024 维，模型不支持时自动回退原生维度；已有记忆时更换模型或维度需要先重建索引。",
  rerank:"对检索出的候选资料重新排序，让更相关的内容排在前面。检索链路需要 Embedding 和重排一起配置；更换重排协议或服务后，建议先用连接测试确认可用。",
};
const DEFAULT_REASONING:ReasoningSettings = {mode:"default",show_summary:false};
const normalizeAddress = (address:string) => address.trim().replace(/\/+$/,"").replace(/\/(?:v1|v1beta)$/,"");

export default function MemorySettings({isAdmin:_isAdmin}:{isAdmin:boolean}) {
  const [saved,setSaved]=useState<Engine|null>(null);
  const [draft,setDraft]=useState<Engine|null>(null);
  const [error,setError]=useState("");
  const [notice,setNotice]=useState("");
  const [busy,setBusy]=useState("");
  const [shown,setShown]=useState<Partial<Record<Kind,boolean>>>({});
  const [tested,setTested]=useState<Partial<Record<Kind,string>>>({});
  const alive=useRef(false), lock=useRef(false);
  const editVersion=useRef(0), pollVersion=useRef(0);
  async function load() {
    setError("");
    try {
      const value=await api.personalMemoryModels();
      if(alive.current){setSaved(value);setDraft(value);setShown({});setTested({});}
    }catch{if(alive.current)setError("加载模型配置失败，请重试。");}
  }
  useEffect(()=>{alive.current=true;void load();return()=>{alive.current=false;};},[]);
  const dirty=JSON.stringify(draft)!==JSON.stringify(saved);
  const migrating=!!saved?.migrations?.some(job=>["pending","replaying","refreshing","cleanup"].includes(job.status));
  useEffect(()=>{
    if(!migrating||dirty)return;
    const timer=window.setInterval(async()=>{
      if(lock.current)return;
      const edits=editVersion.current, request=++pollVersion.current;
      try{
        const value=await api.personalMemoryModels();
        if(alive.current&&!lock.current&&edits===editVersion.current&&request===pollVersion.current){
          setSaved(value);setDraft(value);
        }
      }catch{/* Keep the last confirmed progress; the next poll can recover. */}
    },5000);
    return()=>window.clearInterval(timer);
  },[migrating,dirty]);
  function change(next:Engine){editVersion.current++;setDraft(next);setError("");setNotice("");setTested({});}
  function providerChange(kind:Kind,key:keyof Provider,value:string) {
    if(draft)change({...draft,[kind]:{...(draft[kind]||empty()),[key]:value}});
  }
  function reasonChange(value:ReasoningSettings) {
    if(draft)change({...draft,content:{...(draft.content||empty()),reasoning_settings:value}});
  }
  async function perform(kind?:Kind) {
    if(lock.current||!draft)return;
    lock.current=true;setBusy(kind||"save");setError("");setNotice("");
    let target=saved;
    try {
      if(dirty||!target?.revision||target.status==="disabled"){
        const providers:Record<string,unknown>={};
        for(const name of ["content","embedding","rerank"] as Kind[]){
          const item=draft[name];
          providers[name]=item&&(item.endpoint||item.model||item.api_key)?{
            endpoint:item.endpoint.trim(),model:item.model.trim(),...(item.api_key?{api_key:item.api_key}:{}),
            ...(name==="content"?{reasoning_settings:item.reasoning_settings||DEFAULT_REASONING}:{})
          }:null;
        }
        target=await api.savePersonalMemoryModels({
          expected_revision:saved?.revision||0,
          rerank_protocol:draft.rerank_protocol,...providers,
        });
        if(!alive.current)return;
        setSaved(target);setDraft(target);setShown({});
        setNotice("已保存 · 仅用于你的资料与记忆");
      }
      if(kind&&target){
        const result=await api.testPersonalMemoryModel({kind,revision:target.revision});
        if(alive.current){
          setTested(previous=>({...previous,[kind]:result.state}));
          setNotice(result.dimensions?`连接测试通过 · ${result.dimensions} 维`:"连接测试通过");
          try{
            const refreshed=await api.personalMemoryModels();
            if(alive.current){setSaved(refreshed);setDraft(refreshed);}
          }catch{/* Keep the confirmed test result; the next load can refresh. */}
        }
      }else if(alive.current)setNotice("已保存 · 仅用于你的资料与记忆");
    }catch(e:any){
      if(alive.current)setError(e?.message||"操作未完成，请检查配置；输入内容已保留。");
    }finally{lock.current=false;if(alive.current)setBusy("");}
  }
  return <div className="purpose-settings">
    <header className="purpose-heading"><h2 aria-label="记忆模型">记忆模型<InfoHint label="记忆模型">配置个人记忆和资料检索使用的模型。内容模型负责理解与提炼，Embedding 负责向量检索，重排负责结果排序；只影响你自己的资料与记忆，其他账号的配置和数据不会混用。</InfoHint></h2></header>
    {!draft&&!error&&<p role="status">正在加载模型配置…</p>}
    {error&&<p role="alert" className="capability-detail">{error}{!draft&&<button className="btn" onClick={()=>void load()}>重新加载</button>}</p>}
    {draft&&<section className="purpose-connection" aria-label="个人记忆模型配置">
      {saved?.memory_migration_required&&<div className="memory-migration">
        <p role="status">现有记忆正在使用原配置。重建完成后切换到新模型。</p>
        <button className="btn" type="button" disabled={!!busy||dirty} onClick={async()=>{
          if(lock.current)return;
          lock.current=true;setBusy("rebuild");setError("");
          try{const value=await api.rebuildPersonalMemory();if(alive.current){setSaved(value);setDraft(value);setNotice("已开始重建记忆");}}
          catch(e:any){if(alive.current)setError(e.message||"无法开始重建，请稍后重试。");}
          finally{lock.current=false;if(alive.current)setBusy("");}
        }}>重建记忆索引</button>
      </div>}
      {saved?.migrations?.filter(job=>!["completed"].includes(job.status)).map((job,i)=>
        <p className="help" role="status" key={i}>{({pending:"等待重建",replaying:"重建中",refreshing:"更新画像",cleanup:"清理旧索引",failed:"重建失败",stale:"来源已变化，请重建"} as Record<string,string>)[job.status]||job.status} · {job.completed}/{job.total}</p>)}
      <form onSubmit={event=>{event.preventDefault();void perform();}}>
        {(["content","embedding","rerank"] as Kind[]).map(kind=>{
          const item=draft[kind]||empty(), id="memory-"+kind;
          return <section className="memory-provider" key={kind} aria-label={labels[kind]+"配置"}>
            <h3 aria-label={labels[kind]}>{labels[kind]}<InfoHint label={labels[kind]}>{hints[kind]}</InfoHint></h3>
            {kind==="rerank"&&<div className="purpose-field"><span>协议</span>
              <div className="memory-rerank-options" role="radiogroup" aria-label="重排协议">
                {([["rerank","普通重排"],["jev","Jev"]] as const).map(([value,label])=><label className="checkline" key={value}>
                  <input type="radio" name="personal-memory-rerank" value={value} disabled={!!busy}
                    checked={draft.rerank_protocol===value} onChange={()=>{
                      if(value!==draft.rerank_protocol){change({...draft,rerank_protocol:value,rerank:empty()});setShown(previous=>({...previous,rerank:false}));}
                    }}/>{label}
                </label>)}
              </div>
            </div>}
            <div className="purpose-field"><label htmlFor={id+"-endpoint"}>服务地址</label><div className="purpose-control">
              <input id={id+"-endpoint"} type="url" value={item.endpoint} disabled={!!busy} spellCheck={false}
                required={kind!=="content"||!!item.model||!!item.api_key} placeholder="https://api.example.com/v1"
                onChange={event=>providerChange(kind,"endpoint",event.target.value)}/>
              <button className="iconbtn purpose-test" type="button" title="测试" aria-label={"测试"+labels[kind]}
                disabled={!!busy||!item.endpoint||!item.model} onClick={()=>void perform(kind)}>
                <RefreshCw size={16} className={busy===kind?"purpose-spin":undefined}/>
              </button>
            </div></div>
            <div className="purpose-field"><label htmlFor={id+"-key"}>API Key</label><div className="purpose-secret">
              <input id={id+"-key"} type={shown[kind]?"text":"password"} value={item.api_key||""}
                className={item.credential_mask?"memory-saved-key":undefined}
                autoComplete="off" disabled={!!busy} placeholder={item.credential_mask?"\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022":"填写 API Key"}
                onChange={event=>providerChange(kind,"api_key",event.target.value)}/>
              <button className="iconbtn" type="button" title={shown[kind]?"隐藏密钥":"显示输入的密钥"}
                aria-label={shown[kind]?"隐藏密钥":"显示输入的密钥"} aria-pressed={!!shown[kind]}
                disabled={!!busy||!item.api_key} onClick={()=>setShown(previous=>({...previous,[kind]:!previous[kind]}))}>
                {shown[kind]?<EyeOff size={16}/>:<Eye size={16}/>}
              </button>
            </div></div>
            <div className="purpose-model"><ModelPicker label="模型" value={item.model} disabled={!!busy}
              onChange={value=>providerChange(kind,"model",value)}
              requestKey={JSON.stringify([kind,item.endpoint,item.api_key,draft.rerank_protocol,saved?.revision])}
              trailing={kind==="content"?<ReasoningSelect protocol="openai" endpoint={item.endpoint} model={item.model}
                value={item.reasoning_settings||DEFAULT_REASONING} apiKey={item.api_key||""} disabled={!!busy}
                savedCredential={!!saved?.content?.credential_mask && !!item.endpoint
                  && normalizeAddress(item.endpoint)===normalizeAddress(saved.content?.endpoint||"")}
                discover={api.discoverPersonalMemoryReasoning}
                onChange={reasonChange} onValidChange={()=>{}}/>:undefined}
              load={signal=>api.personalMemoryCatalog({kind,endpoint:item.endpoint,protocol:draft.rerank_protocol,
                ...(item.api_key?{api_key:item.api_key}:{})},signal)}/></div>
            {kind==="embedding"&&<p className="help" role="status">
              {draft.embedding_status==="detected"&&draft.dimensions
                ? `目标 ${draft.target_dimensions||1024} 维 · 当前 ${draft.dimensions} 维 · 记忆库 ${draft.memory_dimensions||2000} 维`
                  + (draft.memory_compatible===false?"（模型不支持缩短，记忆不可用）":"")
                : draft.embedding_status==="failed"
                  ? "维度检测失败，请点击本行测试按钮重试。"
                  : "保存后点击本行测试按钮，系统自动检测维度（默认目标 1024）。"}
            </p>}
            {tested[kind]&&<div className="purpose-capabilities" role="status"><span><Check size={13}/>连接测试通过</span></div>}
          </section>;
        })}
        <div className="purpose-save"><span className="help" role="status">{busy?"处理中…":notice||(dirty?"有未保存的更改":saved?.status==="disabled"?"已停用":saved?.revision?"已保存":"尚未配置")}</span><div>
          {!!saved?.revision&&<button className="btn" type="button" disabled={!!busy||saved.status==="disabled"} onClick={async()=>{
            if(lock.current)return;
            lock.current=true;setBusy("disable");
            try{const value=await api.disablePersonalMemoryModels();if(alive.current){setSaved(value);setDraft(value);setNotice("已停用");}}
            catch{if(alive.current)setError("停用未完成，请重试。");}
            finally{lock.current=false;if(alive.current)setBusy("");}
          }}>停用</button>}
          {dirty&&!!saved?.revision&&<button type="button" className="btn purpose-undo" disabled={!!busy}
            onClick={()=>{setDraft(saved);setShown({});setError("");setNotice("");}}>撤销更改</button>}
          <button type="submit" className={`btn${dirty?" btn-pri":""}`} disabled={!!busy}>保存</button>
        </div></div>
      </form>
      <p className="purpose-scope help"><LockKeyhole size={13}/><span>这些配置仅用于你的工作空间。</span></p>
    </section>}
  </div>;
}
