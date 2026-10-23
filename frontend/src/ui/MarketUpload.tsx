import { useEffect, useRef, useState } from "react";
import { Check, ExternalLink, Link2, Loader2, UploadCloud, X } from "lucide-react";
import { characterApi } from "../api";
import { errText } from "../lib/errors";

type Card = { card_id: string; profile: { name: string; identity: string; tags: string[] }; assets: { asset_id: string; kind: string; url: string }[] };
type UploadResult = { card_id: string; status: "uploaded" | "duplicate" | "failed"; market_card_id?: string; message?: string };

const statusText: Record<string, string> = {
  uploaded: "已上传",
  duplicate: "已存在，未重复上传",
  failed: "上传失败",
};

export default function MarketUpload({ cards, open, onClose, onDone }: {
  cards: Card[];
  open: boolean;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const [bound, setBound] = useState<boolean | null>(null);
  const [receiving, setReceiving] = useState(false);
  const [receiveOnly, setReceiveOnly] = useState(false);
  const [binding, setBinding] = useState<{ code: string; url: string } | null>(null);
  const [bindingState, setBindingState] = useState<"idle" | "waiting" | "done">("idle");
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [results, setResults] = useState<UploadResult[]>([]);
  const timer = useRef<number | null>(null);
  const live = useRef(true);

  useEffect(() => {
    live.current = true;
    return () => { live.current = false; if (timer.current) window.clearTimeout(timer.current); };
  }, []);

  useEffect(() => {
    if (!open) return;
    setError("");
    setResults([]);
    void characterApi.marketBinding()
      .then((value: any) => {
        if (!live.current) return;
        setBound(Boolean(value?.bound)); setReceiving(Boolean(value?.receiving)); setReceiveOnly(Boolean(value?.receive_only));
      })
      .catch(() => { setBound(false); setError("读取市集绑定状态失败。"); });
  }, [open]);

  const startBind = async () => {
    setError("");
    try {
      if (timer.current) window.clearTimeout(timer.current);
      const value: any = await characterApi.marketBindStart("receive");
      setBinding(value);
      setBindingState("waiting");
      const deadline = Date.now() + 10 * 60 * 1000;
      const poll = async () => {
        if (!live.current) return;
        if (Date.now() > deadline) {
          setBindingState("idle"); setBinding(null); setError("授权链接已过期，请重新生成。"); return;
        }
        try {
          const status: any = await characterApi.marketBindStatus(value.code, "receive");
          if (!live.current) return;
          if (status?.bound) {
            setBindingState("done");
            setBound(Boolean(status.current_account));
            setReceiveOnly(true); setReceiving(Boolean(status.current_account));
            onDone(status.current_account ? "已开启当前账号接收。" : "已为网页账号开启接收；本机登录账号保持不变。");
            return;
          }
        } catch { /* 保持等待，超时后由用户重试 */ }
        timer.current = window.setTimeout(poll, 2500);
      };
      timer.current = window.setTimeout(poll, 2500);
    } catch (failure) {
      setError(errText(failure));
    }
  };

  const toggle = (id: string) =>
    setSelected(old => old.includes(id) ? old.filter(item => item !== id) : [...old, id]);

  const upload = async () => {
    if (!selected.length) return;
    setBusy(true); setError(""); setResults([]);
    try {
      const value: any = await characterApi.marketUpload(selected);
      const items: UploadResult[] = value?.items || [];
      setResults(items);
      const uploaded = items.filter(item => item.status === "uploaded").length;
      const duplicate = items.filter(item => item.status === "duplicate").length;
      const failed = items.filter(item => item.status === "failed").length;
      onDone(`上传完成：成功 ${uploaded}，已存在 ${duplicate}，失败 ${failed}。`);
      if (uploaded) setSelected([]);
    } catch (failure) {
      setError(errText(failure));
    } finally {
      setBusy(false);
    }
  };

  if (!open) return null;

  return <div className="market-modal" role="dialog" aria-modal="true" aria-label="上传卡片到市集">
    <div className="market-backdrop" onClick={busy ? undefined : onClose} />
    <div className="market-panel">
      <header>
        <div>
          <h2>上传到市集</h2>
          <p>选择本机人物卡，导出为卡片包后上传。学习记录不会离开这台电脑。</p>
        </div>
        <button className="iconbtn" aria-label="关闭" title="关闭" onClick={onClose} disabled={busy}><X size={18} /></button>
      </header>

      {error && <p role="alert" className="market-error">{error}</p>}

      {bound === null && <p role="status"><Loader2 size={15} className="spin" /> 正在读取市集绑定…</p>}

      {bound === false && <section className="market-bind">
        <h3><Link2 size={16} /> 开启账号接收</h3>
        <p>在市集授权需要接收的账号，不会切换本机当前账号。</p>
        {!binding && <button className="btn btn-pri" onClick={() => void startBind()}>生成授权链接</button>}
        {binding && <div className="market-bind-link">
          <p>在浏览器中打开并完成登录/注册：</p>
          <a href={binding.url} target="_blank" rel="noreferrer">打开市集 <ExternalLink size={13} /></a>
          <p role="status">{bindingState === "done" ? "绑定成功。" : "等待网页确认…"}</p>
          {bindingState === "waiting" && <button className="btn btn-sm" onClick={() => void startBind()}>重新生成</button>}
        </div>}
      </section>}

      {bound === true && <>
        <section className="market-bind">
          <label className="checkline">
            <input type="checkbox" checked={receiving} disabled={busy} onChange={async event => {
              const enabled = event.target.checked;
              setBusy(true); setError("");
              try { await characterApi.marketSetReception(enabled); setReceiving(enabled); }
              catch (failure) {
                setError(errText(failure));
                try {
                  const latest: any = await characterApi.marketReception();
                  setReceiving(Boolean(latest.enabled));
                } catch { /* The visible failure remains until the next status read. */ }
              }
              finally { setBusy(false); }
            }} />
            后台接收当前账号的市集卡片
          </label>
          <button className="linkbtn" type="button" disabled={busy} onClick={async () => {
            if (!window.confirm("撤销当前账号的市集接收授权？已导入的人物会保留。")) return;
            setBusy(true); setError("");
            try { await characterApi.marketRevokeReception(); setBound(false); setBinding(null); setReceiving(false); }
            catch (failure) { setReceiving(false); setError(errText(failure)); }
            finally { setBusy(false); }
          }}>撤销当前账号授权</button>
        </section>
        {receiveOnly ? <p>当前为接收授权。上传卡片请先<a href="#/login">登录同一市集账号</a>。</p> : <>
        <section className="market-pick">
          <div className="market-pick-head">
            <h3>选择要上传的卡片</h3>
            <div className="row">
              <button className="btn btn-sm" onClick={() => setSelected(cards.map(card => card.card_id))}>全选</button>
              <button className="btn btn-sm" onClick={() => setSelected([])}>清空</button>
            </div>
          </div>
          {cards.length === 0 && <p>还没有可上传的人物卡。</p>}
          <div className="market-grid">
            {cards.map(card => {
              const cover = card.assets.find(asset => asset.kind === "image");
              const active = selected.includes(card.card_id);
              return <button key={card.card_id} type="button" className={"market-tile" + (active ? " on" : "")}
                aria-pressed={active} onClick={() => toggle(card.card_id)} disabled={busy}>
                <span className="market-tile-img">{cover ? <img src={characterApi.assetUrl(cover.url)} alt="" /> : <span>{card.profile.name.slice(0, 1)}</span>}</span>
                <span className="market-tile-meta"><b>{card.profile.name}</b><small>{card.profile.identity}</small></span>
                <span className="market-tile-check" aria-hidden="true">{active ? <Check size={14} /> : null}</span>
              </button>;
            })}
          </div>
        </section>

        {results.length > 0 && <ul className="market-results">
          {results.map(item => {
            const card = cards.find(value => value.card_id === item.card_id);
            return <li key={item.card_id} className={item.status}>
              <span>{card?.profile.name || item.card_id}</span>
              <b>{statusText[item.status] || item.status}</b>
              {item.message && <small>{item.message}</small>}
            </li>;
          })}
        </ul>}

        <footer className="market-actions">
          <span>{selected.length} 张已选择</span>
          <div className="row">
            <button className="btn" onClick={onClose} disabled={busy}>取消</button>
            <button className="btn btn-pri" onClick={() => void upload()} disabled={busy || !selected.length}>
              {busy ? <><Loader2 size={15} className="spin" /> 正在上传…</> : <><UploadCloud size={15} /> 上传 {selected.length} 张</>}
            </button>
          </div>
        </footer>
      </>}</>}
    </div>
  </div>;
}
