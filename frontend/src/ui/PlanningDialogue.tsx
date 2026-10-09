import { useEffect, useRef, useState } from "react";
import { api } from "../api";

export default function PlanningDialogue({ draft, selectedNode, onAdopt, onPreview, onBusy, onErr, onClose }: {
  draft: any; selectedNode: string; onAdopt: (draft: any) => Promise<void>;
  onPreview: (turn: any | null) => void; onBusy: (value: boolean) => void; onErr: (message: string) => void; onClose: () => void;
}) {
  const [turns, setTurns] = useState<any[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const lastPreview = useRef("");
  const scroll = useRef<HTMLDivElement>(null);
  const pending = turns.some(t => ["queued", "leased", "running", "expired"].includes(t.status));
  useEffect(() => { onBusy(sending || pending); return () => onBusy(false); }, [sending, pending, onBusy]);
  useEffect(() => {
    let cancelled = false, timer: number;
    const load = async () => {
      try {
        const response = await api.planningDialogue(draft.draft_id);
        if (cancelled) return;
        setTurns(response.turns || []); setLoading(false); setError("");
        const last = response.turns?.at(-1);
        if (last?.scene && last.decision === "pending" && lastPreview.current !== last.task_id) {
          lastPreview.current = last.task_id; onPreview(last);
        }
      } catch { if (!cancelled) { setLoading(false); setError("对话加载失败，稍后会重试。"); } }
      if (!cancelled) timer = window.setTimeout(load, 1500);
    };
    load();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [draft.draft_id, onPreview]);
  useEffect(() => { scroll.current?.scrollTo({ top: scroll.current.scrollHeight, behavior: "smooth" }); }, [turns.length, pending]);
  const send = async (text = input) => {
    if (!text.trim() || sending || pending) return;
    setSending(true); setError("");
    try {
      await api.sendPlanningDialogue(draft.draft_id, { revision: draft.revision, content: text,
        selected_node: selectedNode, idempotency_key: crypto.randomUUID() });
      setInput(""); onPreview(null);
      setTurns((await api.planningDialogue(draft.draft_id)).turns || []);
    } catch (e: any) { setError(e.code === "DRAFT_REVISION_MISMATCH" ? "草稿已更新，请刷新后继续。" : e.message || "发送失败，输入已保留。"); }
    finally { setSending(false); }
  };
  const decide = async (turn: any, adopt: boolean) => {
    setSending(true);
    try {
      if (adopt) { const result = await api.adoptPlanningChange(turn.task_id, draft.revision); await onAdopt(result.draft); }
      else await api.discardPlanningChange(turn.task_id, draft.revision);
      onPreview(null); setTurns((await api.planningDialogue(draft.draft_id)).turns || []);
    } catch (e: any) { onErr(e.code === "DRAFT_REVISION_MISMATCH" ? "候选基于旧草稿，请结合当前内容重新提出修改。" : e.message || "采用失败"); }
    finally { setSending(false); }
  };
  return <div className="planning-dialogue">
    <div className="row" style={{ justifyContent: "space-between" }}><span className="kicker">与学伴调整规划</span><button className="linkbtn" onClick={onClose}>关闭问答</button></div>
    <div className="planning-dialogue-log" ref={scroll} aria-live="polite">
      {loading ? <p className="help">正在恢复对话…</p> : turns.length === 0 && <p className="help">说说你希望怎么调整。学伴会结合当前导图修改，必要时向你追问。</p>}
      {turns.map(t => <div className="dialogue-turn" key={t.task_id}>
        <p className="dialogue-user">{t.content}</p>
        {t.result?.answer && <p className="dialogue-assistant">{t.result.answer}</p>}
        {t.result?.summary && <p className="help">{t.result.summary}</p>}
        {t.current && t.result?.options?.map((option: string) => <button className="btn btn-sm" key={option} disabled={sending || pending} onClick={() => send(option)}>{option}</button>)}
        {t.result?.changes?.length > 0 && <details><summary>查看修改（{t.result.changes.length} 项）</summary><ul>{t.result.changes.map((c: any) => <li key={`${c.node_ref}:${c.action}`}>{({ added: "新增", removed: "删除", updated: "修改", moved: "移动", prerequisite: "先修关系调整" } as any)[c.action]}：{c.title}</li>)}</ul></details>}
        {t.result?.candidate && t.decision === "pending" && <div className="dialogue-candidate-actions">
          <button className="btn btn-sm" onClick={() => onPreview(t)}>预览修改</button>
          <button className="btn btn-pri btn-sm" disabled={sending || pending || t.stale} onClick={() => decide(t, true)}>采用修改</button>
          <button className="btn btn-sm" disabled={sending} onClick={() => decide(t, false)}>保留原稿</button>
          {t.stale && <span className="help">候选已过期，可继续提出修改。</span>}
        </div>}
        {t.decision === "adopted" && <p className="help">已采用到草稿，仍需最终确认。</p>}
        {["queued", "leased", "running", "expired"].includes(t.status) && <p role="status" className="help">学伴正在理解并调整…可离开后回来查看。</p>}
        {t.status === "failed" && <div className="help" role="alert">这轮调整未完成。<button className="linkbtn" disabled={sending} onClick={async () => { try { await api.retryPlanningDialogue(t.task_id); } catch (e: any) { setError(e.message); } }}>重试</button></div>}
      </div>)}
    </div>
    <form onSubmit={e => { e.preventDefault(); send(); }} className="planning-dialogue-compose">
      {selectedNode && <span className="help">已选中导图节点，本轮优先调整该部分。</span>}
      <textarea className="inp" rows={3} maxLength={4000} value={input} onChange={e => setInput(e.target.value)} placeholder="例如：压缩基础课，把知识点穿插进项目…" aria-label="规划修改要求" />
      <button className="btn btn-pri" type="submit" disabled={!input.trim() || sending || pending}>发送修改要求</button>
      {error && <p role="alert" className="help">{error}</p>}
    </form>
  </div>;
}
