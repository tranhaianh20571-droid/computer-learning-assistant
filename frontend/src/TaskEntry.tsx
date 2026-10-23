import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import Dialog from "./ui/Dialog";
import { planningMatchLink } from "./lib/learningLinks";
import SourceComposer from "./ui/SourceComposer";
import {useSourceImports} from "./hooks/useSourceImports";

/**
 * 首页 · 选择学习任务（v2 对齐原型布局）：
 * - 单卡片输入区：类型切换在卡片上方，附件入口在卡片左下，生成按钮右下；
 * - 场景与规划范围不放在首页（默认 self_study / auto），歧义由服务端
 *   AWAITING_SCOPE_CLARIFICATION 澄清流程承接；
 * - 规划提交 → 创建路线壳 + 规划任务 → 跳转课程规划页自动执行；
 *   对话提交 → 建会话并发送第一条消息 → 跳转即时对话页。
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

export default function TaskEntry({ me, onErr, onOk, go }: {
  me: any; onErr: (s: string) => void; onOk: (s: string) => void; go: (p: string) => void;
}) {
  const [type, setType] = useState<"plan" | "chat">("plan");
  const [text, setText] = useState("");
  const [hintIndex, setHintIndex] = useState(0);
  const [busy, setBusy] = useState(false);
  const sources=useSourceImports(me?.user_id||"",onErr);
  const [pendingPlan, setPendingPlan] = useState<any>(null);
  useEffect(() => {
    if (text.trim() || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const count = type === "plan" ? EXAMPLES_PLAN.length : EXAMPLES_CHAT.length;
    const timer = window.setInterval(() => setHintIndex(index => (index + 1) % count), 5000);
    return () => window.clearInterval(timer);
  }, [text, type]);
  useEffect(() => {
    let alive = true;
    if (me) api.currentPlanning().then(result => {
      if (alive && result.planning && !result.planning.confirmed && !result.planning.abandoned) {
        setPendingPlan(result.planning);
      }
    }).catch(() => {});
    return () => { alive = false; };
  }, [me]);
  const [resume, setResume] = useState<{
    courseId: string; title: string; lesson: { id: string; title: string } | null; startable: boolean;
  } | null>(null);

  // 继续学习卡：读最近导航状态 + 课程总览（失败静默，不阻塞入口）
  useEffect(() => {
    if (!me) { setResume(null); return; }
    let alive = true;
    (async () => {
      try {
        const nav = await api.getNavigation();
        if (!alive || !nav.last_course_id) return;
        const ov = await api.courseOverview(nav.last_course_id);
        if (!alive) return;
        const lessons = (ov.units || []).flatMap((u: any) => u.lessons || []);
        const next = lessons.find((l: any) => l.learning_state !== "user_completed" && l.startable) || null;
        setResume({
          courseId: nav.last_course_id,
          title: ov.title || nav.last_course_id,
          lesson: next ? { id: next.lesson_id, title: next.title || "下一讲" } : null,
          startable: Boolean(next) && Boolean(ov.teaching_unlocked),
        });
      } catch { /* 导航状态不可用则不显示卡片 */ }
    })();
    return () => { alive = false; };
  }, [me]);

  const resumeGo = useCallback(() => {
    if (!resume) return;
    if (resume.lesson && resume.startable) {
      api.startLessonV2(resume.courseId, resume.lesson.id)
        .then((r: any) => {
          if (!r) return;
          if (r.conversation_id) {
            sessionStorage.setItem("slice4.pendingTask", r.task_id);
            go(`/lecture/${r.conversation_id}`);
          } else {
            go(`/course/${resume.courseId}`);
          }
        })
        .catch(() => go(`/course/${resume.courseId}`));
    } else {
      go(`/course/${resume.courseId}`);
    }
  }, [go, resume]);

  const [possibleDuplicate, setPossibleDuplicate] = useState<any[] | null>(null);
  const submit = useCallback(async (continuePlanning = false) => {
    if (!me) return onErr("请先登录后再开始。");
    const goal = text.trim() || (sources.hasSources ? type==="plan"?"依据所附资料规划学习":"请帮助我理解所附资料" : "");
    if (!goal || busy) return;
    setBusy(true);
    try {
      if (type === "plan") {
        if (!continuePlanning) {
          const check = await api.inputMatches(goal);
          if (check.matches.length) { setPossibleDuplicate(check.matches); return; }
        }
        setPossibleDuplicate(null);
        const picked=await sources.ingest();
        const title = goal.split(/[。.，,\n]/)[0].slice(0, 40) || "学习路线";
        const route = await api.createRoute(title, "self_study", goal);
        const task = await api.createPlanningTask({
          idempotency_key: `web-${Date.now()}`,
          task_kind: "route_generate",
          planning_scope: "auto",
          target_route_id: route.route_id,
          material_ids: picked,
        });
        sessionStorage.setItem("slice3.pendingTask", task.task_id);
        sessionStorage.setItem("slice3.pendingRoute", route.route_id);
        sessionStorage.removeItem(`task-attachments:${me.user_id}`);
        sources.clear();
        onOk("任务已提交，正在生成候选…");
        go("/planning");
      } else {
        const picked=await sources.ingest();
        const conv = await api.createChat({ material_ids: picked, scenario: "self_study" });
        await api.sendChat(conv.conversation_id, {
          content: goal, use_search: false, idempotency_key: `web-${Date.now()}`,
        });
        sessionStorage.setItem("slice3.convId", conv.conversation_id);
        sessionStorage.removeItem(`task-attachments:${me.user_id}`);
        sources.clear();
        onOk("已发送，进入即时对话。");
        go("/chat");
      }
    } catch (e: any) {
      if(e?.code!=="SOURCE_IMPORT_CANCELLED")
        onErr(e?.sourceImport ? e.message : e?.code ? `${e.code}` : "提交失败");
    } finally {
      setBusy(false);
    }
  }, [busy, go, me, onErr, onOk, text, type, sources]);

  return (
    <div className="home">
      {possibleDuplicate && <Dialog kind="info" title="可能已有相同规划" onClose={() => setPossibleDuplicate(null)} actions={<>
        <button className="btn btn-sm" disabled={busy} onClick={() => setPossibleDuplicate(null)}>返回修改</button>
        <button className="btn btn-sm" disabled={busy} onClick={() => submit(true)}>继续生成</button>
        <button className="btn btn-pri btn-sm" disabled={busy} onClick={() => go(planningMatchLink(possibleDuplicate[0]))}>查看已有课程</button>
      </>}><p>学习目标与已有规划相同，最终课程内容可能不同。</p><ul>{possibleDuplicate.map(m => <li key={m.id}><a href={`#${planningMatchLink(m)}`}>{m.title}</a></li>)}</ul></Dialog>}
      <section aria-labelledby="ask">
        {pendingPlan && <button className="nextpill" style={{ marginBottom: 18 }} onClick={() => {
          sessionStorage.setItem("slice3.pendingTask", pendingPlan.task_id);
          sessionStorage.setItem("slice3.pendingRoute", pendingPlan.target_id);
          go("/planning");
        }}>
          <span className="kicker">继续规划</span>
          <b className="od-truncate" style={{ maxWidth: 320 }}>{pendingPlan.title || "上次的学习规划"}</b>
          <span aria-hidden="true">›</span>
        </button>}
        {resume && (
          <button className="nextpill" style={{ marginBottom: 18 }} onClick={resumeGo}>
            <span className="kicker">继续学习</span>
            <b className="od-truncate" style={{ maxWidth: 320 }}>{resume.title}</b>
            {resume.lesson && <span>· {resume.lesson.title}</span>}
            <span aria-hidden="true">›</span>
          </button>
        )}
        <h1 className="ask" id="ask">{type === "plan" ? "你今天想弄懂什么？" : "你想问什么？"}</h1>
        <p className="muted">
          {type === "plan"
            ? "把目标、问题或资料交给学伴，它会搜索、整理，再生成一张可以继续探索的学习图。"
            : "直接提问，学伴会结合你的资料和上下文一步步回答。"}
        </p>

        <div className="composer task-card">
          <div className="task-type" role="radiogroup" aria-label="任务类型">
            <button role="radio" aria-checked={type === "plan"} onClick={() => setType("plan")}>规划学习</button>
            <button role="radio" aria-checked={type === "chat"} onClick={() => setType("chat")}>即时对话</button>
          </div>
          <label className="sr" htmlFor="goal">{type === "plan" ? "学习目标" : "你的问题"}</label>
          <SourceComposer label={type==="plan"?"学习目标":"你的问题"} disabled={busy}
            placeholder={`例如：${(type==="plan"?EXAMPLES_PLAN:EXAMPLES_CHAT)[hintIndex % (type==="plan"?EXAMPLES_PLAN.length:EXAMPLES_CHAT.length)]}`}
            onChange={(value,links)=>{setText(value);sources.setLinks(links);}}
            onFiles={sources.addFiles} onSubmit={()=>void submit()}/>
          {sources.tray}
          <div className="cbar">
            {me&&sources.pickerControl}
            <span className="grow" />
            <button type="button" className="btn btn-pri submit" aria-describedby={type === "plan" ? "planning-cover-notice" : undefined} disabled={busy || (!text.trim()&&!sources.hasSources) || !me} onClick={() => submit()}>
              {busy ? <><span className="spin" />正在提交…</> : type === "plan" ? "生成规划草稿" : "开始对话"}
            </button>
          </div>
        </div>
        {type === "plan" && <p className="help" id="planning-cover-notice">
          规划确认后会自动生成封面。已启用图片模型时只发送主题与画面描述，可能计费；未配置则使用本地水墨封面。
        </p>}

      </section>
    </div>
  );
}
