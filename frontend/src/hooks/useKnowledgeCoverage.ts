import {useCallback,useEffect,useRef} from "react";
import {api} from "../api";

export function useKnowledgeCoverage(conversation:string,step:any,playing:boolean,buffer:any,audio:any,posRef:any,review=false,boardPainted=true){
  const latest=useRef({step,playing,buffer,audio,review,boardPainted});latest.current={step,playing,buffer,audio,review,boardPainted};
  const samples=useRef(new Map<string,{intervals:number[][];previous:number|null;time:number;seq:number}>());
  const serial=useRef(Promise.resolve());
  const flush=useCallback(async()=>{
    const value=latest.current,s=value.step;if(!s||value.review)return;
    const key=`${conversation}:${s.step_id}:${s.text_version_no}:${value.audio.sourceId||""}`;
    const sample=samples.current.get(key);if(!sample?.intervals.length)return;
    const cursor=value.buffer.cursor(`step:${s.step_no}`);
    const position=value.audio.textOnly?cursor.visible_chars:posRef.current,last=sample.intervals.at(-1)!;
    const tail=position-last[1];
    if(cursor.text_complete&&tail>0&&tail<=(value.audio.textOnly?2:250))last[1]=position;
    const payload={step_id:s.step_id,text_version:s.text_version_no,audio_id:value.audio.sourceId||"",
      client_seq:++sample.seq,intervals:sample.intervals.slice(-100),visible_chars:cursor.visible_chars,board_complete:cursor.text_complete&&value.boardPainted};
    const request=serial.current.catch(()=>{}).then(()=>api.coverage(conversation,payload));
    serial.current=request;await request;
  },[conversation]);
  useEffect(()=>{
    samples.current.clear();
    return()=>{samples.current.clear();};
  },[conversation]);
  useEffect(()=>{
    let lastFlush=Date.now();
    const timer=window.setInterval(()=>{
      const value=latest.current,s=value.step;if(!s||value.review)return;
      const key=`${conversation}:${s.step_id}:${s.text_version_no}:${value.audio.sourceId||""}`;
      let sample=samples.current.get(key);
      if(!sample){sample={intervals:[],previous:0,time:performance.now(),seq:Date.now()};samples.current.set(key,sample);}
      const cursor=value.buffer.cursor(`step:${s.step_no}`);
      const position=value.audio.textOnly?cursor.visible_chars:posRef.current;
      const now=performance.now(),delta=position-(sample.previous??position);
      const permitted=value.playing&&(value.audio.textOnly||["playing","ended"].includes(value.audio.state));
      const maximum=value.audio.textOnly?Math.max(2,(now-sample.time)*.025):Math.max(250,(now-sample.time)*1.8);
      if(permitted&&sample.previous!==null&&delta>0&&delta<=maximum){
        const last=sample.intervals.at(-1);
        if(last&&sample.previous<=last[1]+1)last[1]=position;
        else sample.intervals.push([sample.previous,position]);
        if(sample.intervals.length>100)sample.intervals.shift();
      }
      sample.previous=position;sample.time=now;
      if(now>0&&Date.now()-lastFlush>2000){lastFlush=Date.now();void flush().catch(()=>{});}
    },80);
    return()=>clearInterval(timer);
  },[conversation,flush,posRef]);
  return {flush};
}
