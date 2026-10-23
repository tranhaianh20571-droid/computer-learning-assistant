import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ArrowLeft, ArrowRight, ArrowUpRight, Bookmark, Check, CheckCheck,
  ChevronRight, Download, FileText, LayoutGrid, LogOut, Moon, Send, X } from "lucide-react";
import { api } from "../api";
import QuestionSource from "./QuestionSource";
import { sourceLocation } from "../lib/sourceLocation";
import QuestionText from "./QuestionText";
import AssessmentFigure from "./AssessmentFigure";
import "./unitAssessment.css";
import BrandLogo from "./BrandLogo";
import DrawingWorkspace from "./DrawingWorkspace";
import AssessmentResult from "./AssessmentResult";
import { answerText, gradeStatus, type AssessmentGrade } from "../lib/assessmentResult";
import {hasDrawing} from "../lib/graphics";
import type {DrawingAnswer,DrawingSpec} from "../lib/graphics";

type Answers = Record<string, string | string[] | DrawingAnswer>;
type Lesson = { lesson_id: string; title?: string; goal?: string };
type Question = {
  id: string; kind: string; prompt: string; points: number; lesson_ids?: string[];
  options: { id: string; text: string }[]; source?: any;
  primary_lesson_id?: string; figure?: any; rounding?: { unit?: string; decimals?: number; notation?: string };
  drawing?: DrawingSpec;
};
type Draft = { answers: Answers; flags: string[]; current: number;
  attemptId?: string; submission?: { key: string; answers: string } };
type Props = {
  courseId: string; unitId: string; unitTitle: string; onReview: () => void;
  ownerUserId?: string; courseTitle?: string; unitIndex?: number; lessons?: Lesson[];
};

function readDraft(key: string | null): Draft {
  try {
    const value = key ? JSON.parse(localStorage.getItem(key) || "null") : null;
    if (value && value.answers && typeof value.answers === "object" && !Array.isArray(value.answers)
      && Array.isArray(value.flags) && value.flags.every((v: unknown) => typeof v === "string")) {
      const answers: Answers = {};
      for (const [id, answer] of Object.entries(value.answers)) {
        if (typeof answer === "string" || Array.isArray(answer) && answer.every(v => typeof v === "string") || hasDrawing(answer)) {
          answers[id] = answer as string | string[] | DrawingAnswer;
        }
      }
      return { answers, flags: value.flags, current: Number.isInteger(value.current) ? value.current : 0,
        attemptId: typeof value.attemptId === "string" ? value.attemptId : undefined,
        submission: typeof value.submission?.key === "string" && typeof value.submission?.answers === "string"
          ? value.submission : undefined };
    }
  } catch { /* Storage can be unavailable; keep the live draft in memory. */ }
  return { answers: {}, flags: [], current: 0 };
}

function AssessmentDialog({ title, children, actions, onClose }: {
  title: string; children: ReactNode; actions?: ReactNode; onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { ref.current?.showModal(); }, []);
  return <dialog ref={ref} aria-label={title} onCancel={event => { event.preventDefault(); onClose(); }}>
    <div className="dialog-head"><h2>{title}</h2>
      <button className="icon-btn" onClick={onClose} aria-label="关闭对话框"><X /></button></div>
    <div className="dialog-body">{children}</div><div className="dialog-footer">{actions}</div>
  </dialog>;
}

export default function UnitAssessment(props: Props) {
  const [quiz, setQuiz] = useState<any>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    api.unitAssessment(props.courseId, props.unitId).then(value => { if (!cancelled) setQuiz(value); })
      .catch(() => { if (!cancelled) setError("测验暂不可用，请刷新后重试。"); });
    return () => { cancelled = true; };
  }, [props.courseId, props.unitId]);
  const start = async () => {
    setOpen(true); setError("");
    if (quiz?.assessment_id) return;
    setBusy(true);
    try {
      await api.generateAssessment(props.courseId, props.unitId, crypto.randomUUID());
      setQuiz(await api.unitAssessment(props.courseId, props.unitId));
    } catch (e: any) { setError(e.message || "测验生成失败"); }
    finally { setBusy(false); }
  };
  return <section className="unit-assessment" aria-label={`${props.unitTitle}单元测验`}>
    <div className="row" style={{ justifyContent: "space-between" }}><b>单元测验</b>
      <button className="btn btn-sm" disabled={!quiz?.eligible || busy} onClick={start}>
        {quiz?.assessment_id ? "查看单元测验" : "开始单元测验"}</button></div>
    {quiz && !quiz.eligible && <p className="help">确认完成本单元全部讲次后，可以开始测验。</p>}
    {!open && error && <p role="alert" className="help">{error}</p>}
    {open && <AssessmentWorkspace key={quiz?.assessment_id || "pending"} {...props}
      quiz={quiz} setQuiz={setQuiz} initialError={error} onClose={() => setOpen(false)} />}
  </section>;
}

