import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

/**
 * 首页 · 选择学习任务（设计稿 homeBody）：
 * - 一句 ask + 任务类型/场景/范围 seg + 大输入框 composer + 示例 hint-row；
 * - 资料以 chip 挂载（状态字形 .st-mat），规划与对话共用同一入口；
 * - 规划提交 → 创建路线壳 + 规划任务 → 跳转课程规划页自动执行；
 * - 对话提交 → 建会话并发送第一条消息 → 跳转即时对话页。
 */

const EXAMPLES_PLAN = [
  "我想系统补齐做后端开发需要的基础课",
  "学校《操作系统》第 4 章：进程调度",
  "学 Python",
];
const EXAMPLES_CHAT = [
  "TCP 的 TIME_WAIT 为什么要等 2MSL？",
  "Python 的 GIL 到底限制了什么？",
];

type Mat = { material_id: string; status: string };

function MatGlyph({ status }: { status: string }) {
  const cls = status === "parsed" ? "is-ok" : status === "partial" ? "is-partial" : status === "failed" ? "is-fail" : "";
  const text = status === "parsed" ? "已解析" : status === "partial" ? "部分可用" : status === "failed" ? "解析失败" : status === "parsing" ? "解析中" : "已上传";
  return (
    <span className={`st st-mat ${cls}`}>
      <i className="g" aria-hidden="true" />
      <span className="k">资料</span>
      {text}
    </span>
  );
}

