import katex from "katex";

const DISALLOWED = /\\(?:href|url|html[A-Za-z]*|includegraphics|input|include|style|class|bbox|colorbox)\b/i;

export type FormulaRender = { html: string; plain: string; valid: boolean };
export type FormulaMeasurement = { naturalWidth: number; heights: number[]; widths?:number[] };
const measurementCache = new Map<string, FormulaMeasurement>();

function sourceOf(value: string) {
  let source = String(value || "").trim();
  source = source.replace(/^```(?:latex|tex|math)?\s*/i, "").replace(/\s*```$/i, "").trim();
  if ((source.startsWith("$$") && source.endsWith("$$")) || (source.startsWith("\\[") && source.endsWith("\\]")))
    source = source.slice(2, -2).trim();
  if (source.startsWith("\\(") && source.endsWith("\\)")) source = source.slice(2, -2).trim();
  return source.slice(0, 1000);
}

export function formulaPlainText(value: string) {
  return sourceOf(value)
    .replace(/\\(iff|Longleftrightarrow)/g, "⇔")
    .replace(/\\(leq|le)/g, "≤")
    .replace(/\\(geq|ge)/g, "≥")
    .replace(/\\exists/g, "∃")
    .replace(/\\forall/g, "∀")
    .replace(/\\cdot/g, "·")
    .replace(/\\times/g, "×")
    .replace(/\\to/g, "→")
    .replace(/\\in/g, "∈")
    .replace(/\\sqrt\s*\{([^{}]*)\}/g, "√($1)")
    .replace(/\\text\s*\{([^{}]*)\}/g, "$1")
    .replace(/[{}]/g, "")
    .replace(/\\([A-Za-z]+)/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

export function renderFormula(value: string): FormulaRender {
  const source = sourceOf(value);
  const plain = formulaPlainText(source);
  if (!source || DISALLOWED.test(source)) return { html: "", plain, valid: false };
  try {
    const html = katex.renderToString(source, {
      displayMode: true,
      throwOnError: true,
      trust: false,
      strict: "warn",
      output: "htmlAndMathml",
    });
    return { html, plain, valid: true };
  } catch {
    return { html: "", plain, valid: false };
  }
}

export function formulaHeight(value: string, width: number, fontSize = 20) {
  const plain = formulaPlainText(value);
  const charsPerLine = Math.max(8, Math.floor(width / (fontSize * 0.62)));
  return Math.max(fontSize * 2.2, Math.ceil(Math.max(1, plain.length) / charsPerLine) * fontSize * 1.6 + 16);
}

export function renderFormulaBlock(block: {formula?:string;format?:string;plain_text?:string}): FormulaRender {
  return block.format === "plain"
    ? {html:"",plain:block.plain_text||block.formula||"",valid:false}
    : renderFormula(block.formula||"");
}

/** Measure the same DOM/CSS used on the board at each candidate column span. */
export function measureFormula(block: {formula?:string;format?:string;plain_text?:string}, widths: number[], refresh=false): FormulaMeasurement {
  const key=JSON.stringify([block.formula,block.format,block.plain_text,widths]);
  if(refresh)measurementCache.delete(key);
  const cached=measurementCache.get(key);if(cached)return cached;
  const rendered=renderFormulaBlock(block);
  const probe=document.createElement("div");
  probe.className="teaching-formula-content";
  Object.assign(probe.style,{position:"fixed",left:"-100000px",top:"0",visibility:"hidden",fontSize:"20px",width:"max-content"});
  if(rendered.valid)probe.innerHTML=rendered.html;
  else {const fallback=document.createElement("span");fallback.className="teaching-formula-fallback";fallback.textContent=rendered.plain;probe.append(fallback);}
  document.body.append(probe);
  try {
    const math=probe.querySelector<HTMLElement>(".katex");
    if(math)math.style.whiteSpace="nowrap";
    const naturalWidth=Math.ceil(probe.getBoundingClientRect().width);
    if(math)math.style.whiteSpace="";
    const heights=widths.map(width=>{
      probe.style.width=`${width}px`;
      return Math.ceil(probe.getBoundingClientRect().height);
    });
    const result={naturalWidth,heights,widths};
    if(measurementCache.size>=200)measurementCache.clear();
    measurementCache.set(key,result);
    return result;
  } finally {probe.remove();}
}
