import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { ArrowUpRight, FolderOpen, Globe, Loader2 } from "lucide-react";
import Dialog from "./Dialog";
import "./browserPicker.css";

export type BrowserChoice = { id: string; name: string; icon: string };

function BrowserPicker({ url, finish }: { url: string; finish: (opened: boolean) => void }) {
  const [choices, setChoices] = useState<BrowserChoice[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const first = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const trap = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const modal = panel.current?.closest(".modal");
      const buttons = [...(modal?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") || [])]
        .filter(button => button.getClientRects().length);
      if (!buttons.length) { event.preventDefault(); return; }
      const edge = event.shiftKey ? buttons[0] : buttons.at(-1);
      if (!modal?.contains(document.activeElement) || document.activeElement === edge) {
        event.preventDefault(); (event.shiftKey ? buttons.at(-1) : buttons[0])?.focus();
      }
    };
    document.addEventListener("keydown", trap, true);
    return () => document.removeEventListener("keydown", trap, true);
  }, []);
  useEffect(() => {
    let live = true;
    window.desktopBrowser!.listBrowsers().then(values => { if (live) setChoices(values); })
      .catch(() => { if (live) setError("未能读取浏览器列表，请选择浏览器程序。"); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, []);
  useEffect(() => { if (!loading) first.current?.focus(); }, [loading]);

  async function open(choice: BrowserChoice) {
    setBusy(true); setError("");
    try { await window.desktopBrowser!.openExternal(url, choice.id); finish(true); }
    catch { setError("无法启动所选浏览器，请尝试其他浏览器。"); setBusy(false); }
  }
  async function browse() {
    setBusy(true); setError("");
    try {
      const choice = await window.desktopBrowser!.chooseExecutable();
      if (choice) await open(choice);
      else setBusy(false);
    } catch { setError("未能选择浏览器程序，请重试。"); setBusy(false); }
  }
  return <Dialog title="选择浏览器" kind="info" onClose={() => { if (!busy) finish(false); }}
    actions={<button className="btn btn-sm" disabled={busy} onClick={() => finish(false)}>取消</button>}>
    <div className="browser-picker" ref={panel}>
      {loading && <div className="browser-picker-loading" role="status"><Loader2 className="spin" size={18}/>正在检测浏览器…</div>}
      {!loading && !choices.length && <p className="muted">未检测到已注册的浏览器。</p>}
      <div className="browser-picker-list">
        {choices.map((choice, index) => <button key={choice.id} ref={index === 0 ? first : undefined}
          className="browser-picker-row" aria-label={`使用 ${choice.name}`} disabled={busy} onClick={() => void open(choice)}>
          {choice.icon ? <img src={choice.icon} width={24} height={24} alt=""/> : <Globe size={24}/>}
          <span>{choice.name}</span><ArrowUpRight size={17}/>
        </button>)}
      </div>
      <button className="browser-picker-row browser-picker-browse" disabled={busy} onClick={() => void browse()}>
        <FolderOpen size={22}/><span>选择浏览器程序</span>
      </button>
      {error && <p className="browser-picker-error" role="alert">{error}</p>}
    </div>
  </Dialog>;
}

let selecting = false;
export function chooseBrowser(url: string): Promise<boolean> {
  if (selecting) return Promise.resolve(false);
  selecting = true;
  const previous = document.activeElement;
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  return new Promise(resolve => {
    const finish = (opened: boolean) => {
      root.unmount(); host.remove(); selecting = false;
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus();
      resolve(opened);
    };
    root.render(<BrowserPicker url={url} finish={finish}/>);
  });
}
