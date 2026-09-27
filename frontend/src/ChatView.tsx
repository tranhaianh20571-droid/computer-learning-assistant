import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

/**
 * 切片 3 T08：即时文字协助。
 * - 新追问使 generation 加一；旧回答留历史不回填；刷新只恢复已保存消息，不自动重发；
 * - 可选联网搜索：真实尝试；失败进入等待决策（重试 / 明确离线继续）；
 * - 外部搜索结果仅以「外部来源 N 条」计数呈现（source=external），不与资料内容混淆；
 * - 边界：即时路径不调用板书、TTS 或播放 API。
 */

type Msg = { message_id: string; generation: number; role: string; content: string; status: string };

export default function ChatView({ onErr, onOk, needLogin }: { onErr: (s: string) => void; onOk: (s: string) => void; needLogin: boolean }) {
  const [convId, setConvId] = useState<string>("");
  const [messages, setMessages] = useState<Msg[]>([]);
  const [generation, setGeneration] = useState(0);
  const [waitState, setWaitState] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [useSearch, setUseSearch] = useState(false);
  const [busy, setBusy] = useState(false);
  const bootRef = useRef(false);

  const refresh = useCallback(async (id: string) => {
    try {
      const got = await api.getChat(id);
      setMessages(got.messages);
      setGeneration(got.generation);
      setWaitState(got.wait_state);
    } catch { /* 会话不可用保持本地 */ }
  }, []);

  const ensureConversation = useCallback(async () => {
    if (convId) return convId;
    if (needLogin) {
      onErr("请先登录后使用即时协助。");
      return "";
    }
    try {
      const conv = await api.createChat({ material_ids: [], scenario: "self_study" });
      setConvId(conv.conversation_id);
      return conv.conversation_id;
    } catch (e: any) {
      onErr(e?.message || "会话创建失败");
      return "";
    }
  }, [convId, needLogin, onErr]);

  useEffect(() => {
    // 刷新恢复：只读取已保存消息，不重发任何内容
    if (bootRef.current) return;
    bootRef.current = true;
    const saved = sessionStorage.getItem("slice3.convId");
    if (saved) {
      setConvId(saved);
      refresh(saved);
    }
  }, [refresh]);

  useEffect(() => {
    if (convId) sessionStorage.setItem("slice3.convId", convId);
  }, [convId]);

  const send = useCallback(async () => {
    const id = await ensureConversation();
    if (!id || !input.trim()) return;
    setBusy(true);
    try {
      const r = await api.sendChat(id, {
        content: input, use_search: useSearch, idempotency_key: `web-${Date.now()}`,
      });
      setInput("");
      await refresh(id);
      if (r.error_code === "MODEL_CAPABILITY_UNAVAILABLE") {
        onErr("该模型不支持所需能力，已停止调用（不无限重试）。");
      } else {
        onOk("已回答。");
      }
    } catch (e: any) {
      if (e?.code === "AWAITING_SEARCH_DECISION") {
        await refresh(id);
        onErr("联网搜索失败：请选择重试或离线继续。");
      } else {
        onErr(e?.message || "发送失败");
      }
    } finally {
      setBusy(false);
    }
  }, [ensureConversation, input, onErr, onOk, refresh, useSearch]);

  const resume = useCallback(async (decision: string) => {
    if (!convId) return;
    setBusy(true);
    try {
      await api.resumeChat(convId, decision);
      await refresh(convId);
      onOk(decision === "retry" ? "搜索已重试并完成回答。" : "已离线继续并完成回答。");
    } catch (e: any) {
      onErr(e?.message || "恢复失败");
    } finally {
      setBusy(false);
    }
  }, [convId, onErr, onOk, refresh]);

  return (
    <div className="card stack">
      <h1>即时协助</h1>
      <p className="lede">
        纯文字问答（无音频 / 板书 / 播放）。追问会使当前轮次加一，旧回答保留为历史；
        刷新页面只恢复已保存内容。
      </p>
      <div className="row mono" aria-live="polite">
        <span>conversation={convId || "-"}</span>
        <span>generation={generation}</span>
        {useSearch && <span className="badge running">已启用联网搜索</span>}
      </div>
      <div className="stack" style={{ maxHeight: 380, overflowY: "auto" }} role="log" aria-label="对话历史">
        {messages.map((m) => (
          <div key={m.message_id} className={m.role === "user" ? "row" : "row"}>
            <span className={`badge ${m.role === "user" ? "queued" : "succeeded"}`}>
              {m.role === "user" ? "我" : "助手"}
            </span>
            <span>{m.content}</span>
          </div>
        ))}
        {messages.length === 0 && <p className="lede">暂无消息。提问后回答将在此显示。</p>}
      </div>
      {waitState === "awaiting_search_decision" && (
        <div className="notice" role="status">
          联网搜索失败：重试，或明确选择离线继续（不会伪造联网依据）。
          <div className="row">
            <button onClick={() => resume("retry")} disabled={busy}>重试搜索</button>
            <button className="secondary" onClick={() => resume("offline_continue")} disabled={busy}>离线继续</button>
          </div>
        </div>
      )}
      <div className="field">
        <label htmlFor="chat-input">你的问题</label>
        <textarea
          id="chat-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          rows={3}
          maxLength={4000}
          style={{ width: "100%" }}
        />
      </div>
      <div className="row">
        <button onClick={send} disabled={busy || !input.trim()}>发送</button>
        <label>
          <input type="checkbox" checked={useSearch} onChange={(e) => setUseSearch(e.target.checked)} />
          允许联网搜索（外部结果会单独标记）
        </label>
      </div>
    </div>
  );
}
