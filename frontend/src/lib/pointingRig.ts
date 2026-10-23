export function fitPointer(x:number,y:number,width:number,height:number){
  if(x<0||y<0||x>width||y>height)return null;
  const candidates=[30,150,-30,-150].map(angle=>{
    const dx=Math.cos(angle*Math.PI/180),dy=Math.sin(angle*Math.PI/180);
    const horizontal=(dx>0?width-x-8:x-8)/Math.abs(dx);
    const vertical=(dy>0?height-y-8:y-8)/Math.abs(dy);
    return {x,y,angle,length:Math.max(0,Math.min(130,horizontal,vertical))};
  });
  return candidates.sort((a,b)=>b.length-a.length)[0];
}
