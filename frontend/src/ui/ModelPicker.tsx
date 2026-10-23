import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { ChevronDown, RefreshCw } from "lucide-react";

export default function ModelPicker({ label = "模型名", value, onChange, load, requestKey, disabled = false, trailing }: {
  label?: string; value: string; onChange: (value: string) => void;
  load?: (signal: AbortSignal) => Promise<{ models: string[] }>; requestKey: string; disabled?: boolean; trailing?:ReactNode;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [models, setModels] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const request = useRef<AbortController | null>(null);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    request.current?.abort(); setModels([]); setMessage(""); setOpen(false); setBusy(false);
    return () => request.current?.abort();
  }, [requestKey]);
  useEffect(() => {
    function outside(event: PointerEvent) {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, []);
  async function refresh() {
    if (!load || busy) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setOpen(true); setBusy(true); setMessage("");
    try {
      const result = await load(controller.signal);
      if (controller.signal.aborted) return;
      setModels(result.models || []);
      if (!result.models?.length) setMessage("目录为空，可手动填写模型名。");
    } catch {
      if (!controller.signal.aborted) setMessage("获取失败，请检查地址和 API Key；也可手动填写。");
    } finally { if (!controller.signal.aborted) setBusy(false); }
  }
  return <div className="field model-picker" ref={root} onKeyDown={e => { if (e.key === "Escape") setOpen(false); }}>
    <label htmlFor={id}>{label}</label>
    <div className={trailing?"model-picker-with-trailing":undefined}><div className="model-picker-input">
      <input id={id} value={value} onChange={e => onChange(e.target.value)} disabled={disabled}
        autoComplete="off" spellCheck={false} />
      {load && <button type="button" className="model-picker-toggle" disabled={disabled || busy}
        title="获取可用模型" aria-label={`获取可用模型：${label}`} aria-expanded={open}
        aria-controls={id + "-catalog"} onClick={() => open ? setOpen(false) : void refresh()}>
        <ChevronDown size={18} aria-hidden="true" />
      </button>}
    </div>{trailing}</div>
    {open && <div className="model-picker-menu" id={id + "-catalog"} role="region" aria-label="可用模型">
      <button type="button" className="model-picker-refresh" onClick={refresh} disabled={busy}>
        <RefreshCw size={14} aria-hidden="true" />{busy ? "正在获取…" : "获取可用模型"}
      </button>
      {message && <p role="status">{message}</p>}
      <div className="model-picker-options">
        {models.map(model => <button type="button" key={model} aria-pressed={value === model}
          onClick={() => { onChange(model); setOpen(false); document.getElementById(id)?.focus(); }}>{model}</button>)}
      </div>
    </div>}
  </div>;
}
