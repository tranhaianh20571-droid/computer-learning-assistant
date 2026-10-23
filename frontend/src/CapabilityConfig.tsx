import { useCallback, useEffect, useRef, useState } from "react";
import Dialog from "./ui/Dialog";
import "./capabilities.css";
import { api } from "./api";
import MemorySettings from "./views/MemorySettings";
import MemoryPage from "./views/MemoryPage";
import DocumentParserSettings from "./DocumentParserSettings";
import SourceProviderSettings from "./SourceProviderSettings";
import StorageSettings from "./views/StorageSettings";
import { BookOpen, Cable, Cpu, FlaskConical, HardDrive, Image, ScanLine, Search, ShieldCheck } from "lucide-react";
import PurposeModelSettings from "./ui/PurposeModelSettings";
import InfoHint from "./ui/InfoHint";
import ProviderConnection, { type ProviderConfig, type ProviderFeedback } from "./ui/ProviderConnection";

/** 能力配置页：个人模型、语音与搜索，管理员 OCR，以及连接器和外发记录。 */

const PROTOCOL_LABEL: Record<string, string> = {
  openai_image: "OpenAI 兼容图片生成",
  minimax_video: "MiniMax H3 视频",
  seedance: "Seedance 视频（Ark 兼容）",
  openai: "OpenAI 兼容",
  anthropic: "Anthropic 原生",
  gemini: "Gemini 原生",
  minimax: "MiniMax TTS",
  paddleocr: "PaddleOCR",
  anysearch: "AnySearch",
};

type ConfigItem = ProviderConfig;

type ConnectorItem = {
  binding_id: string;
  device_name: string;
  status: string;
  last_seen_at?: string;
};

