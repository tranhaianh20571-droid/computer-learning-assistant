export type Subtitle={text?:string;word?:string;begin_ms:number;end_ms:number};
export function audioCharacters(text:string,subtitles:Subtitle[],positionMs:number){
  const total=Array.from(text).length;
  if(!subtitles.length)return 0;
  const joined=subtitles.map(cue=>cue.text||cue.word||"").join("");
  if(joined!==text){
    const duration=Math.max(...subtitles.map(cue=>cue.end_ms));
    return Math.min(total,Math.floor(total*Math.max(0,positionMs)/Math.max(1,duration)));
  }
  let shown=0;
  for(const cue of subtitles){
    const length=Array.from(cue.text||cue.word||"").length;
    if(positionMs>=cue.end_ms)shown+=length;
    else if(positionMs>cue.begin_ms){shown+=Math.floor(length*(positionMs-cue.begin_ms)/Math.max(1,cue.end_ms-cue.begin_ms));break;}
    else break;
  }
  return Math.min(total,shown);
}
