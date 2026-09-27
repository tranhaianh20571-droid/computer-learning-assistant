import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

/** 切片 1：能力配置页（个人模型/TTS、管理员云能力、连接器配对、外发确认查询）。 */

const CAP_LABEL: Record<string, string> = {
  text: "文本",
  image: "图像",
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
  openai: "OpenAI 兼容",
  anthropic: "Anthropic 原生",
  gemini: "Gemini 原生",
  minimax: "MiniMax TTS",
  paddleocr: "PaddleOCR",
  tavily_hikari: "Tavily Hikari",
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
          <span key={cap} className={`badge ${meta.cls}`} title={typeof raw === "object" ? raw?.detail : ""}>
            {CAP_LABEL[cap] || cap}：{meta.text}
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
    <div className="stack">
      <div className="card stack">
        <h1>能力配置</h1>
        <p className="lede">
          个人内容模型与 TTS、管理员云能力。凭据加密存储，页面只显示掩码；能力测试只发送非私人样例。
        </p>
        <button className="secondary" onClick={refresh} disabled={busy}>刷新</button>
      </div>

      <ContentModelForm
        onErr={onErr}
        onOk={async (m: string) => { onOk?.(m); await refresh(); }}
        onBusy={setBusy}
      />
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
              <button
                className="secondary"
                onClick={async () => {
                  try {
                    const r = await api.testConfig(c.config_id);
                    onOk?.("能力测试完成");
                    setConfigs((prev) => prev.map((x) => (x.config_id === c.config_id ? { ...x, capability_status: r.capability_status } : x)));
                  } catch (e: any) { onErr?.(e?.message || "测试失败"); }
                }}
              >
                测试能力
              </button>
              <button
                className="secondary"
                onClick={async () => {
                  if (!confirm("删除该配置？删除后 worker 将拒绝调用该地址。")) return;
                  try { await api.deactivateConfig(c.config_id); onOk?.("配置已删除"); await refresh(); }
                  catch (e: any) { onErr?.(e?.message || "删除失败"); }
                }}
              >
                删除
              </button>
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
                className="secondary"
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
          <button className="secondary" onClick={loadDisclosures}>查询</button>
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
                        className="secondary"
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

function ContentModelForm({ onErr, onOk, onBusy }: any) {
  const [protocol, setProtocol] = useState("openai");
  const [endpoint, setEndpoint] = useState("https://api.openai.com/v1");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  return (
    <div className="card stack">
      <h2>个人内容模型</h2>
      <div className="row">
        <label className="field">
          <span>协议</span>
          <select value={protocol} onChange={(e) => setProtocol(e.target.value)}>
            <option value="openai">OpenAI 兼容</option>
            <option value="anthropic">Anthropic 原生</option>
            <option value="gemini">Gemini 原生</option>
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
      <button
        onClick={async () => {
          onBusy?.(true);
          try {
            await api.createConfig({
              kind: "content_model",
              protocol,
              endpoint,
              model_name: model,
              credentials: { api_key: apiKey },
              owner_scope: "user",
            });
            setApiKey("");
            onOk?.("模型配置已保存（凭据已加密）");
          } catch (e: any) { onErr?.(e?.message || "保存失败"); }
          finally { onBusy?.(false); }
        }}
      >
        保存模型配置
      </button>
    </div>
  );
}

function TtsForm({ onErr, onOk, onBusy }: any) {
  const [endpoint, setEndpoint] = useState("https://api.minimax.cn");
  const [apiKey, setApiKey] = useState("");
  const [voice, setVoice] = useState("");
  return (
    <div className="card stack">
      <h2>个人语音（MiniMax）</h2>
      <div className="row">
        <label className="field" style={{ flex: 1 }}>
          <span>服务地址</span>
          <input value={endpoint} onChange={(e) => setEndpoint(e.target.value)} />
        </label>
        <label className="field" style={{ flex: 1 }}>
          <span>音色 ID</span>
          <input value={voice} onChange={(e) => setVoice(e.target.value)} placeholder="按账户可用列表填写" />
        </label>
      </div>
      <label className="field">
        <span>API Key</span>
        <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} autoComplete="off" />
      </label>
      <button
        onClick={async () => {
          onBusy?.(true);
          try {
            await api.createConfig({
              kind: "tts",
              protocol: "minimax",
              endpoint,
              model_name: "speech-2.8-hd",
              credentials: { api_key: apiKey, voice_id: voice },
              owner_scope: "user",
            });
            setApiKey("");
            onOk?.("TTS 配置已保存（默认 speech-2.8-hd）");
          } catch (e: any) { onErr?.(e?.message || "保存失败"); }
          finally { onBusy?.(false); }
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
      <button
        onClick={async () => {
          onBusy?.(true);
          try {
            await api.createConfig({
              kind,
              protocol,
              endpoint,
              model_name: model,
              credentials: { api_key: apiKey },
              owner_scope: "admin",
            });
            setApiKey("");
            onOk?.("云能力配置已保存");
          } catch (e: any) { onErr?.(e?.message || "保存失败"); }
          finally { onBusy?.(false); }
        }}
      >
        保存云能力配置
      </button>
    </div>
  );
}
