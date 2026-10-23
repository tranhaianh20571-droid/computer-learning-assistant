import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { parseSseFrames } from "./lib/sse";
import SourceComposer from "./ui/SourceComposer";
import {useSourceImports} from "./hooks/useSourceImports";

/**
 * 即时对话（设计稿 9 节 chat 布局）：
 * - turn me/ai 消息流；新追问 generation 加一；旧回答留历史不回填；
 * - 刷新只恢复已保存消息，不自动重发；
 * - 搜索由后台按个人设置自动执行并降级；模型不可用等失败按错误码标注轮次；
 * - 外部结果仅计数标记；
 * - 边界：即时路径不调用板书、TTS 或播放 API。
 */

type Msg = { message_id: string; generation: number; role: string; content: string; status: string };

export default function ChatView({ onErr, onOk, needLogin, ownerId="" }: {
  onErr: (s: string) => void; onOk: (s: string) => void; needLogin: boolean;
  ownerId?:string;
}) {
  const [convId, setConvId] = useState<string>("");
  const [messages, setMessages] = useState<Msg[]>([]);
  const [generation, setGeneration] = useState(0);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [composerKey,setComposerKey]=useState(0);
  const sources=useSourceImports(ownerId,onErr);
  const [lastExternal, setLastExternal] = useState<number | null>(null);
  const [liveText, setLiveText] = useState<string | null>(null);
  // 本会话内各轮失败原因；刷新后只读取已保存消息，不推断搜索故障。
  const [turnNotes, setTurnNotes] = useState<Record<number, string>>({});
  const bootRef = useRef(false);
  const streamRef = useRef<HTMLDivElement | null>(null);

  const refresh = useCallback(async (id: string) => {
    try {
      const got = await api.getChat(id);
      setMessages(got.messages);
      setGeneration(got.generation);
      return got;
    } catch { /* 会话不可用保持本地 */ return null; }
  }, []);

  const ensureConversation = useCallback(async () => {
    if (convId) return convId;
    if (needLogin) {
      onErr("请先登录后使用即时对话。");
      return "";
    }
    try {
      const conv = await api.createChat({ material_ids: [], scenario: "self_study" });
      setConvId(conv.conversation_id);
      setTurnNotes({});
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

  useEffect(() => {
    const sm = streamRef.current;
    if (sm) sm.scrollTop = sm.scrollHeight;
  }, [messages.length]);

  const send = useCallback(async () => {
    const id = await ensureConversation();
    if (!id || (!input.trim()&&!sources.hasSources)) return;
    const question = input.trim()||"请帮助我理解所附资料";
    setBusy(true);
    setLastExternal(null);
    let typewriterTimer: number | undefined;
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    try {
      const materialIds=await sources.ingest();
      const res = await api.streamChat(id, {
        content: question, idempotency_key: `web-${Date.now()}`,
        ...(materialIds.length?{material_ids:materialIds}:{}),
      });
      if (!res.ok || !res.body) {
        let code = "ERROR";
        try { code = (await res.json())?.error_code || code; } catch { /* keep */ }
        if (code === "AWAITING_SEARCH_DECISION" || code.startsWith("SEARCH_")) {
          const got = await refresh(id);
          if (got) setTurnNotes(p => ({ ...p, [got.generation]: "ANSWER_INTERRUPTED" }));
          return;
        }
        throw new Error(code);
      }
      setInput("");
      sources.clear();setComposerKey(value=>value+1);
      setLiveText("");
      // 打字机平滑渲染：网络增量先入缓冲，按固定速率上屏（积压越多追赶越快）。
      // 网关批量倒字时依旧呈现逐字效果；真流式供应商下自动表现为原生打字机。
      let buffer = "";
      let revealed = 0;
      let streamDone = false;
      const drained = new Promise<void>((resolve) => {
        const iv = window.setInterval(() => {
          if (revealed >= buffer.length) {
            if (streamDone) {
              window.clearInterval(iv);
              resolve();
            }
            return; // 等 Typewriter 追上网络流
          }
          const take = Math.max(1, Math.ceil((buffer.length - revealed) / 40));
          revealed = Math.min(buffer.length, revealed + take);
          setLiveText(buffer.slice(0, revealed));
        }, 50);
        typewriterTimer = iv;
      });
      reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      let finished = false;
      while (!finished) {
        const { value, done: rdDone } = await reader.read();
        if (rdDone) break;
        buf += dec.decode(value, { stream: true });
        const parsed = parseSseFrames(buf);
        buf = parsed.rest;
        for (const frame of parsed.frames) {
          const data = frame.data || {};
          if (frame.event === "delta") {
            buffer += String(data.text || ""); // 入缓冲，由 Typewriter 上屏
          } else if (frame.event === "done") {
            finished = true;
            streamDone = true;
            if (typeof data.external_result_count === "number" && data.external_result_count > 0) {
              setLastExternal(data.external_result_count);
            }
          } else if (frame.event === "wait") {
            streamDone = true;
            const got = await refresh(id);
            if (got) setTurnNotes((p) => ({ ...p, [got.generation]: "ANSWER_INTERRUPTED" }));
            return;
          } else if (frame.event === "error") {
            streamDone = true;
            const code = String(data.error_code || "ERROR");
            const got = await refresh(id);
            if (got) setTurnNotes((p) => ({ ...p, [got.generation]: code.startsWith("SEARCH_") ? "ANSWER_INTERRUPTED" : code }));
            if (code.startsWith("SEARCH_")) return;
            if (code === "MODEL_CAPABILITY_UNAVAILABLE") {
              onErr("未配置可用模型：请到设置页保存内容模型配置。");
            } else {
              onErr(`回答失败：${code}`);
            }
            return;
          }
        }
      }
      streamDone = true;
      await drained; // 等 Typewriter 把缓冲播完
      await refresh(id);
      onOk("已回答。");
    } catch (e: any) {
      if(e?.code==="SOURCE_IMPORT_CANCELLED")return;
      onErr(e?.code === "MODEL_CAPABILITY_UNAVAILABLE"
        ? "该模型不支持所需能力，已停止调用。"
        : e?.message || "发送失败");
    } finally {
      window.clearInterval(typewriterTimer);
      if (reader) { await reader.cancel().catch(() => {}); reader.releaseLock(); }
      setLiveText(null);
      setBusy(false);
    }
  }, [ensureConversation, input, onErr, onOk, refresh, sources]);

  // 按 generation 分组成轮次（user + assistant）
  const turns: { gen: number; q?: Msg; a?: Msg }[] = [];
  for (const m of messages) {
    let turn = turns.find((t) => t.gen === m.generation);
    if (!turn) {
      turn = { gen: m.generation };
      turns.push(turn);
    }
    if (m.role === "user") turn.q = m;
    else turn.a = m;
  }

  return (
    <div className="chat">
      <div className="chat-head">
        <h1>即时对话</h1>
        <span className="help">
          {(convId ? "会话进行中" : "尚无会话") + " · 不需要路线或课程 · 只有文字，没有板书和音频"}
        </span>
        <span className="sp" />
        <button
          className="btn btn-sm btn-quiet"
          onClick={async () => {
            const id = await ensureConversation();
            if (id) { await refresh(id); onOk("已就绪，继续提问即可。"); }
          }}
        >
          ＋新对话
        </button>
      </div>

      <div className="chat-stream" ref={streamRef}>
        <div className="chat-col" aria-live="polite">
          {turns.map((t) => (
            <div key={t.gen}>
              {t.q && (
                <div className="turn me">
                  <span className="who">我</span>
                  <div className="body">{t.q.content}</div>
                </div>
              )}
              {t.a && (
                <div className="turn ai">
                  <span className="who">学伴</span>
                  <div className="body"><p>{t.a.content}</p></div>
                </div>
              )}
              {!t.a && !(busy && t.gen === generation) && (
                <div className="turn ai">
                  <span className="who">学伴</span>
                  <div className="body">
                    {turnNotes[t.gen] === "MODEL_CAPABILITY_UNAVAILABLE" ? (
                      <div className="note note-warn" role="alert">
                        <span className="tag">未回答</span>
                        <div className="tt">未配置可用模型</div>
                        <div>当前账号没有已保存的内容模型配置。到设置页 → 个人内容模型保存后，重新提问即可。</div>
                      </div>
                    ) : turnNotes[t.gen] && turnNotes[t.gen] !== "ANSWER_INTERRUPTED" ? (
                      <div className="note note-warn" role="alert">
                        <span className="tag">未回答</span>
                        <div className="tt">回答失败：{turnNotes[t.gen]}</div>
                        <div>可直接重新提问（会开启新一轮）。</div>
                      </div>
                    ) : (
                      <div className="note note-info" role="status">
                        <span className="tag">未完成</span>
                        <div className="tt">这一轮回答尚未完成</div>
                        <div>可以继续提问。</div>
                      </div>
                    )}
                  </div>
                </div>
              )}
              {t.gen === generation && <div style={{ borderTop: "1px solid var(--rule)" }} />}
            </div>
          ))}
          {turns.length > 0 && lastExternal !== null && lastExternal > 0 && (
            <div className="note note-info" role="status">
              <span className="tag">外部来源</span>
              <div className="tt">本次回答参考了 <b className="num">{lastExternal}</b> 条外部搜索结果</div>
              <div>来源与你的资料内容分开标记（source=external）。</div>
            </div>
          )}
          {liveText !== null && (
            <div className="turn ai">
              <span className="who">学伴</span>
              <div className="body">
                {liveText === "" ? (
                  <p className="muted">
                    <span className="spin" style={{ marginRight: 8 }} />正在生成回答…
                    <span className="sr">回答生成中</span>
                  </p>
                ) : (
                  <p>{liveText}
                    <span className="live-cursor" aria-hidden="true">▍</span>
                    <span className="sr">回答生成中</span>
                  </p>
                )}
              </div>
            </div>
          )}
          {turns.length === 0 && liveText === null && <p className="muted">暂无消息。提问后回答将在此显示；被打断的旧回答会保留。</p>}
        </div>
      </div>

      <div className="chat-foot">
        <div className="inner">
          <div className="composer2">
            <div className="box">
              <label className="sr" htmlFor="ch-q">继续提问</label>
              <div style={{minWidth:0,flex:1}}>
                <SourceComposer key={composerKey} label="继续提问" placeholder="继续提问" disabled={busy}
                  onChange={(text,links)=>{setInput(text);sources.setLinks(links);}} onFiles={sources.addFiles} onSubmit={()=>void send()}/>
                {sources.tray}{sources.pickerControl}
              </div>
              <button className="btn btn-pri" onClick={send} disabled={busy || (!input.trim()&&!sources.hasSources)}>发送</button>
            </div>
            <div className="hint">
              <span><kbd>Ctrl</kbd>+<kbd>Enter</kbd> 发送</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
