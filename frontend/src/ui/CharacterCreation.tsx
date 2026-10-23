import {useEffect,useRef,useState,type KeyboardEvent} from "react";
import {ArrowLeft,ArrowUp,FileText,Plus,Save,SlidersHorizontal,Sparkles,Square,X} from "lucide-react";
import {api,characterApi} from "../api";
import {errText} from "../lib/errors";

type Message={role:"user"|"assistant";content:string;search_note?:string;build_id?:string};
export default function CharacterCreation({onCustom,onClose,onAccepted}:{onCustom:()=>void;onClose:()=>void;onAccepted:(card:any)=>void}){
  const [view,setView]=useState<"choices"|"chat">("choices");
  const [messages,setMessages]=useState<Message[]>([]);
  const [input,setInput]=useState("");
  const [material,setMaterial]=useState("");
  const [filename,setFilename]=useState("");
  const [build,setBuild]=useState<any>(null);
  const [preview,setPreview]=useState<{build_id:string;reply:string;question:string}|null>(null);
  const [error,setError]=useState("");
  const [busy,setBusy]=useState(false);
  const [fileBusy,setFileBusy]=useState(false);
  const fileRef=useRef<HTMLInputElement>(null);
  const inputRef=useRef<HTMLTextAreaElement>(null);
  const sending=useRef(false);
  const accepted=useRef("");
  const active=Boolean(build?.build_id&&["queued","running","leased"].includes(build.status));
  const remember=(id:string)=>{
    try{sessionStorage.setItem("persona.dialogue.latest",id);}catch{/* session storage may be unavailable */}
  };
  const forget=()=>{
    try{sessionStorage.removeItem("persona.dialogue.latest");}catch{/* session storage may be unavailable */}
  };
  useEffect(()=>{
    if(!build?.request?.complete_card||!build.accepted_revision||build.status!=="succeeded"
      ||accepted.current===build.build_id)return;
    let live=true;
    setBusy(true);
    void characterApi.get(build.card_id).then(card=>{
      if(!live)return;
      accepted.current=build.build_id;forget();onAccepted(card);
    }).catch(problem=>{if(live)setError(errText(problem));})
      .finally(()=>{if(live)setBusy(false);});
    return()=>{live=false;};
  },[build?.build_id,build?.accepted_revision,build?.status,build?.card_id,build?.request?.complete_card,onAccepted]);

  useEffect(()=>{
    const stream=document.querySelector<HTMLElement>(".persona-chat-stream");
    if(stream)stream.scrollTop=stream.scrollHeight;
  },[messages,preview,build?.stages?.length]);

  const appendReply=(result:any)=>{
    const dialogue=result.result?.dialogue;
    if(dialogue)setMessages(rows=>[...rows.filter(row=>row.build_id!==result.build_id),{role:"assistant",
      build_id:result.build_id,
      content:[dialogue.reply,dialogue.question].filter(Boolean).join("\n\n"),
      ...(dialogue.search_note?{search_note:dialogue.search_note}:{})}]);
  };
  useEffect(()=>{
    if(!active)return;
    let live=true,reconnect:number|undefined;
    const controller=new AbortController();
    const refresh=()=>api.characterBuild(build.build_id).then(value=>{
      if(!live)return;
      setBuild(value);
      if(value.status==="succeeded"){setPreview(null);appendReply(value);}
      else if(["failed","expired"].includes(value.status))setError("本轮未完成。已保留对话，可重试。");
    }).catch(problem=>{if(live)setError(errText(problem));});
    const connect=()=>{
      let terminal=false;
      void api.streamCharacterBuild(build.build_id,event=>{
        if(!live)return;
        if(event.event==="preview"&&event.build_id===build.build_id)setPreview(event);
        if(event.event==="done"){terminal=true;void refresh();}
        if(event.event==="error")setPreview(null);
      },controller.signal).catch(()=>{/* The durable task is still recovered through polling. */}).finally(()=>{
        if(live&&!terminal)reconnect=window.setTimeout(connect,1500);
      });
    };
    connect();
    const timer=window.setInterval(()=>void refresh(),1500);
    return()=>{live=false;controller.abort();window.clearTimeout(reconnect);window.clearInterval(timer);};
  },[active,build?.build_id]);
  const chooseAi=async()=>{
    setView("chat");
    setPreview(null);
    setError("");setBusy(true);
    try{
      const id=sessionStorage.getItem("persona.dialogue.latest");
      if(id){
        const restored=await api.characterBuild(id);
        if(restored.request?.dialogue){
          const history:Message[]=(restored.request.dialogue_history||[]).map((item:any)=>({
            role:item.role==="user"?"user":"assistant",content:item.content||""}));
          if(restored.request.message)history.push({role:"user",content:restored.request.message});
          setMessages(history);
          setMaterial(restored.request.source_text||"");
          if(restored.request.source_text)setFilename("已添加的人物材料");
          setBuild(restored);
          if(restored.status==="succeeded"&&restored.result?.dialogue)appendReply(restored);
          setBusy(false);
          return;
        }
      }
    }catch{forget();/* Missing or foreign-owned session IDs start a fresh private draft. */}
    setMessages([{role:"assistant",content:"你想创建什么样的人物？可以直接描述；如果有人物资料，也可以粘贴或上传，我会优先依据材料提炼。"}]);
    setBuild(null);setMaterial("");setFilename("");
    setBusy(false);
    setError("");
  };
  const upload=async(file?:File)=>{
    if(!file)return;
    setFileBusy(true);setError("");
    try{
      const result=await api.characterBuildMaterial(file);
      setMaterial(result.text);setFilename(file.name);
      setMessages(rows=>rows.length?rows:[{role:"assistant",content:"文字已读取。你可以补充希望重点保留的思维、行为或表达特点。"}]);
      inputRef.current?.focus();
    }catch(problem){setError(errText(problem));}
    finally{setFileBusy(false);}
  };
  const send=async(shouldFinish=false)=>{
    if(sending.current||busy||active||fileBusy)return;
    const message=input.trim()||(shouldFinish?"请根据当前对话整理人物卡。":"");
    if(!message&&!material)return;
    sending.current=true;setBusy(true);setError("");setPreview(null);
    const userMessage=message||(filename?"请根据上传的材料提炼人物。":"请根据提供的文字提炼人物。");
    setMessages(rows=>[...rows,{role:"user",content:userMessage}]);
    setInput("");
    try{
      const next=await api.createCharacterBuild({mode:"ai",dialogue:true,topic:"待识别人物",
        purpose:"根据对话和用户材料创作人物，提炼可执行的思维方式、行为与表达边界。",
        source_text:material,message:userMessage,confirmed:true,search_provider:"anysearch",finish:shouldFinish,
        complete_card:true,
        parent_build_id:build?.build_id||"",request_key:crypto.randomUUID(),
        subject:{kind:"real"},research_policy:{max_searches:5,max_reads:20,max_candidates:50}});
      remember(next.build_id);
      setBuild(next);
      if(["failed","expired"].includes(next.status))setError("本轮未完成。已保留对话，可重试。");
      else if(next.status==="succeeded")appendReply(next);
    }catch(problem){
      setError(errText(problem));
      setMessages(rows=>rows.filter((_,index)=>index!==rows.length-1));
      setInput(message);
    }finally{sending.current=false;setBusy(false);}
  };
  const retry=async()=>{
    if(!build?.build_id||sending.current)return;
    sending.current=true;setBusy(true);setError("");setPreview(null);
    try{setBuild(await api.characterBuildAction(build.build_id,"retry"));}
    catch(problem){setError(errText(problem));}
    finally{sending.current=false;setBusy(false);}
  };
  const cancel=async()=>{
    if(!active||busy)return;
    setBusy(true);setError("");
    try{setBuild(await api.characterBuildAction(build.build_id,"cancel"));}
    catch(problem){setError(errText(problem));}
    finally{setBusy(false);}
  };
  const onKeyDown=(event:KeyboardEvent<HTMLTextAreaElement>)=>{
    if(event.key==="Enter"&&!event.shiftKey&&!event.nativeEvent.isComposing){
      event.preventDefault();void send();
    }
  };

  if(view==="choices")return <div className="modal-mask persona-choice-mask" onClick={event=>{
    if(event.target===event.currentTarget)onClose();
  }}>
    <section className="modal persona-choice" role="dialog" aria-modal="true" aria-labelledby="persona-choice-title">
      <header className="modal-head"><h1 id="persona-choice-title">创建人物卡</h1>
        <button className="iconbtn" aria-label="关闭" onClick={onClose}><X size={19}/></button></header>
      <div className="persona-choice-actions">
        <button className="btn" onClick={chooseAi}><Sparkles size={18}/>AI 创作</button>
        <button className="btn" onClick={onCustom}><SlidersHorizontal size={18}/>自定义</button>
      </div>
    </section>
  </div>;

  return <div className="character-create persona-dialogue">
    <header className="character-header persona-dialogue-head">
      <div className="row"><button className="iconbtn" aria-label="返回人物列表" onClick={()=>{forget();onClose();}}><ArrowLeft size={20}/></button><h1>AI 创作</h1></div>
      {build&&!build.request?.complete_card&&<button className="btn" disabled={active||busy||!messages.some(row=>row.role==="user")}
        onClick={()=>void send(true)}><FileText size={16}/>整理人物卡</button>}
    </header>
    <div className="persona-dialogue-grid">
      <section className="persona-dialogue-main" aria-label="AI 人物创作对话">
        <div className="persona-chat-stream">
          {messages.map((row,index)=><article className={`persona-message ${row.role}`} key={`${index}-${row.content.slice(0,18)}`}>
            <span className="persona-message-who">{row.role==="assistant"?"AI":"我"}</span>
            {row.search_note&&<small className="persona-search-note">{row.search_note}</small>}
            <p>{row.content}</p>
          </article>)}
          {preview&&preview.build_id===build?.build_id&&build?.status!=="succeeded"&&(preview.reply||preview.question)&&
            <article className="persona-message assistant persona-streaming" aria-label="AI 正在回复" aria-busy={active}>
              <span className="persona-message-who">AI</span>
              <p>{[preview.reply,preview.question].filter(Boolean).join("\n\n")}</p>
            </article>}
          {build?.result?.dialogue&&<div className="persona-turn-actions">
            {build.result.dialogue.question_count>0&&<span className="help">材料追问 {build.result.dialogue.question_count} / 8</span>}
          </div>}
          {error&&<div role="alert" className="persona-error">{error}
            {build?.build_id&&["failed","expired"].includes(build.status)&&
              <button className="btn btn-sm" disabled={busy} onClick={()=>void retry()}>重试本轮</button>}
          </div>}
        </div>
        <div className="persona-composer">
          {filename&&<div className="persona-attachment"><FileText size={15}/><span>{filename} · 已读取</span>
            {!build?.build_id&&<button className="iconbtn" aria-label="移除资料" onClick={()=>{setMaterial("");setFilename("");}}><X size={16}/></button>}</div>}
          <textarea ref={inputRef} rows={2} aria-label="继续与 AI 交流" placeholder="描述人物，或继续补充你的想法…" value={input}
            disabled={busy||active||fileBusy} onChange={event=>setInput(event.target.value)} onKeyDown={onKeyDown}/>
          <div className="persona-composer-actions">
            <div className="row"><button className="iconbtn" aria-label="上传 DOCX 或 TXT" title="上传 DOCX 或 TXT"
              disabled={busy||active||fileBusy} onClick={()=>fileRef.current?.click()}>{fileBusy?<span className="spin" aria-hidden="true"/>:<Plus size={20}/>}</button>
            </div>
            <button className="persona-send" aria-label={active?"取消本轮":"发送消息"} title={active?"取消本轮":"发送消息"}
              disabled={busy||fileBusy||(!active&&!input.trim()&&!material)} onClick={()=>void (active?cancel():send())}>
              {active||busy?<Square size={16} fill="currentColor"/>:<ArrowUp size={19}/>}
            </button>
          </div>
          <input ref={fileRef} hidden type="file" accept=".docx,.txt,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain"
            onChange={event=>{const file=event.target.files?.[0];event.target.value="";void upload(file);}}/>
        </div>
      </section>
      {build?.result?.dialogue&&<aside className="persona-ai-draft" aria-label="AI 沉淀的人物草稿">
        <details open><summary>AI 沉淀</summary>
          <h3>人物名称</h3><p>{build.result.dialogue.profile?.name||build.result.dialogue.topic}</p>
          <h3>身份与背景</h3><p>{build.result.dialogue.profile?.identity||"继续交流中"}</p>
          <p>{build.result.dialogue.profile?.background}</p>
          <h3>思维机制</h3><p>{build.result.dialogue.thinking_mechanism}</p>
          <h3>判断方式</h3><p>{build.result.dialogue.decision_method}</p>
          <h3>表达习惯</h3><p>{build.result.dialogue.expression_style}</p>
          <h3>知识边界</h3><p>{build.result.dialogue.knowledge_boundary}</p>
          {active&&build.result.completion?.status==="running"&&<p className="help" role="status">
            正在完成声音与形象 · {build.result.completion.completed_images} / {build.result.completion.total_images}
          </p>}
          {build.result.persona&&!build.request?.complete_card&&<>
            <p className="help" role="status">{build.result.persona.quality?.passed
              ?"人物验收完成，可以保存。"
              :"人物草稿已保留，但验收尚未通过；保存后仍是私有草稿，不能发布。"}</p>
            {build.result.persona.quality?.warnings?.length>0&&<details>
              <summary>待完善项</summary><ul>{build.result.persona.quality.warnings.map((warning:string,index:number)=>
                <li key={index}>{warning}</li>)}</ul>
            </details>}
            <button className="btn btn-pri" disabled={busy}
              onClick={async()=>{setBusy(true);try{const saved=await api.characterBuildAction(build.build_id,"accept");forget();onAccepted(saved);}
                catch(problem){setError(errText(problem));}finally{setBusy(false);}}}>
              <Save size={16}/>保存人物卡
            </button>
          </>}
        </details>
      </aside>}
    </div>
  </div>;
}
