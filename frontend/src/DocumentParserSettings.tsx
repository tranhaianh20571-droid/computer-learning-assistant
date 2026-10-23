import { useRef, useState } from "react";
import { Eye, EyeOff, FlaskConical, Trash2 } from "lucide-react";
import { api } from "./api";
import InfoHint from "./ui/InfoHint";

export default function DocumentParserSettings({ config, loaded, refresh }: {
  config?: any; loaded: boolean; refresh: () => Promise<void>;
}) {
  const [key, setKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const lock = useRef(false);
  async function act(action: "save" | "test" | "remove") {
    if (lock.current) return;
    lock.current = true; setBusy(true); setNotice("");
    const credentials = { api_key: key };
    if (action === "save") { setKey(""); setShowKey(false); }
    try {
      if (action === "save") {
        if (config) await api.updateConfig(config.config_id, { credentials });
        else await api.createConfig({ kind: "document_parser", protocol: "paddleocr_vl",
          endpoint: "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs", model_name: "PaddleOCR-VL-1.6",
          credentials, owner_scope: "user" });
        setNotice("密钥已保存。");
      } else if (action === "remove") {
        await api.deactivateConfig(config.config_id);
        setNotice("文档识别已停用。");
      } else {
        const result = await api.testConfig(config.config_id);
        const health = result.capability_status?.connection;
        setNotice(health?.state === "available" ? "连接验证通过；尚未上传文件。" :
          health?.detail === "credential_invalid" ? "密钥无效，请更新。" : "连接验证失败，请稍后重试。");
      }
      await refresh();
    } catch {
      setNotice("操作未完成，请检查登录状态和密钥后重试。");
    } finally { lock.current = false; setBusy(false); }
  }
  return <section className="stack document-settings" aria-label="PaddleOCR 文档识别">
    <h2 aria-label="PaddleOCR 文档识别">PaddleOCR 文档识别<InfoHint label="PaddleOCR 文档识别">识别 PDF / Word 文档中的文字与版面，供资料学习使用。密钥加密保存在服务端；停用后不再用于新的文档解析，已有资料不会被删除。</InfoHint></h2>
    <div className="stack"><label htmlFor="document-parser-key">API Key</label>
      <div className="purpose-secret">
      <input id="document-parser-key" className="inp" type={showKey ? "text" : "password"} autoComplete="off" value={key}
        placeholder={config ? "已保存；填写新密钥可替换" : "填写你的 PaddleOCR API Key"}
        onChange={event => setKey(event.target.value)} disabled={busy || !loaded} />
      <button type="button" className="iconbtn" title={showKey ? "隐藏密钥" : "显示输入的密钥"}
        aria-label={showKey ? "隐藏密钥" : "显示输入的密钥"} aria-pressed={showKey}
        disabled={busy || !loaded || !key} onClick={() => setShowKey(!showKey)}>
        {showKey ? <EyeOff size={16} /> : <Eye size={16} />}
      </button>
      </div>
    </div>
    {config && <span className="help">已保存密钥 · v{config.config_version}</span>}
    <div className="row">
      <button className="btn btn-pri" disabled={!loaded || busy || !key || key !== key.trim() || /[\s*•]/u.test(key)}
        onClick={() => act("save")}>{busy ? "处理中…" : config ? "更新密钥" : "保存密钥"}</button>
      {config && <>
        <button className="iconbtn" title="测试连接" aria-label="测试 PaddleOCR 连接" disabled={busy}
          onClick={() => act("test")}><FlaskConical size={18} /></button>
        <button className="iconbtn" title="停用文档识别" aria-label="停用 PaddleOCR" disabled={busy}
          onClick={() => act("remove")}><Trash2 size={18} /></button>
      </>}
      <a href="https://aistudio.baidu.com/paddleocr" target="_blank" rel="noreferrer">获取密钥</a>
    </div>
    {notice && <p role="status">{notice}</p>}
  </section>;
}
