import { sourceJumpUrl, sourceLocation } from "../lib/sourceLocation";

export default function QuestionSource({source}: {source: any}) {
  if (!source) return null;
  const label = source.kind === "original" ? "资料原题" : source.kind === "adapted" ? "资料改编" : "AI 补充";
  return <div className="help question-source">
    <strong>{label}</strong>
    {sourceLocation(source) && <> · {sourceJumpUrl(source) ?
      <a href={sourceJumpUrl(source)} target="_blank" rel="noopener noreferrer">{sourceLocation(source)}</a> :
      sourceLocation(source)}</>}
    {source.chapter && <> · {source.chapter}</>}
    {source.transcription_verified === false && <> · 识别文本，请核对原页</>}
    {source.reason && <> · {source.reason}</>}
    {source.answer_basis === "ai" && <> · 评分依据由 AI 拟定</>}
    {source.answer_basis === "material" && <> · 参考资料答案</>}
  </div>;
}
