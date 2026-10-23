import type {BoardPage,Block} from "./boardLayout";
import {wrapText,revealLines} from "./boardText.ts";
import {boardElementTarget} from "./boardAlignment.ts";

/** Paint existing geometry without reflowing when visible text changes. */
export function presentBoard(page:BoardPage,visible:Block[],_highlights:string[]=[]){
  const byId=new Map(visible.map(block=>[block.id,block]));
  const activeColor=page.elements.find(element=>element.id==="page_title")?.strokeColor==="#f1f3e9"?"#ffe5a3":"#8b4c0a";
  const elements=page.elements.filter(element=>{
    if(element.customData?.codeBodyText)return false;
    const id=element.customData?.blockId;if(!id)return true;
    const block=byId.get(id);if(!block)return false;
    if(block.aligned_targets){
      const target=boardElementTarget(element,block);
      if(target){
        if(!block.aligned_targets.includes(target))return false;
        if(block.type==="graph"&&element.customData?.edgeIndex!==undefined){
          const edge=block.edges[element.customData.edgeIndex];
          return block.aligned_targets.includes(`node:${edge.from_id}`)&&block.aligned_targets.includes(`node:${edge.to_id}`);
        }
        return true;
      }
      if(block.type==="table")return block.aligned_targets.some((value:string)=>value.startsWith("cell:"));
      if(block.type==="code")return block.aligned_targets.some((value:string)=>value.startsWith("line:"));
    }
    if(element.customData?.semanticStage!==undefined&&block.reveal_progress!==undefined){
      const total=(block.cells||block.frames||block.layers||[]).length+(block.note?1:0);
      return element.customData.semanticStage<Math.ceil(total*block.reveal_progress);
    }
    if(block.type==="table"&&block.reveal_progress!==undefined){
      const progress=Math.max(0,Math.min(1,block.reveal_progress));
      if(element.customData?.elementRole==="table-frame")return progress>0;
      if(element.customData?.tableCellOrder!==undefined)return progress>.16;
    }
    if(block.type!=="graph"||block.reveal_progress===undefined||block.reveal_progress>=1)return true;
    if(block.title&&element.customData?.fullText===block.title)return true;
    const stages=[...block.nodes.map((node:any)=>({nodeId:node.id})),...block.edges.map((edge:any)=>({edge}))];
    const count=Math.ceil((element.customData?.graphStageCount??stages.length)*Math.max(0,block.reveal_progress));
    if(element.customData?.graphStage!==undefined)return element.customData.graphStage<count;
    const nodeIndex=block.nodes.findIndex((node:any)=>element.id===`${id}_node_${node.id}`||element.id===`${id}_node_text_${node.id}`);
    if(nodeIndex>=0)return nodeIndex<count;
    const edgeIndex=Number(element.id.match(/_edge_(?:label_bg_|label_|leader_)?(\d+)$/)?.[1]);
    if(Number.isFinite(edgeIndex))return block.nodes.length+edgeIndex<count;
    const generic=page.elements.filter(item=>item.customData?.blockId===id&&(!block.title||item.customData?.fullText!==block.title));
    return generic.indexOf(element)<Math.ceil(generic.length*block.reveal_progress);
  })
    .map(element=>{
      const aligned=byId.get(element.customData?.blockId);
      if(aligned?.aligned_targets){
        const active=aligned.active_targets?.includes(boardElementTarget(element,aligned));
        return active?{...element,strokeColor:activeColor,strokeWidth:2,
          customData:{...element.customData,alignmentActive:true}}:element;
      }
      const table=byId.get(element.customData?.blockId);
      if(table?.type==="table"&&table.reveal_progress!==undefined&&table.reveal_progress<1){
        const progress=Math.max(0,table.reveal_progress);
        if(element.customData?.elementRole==="table-frame"){
          const local=Math.max(0,Math.min(1,progress/.16*element.customData.edgeCount-element.customData.edgeOrder));
          const [first,last]=element.points;
          return {...element,points:[first,[last[0]*local,last[1]*local]],width:element.width*local,height:element.height*local,opacity:local>0?100:0};
        }
        if(element.customData?.tableCellOrder!==undefined){
          const cells=page.elements.filter(item=>item.customData?.blockId===table.id&&item.customData?.tableCellOrder!==undefined);
          const total=cells.reduce((sum,item)=>sum+Array.from(item.customData.fullText||"").length,0);
          const before=cells.filter(item=>item.customData.tableCellOrder<element.customData.tableCellOrder).reduce((sum,item)=>sum+Array.from(item.customData.fullText||"").length,0);
          const visible=Math.max(0,Math.floor((progress-.16)/.84*total)-before);
          return {...element,text:revealLines(element.customData.fullText,element.customData.wrappedLines,visible)};
        }
      }
      if(!element.customData?.bodyText)return element;
      const block=byId.get(element.customData.blockId);
      const full=element.customData.fullText||block?.text||"";
      return block?{...element,text:revealLines(full,element.customData.wrappedLines||wrapText(full,element.width,element.fontSize),Array.from(block.text).length)}:element;
    });
  // Empty text is restored as a deleted element by Excalidraw.
  return elements.filter(element=>element.type!=="text"||String(element.text||"").trim());
}

/** Preserve restored identity/version for unchanged elements. */
export function stableElements(raw:any[],previous:Map<string,{signature:string;element:any}>,restore:(input:any[])=>any[]){
  return raw.map(input=>{
    // Restored text and temporary frame opacity belong to the current reveal.
    const spec={...input,isDeleted:input.isDeleted??false,opacity:input.opacity??100,
      ...(input.type==="text"?{originalText:input.text}: {})};
    const signature=JSON.stringify(spec),old=previous.get(spec.id);
    if(old?.signature===signature)return old.element;
    const merged=old?{...old.element,...spec,version:old.element.version+1}:spec;
    const element=restore([merged])[0];
    previous.set(spec.id,{signature,element});
    return element;
  });
}
