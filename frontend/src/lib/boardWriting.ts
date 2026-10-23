import type {Block,BoardPage,Placement} from './boardLayout';
import {presentBoard} from './boardPresentation.ts';
import {graphStages} from './graphStages.ts';

export function codeFocusLine(block:Block){
  const lines=String(block.code||"").split("\n");
  const active=(block.active_lines||[]).filter((line:number)=>Number.isInteger(line)&&line>0&&line<=lines.length&&lines[line-1].trim());
  if(active.length)return active.at(-1);
  for(let index=lines.length-1;index>=0;index--)if(lines[index].trim())return index+1;
  return null;
}

/** Use the same fixed geometry and reveal stages as the actual drawing. */
export function boardWritingY(page:BoardPage,placement:Placement,block:Block,progress=1){
  const items=page.elements.filter(e=>e.customData?.blockId===block.id);
  if(block.type==='text'){
    const body=items.find(e=>e.customData?.bodyText);
    const partial=body?presentBoard(page,[block]).find(e=>e.id===body.id):null;
    return body?body.y+Math.max(0,(partial?.text?.split('\n').length||1)-1)*body.fontSize*body.lineHeight+body.fontSize*body.lineHeight/2:placement.y;
  }
  if(placement.codeBody)return placement.codeBody.y+Math.max(0,(codeFocusLine(block)||1)-1)*21+10;
  if(placement.formulaBody)return placement.formulaBody.y+placement.formulaBody.height/2;
  if(placement.figureBody)return placement.figureBody.y+placement.figureBody.height/2;
  if(["array","stack","layers"].includes(block.type)){
    const visible=presentBoard(page,[{...block,reveal_progress:progress}]);
    const current=visible.filter(item=>item.customData?.blockId===block.id).at(-1);
    if(current)return current.y+current.height/2;
  }
  if(block.type==='table'){
    const rendered=presentBoard(page,[{...block,reveal_progress:progress}]);
    const cell=rendered.filter(e=>e.customData?.tableCellOrder!==undefined&&e.text).at(-1)
      ||items.find(e=>e.customData?.elementRole==='table-frame');
    if(cell)return cell.y+cell.height/2;
  }
  if(block.type==='graph'){
    const total=block.diagram==='sequence'?block.nodes.length+block.edges.length:
      items.find(item=>item.customData?.graphStageCount!==undefined)?.customData.graphStageCount??graphStages(block).total;
    const count=Math.max(1,Math.ceil(total*progress));
    let element;
    if(block.diagram==='sequence')element=items.find(e=>e.customData?.graphStage===count-1&&e.type===(count>block.nodes.length?'arrow':'rectangle'));
    else {
      const current=items.filter(e=>e.customData?.graphStage===count-1);
      element=current.find(e=>e.type==='arrow')||current.find(e=>e.customData?.edgeIndex!==undefined)||
        current.find(e=>e.customData?.nodeId);
    }
    if(element)return element.y+element.height/2;
  }
  // Legacy and unsupported blocks have no trustworthy incremental cursor.
  return placement.y+Math.min(placement.height/2,120);
}