export default function TaskEntry({ me, onErr, onOk, go }: {
  me: any; onErr: (s: string) => void; onOk: (s: string) => void; go: (p: string) => void;
}) {
  const [type, setType] = useState<"plan" | "chat">("plan");
  const [scene, setScene] = useState<"self_study" | "school">("self_study");
  const [scope, setScope] = useState("auto");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [mats, setMats] = useState<Mat[]>([]);
  const [picked, setPicked] = useState<string[]>([]);

  useEffect(() => {
    if (!me) return;
    (async () => {
      try {
        const r = await api.listMaterials();
        setMats((r.data || []).filter((m: Mat) => m.status !== "failed"));
      } catch { /* 资料列表失败不阻塞入口 */ }
    })();
  }, [me]);

  const toggleMat = useCallback((id: string) => {
    setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));
  }, []);

  const submit = useCallback(async () => {
    if (!me) return onErr("请先登录后再开始。");
    const goal = text.trim();
    if (!goal || busy) return;
    setBusy(true);
    try {
      if (type === "plan") {
        const title = goal.split(/[。.，,\n]/)[0].slice(0, 40) || "学习路线";
        const route = await api.createRoute(title, scene === "school" ? "school" : "self_study");
        const task = await api.createPlanningTask({
          idempotency_key: `web-${Date.now()}`,
          task_kind: "route_generate",
          planning_scope: scope,
          target_route_id: route.route_id,
          material_ids: picked,
        });
        sessionStorage.setItem("slice3.pendingTask", task.task_id);
        sessionStorage.setItem("slice3.pendingRoute", route.route_id);
        onOk("任务已提交，正在生成候选…");
        go("/planning");
      } else {
        const conv = await api.createChat({ material_ids: picked, scenario: scene });
        await api.sendChat(conv.conversation_id, {
          content: goal, use_search: false, idempotency_key: `web-${Date.now()}`,
        });
        sessionStorage.setItem("slice3.convId", conv.conversation_id);
        onOk("已发送，进入即时对话。");
        go("/chat");
      }
    } catch (e: any) {
      onErr(e?.code ? `${e.code}` : "提交失败");
    } finally {
      setBusy(false);
    }
  }, [busy, go, me, onErr, onOk, picked, scene, scope, text, type]);

  return (
    <div className="home">
      <section aria-labelledby="ask">
        <h1 className="ask" id="ask">{type === "plan" ? "你今天想弄懂什么？" : "你想问什么？"}</h1>
        <p className="muted">
          {type === "plan"
            ? "把目标、问题或资料交给学伴，它会搜索、整理，再生成一张可以继续探索的学习图。"
            : "直接提问，学伴会结合你的资料和上下文一步步回答。只有文字，没有板书和音频。"}
        </p>
        <div className="sentence">
          <span className="w">我要</span>
          <span className="seg" role="radiogroup" aria-label="任务类型">
            <button role="radio" aria-checked={type === "plan"} onClick={() => setType("plan")}>规划学习</button>
            <button role="radio" aria-checked={type === "chat"} onClick={() => setType("chat")}>即时对话</button>
          </span>
          <span className="w">，场景是</span>
          <span className="seg" role="radiogroup" aria-label="学习场景">
            <button role="radio" aria-checked={scene === "self_study"} onClick={() => setScene("self_study")}>自学</button>
            <button role="radio" aria-checked={scene === "school"} onClick={() => setScene("school")}>学校课程</button>
          </span>
          {type === "plan" && (
            <>
              <span className="w">，范围</span>
              <span className="seg" role="radiogroup" aria-label="规划范围">
                <button role="radio" aria-checked={scope === "auto"} onClick={() => setScope("auto")}>自动判断</button>
                <button role="radio" aria-checked={scope === "course"} onClick={() => setScope("course")}>单门课程</button>
                <button role="radio" aria-checked={scope === "route"} onClick={() => setScope("route")}>多课程路线</button>
              </span>
            </>
          )}
        </div>

        {!me && (
          <div className="note note-info" role="note" style={{ marginBottom: 16 }}>
            <span className="tag">登录</span>
            <div className="tt">先登录再开始</div>
            <div>规划和对话都需要账户；提交前请先在侧栏登录。</div>
          </div>
        )}

        <div className="composer">
          <label className="sr" htmlFor="goal">{type === "plan" ? "学习目标" : "你的问题"}</label>
          <textarea
            className="ta"
            id="goal"
            placeholder={type === "plan" ? "例如：我想系统补齐做后端开发需要的基础课" : "例如：TCP 的 TIME_WAIT 为什么要等 2MSL？"}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <div className="cbar">
            {picked.map((id) => {
              const m = mats.find((x) => x.material_id === id);
              return (
                <span className="chip" key={id}>
                  <MatGlyph status={m?.status || "uploaded"} />
                  <span className="od-truncate" style={{ maxWidth: 160 }}>{id}</span>
                  <button type="button" onClick={() => toggleMat(id)} aria-label={`移除资料 ${id}`}>×</button>
                </span>
              );
            })}
            {mats.length > 0 && (
              <span className="seg" role="group" aria-label="附加资料">
                {mats.slice(0, 4).map((m) => (
                  <button
                    key={m.material_id}
                    aria-pressed={picked.includes(m.material_id)}
                    onClick={() => toggleMat(m.material_id)}
                    title={`附加资料 ${m.material_id}`}
                  >
                    {picked.includes(m.material_id) ? "✓ " : "+ "}资料 {m.material_id.slice(0, 8)}
                  </button>
                ))}
              </span>
            )}
            <span className="grow" />
            <button type="button" className="btn btn-pri submit" disabled={busy || !text.trim() || !me} onClick={submit}>
              {busy ? <><span className="spin" />正在提交…</> : type === "plan" ? "生成规划草稿" : "开始对话"}
            </button>
          </div>
        </div>

        {!text.trim() && (
          <div className="hint-row" aria-label="示例">
            {(type === "plan" ? EXAMPLES_PLAN : EXAMPLES_CHAT).map((s) => (
              <button key={s} type="button" onClick={() => setText(s)}>{s}</button>
            ))}
          </div>
        )}

        {type === "plan" && scope === "auto" && (
          <div className="suggest" aria-live="polite">
            <span className="lab">范围</span>
            <div className="v">
              保持<b>自动判断</b>：生成时会结合你的目标决定单门课程还是多课程路线；存在会改变结果的歧义时，会只追问一句，由你决定。
              <span className="help" style={{ display: "block", marginTop: 4 }}>
                判断只看目标本身，不看主题名、附件数量或场景。
              </span>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
