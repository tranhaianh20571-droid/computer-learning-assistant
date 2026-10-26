import {useRef,useState} from "react";
import type {CSSProperties} from "react";
import {timelinePosition,timelineValue,type LecturePosition} from "../lib/lectureTimeline";

export default function LectureProgress({position,steps,total,disabled,onSeek,played={},onBegin,onCancel}:{
  position:LecturePosition;steps:any[];total:number;disabled:boolean;onSeek:(target:LecturePosition)=>Promise<void>;
  played?:Record<string,{fraction:number}>;
  onBegin?:()=>void;onCancel?:()=>void;
}){
  const [drag,setDrag]=useState<number|null>(null);
  const ceiling=useRef(position);
  const current=timelineValue(position,steps),value=drag??current;
  const coverage=Math.min(100,Object.values(played).reduce((sum,item)=>sum+item.fraction,0)/Math.max(1,total)*100);
  const commit=(next:number)=>{
    const target=timelinePosition(next,steps,ceiling.current);
    setDrag(null);if(target)void onSeek(target);else onCancel?.();
  };
  const style={"--lecture-progress":`${Math.min(100,current/Math.max(1,total)*100)}%`,
    "--lecture-covered":`${coverage}%`} as CSSProperties;
  return <div className="lecture-progress" aria-label="讲解进度" style={style}>
    <div className="lecture-progress-track" aria-hidden="true"><span/><b/></div>
    <input type="range" aria-label="回放已讲内容" min={0} max={Math.max(1,total)} step={.001}
      data-played-percent={coverage}
      value={value} disabled={disabled||!total}
      onPointerDown={()=>{ceiling.current=position;onBegin?.();}}
      onChange={event=>{
        const next=Number(event.target.value);
        if(next<=timelineValue(ceiling.current,steps))setDrag(next);
        else event.currentTarget.value=String(timelineValue(ceiling.current,steps));
      }}
      onPointerUp={event=>commit(Number(event.currentTarget.value))}
      onPointerCancel={()=>{setDrag(null);onCancel?.();}}
      onKeyDown={event=>{
        if(["ArrowRight","ArrowUp","PageUp","End"].includes(event.key)){event.preventDefault();return;}
        if(["ArrowLeft","ArrowDown","Home","PageDown"].includes(event.key)){
          event.preventDefault();ceiling.current=position;
          commit(event.key==="Home"?0:Math.max(0,current-(event.key==="PageDown"?.5:.025)));
        }
      }}
      title={disabled?"当前活动结束后可回放":"向左回拖"}/>
    <output className="lecture-progress-label">第 {Math.min(position.step_no,total||position.step_no)} / {total||"—"} 段 · 已讲 {Math.floor(coverage)}%</output>
  </div>;
}
