import { useCallback, useEffect, useRef, useState } from "react";
import Dialog from "./ui/Dialog";
import "./capabilities.css";
import { api } from "./api";

/** 切片 1：能力配置页（个人模型/TTS、管理员云能力、连接器配对、外发确认查询）。 */

const CAP_LABEL: Record<string, string> = {
  text: "文本",
  image: "图像识别",
  image_generate: "图片生成",
  tool_call: "工具调用",
  json_schema: "JSON Schema",
  streaming: "流式",
  cancel: "取消",
};

const CAP_STATE: Record<string, { text: string; cls: string }> = {
  available: { text: "可用", cls: "succeeded" },
  unavailable: { text: "不可用", cls: "failed" },
  unknown: { text: "未知", cls: "queued" },
};

const PROTOCOL_LABEL: Record<string, string> = {
  openai_image: "OpenAI 兼容图片生成",
  openai: "OpenAI 兼容",
  anthropic: "Anthropic 原生",
  gemini: "Gemini 原生",
  minimax: "MiniMax TTS",
  paddleocr: "PaddleOCR",
  tavily_hikari: "Tavily Hikari",
};

const CAP_DETAIL: Record<string, string> = {
  error: "测试未完成，请检查网络、服务地址和模型名后重试",
  capability_test_failed: "测试未完成，请检查服务地址与模型名",
  connection_error: "连接失败，请检查网络和代理",
  timeout: "请求超时，请稍后重试",
  credential_invalid: "凭据被服务拒绝，请更新 API Key",
  unsupported: "服务不支持该能力",
  rate_limit: "服务限流，请稍后重试",
  provider_error: "服务暂时出错，请稍后重试",
};

function CapabilityBadges({ status }: { status: Record<string, any> }) {
  const entries = Object.keys(status || {});
  if (!entries.length) return <span className="help">未探测</span>;
  return (
    <div className="row">
      {entries.map((cap) => {
        const raw = status[cap];
        const state = typeof raw === "string" ? raw : raw?.state || "unknown";
        const meta = CAP_STATE[state] || CAP_STATE.unknown;
        return (
          <span key={cap} className="capability-result"><span className={`badge ${meta.cls}`}>
             {CAP_LABEL[cap] || cap}：{meta.text}
           </span>
              {typeof raw === "object" && raw?.detail && state !== "available" && <small className="help">{CAP_DETAIL[raw.detail] || "服务未确认该能力，请检查配置后重试"}</small>}
           </span>
        );
      })}
    </div>
  );
}

type ConfigItem = {
  config_id: string;
  owner_scope: string;
  kind: string;
  protocol: string;
  endpoint: string;
  model_name: string;
  credential_mask: string;
  capability_status: Record<string, any>;
  config_version: number;
  is_active: boolean;
};

type ConnectorItem = {
  binding_id: string;
  device_name: string;
  status: string;
  last_seen_at?: string;
};

