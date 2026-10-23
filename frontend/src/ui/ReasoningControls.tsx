import { useEffect, useId, useRef, useState } from "react";
import { api } from "../api";
import "./reasoningControls.css";

export type ReasoningSettings = { mode: "default" | "effort" | "budget" | "auto" | "off"; effort?: string; budget_tokens?: number; show_summary: boolean; manual?: boolean; discovered?:boolean };
type ReasoningControlsProfile = { supported_modes: string[]; effort_values: string[]; budget: { min: number; max: number } | null; summary_supported: boolean; control_kind: string };
export type ReasoningCapabilities = ReasoningControlsProfile & { schema_version: number; rules_version: string; source: "rule" | "unknown"; endpoint_scope?: string; default_behavior?: string; verification?: { evidence: string; config_version?: number }; manual_controls?: ReasoningControlsProfile };
const DEFAULT: ReasoningSettings = { mode: "default", show_summary: false };
const UNKNOWN: ReasoningCapabilities = { schema_version: 1, rules_version: "", source: "unknown", supported_modes: ["default"], effort_values: [], budget: null, summary_supported: false, control_kind: "unknown" };
const MODES: Record<string, string> = { default: "供应商默认", effort: "指定思考强度", budget: "指定思考预算", auto: "自动预算", off: "关闭思考" };
const EFFORTS: Record<string, string> = { none: "关闭", minimal: "极简", low: "轻量", medium: "均衡", high: "深入", xhigh: "更深入", max: "最大" };
function targetKey(protocol: string, endpoint: string, model: string) { return JSON.stringify([protocol, endpoint.trim(), model.trim()]); }
function clean(value: ReasoningSettings, caps: ReasoningCapabilities): ReasoningSettings {
  if (!caps.supported_modes.includes(value.mode) || (value.mode === "effort" && !caps.effort_values.includes(value.effort || "")) || (value.mode === "budget" && (!caps.budget || !Number.isSafeInteger(value.budget_tokens) || value.budget_tokens! < caps.budget.min || value.budget_tokens! > caps.budget.max))) return { mode: "default", show_summary: value.show_summary };
  return { mode: value.mode, show_summary: value.show_summary, ...(value.mode === "effort" ? { effort: value.effort } : {}), ...(value.mode === "budget" ? { budget_tokens: value.budget_tokens } : {}) };
}

