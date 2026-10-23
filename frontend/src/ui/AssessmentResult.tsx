import { useState } from "react";
import { Check, ChevronDown, Pencil, Save, X, XCircle } from "lucide-react";
import QuestionText from "./QuestionText";
import DrawingWorkspace from "./DrawingWorkspace";
import ProfessionalFigure from "./ProfessionalFigure";
import { hasDrawing } from "../lib/graphics";
import type { DrawingSpec } from "../lib/graphics";
import { answerText, gradeStatus, type AssessmentGrade } from "../lib/assessmentResult";

export default function AssessmentResult({ grade, answer, options, drawing, storageKey, highlight, onDirty }: {
  grade: AssessmentGrade; answer: unknown; options: {id: string; text: string}[];
  drawing?: DrawingSpec; storageKey: string | null; highlight: boolean;
  onDirty: (dirty: boolean) => void;
}) {
  const [referenceOpen, setReferenceOpen] = useState(true);
  const [correction, setCorrection] = useState(() => {
    try { return storageKey ? localStorage.getItem(storageKey) || "" : ""; } catch { return ""; }
  });
  const [text, setText] = useState(correction);
  const [editing, setEditing] = useState(false);
  const [notice, setNotice] = useState("");
  const status = gradeStatus(grade);
  const correct = status === "correct";
  const submittedDrawing = hasDrawing(answer) ? answer : grade.drawing_answer;
  const save = () => {
    if (!text.trim()) { setNotice("请先填写订正内容。"); return; }
    let stored = false;
    try { if (storageKey) { localStorage.setItem(storageKey, text.trim()); stored = true; } } catch { /* Keep the correction in memory. */ }
    setCorrection(text.trim()); setEditing(false); onDirty(false);
    setNotice(stored ? "订正已保存在当前浏览器，不改变原评分。" : "订正暂存于当前页面，不改变原评分。");
  };
  return <div className={`assessment-result${highlight ? " highlight-errors" : ""}`} aria-label="测验反馈">
    <section className={`result-diagnosis result-${status}`} aria-label={correct ? "批改评价" : "扣分原因"}>
      <h3>{correct ? <Check /> : <XCircle />}{correct ? "回答正确" : status === "partial" ? "部分得分" : "需要订正"}
        <span>{grade.earned}/{grade.maximum} 分</span></h3>
      <p><QuestionText text={grade.reason} /></p>
    </section>
    <div className="result-comparison">
      <section className={`result-answer submitted-answer result-${status}`} aria-label="你的作答">
        <h3>你的作答 <small>提交时的答案</small></h3>
        {submittedDrawing && drawing ? <DrawingWorkspace spec={drawing} value={submittedDrawing} onChange={() => {}} locked />
          : <div className="result-answer-copy"><QuestionText text={answerText(answer, options) || "当前浏览器没有保留这次提交的作答。"} /></div>}
      </section>
      <section className="result-answer reference-answer" aria-label="参考答案">
        <h3><span><Check />参考答案</span><button className="reference-toggle" aria-expanded={referenceOpen}
          onClick={() => setReferenceOpen(!referenceOpen)}><ChevronDown />{referenceOpen ? "收起" : "展开"}</button></h3>
        {referenceOpen && <div className="reference-content">
          {(grade.correct_answer || []).map((value, i) => <div className="reference-step" key={i}>
            {(grade.correct_answer?.length || 0) > 1 && <span className="reference-number">{i + 1}</span>}
            <div className="result-answer-copy"><QuestionText text={value} /></div>
          </div>)}
          {grade.drawing_solution && <div className="drawing-feedback"><ProfessionalFigure graphic={grade.drawing_solution} /></div>}
        </div>}
      </section>
    </div>
    {grade.explanation && <section className="result-explanation"><h3>题目解析</h3><QuestionText text={grade.explanation} /></section>}
    {(grade.goal || !!grade.weaknesses?.length) && <div className="result-knowledge">
      {grade.goal && <p><strong>本题考查</strong><QuestionText text={grade.goal} /></p>}
      {!!grade.weaknesses?.length && <p><strong>建议复习</strong><QuestionText text={grade.weaknesses.join("、")} /></p>}
    </div>}
    <section className="result-correction" aria-label="订正记录">
      <div className="correction-heading"><h3>我的订正</h3>{!editing && <button className="btn" onClick={() => {
        setText(correction); setEditing(true); setNotice("");
      }}><Pencil />{correction ? "编辑订正" : "写订正"}</button>}</div>
      {correction && !editing && <div className="correction-copy"><QuestionText text={correction} /></div>}
      {editing && <form onSubmit={event => { event.preventDefault(); save(); }}>
        <label htmlFor={`correction-${grade.question_id}`}>重新写出思路与答案</label>
        <textarea autoFocus id={`correction-${grade.question_id}`} maxLength={4000} value={text}
          onChange={event => { setText(event.target.value); onDirty(event.target.value.trim() !== correction); }} />
        <div className="correction-actions"><span>订正仅在本机保存，不重新评分。</span>
          <button type="button" className="btn" onClick={() => { setEditing(false); setNotice(""); onDirty(false); }}><X />取消</button>
          <button className="btn primary" type="submit"><Save />保存订正</button></div>
      </form>}
      {notice && <p role="status" className="correction-notice">{notice}</p>}
    </section>
  </div>;
}
