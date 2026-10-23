import {Play,Pause,RotateCcw} from "lucide-react";
import QuestionSource from "./QuestionSource";
import QuestionText from "./QuestionText";

export default function FeynmanDialogue({quiz,audio,busy,onAction}:{quiz:any;audio:any;busy:boolean;onAction:(action:string)=>void}){
  if(!quiz)return null;
  const waiting=quiz.state==="waiting",feedback=quiz.state==="feedback";
  const lesson=quiz.scope==="lesson";
  const phase:Record<string,string>={explain:"原始理解",probe:"针对性追问",restate:"重新解释",
    teach_restate:"补讲与重述",transfer:"新情境检验"};
  return <section className="feynman-dialogue" aria-label={lesson?"本讲费曼对话测验":"知识点费曼问答"} data-feynman-state={quiz.state}>
    <header><strong>{lesson?"本讲费曼对话":"讲给导师听"}</strong><span>{lesson?phase[quiz.learning_state?.current_action]||"理解检验":quiz.probe_count?`追问 ${quiz.probe_count} / ${quiz.max_probes}`:"用自己的话解释"}</span></header>
    {quiz.history.map((item:any)=><article key={item.id} className={item.role==="user"?"feynman-user":"feynman-teacher"}>
      <small>{item.role==="user"?"我的回答":"导师"}</small><p><QuestionText text={audio.text(item)}/></p>
      <QuestionSource source={item.source}/>
    </article>)}
    {!waiting&&!feedback&&<p className="help" role="status">{quiz.retry_available?"问答准备失败，已保留记录。":"正在准备与讲解同步的问答…"}</p>}
    {quiz.retry_available&&<button className="btn btn-sm" disabled={busy} onClick={()=>onAction("retry")}><RotateCcw size={14}/>重试问答</button>}
    {quiz.audio_required&&audio.turn&&<div className="feynman-audio">
      <button className="iconbtn" title={audio.state==="playing"?"暂停问答语音":"播放问答语音"}
        aria-label={audio.state==="playing"?"暂停问答语音":"播放问答语音"} onClick={audio.play}>
        {audio.state==="playing"?<Pause size={18}/>:<Play size={18}/>}</button>
      <span role="status">{audio.state==="failed"?"语音未能播放，请重试":audio.state==="blocked"?"点击播放问答语音":audio.state==="waiting"?"语音缓冲中":audio.done?"已播放完毕":"问答语音"}</span>
    </div>}
    {feedback&&<p className="feynman-result">{quiz.result==="understood"?"本次解释较完整":quiz.result==="review"?"仍有理解缺口，建议复习":"本次暂未验证"}</p>}
    <div className="feynman-actions">
      {feedback&&<button className="btn btn-pri btn-sm" disabled={busy||!audio.done} onClick={()=>onAction("continue")}>{lesson?"完成本讲测验":"继续讲解"}</button>}
      {!lesson&&<button className="btn btn-sm" disabled={busy} onClick={()=>onAction("skip")}>{feedback?"稍后复习，继续":"跳过本次问答"}</button>}
    </div>
  </section>;
}

export function FeynmanLearningRecord({record}:{record:any}){
  if(!record)return null;
  const labels:Record<string,string>={accuracy:"准确性",clarity:"清晰度",mechanism:"机制理解",boundary:"边界意识",transfer:"迁移能力"};
  const statuses:Record<string,string>={demonstrated:"已有证据",partial:"尚不完整",gap:"需要复习",unknown:"尚未验证"};
  return <details className="feynman-learning-record">
    <summary>本讲学习记录</summary>
    {record.initial_explanation&&<section><h4>最初的解释</h4><p>{record.initial_explanation}</p></section>}
    {Object.entries(labels).map(([key,label])=>{
      const value=record.dimensions?.[key];
      return <p key={key}><strong>{label}：{statuses[value?.status]||"尚未验证"}</strong>{value?.evidence&&<> · {value.evidence}</>}</p>;
    })}
    {record.corrections?.map((item:any,index:number)=><section key={index}><h4>{item.gap}</h4><p>{item.feedback}</p></section>)}
    {record.transfer_tests?.map((item:any,index:number)=><section key={index}><h4>新情境检验</h4><p>{item.question}</p><p>{item.answer}</p><p>{item.feedback}</p></section>)}
    {record.review_items?.length>0&&<section><h4>待复习</h4><ul>{record.review_items.map((item:string,index:number)=><li key={index}>{item}</li>)}</ul></section>}
  </details>;
}