type DiscoveryOption={value:string;settings:ReasoningSettings;evidence:"declared"|"request_accepted"};
type DiscoveryResult={state:string;parameter_validation:string;options:DiscoveryOption[];error_code?:string;provider_status?:number};
export function ReasoningSelect({protocol,endpoint,model,value,onChange,onValidChange,disabled=false,apiKey="",savedConfig,visible=true,savedCredential=false,discover=api.discoverReasoning}:{
  protocol:string;endpoint:string;model:string;value:ReasoningSettings;
  onChange:(value:ReasoningSettings)=>void;onValidChange:(valid:boolean)=>void;disabled?:boolean;
  apiKey?:string;visible?:boolean;savedConfig?:{config_id:string;config_version:number;protocol:string;endpoint:string};
  savedCredential?:boolean;discover?:(body:any,signal?:AbortSignal)=>Promise<DiscoveryResult>;
}){
  const key=targetKey(protocol,endpoint,model),previous=useRef(key);
  const [result,setResult]=useState<{key:string;data:DiscoveryResult}|null>(null);
  const [loading,setLoading]=useState(false);
  const [failed,setFailed]=useState(false);
  const [retry,setRetry]=useState(0);
  const refreshNext=useRef<string|null>(null);
  const change=useRef(onChange);
  const setting=useRef(value);
  const sequence=useRef(0);
  useEffect(()=>{change.current=onChange;setting.current=value;},[onChange,value]);
  const normalized=(address:string)=>address.trim().replace(/\/+$/,"").replace(/\/(?:v1|v1beta)$/,"");
  const reuse=Boolean(savedConfig&&protocol===savedConfig.protocol&&normalized(endpoint)===normalized(savedConfig.endpoint));
  const reusable=reuse||savedCredential;
  const configId=savedConfig?.config_id,configVersion=savedConfig?.config_version;
  useEffect(()=>{
    const request=++sequence.current;
    if(previous.current!==key){previous.current=key;change.current({mode:"default",show_summary:setting.current.show_summary});}
    const controller=new AbortController();
    setResult(null);setLoading(false);setFailed(false);
    if(!visible||!model.trim()||!endpoint.trim()||(!apiKey.trim()&&!reusable))return ()=>controller.abort();
    setLoading(true);
    const timer=setTimeout(()=>{
      const refresh=refreshNext.current===key;
      refreshNext.current=null;
      discover({protocol,endpoint:endpoint.trim(),model_name:model.trim(),
        ...(refresh?{refresh:true}:{}),
        ...(apiKey.trim()?{api_key:apiKey.trim()}:{}),
        ...(reuse&&configId?{config_id:configId,config_version:configVersion}:{})},controller.signal)
        .then(data=>{
          if(!data||!Array.isArray(data.options))throw new Error("invalid_reasoning_discovery");
          if(!controller.signal.aborted&&sequence.current===request)setResult({key,data});
        })
        .catch(()=>{if(!controller.signal.aborted&&sequence.current===request)setFailed(true);})
        .finally(()=>{if(!controller.signal.aborted&&sequence.current===request)setLoading(false);});
    },800);
    return ()=>{clearTimeout(timer);controller.abort();};
  },[key,protocol,endpoint,model,apiKey,reusable,reuse,configId,configVersion,visible,retry,discover]);
  const data=result?.key===key?result.data:null;
  const options=data?.options||[];
  const choices:[string,string][]=[["default","默认"]];
  for(const option of options){
    const selected=option.settings;
    const label=selected.mode==="effort"?(selected.effort||option.value):
      selected.mode==="budget"?`${selected.budget_tokens?.toLocaleString()} token`:selected.mode==="auto"?"自动":"关闭";
    choices.push([option.value,label]);
  }
  const selected=value.mode==="effort"?"effort:"+value.effort:value.mode==="budget"?"budget:"+value.budget_tokens:value.mode;
  if(!choices.some(([id])=>id===selected))choices.push([selected,"已保存"]);
  // Discovery is advisory and never gates saving or running with defaults.
  useEffect(()=>{onValidChange(true);},[onValidChange]);
  const failures:Record<string,string>={
    MODEL_TIMEOUT:"检测超时",MODEL_CONNECTION_ERROR:"无法连接模型服务",
    MODEL_CREDENTIAL_INVALID:"模型服务拒绝凭据",MODEL_RATE_LIMITED:"请求过于频繁，请稍后重试",
    MODEL_PROVIDER_ERROR:"模型服务请求失败",MODEL_RESPONSE_TOO_LARGE:"服务返回超出检测限制",
  };
  const status=loading?"正在检测可用档位…":
    failed?"检测请求失败":data?.error_code?`${failures[data.error_code]||"检测未完成"}${data.provider_status?`（${data.provider_status}）`:""}`:
    data&&!options.length?"未发现可确认档位":!apiKey.trim()&&!reusable?"填写 API Key 后自动检测":"";
  const canRetry=!loading&&(apiKey.trim()||reusable)&&(failed||Boolean(data?.error_code)||Boolean(data&&!options.length));
  const title=loading?"正在向当前服务检测思考档位，默认仍可使用":
    data?.error_code&&options.length?"部分档位请求已被接受，其余尚未确认；接受不代表参数实际生效":
    failed||data?.error_code?`${status}，默认仍可使用，可重新检测`:
    !apiKey.trim()&&!reusable?"填写 API Key 后自动检测":
    !data?"填写完整连接后自动检测":
    data.parameter_validation==="unconfirmed"?"网关未明确校验思考参数；请求被接受不代表参数实际生效":
    options.length?"当前服务的检测结果；请求被接受不代表思考强度实际生效":"尚未发现可确认的思考档位，默认仍可使用";
  return <select className="reasoning-select" aria-label="思考强度"
    title={title} aria-busy={loading} data-discovery-state={loading?"detecting":failed?"failed":data?.state||"idle"}
    value={selected} disabled={disabled||!model.trim()} onChange={event=>{
      if(event.target.value==="__retry__"){refreshNext.current=key;setRetry(count=>count+1);return;}
      const option=options.find(item=>item.value===event.target.value);
      if(option)onChange({...option.settings,show_summary:value.show_summary});
      else if(event.target.value==="default")onChange({mode:"default",show_summary:value.show_summary});
    }}>{choices.map(([id,label])=><option key={id} value={id}>{label}</option>)}
      {status&&<option value="__status__" disabled>{status}</option>}
      {canRetry&&<option value="__retry__">重新检测</option>}
    </select>;
}
export default function ReasoningControls({ protocol, endpoint, model, value, onChange, onValidChange, initialCapabilities, savedConfig, disabled = false }: {
  protocol: string; endpoint: string; model: string; value: ReasoningSettings; onChange: (value: ReasoningSettings) => void; onValidChange: (valid: boolean) => void; initialCapabilities?: ReasoningCapabilities; disabled?: boolean;
  savedConfig?: { config_id: string; config_version: number; protocol: string; endpoint: string; model_name: string; reasoning_settings?: ReasoningSettings };
}) {
  const key = targetKey(protocol, endpoint, model);
  const previousKey = useRef(key);
  const currentKey = useRef(key); currentKey.current = key;
  const settings = useRef(value); settings.current = value;
  const change = useRef(onChange); change.current = onChange;
  const [description, setDescription] = useState<{ key: string; caps: ReasoningCapabilities } | null>(initialCapabilities ? { key, caps: initialCapabilities } : null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [testing, setTesting] = useState(false);
  const [evidence, setEvidence] = useState("");
  const testRequest = useRef<AbortController | null>(null);
  const detectedCaps = description?.key === key ? description.caps : UNKNOWN;
  const manual = value.manual === true && detectedCaps.source === "unknown" && Boolean(detectedCaps.manual_controls);
  const caps = manual ? { ...detectedCaps, ...detectedCaps.manual_controls! } : detectedCaps;
  const manualSetting = manual ? { manual: true } : {};
  const id = useId();
  useEffect(() => {
    let stopped = false;
    const request = new AbortController();
    testRequest.current?.abort(); setTesting(false); setEvidence(""); setError("");
    if (previousKey.current !== key) { change.current({ mode: "default", show_summary: settings.current.show_summary }); previousKey.current = key; }
    if (!endpoint.trim() || !model.trim()) { setDescription(null); setLoading(false); return () => { stopped = true; request.abort(); }; }
    setLoading(true);
    const timer = setTimeout(async () => {
      try {
        const response = await api.describeReasoning({ protocol, endpoint: endpoint.trim(), model_name: model.trim() }, request.signal);
        if (stopped || currentKey.current !== key) return;
        const next = response as ReasoningCapabilities;
        if (!next || !Array.isArray(next.supported_modes) || !Array.isArray(next.effort_values)) throw new Error("invalid_reasoning_capabilities");
        setDescription({ key, caps: next });
        if (next.source !== "unknown") {
          const cleaned = clean(settings.current, next);
          if (JSON.stringify(cleaned) !== JSON.stringify(settings.current)) change.current(cleaned);
        }
      } catch {
        if (stopped || request.signal.aborted || currentKey.current !== key) return;
        setError("能力说明暂不可用，当前设置已保留。请重新检测后再保存。");
      } finally { if (!stopped && currentKey.current === key) setLoading(false); }
    }, 300);
    return () => { stopped = true; clearTimeout(timer); request.abort(); testRequest.current?.abort(); };
  }, [key, protocol, endpoint, model, refresh]);
  const budgetValid = value.mode !== "budget" || Boolean(caps.budget && Number.isSafeInteger(value.budget_tokens) && value.budget_tokens! >= caps.budget.min && value.budget_tokens! <= caps.budget.max);
  const valid = !loading && !error && (!value.manual || manual) && budgetValid && caps.supported_modes.includes(value.mode) && (value.mode !== "effort" || caps.effort_values.includes(value.effort || ""));
  useEffect(() => { onValidChange(valid); }, [valid, onValidChange]);
  useEffect(() => { setEvidence(""); testRequest.current?.abort(); setTesting(false); }, [key, value.mode, value.effort, value.budget_tokens, value.show_summary]);
  const canonical = (setting: ReasoningSettings) => JSON.stringify([setting.mode, setting.show_summary, setting.mode === "effort" ? setting.effort : null, setting.mode === "budget" ? setting.budget_tokens : null, setting.manual === true]);
  const savedMatches = Boolean(savedConfig && key === targetKey(savedConfig.protocol, savedConfig.endpoint, savedConfig.model_name) && canonical(value) === canonical(savedConfig.reasoning_settings || DEFAULT));
  const modes = ["default", ...caps.supported_modes.filter(mode => mode !== "default" && mode in MODES)];
  return <div className="reasoning-controls">
    <div className="reasoning-heading"><div><h3>思考强度</h3><p>按协议、服务地址与模型名识别原生参数。</p></div><span className="reasoning-detection" role="status">{loading ? "正在识别…" : manual ? "手动配置 · 未验证" : caps.source === "rule" ? "规则识别" : "能力未知"}</span></div>
    {detectedCaps.source === "unknown" && detectedCaps.manual_controls && <label className="reasoning-summary"><input type="checkbox" checked={value.manual === true} disabled={disabled || loading} onChange={event => onChange({ mode: "default", show_summary: value.show_summary, ...(event.target.checked ? { manual: true } : {}) })} /><span><strong>手动指定思考参数</strong><small>模型未收录，可按服务商说明选择参数；保存不会自动调用模型。</small></span></label>}
    {manual && <p className="reasoning-help">以下是当前协议的候选参数，尚未确认此模型支持。服务不支持时会报错；可保存后验证已保存组合，请求被接受不代表参数实际生效。</p>}
    <div className="reasoning-mode-grid" role="group" aria-label="思考模式">{modes.map(mode => <label key={mode} className={`reasoning-mode${value.mode === mode ? " is-selected" : ""}`}><input type="radio" name={`${id}-mode`} checked={value.mode === mode} disabled={disabled || loading} onChange={() => onChange({ mode: mode as ReasoningSettings["mode"], show_summary: value.show_summary, ...manualSetting, ...(mode === "effort" ? { effort: caps.effort_values.find(effort => effort === "low") || caps.effort_values[0] } : {}), ...(mode === "budget" ? { budget_tokens: caps.budget?.min } : {}) })} /><span>{MODES[mode]}</span></label>)}</div>
    {value.mode === "effort" && <div className="reasoning-efforts" role="group" aria-label="原生思考档位">{caps.effort_values.map(effort => <button key={effort} type="button" aria-pressed={value.effort === effort} disabled={disabled || loading} onClick={() => onChange({ mode: "effort", effort, show_summary: value.show_summary, ...manualSetting })}><strong>{EFFORTS[effort] || effort}</strong><small>{effort}</small></button>)}</div>}
    {value.mode === "budget" && caps.budget && <label className="field reasoning-budget" htmlFor={`${id}-budget`}>思考 token 预算<input id={`${id}-budget`} type="number" inputMode="numeric" min={caps.budget.min} max={caps.budget.max} step={1} aria-invalid={!budgetValid} aria-describedby={`${id}-budget-help`} value={value.budget_tokens ?? ""} disabled={disabled || loading} onChange={event => onChange({ mode: "budget", show_summary: value.show_summary, ...manualSetting, ...(event.target.value !== "" ? { budget_tokens: Number(event.target.value) } : {}) })} /><span id={`${id}-budget-help`}>{budgetValid ? `${manual ? "可填写范围" : "支持范围"} ${caps.budget.min.toLocaleString()}–${caps.budget.max.toLocaleString()} token；输出上限也会影响可用预算。` : "请输入范围内的整数预算。"}</span></label>}
    {caps.source === "unknown" && !manual && value.mode !== "default" && <p className="reasoning-help">当前无法确认已保存的思考参数，设置已保留。请重新检测，或明确选择供应商默认后保存。</p>}
    {value.mode === "default" && <p className="reasoning-help">沿用供应商默认，不发送自定义思考强度。{caps.source === "unknown" && !manual ? "可手动指定参数，支持情况需另行验证。" : ""}</p>}
    {value.mode === "auto" && <p className="reasoning-help">由模型自动分配思考预算；这与沿用供应商默认分别保存。</p>}
    {value.mode === "off" && <p className="reasoning-help">使用该模型协议支持的关闭参数。</p>}
    <details className="reasoning-advanced"><summary>参数依据与检测</summary><dl><dt>识别依据</dt><dd>{manual ? "用户手动选择协议参数，模型支持情况未确认" : caps.source === "rule" ? `内置协议与模型规则 · ${caps.rules_version}` : "未匹配到经过确认的规则"}</dd><dt>请求协议</dt><dd>{protocol === "openai" ? "OpenAI Chat Completions" : protocol === "anthropic" ? "Anthropic Messages" : "Gemini 原生"}</dd><dt>原生控制</dt><dd>{caps.control_kind || "未知"}</dd><dt>能力来源</dt><dd>{caps.endpoint_scope === "compatible_unverified" ? (manual ? "兼容网关：参数由用户指定，尚未经真实服务验证。" : "兼容网关：根据模型名推断，尚未经真实服务验证。") : ""}仅读取规则，不调用真实模型，也不产生模型费用。请求被接受不能证明参数一定生效。</dd>{caps.default_behavior && <><dt>默认行为</dt><dd>{caps.default_behavior}</dd></>}</dl></details>
    <div className="reasoning-actions"><button className="btn btn-sm" type="button" disabled={disabled || loading || !model.trim() || !endpoint.trim()} onClick={() => setRefresh(value => value + 1)}>重新检测档位</button>{savedConfig && <button className="btn btn-sm" type="button" disabled={disabled || testing || loading || !valid || !savedMatches} onClick={async () => {
      const request = new AbortController(); testRequest.current?.abort(); testRequest.current = request; const testedKey = key;
      setTesting(true); setEvidence("");
      try {
        const result = await api.testReasoning(savedConfig.config_id, savedConfig.config_version, request.signal);
        if (request.signal.aborted || currentKey.current !== testedKey) return;
        setEvidence(result.evidence === "request_accepted" ? "已保存组合的请求被接受；这不保证档位参数实际生效。" : result.evidence === "unsupported" ? "当前服务不接受已保存的思考组合，请调整设置后保存。" : "未能确认当前组合，未将此结果标为支持。");
      } catch { if (!request.signal.aborted && currentKey.current === testedKey) setEvidence("验证未完成，请检查连接与配置后重试；尚无可确认的支持证据。"); }
      finally { if (!request.signal.aborted && currentKey.current === testedKey) setTesting(false); }
    }}>{testing ? "正在验证…" : "验证已保存组合"}</button>}</div>
    {savedConfig && <p className="reasoning-help">{savedMatches ? "验证会向已配置的模型发送一次最小请求，可能计费。" : "先保存当前修改，再验证这一组合。"}</p>}
    {error && <p role="status" className="reasoning-help reasoning-error">{error}</p>}{evidence && <p role="status" className="reasoning-help">{evidence}</p>}
  </div>;
}
