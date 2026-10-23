import {renderFormula} from "../lib/formula";
import "katex/dist/katex.min.css";
import "./questionText.css";

export default function QuestionText({text}: {text: string}) {
  const parts = String(text || "").split(/(\$\$[\s\S]*?\$\$|\$[^$\n]{1,1000}\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\])/g);
  return <span className="question-copy">{parts.map((part,index) => {
    if (index % 2 === 0) return <span key={index}>{part}</span>;
    const formula = part.startsWith("$") && !part.startsWith("$$") ? part.slice(1,-1) : part;
    const rendered = renderFormula(formula);
    return rendered.valid
      ? <span key={index} className="question-math" dangerouslySetInnerHTML={{__html:rendered.html}}/>
      : <span key={index}>{part}</span>;
  })}</span>;
}
