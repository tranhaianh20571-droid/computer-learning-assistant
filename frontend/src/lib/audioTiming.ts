export type Subtitle={text?:string;word?:string;begin_ms:number;end_ms:number;display_start?:number;display_end?:number};
export function exactAudioAlignment(text:string,subtitles:Subtitle[]){
  const chars=Array.from(text);
  let cursor=0,time=0;
  if(!subtitles.length)return false;
  const mapped=subtitles.every(cue=>Number.isInteger(cue.display_start)&&Number.isInteger(cue.display_end));
  if(!mapped&&subtitles.map(cue=>cue.text||cue.word||"").join("")!==text)return false;
  for(const cue of subtitles){
    if(!Number.isFinite(cue.begin_ms)||!Number.isFinite(cue.end_ms)||cue.begin_ms<time||cue.end_ms<=cue.begin_ms)return false;
    const value=cue.text||cue.word||"";
    const start=cue.display_start??cursor,end=cue.display_end??(start+Array.from(value).length);
    if(!value||!Number.isInteger(start)||!Number.isInteger(end)||start<cursor||end>chars.length||end<=start
      ||chars.slice(start,end).join("")!==value||!/^[\s\p{P}]*$/u.test(chars.slice(cursor,start).join("")))return false;
    cursor=end;time=cue.end_ms;
  }
  return /^[\s\p{P}]*$/u.test(chars.slice(cursor).join(""));
}
export function audioCharacters(text:string,subtitles:Subtitle[],positionMs:number,strict=false){
  if(strict&&!exactAudioAlignment(text,subtitles))return 0;
  const total=Array.from(text).length;
  if(!subtitles.length)return 0;
  if(subtitles.every(cue=>Number.isInteger(cue.display_start)&&Number.isInteger(cue.display_end))){
    let shown=0;
    for(const cue of subtitles){
      const start=cue.display_start!,end=cue.display_end!;
      if(start<shown||end<start||end>total)return shown;
      if(positionMs>=cue.end_ms)shown=end;
      else if(positionMs>cue.begin_ms){shown=start+Math.max(strict?1:0,Math.floor((end-start)*(positionMs-cue.begin_ms)/Math.max(1,cue.end_ms-cue.begin_ms)));break;}
      else break;
    }
    return Math.min(total,shown);
  }
  const joined=subtitles.map(cue=>cue.text||cue.word||"").join("");
  if(joined!==text){
    const duration=Math.max(...subtitles.map(cue=>cue.end_ms));
    return Math.min(total,Math.floor(total*Math.max(0,positionMs)/Math.max(1,duration)));
  }
  let shown=0;
  for(const cue of subtitles){
    const length=Array.from(cue.text||cue.word||"").length;
    if(positionMs>=cue.end_ms)shown+=length;
    else if(positionMs>cue.begin_ms){shown+=Math.max(strict?1:0,Math.floor(length*(positionMs-cue.begin_ms)/Math.max(1,cue.end_ms-cue.begin_ms)));break;}
    else break;
  }
  return Math.min(total,shown);
}