export default function CapabilityConfig({ isAdmin, ownerId, onErr, onOk }: {
  isAdmin: boolean; ownerId: string; onErr?: (message: string) => void; onOk?: (message: string) => void;
}) {
  const pages = [["models", "内容模型"], ["research", "人物蒸馏模型"], ["memory", "记忆模型"], ["media", "图像与语音"], ["search", "搜索"], ["documents", "文档识别"], ["firecrawl", "Firecrawl"], ["bilinote", "BiliNote"], ["storage","存储与性能"]];
  const categoryIcons:Record<string,React.ReactNode>={storage:<HardDrive/>,models:<Cpu/>,memory:<BookOpen/>,media:<Image/>,search:<Search/>,documents:<ScanLine/>,"personal-memory":<ShieldCheck/>,connections:<Cable/>,admin:<ScanLine/>};
  const [page, setPage] = useState(() => new URLSearchParams(location.hash.split("?")[1] || "").get("section") || "models");
  useEffect(() => {
    function syncPage() { setPage(new URLSearchParams(location.hash.split("?")[1] || "").get("section") || "models"); }
    window.addEventListener("hashchange", syncPage);
    return () => window.removeEventListener("hashchange", syncPage);
  }, []);
  const currentPage = ["personal-memory","connections"].includes(page) || (page === "admin" && isAdmin) || pages.some(([key]) => key === page) ? page : "models";
  const [memoryVisited, setMemoryVisited] = useState(currentPage === "memory");
  useEffect(() => { if (currentPage === "memory") setMemoryVisited(true); }, [currentPage]);
  const [allConfigs, setConfigs] = useState<ConfigItem[]>([]);
  const searchConfig = allConfigs.find(config => config.kind === "search" && config.protocol === "anysearch" && config.owner_scope === "user" && config.is_active);
  const [loaded, setLoaded] = useState(false);
  const [configLoading, setConfigLoading] = useState(true);
  const [configError, setConfigError] = useState("");
  const [connectorLoading, setConnectorLoading] = useState(true);
  const [connectorsLoaded, setConnectorsLoaded] = useState(false);
  const [connectorError, setConnectorError] = useState("");
  const [connectors, setConnectors] = useState<ConnectorItem[]>([]);
  const [disclosures, setDisclosures] = useState<any[]>([]);
  const [taskId, setTaskId] = useState("");
  const [pairing, setPairing] = useState<any>(null);
  const [deviceName, setDeviceName] = useState("");
  const [, setBusy] = useState(false);
  const [deleting, setDeleting] = useState<ConfigItem | null>(null);
  const [deleteError, setDeleteError] = useState("");
  const [operation, setOperation] = useState<string | null>(null);
  const operationLock = useRef(false);
  const alive = useRef(false);
  const refreshVersion = useRef(0);
  const connectorVersion = useRef(0);
  const configRequest = useRef<AbortController | null>(null);
  const connectorRequest = useRef<AbortController | null>(null);
  const [providerFeedback, setProviderFeedback] = useState<Record<string, ProviderFeedback>>({});
  const currentConfig = (kind: string) => allConfigs.filter(config => config.kind === kind && config.is_active
    && config.owner_scope === (kind === "ocr" ? "admin" : "user")).at(-1);
  function providerForm(kind: "image_generation" | "video_generation" | "tts" | "ocr") {
    const config = currentConfig(kind);
    return <ProviderConnection key={`${ownerId}:${kind}:${config?.config_id || "new"}:${config?.config_version || 0}`}
      kind={kind} config={config} feedback={providerFeedback[kind]}
      onSaved={(saved, feedback) => {
        setProviderFeedback(previous => ({ ...previous, [kind]: feedback }));
        setConfigs(previous => [...previous.filter(item => item.config_id !== saved.config_id), saved]);
      }}
      onRemove={saved => { setDeleteError(""); setDeleting(saved); }} />;
  }

  const refreshConfigs = useCallback(async () => {
    const version = ++refreshVersion.current;
    configRequest.current?.abort();
    const controller = new AbortController();
    configRequest.current = controller;
    setConfigLoading(true); setConfigError("");
    try {
      const cfg = await api.listConfigs(undefined, controller.signal);
      if (!alive.current || version !== refreshVersion.current) return;
      setConfigs(cfg.data || []);
      setLoaded(true);
    } catch {
      if (!controller.signal.aborted && alive.current && version === refreshVersion.current) setConfigError("加载能力配置失败，请重试。");
    } finally {
      if (alive.current && version === refreshVersion.current) setConfigLoading(false);
    }
  }, []);

  const refreshConnectors = useCallback(async () => {
    const version = ++connectorVersion.current;
    connectorRequest.current?.abort();
    const controller = new AbortController();
    connectorRequest.current = controller;
    setConnectorLoading(true); setConnectorError("");
    try {
      const conn = await api.listConnectors(controller.signal);
      if (!alive.current || version !== connectorVersion.current) return;
      setConnectors(conn.data || []); setConnectorsLoaded(true);
    } catch {
      if (!controller.signal.aborted && alive.current && version === connectorVersion.current) setConnectorError("加载连接器失败，请重试。");
    } finally {
      if (alive.current && version === connectorVersion.current) setConnectorLoading(false);
    }
  }, []);

  const refresh = useCallback(async () => {
    void refreshConnectors();
    await refreshConfigs();
  }, [refreshConfigs, refreshConnectors]);

  useEffect(() => {
    alive.current = true;
    void refresh();
    return () => {
      alive.current = false; refreshVersion.current++; connectorVersion.current++;
      configRequest.current?.abort(); connectorRequest.current?.abort();
    };
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
    <div className="capabilities-page">
      {deleting && <Dialog title="删除配置" kind="info" onClose={() => { if (!operationLock.current) setDeleting(null); }} actions={<>
        <button className="btn" disabled={Boolean(operation)} onClick={() => setDeleting(null)}>取消</button>
        <button className="btn btn-danger" disabled={Boolean(operation)} onClick={async () => {
          if (operationLock.current) return;
          operationLock.current = true; setOperation(`delete:${deleting.config_id}`);
          try {
            await api.deactivateConfig(deleting.config_id);
            setConfigs(prev => prev.filter(c => c.config_id !== deleting.config_id));
            setProviderFeedback(previous => ({ ...previous, [deleting.kind]: { notice: "", error: "" } }));
            setDeleting(null); onOk?.("配置已删除");
          }
          catch (e: any) { setDeleteError(e?.message || "删除失败，请重试"); }
          finally { operationLock.current = false; setOperation(null); }
        }}>{operation ? "正在删除…" : "确认删除"}</button>
      </>}>
        <p>确定删除 {PROTOCOL_LABEL[deleting.protocol] || deleting.protocol} {deleting.model_name}？后续任务将停止使用该配置。</p>
        {deleteError && <p role="alert" className="capability-detail">{deleteError}</p>}
      </Dialog>}
      <aside className="settings-navigation" hidden={["connections","personal-memory","admin"].includes(currentPage)}>
      <header className="settings-heading">
        <div><h1>设置</h1><p className="help">个人模型与服务</p></div>
      </header>
      <nav className="settings-tabs" aria-label="设置分类">
        {pages.map(([key, label]) => <button key={key} type="button" aria-current={currentPage === key ? "page" : undefined}
          onClick={() => { setPage(key); location.hash = "/capabilities?section=" + key; }}><span className="settings-category-icon" aria-hidden="true">{categoryIcons[key]}</span>{label}</button>)}
      </nav>
      </aside>

      <div className="settings-body">
      {currentPage==="storage"&&<StorageSettings key={ownerId} isAdmin={isAdmin}/>}
      {(currentPage==="firecrawl"||currentPage==="bilinote")&&<SourceProviderSettings key={ownerId+currentPage} provider={currentPage}/>}
      <div hidden={!["models","research"].includes(currentPage)}>
      <PurposeModelSettings key={ownerId + ":" + (currentPage === "research" ? "research" : "models")} researchOnly={currentPage === "research"} showAdvanced={new URLSearchParams(location.hash.split("?")[1] || "").get("advanced") === "1"} visible={["models","research"].includes(currentPage)} onSaved={refreshConfigs}/>
      </div>
      {currentPage === "media" && <div>
        <header className="purpose-heading"><h2 aria-label="图像与语音">图像与语音<InfoHint label="图像与语音">课程封面、人物视频和讲解语音。三项服务分别配置，可以只启用其中一部分；密钥加密保存在服务端，保存后只显示掩码。</InfoHint></h2></header>
        {configLoading && !loaded && <p role="status">正在加载能力配置…</p>}
        {configError && <div role="alert">{configError}<button className="btn" onClick={refreshConfigs}>重试加载配置</button></div>}
        {loaded && !configError && <>{providerForm("image_generation")}{providerForm("video_generation")}{providerForm("tts")}</>}
      </div>}
      <div hidden={currentPage !== "search"}>
      <AnySearchForm key={ownerId} config={searchConfig} loaded={loaded} onBusy={setBusy} onRefresh={refresh}
        onSaved={config => { if (alive.current) setConfigs(previous => [...previous.filter(item => item.config_id !== config.config_id), config]); }}
        onDisabled={configId => { if (alive.current) setConfigs(previous => previous.filter(item => item.config_id !== configId)); }} />
      </div>
      {currentPage === "documents" && <DocumentParserSettings key={ownerId} loaded={loaded && !configLoading && !configError}
        config={allConfigs.filter(item => item.kind === "document_parser" && item.protocol === "paddleocr_vl" && item.is_active).at(-1)}
        refresh={refreshConfigs} />}
      {(memoryVisited || currentPage === "memory") && <div hidden={currentPage !== "memory"}>
        <MemorySettings key={ownerId} isAdmin={isAdmin} />
      </div>}
      {currentPage === "personal-memory" && <div className="personal-memory-settings"><MemoryPage onErr={onErr || (() => {})} /></div>}
      {isAdmin && currentPage === "admin" && <div>
        {configLoading && !loaded && <p role="status">正在加载能力配置…</p>}
        {configError && <div role="alert">{configError}<button className="btn" onClick={refreshConfigs}>重试加载配置</button></div>}
        {loaded && !configError && providerForm("ocr")}
      </div>}

      <div hidden={currentPage !== "connections"}>
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
        {connectorLoading && <p className="help" role="status">正在加载连接器…</p>}
        {connectorError && <div><p role="alert">{connectorError}</p><button className="btn" onClick={refreshConnectors}>重试加载连接器</button></div>}
        {connectorsLoaded && !connectorLoading && !connectorError && connectors.length === 0 && <p className="help">暂无连接器。</p>}
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
      </div>
    </div>
  );
}

const SEARCH_DETAIL: Record<string, string> = {
  SEARCH_TIMEOUT: "搜索服务响应较慢，请稍后测试。",
  SEARCH_CONNECTION_ERROR: "暂时无法连接搜索服务，请检查网络后再测试。",
  SEARCH_RATE_LIMITED: "搜索请求较频繁，请稍后测试。",
  SEARCH_CREDENTIAL_INVALID: "API Key 无效，请更新后测试。",
  SEARCH_QUOTA_EXCEEDED: "AnySearch 额度不足，可补充额度后再测试。",
  SEARCH_PROVIDER_UNAVAILABLE: "搜索服务暂不可用，请稍后测试。",
  SEARCH_OUTPUT_INVALID: "搜索服务未返回有效结果，请稍后测试。",
  SEARCH_RESPONSE_TOO_LARGE: "搜索服务响应异常，请稍后测试。",
  SEARCH_REQUEST_REJECTED: "搜索服务未接受请求，请稍后测试。",
  SEARCH_CONFIG_UNAVAILABLE: "搜索配置尚不可用，请测试或更新 API Key。",
  SEARCH_BUDGET_EXCEEDED: "本次搜索暂不可用，请稍后测试。",
  SEARCH_INTERRUPTED: "测试未完成，请稍后再试。",
  credential_invalid: "API Key 无效，请更新后测试。",
  connection_error: "暂时无法连接搜索服务，请检查网络后再测试。",
  timeout: "搜索服务响应较慢，请稍后测试。",
  rate_limit: "搜索请求较频繁，请稍后测试。",
};

function searchDetail(detail: unknown): string {
  return typeof detail === "string" && Object.hasOwn(SEARCH_DETAIL, detail)
    ? SEARCH_DETAIL[detail] : "上次验证未确认可用，可稍后测试；学习时仍会按需尝试搜索。";
}

function AnySearchForm({ config, loaded, onBusy, onRefresh, onSaved, onDisabled }: {
  config?: ConfigItem; loaded: boolean; onBusy: (busy: boolean) => void;
  onRefresh: () => Promise<void>; onSaved: (config: ConfigItem) => void;
  onDisabled: (configId: string) => void;
}) {
  const [apiKey, setApiKey] = useState("");
  const [working, setWorking] = useState<"save" | "test" | "disable" | null>(null);
  const [notice, setNotice] = useState("");
  const lock = useRef(false);
  const alive = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  // 展示掩码永远不作为凭据；也拒绝带空白/控制字符的输入，不暗中修剪 Key。
  const validKey = Boolean(apiKey) && apiKey.length <= 4096 && apiKey === apiKey.trim()
    && !/[\s\u0000-\u001f\u007f-\u009f*•●…]/u.test(apiKey)
    && apiKey !== config?.credential_mask;

  const perform = async (action: "save" | "test" | "disable") => {
    if (!loaded || lock.current || (action === "save" ? !validKey : !config)) return;
    lock.current = true; setWorking(action); setNotice(""); onBusy(true);
    const current = config;
    // 清空受控输入后再等待网络，无论保存成功或失败都不保留明文。
    const credentials = action === "save" ? { api_key: apiKey } : undefined;
    if (action === "save") setApiKey("");
    let saved = current;
    let persisted = false;
    try {
      if (action === "save") {
        saved = current
          ? await api.updateConfig(current.config_id, { credentials })
          : await api.createConfig({ kind: "search", protocol: "anysearch", endpoint: "https://api.anysearch.com",
            model_name: "", credentials, owner_scope: "user" });
        persisted = true;
        if (!alive.current) return;
        onSaved(saved!);
        setNotice("Key 已保存，学习时将按需搜索。");
      }
      if (!saved || !alive.current) return;
      if (action === "disable") {
        await api.deactivateConfig(saved.config_id);
        if (!alive.current) return;
        onDisabled(saved.config_id);
        setNotice("搜索已停用。学习将使用现有资料继续。");
      } else if (action === "test") {
        const result = await api.testConfig(saved.config_id);
        if (!alive.current) return;
        const tested = result.capability_status?.text;
        const testedState = typeof tested === "string" ? tested : tested?.state;
        setNotice(testedState === "available" ? "AnySearch 测试通过。" : searchDetail(tested?.detail));
      }
    } catch (error: unknown) {
      if (!alive.current) return;
      const safe = error as { status?: number; code?: string };
      setNotice(safe.status === 409 ? "配置已发生变化，请刷新后再更新或测试。"
        : safe.status === 401 ? "会话已失效，请刷新页面后重试。"
        : safe.status === 403 ? "当前会话无权管理此搜索配置，请刷新后重试。"
        : safe.status === 429 ? SEARCH_DETAIL.SEARCH_RATE_LIMITED
        : safe.code && Object.hasOwn(SEARCH_DETAIL, safe.code) ? searchDetail(safe.code)
        : safe.status === 422 ? "配置格式不正确，请检查 API Key 的格式后重试。"
        : action === "disable" ? "停用未完成，请稍后重试。"
        : action === "save" ? persisted ? "Key 已保存，学习时将按需搜索。" : "保存未完成，请稍后重试。"
        : "测试未完成，可稍后重试；学习时仍会按需尝试搜索。");
    } finally {
      if (alive.current) await onRefresh();
      lock.current = false;
      if (alive.current) { setWorking(null); onBusy(false); }
    }
  };

  return <section className="card stack" aria-labelledby="anysearch-heading">
    <h2 id="anysearch-heading" aria-label="AnySearch 搜索">AnySearch 搜索<InfoHint label="AnySearch 搜索">使用你自己的 AnySearch 账号额度，学伴按需发送最小主题查询；AnySearch 可能通过内部上游服务完成搜索。保存后会在学习中按需自动使用，无需先测试，也可随时停用。连接测试只发送非私人样例，会使用少量搜索额度；未配置或实际搜索失败时，学习会使用现有资料继续。</InfoHint></h2>
    {!loaded ? <p className="help" role="status">正在加载搜索设置…</p>
      : !config && <p className="help">尚未配置搜索。你仍可使用现有资料学习。</p>}
    <label className="field">
      <span>{config ? "新的 API Key" : "API Key"}</span>
      <input type="password" autoComplete="off" spellCheck={false} maxLength={4096}
        value={apiKey} disabled={!loaded || Boolean(working)} placeholder={config ? "只在更新 Key 时填写" : "填写自己的 AnySearch API Key"}
        onChange={event => { setApiKey(event.target.value); setNotice(""); }} />
    </label>
    <div className="row">
      <button className="btn btn-pri" disabled={!loaded || Boolean(working) || !validKey} onClick={() => perform("save")}>
        {working === "save" ? "正在保存…" : config ? "更新 Key" : "保存 Key"}
      </button>
      {config && <>
        <button className="iconbtn" title="测试" aria-label="测试搜索" disabled={Boolean(working)} onClick={() => perform("test")}><FlaskConical size={16} /></button>
        <button className="btn" disabled={Boolean(working)} onClick={() => perform("disable")}>{working === "disable" ? "正在停用…" : "停用搜索"}</button>
      </>}
    </div>
    <p className="help">连接测试会使用少量搜索额度。</p>
    {notice && <p className="help" role="status">{notice}</p>}
  </section>;
}
