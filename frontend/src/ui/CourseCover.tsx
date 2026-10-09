import { useEffect, useRef, useState } from "react";
import { ImagePlus, RefreshCw } from "lucide-react";
import { api } from "../api";
import { errText } from "../lib/errors";

export default function CourseCover({ kind, id, title, onOpen }: {
  kind: string; id: string; title: string; onOpen?: () => void;
}) {
  const [cover, setCover] = useState<any>(null);
  const [src, setSrc] = useState("");
  const [failed, setFailed] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);
  const objectUrl = useRef("");
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    setCover(null); setSrc(""); setFailed(false); setError("");
    if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
    objectUrl.current = "";
    const refresh = async () => {
      try {
        const result = await api.getCover(kind, id);
        if (cancelled) return;
        setCover(result);
        if (["queued", "leased", "running"].includes(result.status)) timer = window.setTimeout(refresh, 1500);
      } catch { if (!cancelled) { setFailed(true); setError("封面状态加载失败，可重试。"); } }
    };
    refresh();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [kind, id, refreshKey]);
  useEffect(() => {
    let cancelled = false;
    if (cover?.cover_eligible !== false && cover?.status !== "suppressed" && cover?.image_url) api.coverImage(cover.image_url).then(url => {
      if (cancelled) { URL.revokeObjectURL(url); return; }
      const previous = objectUrl.current;
      objectUrl.current = url; setSrc(url); setFailed(false);
      if (previous) URL.revokeObjectURL(previous);
    }).catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; };
  }, [cover?.image_url]);
  useEffect(() => () => { if (objectUrl.current) URL.revokeObjectURL(objectUrl.current); }, []);
  const generate = async () => {
    if (generating) return;
    setGenerating(true); setError("");
    try { const result = await api.authorizeCover(kind, id, cover?.status === "succeeded" || terminal || failed); if (result) setRefreshKey(value => value + 1); }
    catch (failure) { setError(errText(failure)); }
    finally { setGenerating(false); }
  };
  const terminal = ["failed", "expired", "cancelled", "awaiting_confirmation"].includes(cover?.status);
  const fallback = failed || terminal || cover?.method === "procedural" || ["not_generated", "unavailable", "deferred"].includes(cover?.status);
  const procedural = cover?.actual_method === "procedural" || cover?.method === "procedural";
  if (cover?.cover_eligible === false || cover?.status === "suppressed") return null;
  const missing = cover?.status === "not_generated";
  const art = src ? <img src={src} alt={`${title}的水墨封面`} onError={() => {
    if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
    objectUrl.current = ""; setSrc(""); setFailed(true);
  }} /> : fallback ?
    <svg viewBox="0 0 960 720" aria-hidden="true"><rect width="960" height="720" fill="#f5f2e9" />
      <path d="M90 475 Q210 220 335 380 T630 290 T850 435 L850 590 L90 590Z" fill="#7c8580" opacity=".13" />
      <path d="M95 500 Q250 315 385 438 T850 440" fill="none" stroke="#465550" strokeWidth="3" opacity=".35" />
      <path d="M175 230 L183 550 M225 223 L230 545 M275 218 L283 536 M155 280 Q280 270 365 277 M158 332 Q280 324 377 330 M162 385 Q280 379 386 382" fill="none" stroke="#59645b" strokeWidth="2" opacity=".2" />
    </svg> : <div className="ink-cover-pending" role="status">
      <ImagePlus size={28} aria-hidden="true" /><span>{missing ? "封面尚未生成" : "封面生成中"}</span>
    </div>;
  return <div className="ink-cover-block" data-cover-state={src ? "ready" : fallback ? "fallback" : missing ? "not-generated" : "pending"}>
    {onOpen ? <button className="ink-cover-art" onClick={onOpen} aria-label={`打开课程 ${title}`}>{art}</button> : <div className="ink-cover-art">{art}</div>}
    {(missing || terminal || failed || procedural) && <div className="cover-actions">
      <button type="button" className="btn btn-sm" disabled={generating} onClick={generate} title="选择图片模型并确认外发范围"><ImagePlus size={16} />{generating ? "正在确认…" : terminal ? "重新确认封面" : "生成封面"}</button>
      {failed && <button type="button" className="btn btn-sm" title="重新加载封面" aria-label="重新加载封面" onClick={() => setRefreshKey(value => value + 1)}><RefreshCw size={16} /></button>}
    </div>}
    {terminal && cover?.error_code && <p className="help" role="status">{errText({ code: cover.error_code })}</p>}
    {error && <p className="help" role="alert">{error}</p>}
  </div>;
}
