/** Focus only geometry which has actually been painted, never table frames. */
export function paintedFocus(elements:any[],block:any){
  if(!block)return null;
  const items=elements.filter(item=>item.customData?.blockId===block.id);
  const allTexts=items.filter(item=>item.type==="text"&&String(item.text||"").trim());
  const emphasized=allTexts.filter(item=>item.customData?.alignmentActive);
  const texts=emphasized.length?emphasized:allTexts;
  // New graphs follow the current complete relation; legacy graphs retain node focus.
  const staged=block.type==="graph"&&block.diagram!=="sequence"
    ?texts.filter(item=>item.customData?.graphStage!==undefined):[];
  const latest=staged.length?Math.max(...staged.map(item=>item.customData.graphStage)):-1;
  const current=staged.filter(item=>item.customData.graphStage===latest);
  const text=[...current].sort((a,b)=>a.y+a.height-b.y-b.height||a.x-b.x).at(-1)||
    (block.type==="graph"&&block.diagram!=="sequence"
      ?texts.filter(item=>String(item.id).includes("_node_text_")):texts).at(-1);
  if(text){
    const lines=String(text.text).split("\n"),last=lines.at(-1)||"";
    const canvas=typeof document!=="undefined"?document.createElement("canvas"):null;
    const context=canvas?.getContext("2d");
    if(context)context.font=`${text.fontSize||16}px Cascadia, Consolas, sans-serif`;
    const measured=context?.measureText(last).width??(text.width||0);
    return {x:text.x+Math.min(text.width||measured,measured),y:text.y+(lines.length-.5)*(text.fontSize||16)*(text.lineHeight||1.25)};
  }
  if(["text","table","code","formula"].includes(block.type))return null;
  const shape=items.filter(item=>item.type==="rectangle"||item.type==="ellipse").at(-1);
  return shape?{x:shape.x+shape.width/2,y:shape.y+shape.height/2}:null;
}
