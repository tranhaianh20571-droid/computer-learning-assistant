import {useEffect, useRef, useState} from "react";
import {Check, Eye, EyeOff, LockKeyhole, RefreshCw} from "lucide-react";
import {api} from "../api";
import ModelPicker from "./ModelPicker";
import InfoHint from "./InfoHint";
import {ReasoningSelect,type ReasoningSettings} from "./ReasoningControls";

const PURPOSES = [["planning", "课程规划"], ["teaching", "课程讲解"], ["dialogue", "即时对话"], ["research", "人物蒸馏"]];
const DEFAULT_REASONING: ReasoningSettings = {mode:"default", show_summary:false};
type Configuration = {
  config_id:string; config_version:number; protocol:string; endpoint:string; model_name:string;
  credential_mask:string; reasoning_settings?:ReasoningSettings; capability_status?:Record<string, {state?:string} | string>;
};
export type Binding = {role:string; revision:number; config_id:string|null; configuration:Configuration|null};

export default function PurposeModelSettings({visible, onSaved,researchOnly=false}:{visible:boolean; onSaved:()=>Promise<void>;researchOnly?:boolean;showAdvanced?:boolean}) {
  const [rows,setRows] = useState<Binding[]>([]);
  const [active,setActive] = useState(researchOnly?"research":"planning");
  const [loading,setLoading] = useState(true);
  const [error,setError] = useState("");
  const alive = useRef(false);
  const version = useRef(0);
  async function load() {
    const request = ++version.current;
    setLoading(true); setError("");
    try {
      const result = await api.modelRoles();
      if (alive.current && request === version.current) setRows(result.data || []);
    } catch {
      if (alive.current && request === version.current) setError("加载模型配置失败，请重试。");
    } finally {
      if (alive.current && request === version.current) setLoading(false);
    }
  }
  useEffect(()=>{alive.current=true; void load(); return ()=>{alive.current=false;version.current++;};},[]);
  const heading=researchOnly?"人物蒸馏模型":"内容模型";
  const description=researchOnly
    ? "用于人物研究与蒸馏，和内容模型分开绑定。它会读取人物资料、提炼特征并生成可复用的人物设定；只影响人物构建相关任务，不会改变普通对话和课程使用的模型。未单独配置时，也不会自动借用内容模型。"
    : "用于对话、课程规划和讲解。每个用途可以单独绑定一个模型服务；修改只影响之后的新任务，已经生成的课程和对话不会被改写。API Key 加密保存在服务端，保存后只显示掩码。";
  return <div className="purpose-settings">
    <header className="purpose-heading"><h2 aria-label={heading}>{heading}<InfoHint label={heading}>{description}</InfoHint></h2></header>
    {loading && <p role="status">正在加载模型配置…</p>}
    {error && <div role="alert">{error} <button className="btn" onClick={()=>void load()}>重新加载</button></div>}
    {!loading && !error && <>
      {!researchOnly&&<nav className="purpose-tabs" aria-label="内容模型用途">
        {PURPOSES.filter(([role])=>researchOnly?role.startsWith("research"):!role.startsWith("research")).map(([role,name])=><button key={role} type="button" aria-current={role===active?"page":undefined} onClick={()=>setActive(role)}>{name}</button>)}
      </nav>}
      {PURPOSES.filter(([role])=>researchOnly?role.startsWith("research"):!role.startsWith("research")).map(([role,name])=>{
        const row=rows.find(item=>item.role===role);
        return row ? <div key={role} hidden={role!==active}>
          <PurposeConnection binding={row} name={name} visible={visible && role===active}
            onUpdate={setRows} onSaved={onSaved}/>
        </div> : null;
      })}
    </>}
  </div>;
}