export default function CapabilityConfig({ isAdmin, onErr, onOk }: any) {
  const [configs, setConfigs] = useState<ConfigItem[]>([]);
  const [connectors, setConnectors] = useState<ConnectorItem[]>([]);
  const [disclosures, setDisclosures] = useState<any[]>([]);
  const [taskId, setTaskId] = useState("");
  const [pairing, setPairing] = useState<any>(null);
  const [deviceName, setDeviceName] = useState("");
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState<ConfigItem | null>(null);
  const [deleteError, setDeleteError] = useState("");
  const [editing, setEditing] = useState<ConfigItem | null>(null);
  const [operation, setOperation] = useState<string | null>(null);
  const operationLock = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const [cfg, conn] = await Promise.all([api.listConfigs(), api.listConnectors()]);
      setConfigs(cfg.data || []);
      setConnectors(conn.data || []);
    } catch (e: any) {
      onErr?.(e?.message || "加载失败");
    }
  }, [onErr]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const loadDisclosures = useCallback(async () => {
    if (!taskId) return;
    try {
      const r = await api.listDisclosures(taskId);
      setDisclosures(r.data || []);
    } catch (e: any) {
      onErr?.(e?.message || "加载外发确认失败");
    }
  }, [taskId, onErr]);

  return (
    <div className="stack capabilities-page">
      {editing && <ConfigEditor key={editing.config_id} config={editing} onClose={() => setEditing(null)} onSaved={async () => { setEditing(null); onOk?.("配置已更新；能力将重新测试"); await refresh(); }} />}
      {deleting && <Dialog title="删除配置" kind="info" onClose={() => { if (!operationLock.current) setDeleting(null); }} actions={<>
        <button className="btn" disabled={Boolean(operation)} onClick={() => setDeleting(null)}>取消</button>
        <button className="btn btn-danger" disabled={Boolean(operation)} onClick={async () => {
          if (operationLock.current) return;
          operationLock.current = true; setOperation(`delete:${deleting.config_id}`);
          try { await api.deactivateConfig(deleting.config_id); setConfigs(prev => prev.filter(c => c.config_id !== deleting.config_id)); setDeleting(null); onOk?.("配置已删除"); }
          catch (e: any) { setDeleteError(e?.message || "删除失败，请重试"); }
          finally { operationLock.current = false; setOperation(null); }
        }}>{operation ? "正在删除…" : "确认删除"}</button>
      </>}>
        <p>确定删除 {PROTOCOL_LABEL[deleting.protocol] || deleting.protocol} {deleting.model_name}？后续任务将停止使用该配置。</p>
        {deleteError && <p role="alert" className="capability-detail">{deleteError}</p>}
      </Dialog>}
      <div className="card stack">
        <h1>能力配置</h1>
        <p className="lede">
          个人内容模型与 TTS、管理员云能力。凭据加密存储，页面只显示掩码；能力测试只发送非私人样例。
        </p>
        <button className="btn" onClick={refresh} disabled={busy}>刷新</button>
      </div>

      <ContentModelForm
        onErr={onErr}
        onOk={async (m: string) => { onOk?.(m); await refresh(); }}
        onBusy={setBusy}
      />
      <ContentModelForm imageGeneration onErr={onErr} onOk={async (m: string) => { onOk?.(m); await refresh(); }} onBusy={setBusy} />
      <TtsForm
        onErr={onErr}
        onOk={async (m: string) => { onOk?.(m); await refresh(); }}
        onBusy={setBusy}
      />
      {isAdmin && (
        <AdminCloudForm
          onErr={onErr}
          onOk={async (m: string) => { onOk?.(m); await refresh(); }}
          onBusy={setBusy}
        />
      )}

      <div className="card stack">
        <h2>已保存的配置</h2>
        <p className="help">个人同类配置默认使用最近新增的一条。状态未知表示尚未确认能力，可点击“测试能力”查看结果。</p>
        {configs.length === 0 && <p className="help">暂无配置。</p>}
        {configs.map((c) => (
          <div key={c.config_id} className="stack" style={{ borderTop: "1px solid var(--border)", paddingTop: "0.75rem" }}>
            <div className="row">
              <strong>{PROTOCOL_LABEL[c.protocol] || c.protocol}</strong>
              <span className="badge queued">{c.kind}</span>
              {c.owner_scope === "admin" && <span className="badge partial">管理员</span>}
              <span className="mono">v{c.config_version}</span>
            </div>
            <div className="mono">{c.endpoint}{c.model_name ? ` · ${c.model_name}` : ""}</div>
            <div className="row">
              <span className="help">凭据：</span>
              <span className="mono">{c.credential_mask}</span>
            </div>
            <CapabilityBadges status={c.capability_status} />
            <div className="row">
              <button className="btn" disabled={Boolean(operation)} onClick={() => setEditing(c)}>编辑</button>
              <button
                className="btn"
                disabled={Boolean(operation)}
                onClick={async () => {
                  if (operationLock.current) return;
                  operationLock.current = true; setOperation(`test:${c.config_id}`);
                  try {
                    const r = await api.testConfig(c.config_id);
                    const values = Object.values(r.capability_status || {}) as any[];
                    const failed = values.some(raw => (typeof raw === "string" ? raw : raw?.state) === "unavailable");
                    const unknown = values.some(raw => (typeof raw === "string" ? raw : raw?.state) === "unknown");
                    onOk?.(failed ? "测试完成，部分能力不可用，请查看原因" : unknown ? "测试完成，部分能力仍未确认，请查看原因" : "能力测试通过");
                    setConfigs((prev) => prev.map((x) => (x.config_id === c.config_id ? { ...x, capability_status: r.capability_status } : x)));
                  } catch (e: any) { onErr?.(e?.message || "测试失败"); }
                  finally { operationLock.current = false; setOperation(null); }
                }}
              >
                {operation === `test:${c.config_id}` ? "正在测试…" : "测试能力"}
              </button>
              <button className="btn" disabled={Boolean(operation)} onClick={() => { setDeleteError(""); setDeleting(c); }}>删除</button>
            </div>
          </div>
        ))}
      </div>

      <div className="card stack">
        <h2>本机连接器</h2>
        <p className="help">配对码一次性、10 分钟有效。连接器主动建立出站 WSS 通道，只允许本机回环目标。</p>
        <div className="row">
          <input
            placeholder="设备名（可选）"
            value={deviceName}
            onChange={(e) => setDeviceName(e.target.value)}
            aria-label="设备名"
          />
          <button
            onClick={async () => {
              try { setPairing(await api.createPairing(deviceName)); }
              catch (e: any) { onErr?.(e?.message || "生成配对码失败"); }
            }}
          >
            生成配对码
          </button>
        </div>
        {pairing && (
          <div className="notice">
            <div>配对码（仅显示一次）：<span className="mono">{pairing.pairing_code}</span></div>
            <div className="help">有效期至 {pairing.expires_at}</div>
          </div>
        )}
        {connectors.length === 0 && <p className="help">暂无连接器。</p>}
        {connectors.map((c) => (
          <div key={c.binding_id} className="row" style={{ borderTop: "1px solid var(--border)", paddingTop: "0.5rem" }}>
            <span className="mono">{c.device_name || c.binding_id}</span>
            <span className={`badge ${c.status === "online" ? "succeeded" : c.status === "revoked" ? "failed" : "queued"}`}>
              {c.status}
            </span>
            {c.status !== "revoked" && (
              <button
                className="btn"
                onClick={async () => {
                  try { await api.revokeConnector(c.binding_id); onOk?.("连接器已撤销"); await refresh(); }
                  catch (e: any) { onErr?.(e?.message || "撤销失败"); }
                }}
              >
                撤销
              </button>
            )}
          </div>
        ))}
      </div>

      <div className="card stack">
        <h2>外发确认记录</h2>
        <p className="help">按任务查询已确认的外发授权；撤销后 worker 将停止外发。</p>
        <div className="row">
          <input
            placeholder="task_id"
            value={taskId}
            onChange={(e) => setTaskId(e.target.value)}
            aria-label="任务 ID"
          />
          <button className="btn" onClick={loadDisclosures}>查询</button>
        </div>
        {disclosures.length > 0 && (
          <table className="table">
            <thead>
              <tr><th>授权</th><th>类别</th><th>状态</th><th>操作</th></tr>
            </thead>
            <tbody>
              {disclosures.map((d) => (
                <tr key={d.grant_id}>
                  <td className="mono">{d.grant_id}</td>
                  <td>{d.content_category}</td>
                  <td>{d.revoked_at ? "已撤销" : "有效"}</td>
                  <td>
                    {!d.revoked_at && (
                      <button
                        className="btn"
                        onClick={async () => {
                          try { await api.revokeDisclosure(d.grant_id); onOk?.("授权已撤销"); await loadDisclosures(); }
                          catch (e: any) { onErr?.(e?.message || "撤销失败"); }
                        }}
                      >
                        撤销
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function ConfigEditor({ config, onClose, onSaved }: { config: ConfigItem; onClose: () => void; onSaved: () => Promise<void> }) {
  const [endpoint, setEndpoint] = useState(config.endpoint);
  const [model, setModel] = useState(config.model_name);
  const [apiKey, setApiKey] = useState("");
  const [voice, setVoice] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const saveLock = useRef(false);
  const needsModel = ["content_model", "image_generation", "tts"].includes(config.kind);
  const needsVoice = config.kind === "tts" && Boolean(apiKey.trim());
  return <Dialog title="编辑配置" kind="info" onClose={() => { if (!saveLock.current) onClose(); }} actions={<>
    <button className="btn" disabled={saving} onClick={onClose}>取消</button>
    <button className="btn btn-pri" disabled={saving || !endpoint.trim() || (needsModel && !model.trim()) || (needsVoice && !voice.trim())} onClick={async () => {
      if (saveLock.current) return;
      saveLock.current = true; setSaving(true);
      try {
        const credentials = apiKey.trim() ? { api_key: apiKey.trim(), ...(config.kind === "tts" ? { voice_id: voice.trim() } : {}) } : undefined;
        await api.updateConfig(config.config_id, { endpoint: endpoint.trim(), model_name: model.trim(), ...(credentials ? { credentials } : {}) });
        await onSaved();
      } catch (e: any) { setSaveError(e?.message || "更新失败，请重试"); }
      finally { saveLock.current = false; setSaving(false); }
    }}>{saving ? "正在保存…" : "保存修改"}</button>
  </>}>
    <div className="capability-editor">
      <p>{PROTOCOL_LABEL[config.protocol] || config.protocol}</p>
      <label className="field">服务地址<input disabled={saving} value={endpoint} onChange={e => setEndpoint(e.target.value)} /></label>
      <label className="field">模型名<input disabled={saving} value={model} onChange={e => setModel(e.target.value)} /></label>
      <label className="field">新的 API Key<input type="password" autoComplete="off" disabled={saving} value={apiKey} placeholder="留空保留现有凭据" onChange={e => setApiKey(e.target.value)} /></label>
      {needsVoice && <label className="field">音色 ID<input disabled={saving} value={voice} onChange={e => setVoice(e.target.value)} /><span>更换语音凭据时，请填写该账号可用的音色 ID。</span></label>}
      <p>API Key 留空会保留现有凭据。修改后需要重新确认能力状态。</p>
      {saveError && <p role="alert" className="capability-detail">{saveError}</p>}
    </div>
  </Dialog>;
}

function ContentModelForm({ onErr, onOk, onBusy, imageGeneration = false }: any) {
  const [protocol, setProtocol] = useState(imageGeneration ? "openai_image" : "openai");
  const [endpoint, setEndpoint] = useState("https://api.openai.com/v1");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [working, setWorking] = useState(false);
  const saveLock = useRef(false);
  return (
    <div className="card stack">
      <h2>{imageGeneration ? "课程封面图片模型（可选）" : "个人内容模型"}</h2>
      {imageGeneration && <p className="help">水墨程序绘图无需此配置。图片生成使用独立模型，调用前确认外发；能力测试会生成一张合成样例图片，可能计费。</p>}
      <div className="row">
        <label className="field">
          <span>协议</span>
          <select value={protocol} onChange={(e) => setProtocol(e.target.value)}>
            {imageGeneration ? <option value="openai_image">OpenAI 兼容图片生成</option> : <>
            <option value="openai">OpenAI 兼容</option>
            <option value="anthropic">Anthropic 原生</option>
            <option value="gemini">Gemini 原生</option></>}
          </select>
        </label>
        <label className="field" style={{ flex: 1 }}>
          <span>服务地址</span>
          <input value={endpoint} onChange={(e) => setEndpoint(e.target.value)} />
        </label>
      </div>
      <div className="row">
        <label className="field" style={{ flex: 1 }}>
          <span>模型名</span>
          <input value={model} onChange={(e) => setModel(e.target.value)} />
        </label>
        <label className="field" style={{ flex: 1 }}>
          <span>API Key</span>
          <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} autoComplete="off" />
        </label>
      </div>
      <button className="btn btn-pri"
        disabled={working || !endpoint.trim() || !model.trim() || !apiKey.trim()}
        onClick={async () => {
          if (saveLock.current) return;
          saveLock.current = true; setWorking(true); onBusy?.(true);
          try {
            await api.createConfig({
              kind: imageGeneration ? "image_generation" : "content_model",
              protocol,
              endpoint: endpoint.trim(),
              model_name: model.trim(),
              credentials: { api_key: apiKey.trim() },
              owner_scope: "user",
            });
            setApiKey("");
            await onOk?.("模型配置已保存（凭据已加密）");
          } catch (e: any) { onErr?.(e?.message || "保存失败"); }
          finally { saveLock.current = false; setWorking(false); onBusy?.(false); }
        }}
      >
        {working ? "正在保存…" : "保存模型配置"}
      </button>
    </div>
  );
}

function TtsForm({ onErr, onOk, onBusy }: any) {
  const [endpoint, setEndpoint] = useState("https://api.minimaxi.com");
  const [apiKey, setApiKey] = useState("");
  const [voice, setVoice] = useState("");
  const [voices,setVoices] = useState<{voice_id:string;name:string;category:string;description:string}[]>([]);
  const [working,setWorking] = useState<"query"|"save"|null>(null);
  const [catalogMessage,setCatalogMessage] = useState("");
  const categories=[{id:"system_voice",name:"系统音色"},{id:"voice_cloning",name:"克隆音色"},{id:"voice_generation",name:"文生音色"}];
  const resetCatalog=()=>{setVoices([]);setVoice("");setCatalogMessage("");};
  const selected=voices.find(item=>item.voice_id===voice);
  return (
    <div className="card stack">
      <h2>个人语音（MiniMax）</h2>
      <div className="row">
        <label className="field" style={{ flex: 1 }}>
          <span>服务地址</span>
          <input value={endpoint} disabled={Boolean(working)} onChange={(e) => {setEndpoint(e.target.value);resetCatalog();}} />
        </label>
        <label className="field" style={{ flex: 1 }}>
          <span>音色</span>
          <select aria-label="音色" style={{width:"100%",minWidth:0}} value={voice} onChange={e=>setVoice(e.target.value)} disabled={Boolean(working)||!voices.length}>
            <option value="">{working==="query"?"正在获取音色…":voices.length?"请选择音色":"先获取可用音色"}</option>
            {categories.map(category=>{
              const items=voices.filter(item=>item.category===category.id);
              return items.length?<optgroup key={category.id} label={category.name}>
                {items.map(item=><option key={item.voice_id} value={item.voice_id}>{item.name===item.voice_id?item.name:`${item.name} · ${item.voice_id}`}</option>)}
              </optgroup>:null;
            })}
          </select>
        </label>
        <button className="btn" style={{alignSelf:"flex-end"}} disabled={Boolean(working)||!apiKey.trim()||!endpoint.trim()}
          onClick={async()=>{
            setWorking("query");onBusy?.(true);setCatalogMessage("");setVoices([]);setVoice("");
            try{
              const result=await api.listTtsVoices({endpoint,api_key:apiKey});
              const items=result.data||[];setVoices(items);
              setCatalogMessage(items.length?`已获取 ${items.length} 个音色` : "当前账号未返回可用音色。");
            }catch(e:any){
              setCatalogMessage("获取失败，请检查服务地址与 API Key 后重试。");onErr?.(e?.message||"获取音色失败");
            }finally{setWorking(null);onBusy?.(false);}
          }}>{working==="query"?"正在获取…":"获取可用音色"}</button>
      </div>
      {catalogMessage&&<p className="help" role="status">{catalogMessage}</p>}
      {selected?.description&&<p className="help">{selected.description}</p>}
      <label className="field">
        <span>API Key</span>
        <input type="password" value={apiKey} disabled={Boolean(working)} onChange={(e) => {setApiKey(e.target.value);resetCatalog();}} autoComplete="off" />
      </label>
      <button
        disabled={Boolean(working)||!voice||!apiKey.trim()||!endpoint.trim()}
        onClick={async () => {
          setWorking("save");onBusy?.(true);
          try {
            await api.createConfig({
              kind: "tts",
              protocol: "minimax",
              endpoint: endpoint.trim(),
              model_name: "speech-2.8-hd",
              credentials: { api_key: apiKey.trim(), voice_id: voice },
              owner_scope: "user",
            });            onOk?.("TTS 配置已保存（默认 speech-2.8-hd）");
          } catch (e: any) { onErr?.(e?.message || "保存失败"); }
          finally { setWorking(null);onBusy?.(false); }
        }}
      >
        保存 TTS 配置
      </button>
    </div>
  );
}

function AdminCloudForm({ onErr, onOk, onBusy }: any) {
  const [kind, setKind] = useState("ocr");
  const [protocol, setProtocol] = useState("paddleocr");
  const [endpoint, setEndpoint] = useState("https://ocr.example.com/api");
  const [model, setModel] = useState("PaddleOCR-VL-1.6");
  const [apiKey, setApiKey] = useState("");
  const [working, setWorking] = useState(false);
  const saveLock = useRef(false);
  return (
    <div className="card stack">
      <h2>管理员云能力</h2>
      <p className="help">仅管理员可见与配置；普通用户不可读取这些配置的凭据。</p>
      <div className="row">
        <label className="field">
          <span>类型</span>
          <select
            value={kind}
            onChange={(e) => {
              const k = e.target.value;
              setKind(k);
              setProtocol(k === "ocr" ? "paddleocr" : "tavily_hikari");
              setEndpoint(k === "ocr" ? "https://ocr.example.com/api" : "http://127.0.0.1:8787");
              setModel(k === "ocr" ? "PaddleOCR-VL-1.6" : "");
            }}
          >
            <option value="ocr">云端 OCR</option>
            <option value="search">联网搜索</option>
          </select>
        </label>
        <label className="field" style={{ flex: 1 }}>
          <span>服务地址</span>
          <input value={endpoint} onChange={(e) => setEndpoint(e.target.value)} />
        </label>
      </div>
      <div className="row">
        {kind === "ocr" && (
          <label className="field" style={{ flex: 1 }}>
            <span>模型</span>
            <input value={model} onChange={(e) => setModel(e.target.value)} />
          </label>
        )}
        <label className="field" style={{ flex: 1 }}>
          <span>API Key</span>
          <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} autoComplete="off" />
        </label>
      </div>
      <button className="btn btn-pri"
        disabled={working || !endpoint.trim() || !apiKey.trim()}
        onClick={async () => {
          if (saveLock.current) return;
          saveLock.current = true; setWorking(true); onBusy?.(true);
          try {
            await api.createConfig({
              kind,
              protocol,
              endpoint: endpoint.trim(),
              model_name: model.trim(),
              credentials: { api_key: apiKey.trim() },
              owner_scope: "admin",
            });
            setApiKey("");
            await onOk?.("云能力配置已保存");
          } catch (e: any) { onErr?.(e?.message || "保存失败"); }
          finally { saveLock.current = false; setWorking(false); onBusy?.(false); }
        }}
      >
        {working ? "正在保存…" : "保存云能力配置"}
      </button>
    </div>
  );
}
