import { useEffect, useState } from "react";
import { api } from "../api";

export default function UnitAssessment({ courseId, unitId, unitTitle, onReview }: {
  courseId: string; unitId: string; unitTitle: string; onReview: () => void;
}) {
  const [quiz, setQuiz] = useState<any>(null);
  const [open, setOpen] = useState(false);
  const [answers, setAnswers] = useState<Record<string, string | string[]>>({});
  const [attempt, setAttempt] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const key = `assessment.attempt.${unitId}`;
  useEffect(() => {
    let cancelled = false;
    api.unitAssessment(courseId, unitId).then(value => { if (!cancelled) setQuiz(value); }).catch(() => { if (!cancelled) setError("测验暂不可用，请刷新后重试。"); });
    return () => { cancelled = true; };
  }, [courseId, unitId]);
  useEffect(() => {
    if (!open) return;
    let cancelled = false, timer: number;
    const refresh = async () => {
      try {
        const value = await api.unitAssessment(courseId, unitId);
        if (cancelled) return;
        setQuiz(value);
        const ident = sessionStorage.getItem(key);
        if (ident) {
          const grade = await api.assessmentAttempt(ident);
          if (!cancelled) setAttempt(grade);
        }
      } catch { if (!cancelled) setError("测验状态读取失败，可稍后重试。"); }
      if (!cancelled) timer = window.setTimeout(refresh, 1500);
    };
    refresh();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [open, courseId, unitId, key]);
  const start = async () => {
    setOpen(true); setError("");
    if (quiz?.assessment_id) return;
    setBusy(true);
    try { await api.generateAssessment(courseId, unitId, crypto.randomUUID()); setQuiz(await api.unitAssessment(courseId, unitId)); }
    catch (e: any) { setError(e.message || "测验生成失败"); }
    finally { setBusy(false); }
  };
  const toggle = (q: any, id: string) => setAnswers(previous => {
    const current = Array.isArray(previous[q.id]) ? previous[q.id] as string[] : [];
    return { ...previous, [q.id]: q.kind === "single" ? [id] : current.includes(id) ? current.filter(v => v !== id) : [...current, id] };
  });
  const grading = attempt && ["queued", "leased", "running", "expired"].includes(attempt.status);
  const answered = quiz?.questions?.every((q: any) => typeof answers[q.id] === "string" ? (answers[q.id] as string).trim().length > 0 : (answers[q.id] as string[] | undefined)?.length);
  const submit = async () => {
    setBusy(true); setError("");
    try {
      const value = await api.submitAssessment(quiz.assessment_id, answers, crypto.randomUUID());
      sessionStorage.setItem(key, value.attempt_id); setAttempt(await api.assessmentAttempt(value.attempt_id));
    } catch (e: any) { setError(e.message || "提交失败，答案已保留。"); }
    finally { setBusy(false); }
  };
  return <section className="unit-assessment" aria-label={`${unitTitle}单元测验`}>
    <div className="row" style={{ justifyContent: "space-between" }}><div><b>单元测验</b><p className="help">检查本单元的理解和应用，成绩不改变讲次完成记录。</p></div>
      <button className="btn btn-sm" disabled={!quiz?.eligible || busy} onClick={start}>{quiz?.assessment_id ? "查看单元测验" : "开始单元测验"}</button>
    </div>
    {quiz && !quiz.eligible && <p className="help">确认完成本单元全部讲次后，可以开始测验。</p>}
    {open && <div className="assessment-content">
      <button className="linkbtn" onClick={() => setOpen(false)}>收起测验</button>
      {quiz?.status !== "succeeded" && <p role="status">{quiz?.status === "failed" ? "测验未生成，请重试。" : "正在根据本单元讲过的内容出题…"}</p>}
      {quiz?.status === "failed" && <button className="btn btn-sm" onClick={() => api.retryAssessment(quiz.task_id).catch(() => setError("重试失败"))}>重试生成测验</button>}
      {quiz?.status === "succeeded" && !attempt?.feedback && <form onSubmit={event => { event.preventDefault(); submit(); }}>
        <h3>{quiz.title}</h3>
        {quiz.questions.map((q: any, index: number) => <fieldset className="assessment-question" key={q.id} disabled={busy || grading}>
          <legend>{index + 1}. {q.prompt} <span className="help">（{q.points} 分）</span></legend>
          <p className="help">考查：{q.goal}</p>
          {q.kind === "single" || q.kind === "multiple" ? q.options.map((option: any) => <label key={option.id} className="assessment-option">
            <input type={q.kind === "single" ? "radio" : "checkbox"} name={q.id} checked={(answers[q.id] as string[] | undefined)?.includes(option.id) || false} onChange={() => toggle(q, option.id)} />{option.id}. {option.text}
          </label>) : <textarea className="inp" rows={4} maxLength={4000} aria-label={`第${index + 1}题答案`} value={(answers[q.id] as string) || ""} onChange={event => setAnswers(a => ({ ...a, [q.id]: event.target.value }))} />}
        </fieldset>)}
        <button className="btn btn-pri" disabled={!answered || busy || grading}>提交答案</button>
      </form>}
      {grading && <p role="status">正在评价作答，离开后回来仍可查看结果。</p>}
      {attempt?.status === "failed" && <button className="btn btn-sm" onClick={() => api.retryAssessment(attempt.task_id).catch(() => setError("重试失败"))}>重试评价</button>}
      {attempt?.feedback && <div className="assessment-feedback" aria-label="测验反馈">
        <h3>本次得分：{attempt.feedback.earned} / {attempt.feedback.maximum}</h3>
        {attempt.feedback.grades.map((g: any, index: number) => <section key={g.question_id}>
          <b>{index + 1}. {g.prompt} · {g.earned}/{g.maximum} 分</b><p>{g.reason}</p><p>{g.explanation}</p>
          {g.weaknesses?.length > 0 && <p>建议复习：{g.weaknesses.join("、")}</p>}
          <details><summary>参考答案</summary><p>{g.correct_answer.join("、")}</p></details>
        </section>)}
        <p>{attempt.feedback.advice}</p><button className="btn btn-sm" onClick={onReview}>回看本单元</button>
        <button className="btn btn-sm" onClick={() => { sessionStorage.removeItem(key); setAttempt(null); setAnswers({}); }}>再测一次</button>
      </div>}
    </div>}
    {error && <p className="help" role="alert">{error}</p>}
  </section>;
}
