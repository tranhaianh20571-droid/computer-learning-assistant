import type {BoardPage,Block} from "./boardLayout";
import {wrapText,revealLines} from "./boardText.ts";

/** Paint existing geometry without reflowing when visible text changes. */
export function presentBoard(page:BoardPage,visible:Block[],_highlights:string[]=[]){
  const byId=new Map(visible.map(block=>[block.id,block]));
  const elements=page.elements.filter(element=>{
    if(element.customData?.codeBodyText)return false;
    const id=element.customData?.blockId;if(!id)return true;
    const block=byId.get(id);if(!block)return false;
    if(block.type==="table"&&block.reveal_progress!==undefined&&element.customData?.tableRow!==undefined){
      return element.customData.tableRow===0||element.customData.tableRow<=Math.ceil(block.rows.length*block.reveal_progress);
    }
    if(block.type!=="graph"||block.reveal_progress===undefined||block.reveal_progress>=1)return true;
    if(block.title&&element.customData?.fullText===block.title)return true;
    const stages=[...block.nodes.map((node:any)=>({nodeId:node.id})),...block.edges.map((edge:any)=>({edge}))];
    const count=Math.ceil(stages.length*block.reveal_progress);
    if(element.customData?.graphStage!==undefined)return element.customData.graphStage<count;
    const nodeIndex=block.nodes.findIndex((node:any)=>element.id===`${id}_node_${node.id}`||element.id===`${id}_node_text_${node.id}`);
    if(nodeIndex>=0)return nodeIndex<count;
    const edgeIndex=Number(element.id.match(/_edge_(?:label_bg_|label_|leader_)?(\d+)$/)?.[1]);
    if(Number.isFinite(edgeIndex))return block.nodes.length+edgeIndex<count;
    const generic=page.elements.filter(item=>item.customData?.blockId===id&&(!block.title||item.customData?.fullText!==block.title));
    return generic.indexOf(element)<Math.ceil(generic.length*block.reveal_progress);
  })
    .map(element=>{
      if(!element.customData?.bodyText)return element;
      const block=byId.get(element.customData.blockId);
      const full=element.customData.fullText||block?.text||"";
      return block?{...element,text:revealLines(full,element.customData.wrappedLines||wrapText(full,element.width,element.fontSize),Array.from(block.text).length)}:element;
    });
  return elements;
}

/** Preserve restored identity/version for unchanged elements. */
export function stableElements(raw:any[],previous:Map<string,{signature:string;element:any}>,restore:(input:any[])=>any[]){
  return raw.map(spec=>{
    const signature=JSON.stringify(spec),old=previous.get(spec.id);
    if(old?.signature===signature)return old.element;
    const merged=old?{...old.element,...spec,version:old.element.version+1}:spec;
    const element=restore([merged])[0];
    previous.set(spec.id,{signature,element});
    return element;
  });
}