export function PurposeConnection({binding,name,visible,onUpdate,onSaved}:{
  binding:Binding;name:string;visible:boolean;onUpdate:(rows:Binding[])=>void;onSaved:()=>Promise<void>;
}) {
  const config=binding.configuration;
  const [protocol,setProtocol]=useState(config?.protocol || "openai");
  const [endpoint,setEndpoint]=useState(config?.endpoint || "https://api.openai.com/v1");
  const [model,setModel]=useState(config?.model_name || "");
  const [key,setKey]=useState("");
  const [revealed,setRevealed]=useState("");
  const [showKey,setShowKey]=useState(false);
  const [editingKey,setEditingKey]=useState(false);
  const [reasoning,setReasoning]=useState<ReasoningSettings>(config?.reasoning_settings || DEFAULT_REASONING);
  const [validReasoning,setValidReasoning]=useState(true);
  const [busy,setBusy]=useState("");
  const [error,setError]=useState("");
  const [notice,setNotice]=useState("");
  const [tested,setTested]=useState<Configuration["capability_status"]>();
  const lock=useRef(false);
  const alive=useRef(false);
  const revealVersion=useRef(0);
  const root=useRef<HTMLElement>(null);
  useEffect(()=>{alive.current=true;return ()=>{alive.current=false;revealVersion.current++;};},[]);
  useEffect(()=>{
    setProtocol(config?.protocol || "openai");setEndpoint(config?.endpoint || "https://api.openai.com/v1");
    setModel(config?.model_name || "");setKey("");setRevealed("");setShowKey(false);setEditingKey(false);
    setReasoning(config?.reasoning_settings || DEFAULT_REASONING);setTested(undefined);
  },[config?.config_id,config?.config_version]);
  useEffect(()=>{if(!visible){revealVersion.current++;setRevealed("");setShowKey(false);}},[visible]);
  const dirty=!config || protocol!==config.protocol || endpoint!==config.endpoint || model!==config.model_name || !!key
    || JSON.stringify(reasoning)!==JSON.stringify(config.reasoning_settings || DEFAULT_REASONING);
  function changed() {setNotice("");setError("");setTested(undefined);}
  async function persist() {
    if (!endpoint.trim() || !model.trim() || (!config && !key.trim()) || !validReasoning) {
      setError("请填写有效的地址、模型和 API Key，并检查思考设置。");
      root.current?.querySelector<HTMLInputElement>(!endpoint.trim()?'input[type="url"]':!model.trim()?'.model-picker input':'.purpose-secret input')?.focus();
      return null;
    }
    const result=await api.saveRoleConfiguration(binding.role,{
      expected_revision:binding.revision, config_id:binding.config_id,
      config_version:config?.config_version || null, protocol, endpoint:endpoint.trim(), model_name:model.trim(),
      ...(key.trim()?{api_key:key.trim()}:{}),reasoning_settings:reasoning,
    });
    if(!alive.current)return null;
    onUpdate(result.data);
    setKey("");setRevealed("");setShowKey(false);revealVersion.current++;
    setNotice("已保存 · 新任务将使用此配置");
    // A list refresh failure must not turn a committed save into a failed save.
    void onSaved().catch(()=>{});
    return result.data.find((row:Binding)=>row.role===binding.role)?.configuration as Configuration;
  }
  async function perform(action:"save"|"test") {
    if(lock.current)return;
    lock.current=true;setBusy(action);setError("");setNotice("");
    try {
      const target=dirty ? await persist() : config;
      if(!target)return;
      if(action==="test"){
        const result=await api.testConfig(target.config_id);
        if(!alive.current)return;
        setTested(result.capability_status || {});
        const states=Object.values(result.capability_status || {}).map((value:any)=>typeof value==="string"?value:value.state);
        setNotice(states.length && states.every(value=>value==="available")?"连接测试通过":"测试完成，请查看能力状态。");
      }else if(!dirty)setNotice("已保存");
    }catch(e:any){
      if(alive.current)setError(e?.status===409?"配置已发生变化，请刷新后重试。输入内容已保留。":
        e?.status===401?"登录已失效，请重新登录。":"操作未完成，请检查配置后重试；输入内容已保留。");
    }finally{lock.current=false;if(alive.current)setBusy("");}
  }
  async function toggleKey() {
    if(showKey){revealVersion.current++;setShowKey(false);setRevealed("");return;}
    if(key || !config){setShowKey(true);return;}
    if(lock.current)return;
    const request=++revealVersion.current;
    lock.current=true;setBusy("reveal");setError("");
    try{
      const result=await api.revealContentKey(config.config_id);
      if(alive.current && request===revealVersion.current){setRevealed(result.api_key);setShowKey(true);}
    }catch{if(alive.current && request===revealVersion.current)setError("无法显示密钥，请稍后重试。");}
    finally{lock.current=false;if(alive.current)setBusy("");}
  }
  const capabilities=tested || config?.capability_status || {};
  const labels:Record<string,string>={text:"文本",image:"图像识别",tool_call:"工具调用",json_schema:"结构化输出",streaming:"流式",cancel:"取消"};
  const id=`purpose-${binding.role}`;
  return <section className="purpose-connection" ref={root} aria-label={`${name}模型配置`}>
    <form onSubmit={event=>{event.preventDefault();void perform("save");}}>
      <div className="purpose-field"><label htmlFor={id+"-protocol"}>协议</label><select id={id+"-protocol"} disabled={!!busy} value={protocol} onChange={event=>{setProtocol(event.target.value);changed();}}>
        <option value="openai_responses">Responses (/responses)</option><option value="openai">Chat Completions (/chat/completions)</option><option value="anthropic">Anthropic Messages (/v1/messages)</option><option value="gemini">Gemini (/v1beta)</option>
      </select></div>
      <div className="purpose-field"><label htmlFor={id+"-endpoint"}>服务地址</label><div>
        <div className="purpose-control"><input id={id+"-endpoint"} type="url" required disabled={!!busy} value={endpoint} spellCheck={false} onChange={event=>{setEndpoint(event.target.value);changed();}}/>
          <button className="iconbtn purpose-test" type="button" title="测试" aria-label={`测试${name}模型`} disabled={!!busy} onClick={()=>void perform("test")}>
            <RefreshCw size={16} className={busy==="test"?"purpose-spin":undefined}/>
          </button></div>
        <p className="help">填写基础地址即可，支持带 /v1 或不带 /v1。</p>
      </div></div>
      <div className="purpose-field"><label htmlFor={id+"-key"}>API Key</label><div className="purpose-secret">
        <input id={id+"-key"} type={showKey?"text":"password"} disabled={!!busy} autoComplete="off" value={key || (showKey?revealed:config&&!editingKey?"••••••••••••••••••••••••":"")}
          placeholder={config?"留空保留现有密钥":"填写 API Key"} onFocus={()=>setEditingKey(true)} onBlur={()=>setEditingKey(false)}
          onChange={event=>{setKey(event.target.value);setRevealed("");changed();}}/>
        <button className="iconbtn" type="button" title={showKey?"隐藏密钥":"显示密钥"} aria-label={showKey?"隐藏密钥":"显示密钥"} aria-pressed={showKey} disabled={!!busy} onClick={()=>void toggleKey()}>{showKey?<EyeOff size={16}/>:<Eye size={16}/>}</button>
      </div></div>
      <div className="purpose-model">
        <ModelPicker label="模型" value={model} onChange={value=>{setModel(value);changed();}} disabled={!!busy}
          requestKey={JSON.stringify([config?.config_id,protocol,endpoint,key])}
          trailing={<ReasoningSelect protocol={protocol} endpoint={endpoint} model={model} value={reasoning}
            apiKey={key} savedConfig={config||undefined} visible={visible}
            onChange={value=>{setReasoning(value);changed();}} onValidChange={setValidReasoning} disabled={!!busy}/>}
          load={signal=>api.discoverModels({endpoint,protocol,...(config && protocol===config.protocol?{config_id:config.config_id}:{}),...(key?{api_key:key}:{})},signal)}/>
      </div>
      <div className="purpose-capability-area"><div className="purpose-capabilities" aria-label="能力状态">{Object.entries(capabilities).map(([cap,value])=>{
        const status=typeof value==="string"?value:value.state;
        return <span key={cap} className={status==="unavailable"?"capability-detail":""}>
          {status==="available"&&<Check size={13}/>} {labels[cap]||cap}{status!=="available"?status==="unavailable"?" · 不可用":" · 未确认":""}
        </span>;
      })}</div></div>
      {error&&<p className="capability-detail" role="alert">{error}</p>}
      <div className="purpose-save"><span className="help" role="status">{!busy&&!dirty&&!notice&&<Check/>}{busy==="save"?"正在保存…":busy==="test"?"正在测试…":notice || (dirty?"有未保存的更改":"已保存")}</span>
        <div>{dirty&&config&&<button type="button" className="btn purpose-undo" disabled={!!busy} onClick={()=>{
          setProtocol(config.protocol);setEndpoint(config.endpoint);setModel(config.model_name);setKey("");setRevealed("");setShowKey(false);
          setReasoning(config.reasoning_settings || DEFAULT_REASONING);changed();
        }}>撤销更改</button>}<button type="submit" className={`btn${dirty?" btn-pri":""}`} disabled={!!busy || !validReasoning}>保存</button></div>
      </div>
    </form>
    <p className="purpose-scope help"><LockKeyhole size={13}/><span>这些配置仅用于你的工作空间。</span></p>
  </section>;
}
