const characterWidth=(c:string,font:number)=>/[\u2e80-\uffff]/u.test(c)?font:font*.57;
let context:CanvasRenderingContext2D|null=null;
let fontFamily="Cascadia, monospace";
export function setBoardFontFamily(family:string){fontFamily=family;}
export function measureBoardText(value:string,font:number){
  if(typeof document==="undefined")return Array.from(value).reduce((sum,c)=>sum+characterWidth(c,font),0);
  context??=document.createElement("canvas").getContext("2d");
  if(!context)return value.length*font;
  context.font=`${font}px ${fontFamily}`;
  return context.measureText(value).width;
}
export function boardFontFamily(){return fontFamily;}
export const textWidth=(value:string,font:number)=>Array.from(value).reduce((sum,c)=>sum+(/[\u2e80-\uffff]/u.test(c)?font*1.02:font*.64),0);

// Graph labels keep words, filenames and short commands together. Other board
// blocks retain their existing wrapping rules.
export function graphTextWidth(value:string,font:number){
  const runs=String(value).match(/[A-Za-z0-9_./:\\@+=$%~\-]+(?:[ \t]+[A-Za-z0-9_./:\\@+=$%~\-]+)*/g)||[];
  return Math.max(0,...runs.map(run=>textWidth(run,font)));
}
export function wrapGraphText(value:string,width:number,font=16){
  const lines:string[]=[];
  for(const line of String(value).replace(/\r\n/g,"\n").split("\n")){
    const tokens=line.match(/[A-Za-z0-9_./:\\@+=$%~\-]+(?:[ \t]+[A-Za-z0-9_./:\\@+=$%~\-]+)*|./gu)||[];
    let buffer="",used=0;
    for(const token of tokens){
      const advance=textWidth(token,font);
      if(buffer&&used+advance>width){lines.push(buffer.trimEnd());buffer="";used=0;}
      if(!buffer&&/^\s+$/.test(token))continue;
      buffer+=token;used+=advance;
    }
    lines.push(buffer.trimEnd());
  }
  return lines;
}
export function wrapText(value:string,width:number,font=16){
  const lines:string[]=[];
  for(const line of String(value).replace(/\r\n/g,"\n").split("\n")){
    let buffer="";
    // Keep identifiers and comparison operators together when they fit a line.
    const tokens=line.match(/[A-Za-z0-9_]+|<=|>=|==|!=|&&|\|\||[^]/gu)||[];
    for(const token of tokens){
      if(measureBoardText(token,font)>width){
        for(const c of Array.from(token)){
          if(buffer&&measureBoardText(buffer+c,font)>width-2){lines.push(buffer);buffer="";}
          buffer+=c;
        }
      }else{
        if(buffer&&measureBoardText(buffer+token,font)>width-2){lines.push(buffer);buffer="";}
        buffer+=token;
      }
    }
    lines.push(buffer);
  }
  return lines;
}

/** Clip prewrapped lines using the original source cursor, without rewrapping a prefix. */
export function revealLines(full:string,lines:string[],count:number){
  let remaining=count,source=Array.from(full.replace(/\r\n/g,"\n")),cursor=0;
  const shown:string[]=[];
  for(const line of lines){
    if(remaining<=0)break;
    const chars=Array.from(line),take=Math.min(remaining,chars.length);
    shown.push(chars.slice(0,take).join(""));remaining-=take;cursor+=chars.length;
    if(take<chars.length)break;
    if(source[cursor]==="\n"){cursor++;remaining--;}
  }
  return shown.join("\n");
}
