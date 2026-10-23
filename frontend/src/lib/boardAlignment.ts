export type BoardCue={block_id:string;target:string;start:number;end:number};

/** The saved narration references decide when a complete board phrase appears. */
export function alignedBoard(blocks:any[],beats:{cues?:BoardCue[]}[],shown:number){
  const revealed=new Map<string,Set<string>>(),active=new Map<string,Set<string>>();
  let writing:{block_id:string;target:string;progress:number}|null=null;
  for(const beat of beats)for(const cue of beat.cues||[]){
    if(shown<=cue.start)continue;
    const targets=revealed.get(cue.block_id)||new Set<string>();
    if(!targets.has(cue.target))writing={block_id:cue.block_id,target:cue.target,progress:1};
    targets.add(cue.target);revealed.set(cue.block_id,targets);
    // A diagram is one stable context; speech emphasizes its nodes/edges without
    // drawing a new feedback label above an already-explained lower node.
    if(blocks.find(block=>block.id===cue.block_id)?.type==="graph"){
      for(const item of beats.flatMap(beat=>beat.cues||[]))if(item.block_id===cue.block_id)targets.add(item.target);
    }
    if(shown<cue.end){
      const current=active.get(cue.block_id)||new Set<string>();
      current.add(cue.target);active.set(cue.block_id,current);
    }
  }
  const visible=blocks.filter(block=>revealed.has(block.id)).map(block=>{
    const targets=[...revealed.get(block.id)!],current=[...(active.get(block.id)||[])];
    const total=new Set(beats.flatMap(beat=>(beat.cues||[]).filter(cue=>cue.block_id===block.id).map(cue=>cue.target))).size;
    return {...block,reveal_progress:targets.length===total?1:.5,aligned_targets:targets,active_targets:current,
      ...(block.type==="code"?{
        code:String(block.code).split("\n").map((line,index)=>targets.includes(`line:${index+1}`)?line:"").join("\n"),
        active_lines:current.filter(target=>target.startsWith("line:")).map(target=>Number(target.slice(5))),
      }:{})};
  });
  return {visible,writing};
}

export function boardElementTarget(element:any,block:any):string|null{
  const data=element.customData||{};
  if(data.fullText===block.title&&block.title)return "title";
  if(block.type==="text"&&data.bodyText)return "text";
  if(block.type==="table"&&data.tableRow!==undefined)return `cell:${data.tableRow}:${data.tableColumn}`;
  if(block.type==="graph"){
    if(data.nodeId)return `node:${data.nodeId}`;
    if(data.edgeIndex!==undefined)return `edge:${data.edgeIndex}`;
  }
  if(data.semanticStage!==undefined){
    const items=block.cells||block.frames||block.layers||[];
    return items[data.semanticStage]?`item:${items[data.semanticStage].id}`:"note";
  }
  if(block.type==="chart")return "chart";
  return null;
}
