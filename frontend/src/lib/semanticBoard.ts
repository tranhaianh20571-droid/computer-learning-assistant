import {wrapText} from "./boardText.ts";

/** Render domain objects directly; their IDs survive reveal and highlight updates. */
export function semanticBlock(block:any,width:number,top:number){
  const elements:any[]=[];
  let sequence=0,height=0;
  const add=(type:string,data:any,stage:number)=>elements.push({type,id:`${block.id}_semantic_${sequence++}`,
    roughness:0,strokeWidth:1,strokeColor:"#252927",backgroundColor:"transparent",
    customData:{blockId:block.id,semanticStage:stage},...data});
  const text=(value:string,x:number,y:number,w:number,font:number,stage:number,color="#252927")=>{
    const lines=wrapText(value,w,font),h=lines.length*font*1.5;
    add("text",{x,y:top+y,width:w,height:h,text:lines.join("\n"),fontFamily:3,fontSize:font,lineHeight:1.5,strokeColor:color},stage);
    return h;
  };
  if(block.type==="array"){
    const columns=Math.max(1,Math.min(block.cells.length,Math.floor(width/72))),cellWidth=(width-8*(columns-1))/columns;
    const rows=Math.ceil(block.cells.length/columns);
    const rowHeight=Math.max(88,...block.cells.map((cell:any)=>wrapText(cell.value,cellWidth-12,16).length*24+52));
    block.cells.forEach((cell:any,i:number)=>{
      const x=(i%columns)*(cellWidth+8),y=Math.floor(i/columns)*rowHeight;
      const focus=cell.state==="focus",found=cell.state==="found",faint=cell.state==="discarded";
      const opacity=faint?45:100,color=focus?"#8b663a":found?"#56836b":"#252927";
      add("rectangle",{x,y:top+y,width:cellWidth,height:rowHeight-36,strokeColor:color,
        backgroundColor:focus?"#eaf0ed":"#eef3ef",fillStyle:"solid",opacity,roundness:{type:3,value:4}},i);
      text(cell.value,x+6,y+10,cellWidth-12,16,i,color);
      text([cell.index,cell.pointer].filter(Boolean).join(" · "),x,y+rowHeight-28,cellWidth,11,i);
      if(faint)for(const item of elements.filter(item=>item.customData.semanticStage===i))item.opacity=45;
    });
    height=rows*rowHeight;
    if(block.note)height+=text(block.note,0,height,width,13,block.cells.length,"#31588b")+8;
  }else if(block.type==="stack"){
    block.frames.forEach((frame:any,i:number)=>{
      const body=[...frame.variables,frame.waiting_for].filter(Boolean).join("\n");
      const bodyHeight=wrapText(body,width-32,13).length*19.5;
      const h=36+bodyHeight+16;
      add("rectangle",{x:0,y:top+height,width,height:h,strokeColor:frame.active?"#56836b":"#899d90",
        backgroundColor:frame.active?"#dce9f8":"#eef3ef",fillStyle:"solid",roundness:{type:3,value:4}},i);
      text(`${i===0?"栈顶 · ":""}${frame.label}`,16,height+10,width-32,16,i);
      text(body,16,height+38,width-32,13,i,"#31588b");
      height+=h+12;
    });
  }else if(block.type==="layers"){
    const inset=Math.min(16,Math.max(4,(width-160)/8));
    block.layers.forEach((layer:any,i:number)=>{
      const x=i*inset,w=width-x-(block.layers.length-i-1)*inset;
      const detailHeight=wrapText(layer.detail,w-32,14).length*21;
      const h=54+detailHeight;
      add("rectangle",{x:x+4,y:top+height+5,width:w-4,height:h,strokeColor:"transparent",backgroundColor:"#c5cec8",fillStyle:"solid",opacity:30},i);
      add("rectangle",{x,y:top+height,width:w-4,height:h,strokeColor:"#899d90",
        backgroundColor:i%2?"#eaf0ed":"#eef3ef",fillStyle:"solid",roundness:{type:3,value:4}},i);
      text(layer.label,x+16,height+12,w-36,17,i,"#31588b");
      text(layer.detail,x+16,height+42,w-36,14,i);
      height+=h+12;
    });
  }
  return {elements,height:height+8};
}
