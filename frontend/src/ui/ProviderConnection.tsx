import { useRef, useState } from "react";
import { Check, Eye, EyeOff, RefreshCw } from "lucide-react";
import { api } from "../api";
import InfoHint from "./InfoHint";
import ModelPicker from "./ModelPicker";

export type ProviderConfig = {
  config_id: string; config_version: number; kind: string; protocol: string;
  owner_scope: string; endpoint: string; model_name: string; credential_mask: string;
  is_active: boolean; capability_status: Record<string, any>;
};
type Kind = "image_generation" | "video_generation" | "tts" | "ocr";
export type ProviderFeedback = { notice: string; error: string };
const OPTIONS: Record<Kind, { title: string; protocol: string; endpoint: string; model: string; description: string; note?: string }> = {
  image_generation: { title: "课程封面图片模型", protocol: "openai_image", endpoint: "https://api.openai.com/v1", model: "",
    description: "规划确认后生成课程封面；未启用时使用本地水墨封面。只影响封面生成，不会改变课程内容本身。",
    note: "测试会生成合成样例图片，可能计费。" },
  video_generation: { title: "人物视频模型", protocol: "minimax_video", endpoint: "https://api.minimax.cn", model: "MiniMax-H3",
    description: "用于人物视频生成，例如人物形象展示和视频片段。未配置时人物页面继续使用静态形象或已有素材。" },
  tts: { title: "个人语音", protocol: "minimax", endpoint: "https://api.minimaxi.com", model: "speech-2.8-hd",
    description: "用于讲解语音和人物试听。配置后课堂讲解、人物试听会使用对应服务与音色；未配置时使用应用已有的默认语音策略。" },
  ocr: { title: "管理员云 OCR", protocol: "paddleocr", endpoint: "https://ocr.example.com/api", model: "PaddleOCR-VL-1.6",
    description: "管理员管理的文档识别服务，用于从 PDF / Word 等资料中提取文字和版面。普通成员只能使用，不能修改这组凭据。" },
};
const PROTOCOLS: Record<string, string> = { openai_image: "OpenAI 兼容图片生成", minimax_video: "MiniMax H3 视频",
  seedance: "Seedance 视频（Ark 兼容）", minimax: "MiniMax TTS", paddleocr: "PaddleOCR" };
const CAPABILITIES: Record<string, string> = { text: "文本", image: "图像识别", image_generate: "图片生成",
  video_generate: "视频生成", streaming: "流式", cancel: "取消", tool_call: "工具调用", json_schema: "JSON Schema" };

