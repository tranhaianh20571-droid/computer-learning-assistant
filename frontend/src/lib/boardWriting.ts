import type {Block,BoardPage,Placement} from './boardLayout';
import {presentBoard} from './boardPresentation.ts';

/** Use the same fixed geometry and reveal stages as the actual drawing. */
export function boardWritingY(page:BoardPage,placement:Placement,block:Block,progress=1){
  const items=page.elements.filter(e=>e.customData?.blockId===block.id);
  if(block.type==='text'){
    const body=items.find(e=>e.customData?.bodyText);
    const partial=body?presentBoard(page,[block]).find(e=>e.id===body.id):null;
    return body?body.y+Math.max(0,(partial?.text?.split('\n').length||1)-1)*body.fontSize*body.lineHeight+body.fontSize*body.lineHeight/2:placement.y;
  }
  if(placement.codeBody)return placement.codeBody.y+Math.max(0,String(block.code).split('\n').length-1)*21+10;
  if(placement.formulaBody)return placement.formulaBody.y+placement.formulaBody.height/2;
  if(block.type==='table'){
    const row=Math.ceil(block.rows.length*Math.max(0,Math.min(1,progress)));
    const cell=items.find(e=>e.type==='rectangle'&&e.customData?.tableRow===row);
    if(cell)return cell.y+cell.height/2;
  }
  if(block.type==='graph'){
    const count=Math.max(1,Math.ceil((block.nodes.length+block.edges.length)*progress));
    let element;
    if(block.diagram==='sequence')element=items.find(e=>e.customData?.graphStage===count-1&&e.type===(count>block.nodes.length?'arrow':'rectangle'));
    else {
      const id=count>block.nodes.length?`${block.id}_edge_${count-block.nodes.length-1}`:`${block.id}_node_${block.nodes[count-1]?.id}`;
      element=items.find(e=>e.id===id);
    }
    if(element)return element.y+element.height/2;
  }
  // Legacy and unsupported blocks have no trustworthy incremental cursor.
  return placement.y+Math.min(placement.height/2,120);
}
