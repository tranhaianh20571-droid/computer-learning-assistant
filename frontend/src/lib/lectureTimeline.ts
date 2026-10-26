import {audioCharacters,type Subtitle} from "./audioTiming.ts";

export type LecturePosition={step_no:number;visible_chars:number;position_ms:number};
export function comparePosition(a:LecturePosition,b:LecturePosition){
  return a.step_no-b.step_no||a.visible_chars-b.visible_chars||a.position_ms-b.position_ms;
}
export function timelineValue(position:LecturePosition,steps:{step_no:number;text:string}[]){
  const text=steps.find(step=>step.step_no===position.step_no)?.text||"";
  return position.step_no-1+position.visible_chars/Math.max(1,Array.from(text).length);
}
export function timelinePosition(value:number,steps:{step_no:number;text:string;audio_subtitles?:Subtitle[]}[],ceiling:LecturePosition){
  const stepNo=Math.floor(Math.max(0,value))+1,step=steps.find(s=>s.step_no===stepNo);
  if(!step)return null;
  const length=Array.from(step.text).length;
  const chars=Math.min(length,Math.max(0,Math.floor((value-(stepNo-1))*length)));
  const subtitles=step.audio_subtitles||[];
  let positionMs=0;
  if(subtitles.length){
    // Find the earliest audio position that reveals the requested cursor.
    let lo=0,hi=Math.max(...subtitles.map(cue=>cue.end_ms));
    while(lo<hi){const mid=Math.floor((lo+hi)/2);
      if(audioCharacters(step.text,subtitles,mid)>=chars)hi=mid;else lo=mid+1;}
    positionMs=lo;
  }
  const target={step_no:stepNo,visible_chars:chars,position_ms:positionMs};
  return comparePosition(target,ceiling)<=0?target:null;
}