/** The parent keys each connection by its saved version; failed requests keep the current draft. */
export default function ProviderConnection({ kind, config, feedback, onSaved, onRemove }: {
  kind: Kind; config?: ProviderConfig; feedback?: ProviderFeedback;
  onSaved: (config: ProviderConfig, feedback: ProviderFeedback) => void; onRemove: (config: ProviderConfig) => void;
}) {
  const defaults = OPTIONS[kind];
  const [protocol, setProtocol] = useState(config?.protocol || defaults.protocol);
  const [endpoint, setEndpoint] = useState(config?.endpoint || defaults.endpoint);
  const [model, setModel] = useState(config?.model_name ?? defaults.model);
  const [key, setKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [voice, setVoice] = useState("");
  const [voices, setVoices] = useState<{ voice_id: string; name: string; category: string }[]>([]);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState(feedback?.notice || "");
  const [error, setError] = useState(feedback?.error || "");
  const [tested, setTested] = useState<Record<string, any>>();
  const lock = useRef(false);
  const sameProtocol = !!config && protocol === config.protocol;
  const dirty = !config || !sameProtocol || endpoint !== config.endpoint || model !== config.model_name || !!key;
  const valid = !!endpoint.trim() && (kind === "ocr" || !!model.trim()) && (sameProtocol || !!key.trim())
    && (kind !== "tts" || (!key && !!config) || !!voice);
  const id = "provider-" + kind;
  function changed() { setNotice(""); setError(""); setTested(undefined); }
  async function save() {
    if (!valid) { setError("请填写地址、模型和 API Key；语音密钥更换时需同时选择音色。"); return null; }
    if (!dirty) return config!;
    const body = { endpoint: endpoint.trim(), model_name: model.trim(),
      ...(key.trim() ? { credentials: { api_key: key.trim(), ...(kind === "tts" ? { voice_id: voice } : {}) } } : {}) };
    const saved = config && sameProtocol
      ? await api.updateConfig(config.config_id, body)
      : await api.createConfig({ ...body, kind, protocol, owner_scope: kind === "ocr" ? "admin" : "user" });
    setKey(""); setShowKey(false);
    return saved as ProviderConfig;
  }
  async function perform(action: "save" | "test" | "voices") {
    if (lock.current) return;
    lock.current = true; setBusy(action); setError(""); setNotice("");
    let saved: ProviderConfig | null = null;
    const outcome: ProviderFeedback = { notice: "", error: "" };
    try {
      if (action === "voices") {
        const result = await api.listTtsVoices({ endpoint, api_key: key });
        setVoices(result.data || []);
        setNotice(result.data?.length ? `已获取 ${result.data.length} 个音色` : "当前账号未返回可用音色。");
      } else {
        saved = await save();
        if (!saved) return;
        if (action === "test") {
          const result = await api.testConfig(saved.config_id);
          saved = { ...saved, capability_status: result.capability_status || {} };
          setTested(saved.capability_status);
          const values = Object.values(saved.capability_status);
          outcome.notice = values.length && values.every(raw => (typeof raw === "string" ? raw : raw?.state) === "available")
            ? "连接测试通过" : "测试完成，请查看能力状态。";
        } else outcome.notice = "已保存";
        setNotice(outcome.notice);
      }
    } catch {
      outcome.error = saved ? "配置已保存，测试未完成，请稍后重试。" : "操作未完成，请检查配置后重试；输入内容已保留。";
      setError(outcome.error);
    } finally {
      lock.current = false; setBusy("");
      // Publish even if the optional probe fails, so retry cannot create another configuration.
      if (saved) onSaved(saved, outcome);
    }
  }
  return <section className="purpose-connection provider-connection" aria-label={defaults.title + "配置"}>
    <h3 aria-label={defaults.title}>{defaults.title}<InfoHint label={defaults.title}>{defaults.description}</InfoHint></h3>
    {defaults.note && <p className="help">{defaults.note}</p>}
    <form onSubmit={event => { event.preventDefault(); void perform("save"); }}>
      <div className="purpose-field"><label htmlFor={id + "-protocol"}>协议</label>
        <select id={id + "-protocol"} value={protocol} disabled={!!busy} onChange={event => {
          const next = event.target.value; setProtocol(next); setKey(""); setShowKey(false); changed();
          if (kind === "video_generation") {
            setEndpoint(next === "seedance" ? "https://ark.cn-beijing.volces.com/api/v3" : defaults.endpoint);
            setModel(next === "seedance" ? "" : defaults.model);
          }
        }}>
          {(kind === "video_generation" ? ["minimax_video", "seedance"] : [defaults.protocol]).map(value =>
            <option key={value} value={value}>{PROTOCOLS[value]}</option>)}
        </select>
      </div>
      <div className="purpose-field"><label htmlFor={id + "-endpoint"}>服务地址</label><div>
        <div className="purpose-control"><input id={id + "-endpoint"} type="url" required disabled={!!busy} value={endpoint}
          spellCheck={false} onChange={event => { setEndpoint(event.target.value); setVoices([]); setVoice(""); changed(); }} />
          <button type="button" className="iconbtn purpose-test" title="测试" aria-label={"测试" + defaults.title}
            disabled={!!busy || !valid} onClick={() => void perform("test")}><RefreshCw size={16} className={busy === "test" ? "purpose-spin" : undefined} /></button>
        </div>
      </div></div>
      <div className="purpose-field"><label htmlFor={id + "-key"}>API Key</label><div className="purpose-secret">
        <input id={id + "-key"} type={showKey ? "text" : "password"} value={key} disabled={!!busy} autoComplete="off"
          placeholder={sameProtocol ? config!.credential_mask + " · 留空保留现有密钥" : "填写 API Key"}
          onChange={event => { setKey(event.target.value); setVoices([]); setVoice(""); changed(); }} />
        <button type="button" className="iconbtn" title={showKey ? "隐藏密钥" : "显示输入的密钥"} aria-label={showKey ? "隐藏密钥" : "显示输入的密钥"}
          aria-pressed={showKey} disabled={!!busy || !key} onClick={() => setShowKey(!showKey)}>{showKey ? <EyeOff size={16} /> : <Eye size={16} />}</button>
      </div></div>
      <div className="purpose-model"><ModelPicker label="模型" value={model} disabled={!!busy} onChange={value => { setModel(value); changed(); }}
        requestKey={JSON.stringify([config?.config_id, protocol, endpoint, key])}
        load={["openai_image", "seedance"].includes(protocol) ? signal => api.discoverModels({
          endpoint, protocol, ...(sameProtocol ? { config_id: config!.config_id } : {}), ...(key ? { api_key: key } : {}),
        }, signal) : undefined} /></div>
      {kind === "tts" && <div className="purpose-field"><label htmlFor={id + "-voice"}>音色</label><div>
        <div className="purpose-control"><select id={id + "-voice"} value={voice} disabled={!!busy || !key} onChange={event => { setVoice(event.target.value); changed(); }}>
          <option value="">{config && !key ? "已保存音色 · 保留不变" : "请选择音色"}</option>
          {[["system_voice", "系统音色"], ["voice_cloning", "克隆音色"], ["voice_generation", "文生音色"]].map(([category, label]) =>
            <optgroup key={category} label={label}>{voices.filter(item => item.category === category).map(item =>
              <option key={item.voice_id} value={item.voice_id}>{item.name} · {item.voice_id}</option>)}</optgroup>)}
        </select><button type="button" className="iconbtn purpose-test" title="获取可用音色" aria-label="获取可用音色" disabled={!!busy || !key || !endpoint}
          onClick={() => void perform("voices")}><RefreshCw size={16} /></button></div>
        {config && <p className="help">留空保留已保存的密钥和音色；更换密钥时重新获取音色。</p>}
      </div></div>}
      <div className="purpose-capability-area"><div className="purpose-capabilities" aria-label="能力状态">
        {Object.entries(tested || config?.capability_status || {}).map(([cap, raw]) => {
          const state = typeof raw === "string" ? raw : raw?.state;
          return <span key={cap}>{state === "available" && <Check size={13} />}{CAPABILITIES[cap] || cap}{state === "available" ? "" : state === "unavailable" ? " · 不可用" : " · 未确认"}</span>;
        })}
      </div></div>
      {error && <p className="capability-detail" role="alert">{error}</p>}
      <div className="purpose-save"><span className="help" role="status">{busy ? "处理中…" : notice || (dirty ? "有未保存的更改" : "已保存")}</span><div>
        {config && <button type="button" className="btn" disabled={!!busy} onClick={() => onRemove(config)}>删除</button>}
        <button type="submit" className="btn btn-pri" disabled={!!busy || !valid}>保存</button>
      </div></div>
    </form>
  </section>;
}