function AssessmentWorkspace({ quiz, setQuiz, initialError, onClose, ...props }: Props & {
  quiz: any; setQuiz: (v: any) => void; initialError: string; onClose: () => void;
}) {
  const { courseId, unitId, unitTitle, ownerUserId, lessons = [], courseTitle = "", unitIndex = 1, onReview } = props;
  const storageKey = ownerUserId && quiz?.assessment_id
    ? `assessment.draft.${ownerUserId}.${courseId}.${unitId}.${quiz.assessment_id}` : null;
  const [draft, setDraft] = useState(() => readDraft(storageKey));
  const [attempt, setAttempt] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(initialError);
  const [saved, setSaved] = useState(Boolean(storageKey));
  const [dark, setDark] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [resultFilter, setResultFilter] = useState("all");
  const [highlightErrors, setHighlightErrors] = useState(true);
  const [modal, setModal] = useState<"source" | "exit" | "missing" | "submit" | null>(null);
  const screen = useRef<HTMLDivElement>(null);
  const locked = useRef(false);
  const correctionDirty = useRef(false);
  const submission = useRef(draft.submission);
  const questions: Question[] = quiz?.questions || [];
  const current = Math.max(0, Math.min(draft.current, questions.length - 1));
  const q = questions[current];
  const grading = attempt && ["queued", "leased", "running", "expired"].includes(attempt.status);
  const feedback = attempt?.feedback;
  const grade = feedback?.grades?.find((g: any) => g.question_id === q?.id);
  const grades = new Map<string, AssessmentGrade>((feedback?.grades || []).map((value: AssessmentGrade) => [value.question_id, value]));
  const submittedAnswers: Answers = (() => {
    try { return draft.submission ? JSON.parse(draft.submission.answers) : draft.answers; }
    catch { return draft.answers; }
  })();
  const visibleResult = (index: number) => resultFilter === "all" || gradeStatus(grades.get(questions[index].id)) === resultFilter;
  const statusLabel = (value?: AssessmentGrade) => ({ correct: "回答正确", partial: "部分得分", wrong: "需要订正", pending: "暂无评分" })[gradeStatus(value)];
  const exportReport = () => {
    const lines = [`${unitTitle} · 测验批改报告`, `本次得分：${feedback.earned} / ${feedback.maximum}`, "",
      ...questions.flatMap((question, index) => {
        const value = grades.get(question.id);
        let correction = "";
        try {
          if (storageKey && draft.attemptId) correction = localStorage.getItem(`${storageKey}.correction.${draft.attemptId}.${question.id}`) || "";
        } catch { /* Export the server grading even when local storage is unavailable. */ }
        return [`第 ${index + 1} 题：${question.prompt}`,
          `你的作答：${hasDrawing(submittedAnswers[question.id]) ? "作图答案请在测验页面查看。" : answerText(submittedAnswers[question.id], question.options) || "作答未保留。"}`,
          `得分：${value ? `${value.earned} / ${value.maximum}` : "暂无评分"}`,
          `批改：${value?.reason || ""}`, `参考答案：${value?.correct_answer?.join("\n") || ""}`,
          `解析：${value?.explanation || ""}`, `我的订正：${correction || "未保存本机订正。"}`, ""];
      }), feedback.advice || ""];
    const url = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/plain;charset=utf-8" }));
    const link = document.createElement("a"); link.href = url; link.download = "测验批改报告.txt"; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const hasAnswer = (question: Question) => {
    const value = draft.answers[question.id];
    if(question.kind==="drawing")return hasDrawing(value);
    return typeof value === "string" ? value.trim().length > 0 : Array.isArray(value) && value.length > 0;
  };
  const done = questions.filter(hasAnswer).length;
  const groups = lessons.map((lesson, i) => ({ id: lesson.lesson_id, title: lesson.title || lesson.goal || unitTitle,
    number: i + 1, indices: questions.flatMap((question, index) =>
      (question.primary_lesson_id || question.lesson_ids?.find(id => lessons.some(l => l.lesson_id === id))) === lesson.lesson_id ? [index] : []) }));
  const unassigned = questions.flatMap((question, index) =>
    !question.lesson_ids?.some(id => lessons.some(l => l.lesson_id === id)) ? [index] : []);
  if (unassigned.length) groups.push({ id: unitId, title: unitTitle, number: groups.length + 1, indices: unassigned });
  const group = groups.find(g => g.indices.includes(current));
  const promptParts = q?.prompt.split(/\n[ \t]*\n/) || [];

  useEffect(() => {
    const root = document.getElementById("root");
    const previous = root?.inert;
    if (root) root.inert = true;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (correctionDirty.current) { event.preventDefault(); event.returnValue = ""; }
    };
    window.addEventListener("beforeunload", beforeUnload);
    screen.current?.focus();
    return () => {
      window.removeEventListener("beforeunload", beforeUnload);
      document.body.style.overflow = overflow; if (root) root.inert = previous || false;
    };
  }, []);
  useEffect(() => {
    try {
      if (!storageKey) { setSaved(false); return; }
      localStorage.setItem(storageKey, JSON.stringify(draft));
      setSaved(true);
    } catch { setSaved(false); }
  }, [draft, storageKey]);
  useEffect(() => { setError(initialError); }, [initialError]);
  useEffect(() => {
    if (navOpen) screen.current?.querySelector<HTMLButtonElement>(".aside-close")?.focus();
  }, [navOpen]);
  useEffect(() => {
    let cancelled = false, timer: number;
    const refresh = async () => {
      try {
        const value = await api.unitAssessment(courseId, unitId);
        if (cancelled) return;
        setQuiz(value);
        if (draft.attemptId) {
          const result = await api.assessmentAttempt(draft.attemptId);
          if (!cancelled) setAttempt(result);
        }
      } catch (e: any) { if (!cancelled) setError(e.message || "测验状态读取失败，可稍后重试。"); }
      if (!cancelled) timer = window.setTimeout(refresh, 1500);
    };
    refresh();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [courseId, unitId, draft.attemptId, setQuiz]);
  const navigate = (index: number) => {
    if (correctionDirty.current) { setError("请先保存或取消当前订正，再切换题目。"); return; }
    setError("");
    setDraft(value => ({ ...value, current: index })); setNavOpen(false);
    screen.current?.scrollTo({ top: 0 });
  };
  const toggle = (id: string) => {
    if (!q || busy || draft.attemptId || feedback) return;
    setDraft(value => {
      const selected = Array.isArray(value.answers[q.id]) ? value.answers[q.id] as string[] : [];
      return { ...value, answers: { ...value.answers, [q.id]: q.kind === "single" ? [id]
        : selected.includes(id) ? selected.filter(v => v !== id) : [...selected, id] } };
    });
  };
  const retry = async (taskId: string) => {
    if (locked.current) return;
    locked.current = true; setBusy(true); setError("");
    try { await api.retryAssessment(taskId); }
    catch (e: any) { setError(e.message || "重试失败"); }
    finally { locked.current = false; setBusy(false); }
  };
  const submit = async () => {
    if (locked.current || !questions.length || done !== questions.length) return;
    locked.current = true; setModal(null); setBusy(true); setError("");
    const answers = Object.fromEntries(questions.map(question => [question.id, draft.answers[question.id]]));
    const serialized = JSON.stringify(answers);
    if (submission.current?.answers !== serialized) submission.current = { key: crypto.randomUUID(), answers: serialized };
    const fixed = submission.current!;
    const pending = { ...draft, submission: fixed };
    setDraft(pending);
    if (storageKey) { try { localStorage.setItem(storageKey, JSON.stringify(pending)); } catch { setSaved(false); } }
    try {
      const value = await api.submitAssessment(quiz.assessment_id, answers, fixed.key);
      setDraft(previous => ({ ...previous, attemptId: value.attempt_id }));
      setAttempt({ ...value, status: "queued" });
      setAttempt(await api.assessmentAttempt(value.attempt_id));
    } catch (e: any) { setError(e.message || "提交失败，答案已保留。"); }
    finally { locked.current = false; setBusy(false); }
  };
  const checkSubmit = () => {
    if (busy || draft.attemptId) return;
    setModal(done === questions.length ? "submit" : "missing");
  };
  const repeat = () => {
    if (correctionDirty.current) { setError("请先保存或取消当前订正，再开始测验。"); return; }
    submission.current = undefined;
    setAttempt(null); setDraft({ answers: {}, flags: [], current: 0 }); setError(""); setResultFilter("all");
  };
  const source = q?.source;
  const sourceLabel = source?.kind === "original" ? "资料原题" : source?.kind === "adapted" ? "资料改编" : "AI 补充";
  const draftStatus = <span className="saved">{feedback ? <CheckCheck /> : saved && <CheckCheck />}
    {feedback ? "整卷批改已完成" : saved ? "本机草稿已保存" : "草稿暂存于当前页面"}</span>;
  const lockedAnswer = Boolean(busy || draft.attemptId);
  const modalClose = () => setModal(null);
  const close = () => {
    if (correctionDirty.current) { setModal(null); setError("请先保存或取消当前订正，再退出测验。"); return; }
    onClose();
  };
  return createPortal(<div ref={screen} tabIndex={-1} className={`assessment-screen${feedback ? " assessment-report" : ""}${navOpen ? " nav-open" : ""}`}
    data-palette="blue" data-theme={dark ? "dark" : "light"} onKeyDown={event => {
      if (event.key === "Escape" && !modal) { if (navOpen) setNavOpen(false); else setModal("exit"); }
    }}>
    <header inert={navOpen}>
      <div className="brand"><BrandLogo decorative /><span className="brand-name">学伴</span>
        <span className="brand-sep" /><span className="brand-small">{feedback ? "测验批改" : "单元测验"}</span></div>
      <div className="header-actions">{draftStatus}<button className="icon-btn" title="切换深浅色" aria-label="切换深浅色" onClick={() => setDark(!dark)}><Moon /></button>
        {feedback && <button className="btn" aria-label="导出批改报告" onClick={exportReport}><Download />导出报告</button>}
        <button className="btn" onClick={() => setModal("exit")} aria-label="退出测验"><LogOut />退出测验</button></div>
    </header>
    <div className="workspace">
      <main className="main" inert={navOpen}>
        <section className="course-head" aria-label="测验概览">
          <div className="breadcrumbs"><span>{courseTitle}</span><ChevronRight /><span>第{unitIndex}单元</span></div>
          <div className="heading-line"><h1>{unitTitle}</h1><span className="unit-label">{lessons.length} 个讲次 · {questions.length} 道题</span>
            <button className="btn mobile-nav" onClick={() => setNavOpen(true)}><LayoutGrid />答题卡</button></div>
          {feedback && <div className="report-overview"><h2 className="result-score">本次得分：{feedback.earned} / {feedback.maximum}</h2>
            <span>整卷已提交 · {feedback.grades.length} / {questions.length} 题已批改</span>
            <label><input type="checkbox" checked={highlightErrors} onChange={event => setHighlightErrors(event.target.checked)} />强调错误</label></div>}
          <div className="mobile-status"><span>{feedback ? `${feedback.earned} / ${feedback.maximum} 分` : `${done} / ${questions.length} 已作答`}</span>{draftStatus}</div>
          <div className="progress-track" role="progressbar" aria-label="已作答进度" aria-valuemin={0}
            aria-valuemax={questions.length || 1} aria-valuenow={done}>
            <div className="progress-fill" style={{ width: `${questions.length ? done / questions.length * 100 : 0}%` }} /></div>
        </section>
        <section className="question-stage" aria-label="当前题目"><div className="question-inner">
          {error && <p role="alert" className="status-message">{error}</p>}
          {quiz?.status !== "succeeded" && <div role="status">
            <p>{quiz?.status === "failed" ? "测验未生成，请重试。" : "正在根据本单元讲过的内容出题…"}
              {quiz?.generation_progress?.total > 0 && ` ${quiz.generation_progress.completed}/${quiz.generation_progress.total} 讲已完成`}</p>
            {quiz?.status === "failed" && <button className="btn" disabled={busy} onClick={() => retry(quiz.task_id)}>重试生成测验</button>}
          </div>}
          {q && <>
            <div className="question-top"><div className="question-index"><strong>{String(current + 1).padStart(2, "0")}</strong><span>/ {questions.length}</span></div>
              <div className="question-tools"><div className="question-meta"><span className="type">
                {{ single: "单选题", multiple: "多选题", short: "简答题", code_reading: "代码阅读",drawing:"作图题" }[q.kind]}</span><span className="dot-sep" /><span>{q.points} 分</span></div>
                <button className="flag-btn" aria-pressed={draft.flags.includes(q.id)} onClick={() => setDraft(value => ({ ...value,
                  flags: value.flags.includes(q.id) ? value.flags.filter(id => id !== q.id) : [...value.flags, q.id] }))}>
                  <Bookmark />{draft.flags.includes(q.id) ? "已标记" : "标记"}</button></div></div>
            <div className="question-context">{group?.id === unitId ? group.title : `第 ${group?.number || 1} 讲 · ${group?.title || unitTitle}`}</div>
            <h2 className="question-title"><QuestionText text={promptParts[0]} /></h2>
            {promptParts.length > 1 && <p className="scenario"><QuestionText text={promptParts.slice(1).join("\n\n")} /></p>}
            <AssessmentFigure figure={q.figure} />
            {q.rounding?.decimals !== undefined && q.rounding.notation !== "exact" && <p className="help">
              {q.rounding.notation === "p_value" ? "p 值保留三位小数；小于 0.001 时写 <0.001"
                : q.rounding.decimals === 0 ? `取整数${q.rounding.unit ? `（${q.rounding.unit}）` : ""}`
                : `保留 ${q.rounding.decimals} 位小数${q.rounding.unit ? `（${q.rounding.unit}）` : ""}`}</p>}
            {!feedback && <fieldset className="answer-fields" disabled={lockedAnswer}>
              {q.kind==="drawing"&&q.drawing?<DrawingWorkspace key={q.id} spec={q.drawing}
                value={hasDrawing(draft.answers[q.id])?draft.answers[q.id] as DrawingAnswer:undefined} locked={lockedAnswer}
                onChange={answer=>setDraft(previous=>({...previous,answers:{...previous.answers,[q.id]:answer}}))}/>:
                q.kind === "single" || q.kind === "multiple" ? <div className="options" role="group" aria-label={q.kind === "multiple" ? "选择所有正确选项" : "选择一个答案"}>
                {q.options.map(option => <label className="option" key={option.id}>
                  <input type={q.kind === "single" ? "radio" : "checkbox"} name={q.id} value={option.id}
                    checked={Array.isArray(draft.answers[q.id]) && (draft.answers[q.id] as string[]).includes(option.id)}
                    onChange={() => toggle(option.id)} /><span className="option-letter">{option.id}</span>
                  <span className="option-copy"><QuestionText text={option.text} /></span></label>)}
              </div> : <><label className="answer-label" htmlFor={`written-${q.id}`}><span>你的回答</span><span>
                {typeof draft.answers[q.id] === "string" ? (draft.answers[q.id] as string).length : 0} / 4000</span></label>
                <textarea id={`written-${q.id}`} className="answer-area" maxLength={4000} aria-label={`第${current + 1}题答案`}
                  value={typeof draft.answers[q.id] === "string" ? draft.answers[q.id] as string : ""}
                  onChange={event => setDraft(value => ({ ...value, answers: { ...value.answers, [q.id]: event.target.value } }))} /></>}
            </fieldset>}
            {grade && <AssessmentResult key={`${draft.attemptId}.${q.id}`} grade={grade} answer={submittedAnswers[q.id]}
              options={q.options} drawing={q.drawing} highlight={highlightErrors}
              onDirty={dirty => { correctionDirty.current = dirty; }}
              storageKey={storageKey && draft.attemptId ? `${storageKey}.correction.${draft.attemptId}.${q.id}` : null} />}
            {source && <div className="source-row"><button className="source-button" onClick={() => setModal("source")}><FileText />
              <span>{sourceLabel}{sourceLocation(source, false) ? ` · ${sourceLocation(source, false)}` : ""}</span><ArrowUpRight /></button>
              <span className="source-note">{source.chapter || ""}</span></div>}
            {grading && <p role="status" className="status-message">正在评价作答，离开后回来仍可查看结果。
              {attempt?.grading_progress && ` ${attempt.grading_progress.completed} 讲已完成`}</p>}
            {attempt?.status === "failed" && <button className="btn" disabled={busy} onClick={() => retry(attempt.task_id)}>重试评价</button>}
            {feedback && current === questions.length - 1 && <p>{feedback.advice}</p>}
          </>}
        </div></section>
        {q && <footer className="nav-footer"><div className="footer-inner">
          <button className="btn" disabled={current === 0} onClick={() => navigate(current - 1)}><ArrowLeft />上一题</button>
          {current < questions.length - 1 ? <button className="btn primary" onClick={() => navigate(current + 1)}>下一题<ArrowRight /></button>
            : feedback ? <button className="btn primary" onClick={() => { if (correctionDirty.current) { setError("请先保存或取消当前订正。"); return; } onClose(); onReview(); }}>回看本单元<ArrowRight /></button>
              : <button className="btn primary" disabled={lockedAnswer} onClick={checkSubmit}>检查并提交<Check /></button>}
        </div></footer>}
      </main>
      <aside aria-label="答题导航">
        <div><div className="aside-title"><h2>答题卡</h2><span className="count-label">共 {questions.length} 题</span>
          <button className="icon-btn aside-close" aria-label="关闭答题卡" onClick={() => setNavOpen(false)}><X /></button></div>
          <div className="completion"><b>{feedback ? feedback.earned : done}</b> / {feedback ? `${feedback.maximum} 分` : `${questions.length} 已作答`}</div>
          {feedback ? <><label className="result-filter">筛选题目<select value={resultFilter} onChange={event => setResultFilter(event.target.value)}>
            <option value="all">全部题目</option><option value="wrong">需要订正</option><option value="partial">部分得分</option><option value="correct">回答正确</option>
          </select></label><div className="legend"><span><i className="wrong-dot" />错误</span><span><i className="partial-dot" />部分得分</span><span><i className="correct-dot" />正确</span></div></>
            : <div className="legend"><span><i className="answered-dot" />已答</span><span><i />未答</span><span><i className="flag-dot" />标记</span></div>}</div>
        <div className="question-map">
          {feedback && !questions.some((_, i) => visibleResult(i)) && <p className="result-empty" role="status">此分类下暂无题目。</p>}
          {groups.filter(g => g.indices.some(i => !feedback || visibleResult(i))).map(g => <section className="lesson-group" key={g.id}>
          <h3 className="lesson-title"><span>{String(g.number).padStart(2, "0")} &nbsp; {g.title}</span>
            <span className="muted">{g.indices.filter(i => feedback ? grades.has(questions[i].id) : hasAnswer(questions[i])).length}/{g.indices.length}</span></h3>
          <div className="numbers">{g.indices.filter(i => !feedback || visibleResult(i)).map(i => <button key={questions[i].id}
            className={`number${feedback ? ` result-${gradeStatus(grades.get(questions[i].id))}` : hasAnswer(questions[i]) ? " answered" : ""}${current === i ? " current" : ""}${draft.flags.includes(questions[i].id) ? " flagged" : ""}`}
            aria-label={`第${i + 1}题，${feedback ? statusLabel(grades.get(questions[i].id)) : hasAnswer(questions[i]) ? "已答" : "未答"}${draft.flags.includes(questions[i].id) ? "，已标记" : ""}`}
            aria-current={i === current ? "step" : undefined} onClick={() => navigate(i)}>{String(i + 1).padStart(2, "0")}</button>)}</div>
        </section>)}</div>
        <div className="submit-block"><div className="submit-count"><span>{feedback ? `已批改 ${grades.size} 题` : `剩余 ${questions.length - done} 题`}</span><span>{draft.flags.length} 题已标记</span></div>
          {feedback ? <button className="btn" onClick={repeat}>再测一次</button> : <button className="btn" disabled={!q || lockedAnswer} onClick={checkSubmit}><Send />提交测验</button>}
          <div className="aside-save">{draftStatus}</div></div>
      </aside>
    </div>
    {modal && <AssessmentDialog title={{ source: "题目来源", exit: feedback ? "离开批改报告？" : "暂时离开测验？", missing: "还有题目未完成", submit: "确认提交测验" }[modal]}
      onClose={modalClose} actions={<>
        <button className="btn" onClick={modalClose}>{feedback ? "返回报告" : modal === "source" ? "返回作答" : modal === "exit" ? "继续作答" : "返回检查"}</button>
        {modal === "exit" && <button className="btn primary" onClick={close}>保存并退出</button>}
        {modal === "missing" && <button className="btn primary" onClick={() => { modalClose(); navigate(questions.findIndex(question => !hasAnswer(question))); }}>前往第一道未答题</button>}
        {modal === "submit" && <button className="btn primary" disabled={busy} onClick={submit}>提交测验</button>}
      </>}>
      {modal === "source" ? <QuestionSource source={source} />
        : modal === "exit" ? feedback ? "批改结果已完成，可稍后回来看。已保存的订正仅保留在当前浏览器。" : saved ? "草稿保存在当前浏览器，下次打开可以继续。" : "草稿暂存于当前页面。"
          : modal === "missing" ? <>已作答 <strong>{done} / {questions.length}</strong> 题，剩余 {questions.length - done} 题。</>
            : <>{questions.length} 道题均已作答，{draft.flags.length} 道题已标记。</>}
    </AssessmentDialog>}
  </div>, document.body);
}
