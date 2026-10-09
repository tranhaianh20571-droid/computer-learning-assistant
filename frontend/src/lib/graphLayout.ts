/** Measure captions before node placement; reserve space for every label. */
import { wrapGraphText, graphTextWidth } from "./boardText.ts";

export function graphMinimumWidth(block:any,columns:number){
  const nodes=block.nodes||[],edges=(block.edges||[]).length<=12?block.edges||[]:[];
  const indexes=new Map<string,number>(nodes.map((node:any,index:number)=>[node.id,index]));
  const adjacent=(edge:any)=>indexes.has(edge.from_id)&&indexes.has(edge.to_id)&&
    indexes.get(edge.to_id)===indexes.get(edge.from_id)!+columns;
  const external=edges.filter((edge:any)=>!adjacent(edge));
  const nodeRun=Math.max(0,...nodes.map((node:any)=>graphTextWidth((node.group||"")+"\n"+node.label,14)));
  const inlineRun=Math.max(0,...edges.filter(adjacent).map((edge:any)=>graphTextWidth(edge.label||"",11)));
  const outsideRun=Math.max(0,...external.map((edge:any)=>graphTextWidth(edge.label||"",11)));
  return Math.max(96,nodeRun+16,inlineRun+12)*columns+18*(columns-1)+16+
    (external.length?Math.max(76,outsideRun+22):0);
}

export function graphLayout(block: any, width: number, columns: number, font: number, gap: number,
    selected = "", highlights: string[] = []) {
  const allEdges=block.edges||[];
  const edges=allEdges.filter((edge:any)=>selected?edge.from_id===selected||edge.to_id===selected:allEdges.length<=12);
  const nodes=block.nodes,positions:Record<string,any>={},elements:any[]=[];
  const indexById=new Map<string,number>(nodes.map((node:any,index:number)=>[node.id,index]));
  const adjacent=(edge:any)=>{
    const from=indexById.get(edge.from_id),to=indexById.get(edge.to_id);
    return from!==undefined&&to!==undefined&&to===from+columns;
  };
  const external=edges.filter((edge:any)=>!adjacent(edge));
  const captionFont=11,lineHeight=captionFont*1.5;
  const captionRun=Math.max(0,...external.map((edge:any)=>graphTextWidth(edge.label||"",captionFont)));
  // A simple vertical chain needs no routing gutter: use the whole column.
  const gutter=external.length?Math.max(76,captionRun+22):0;
  const nodeWidth=Math.max(48,(width-gutter-16-(columns-1)*18)/columns);
  const nodeText=(node:any)=>(node.group?node.group+"\n":"")+node.label;
  const rowGaps:number[]=[];
  const captionHeights=new Map<any,number>();
  const laneHeights=new Map<string,number>();
  for(const edge of edges.filter(adjacent)){
    if(!edge.label)continue;
    const captionHeight=wrapGraphText(edge.label,nodeWidth-12,captionFont).length*lineHeight;
    captionHeights.set(edge,captionHeight);
    laneHeights.set(edge.from_id,(laneHeights.get(edge.from_id)||0)+captionHeight+6);
  }
  let y=4;
  for(let i=0;i<nodes.length;i+=columns){
    const row=nodes.slice(i,i+columns),rowNo=Math.floor(i/columns);
    const height=Math.max(...row.map((node:any)=>wrapGraphText(nodeText(node),nodeWidth-16,font).length))*font*1.5+16;
    row.forEach((node:any,j:number)=>positions[node.id]={x:8+j*(nodeWidth+18),y,w:nodeWidth,h:height,row:rowNo});
    const captionHeight=Math.max(0,...row.map((node:any)=>Math.max(0,(laneHeights.get(node.id)||0)-6)));
    rowGaps[rowNo]=Math.max(gap,captionHeight+20);
    y+=height+rowGaps[rowNo];
  }
  let height=Math.max(40,y-(rowGaps.at(-1)||gap)+4);
  const edgeLabels:any[]=[],occupied:{x:number;y:number;width:number;height:number}[]=[];
  const captionOffsets=new Map<string,number>();
  const intersects=(a:any,b:any)=>a.x<b.x+b.width+5&&a.x+a.width+5>b.x&&a.y<b.y+b.height+5&&a.y+a.height+5>b.y;
  edges.forEach((edge:any,index:number)=>{
    const a=positions[edge.from_id],b=positions[edge.to_id];if(!a||!b)return;
    const ax=a.x+a.w,ay=a.y+a.h/2,bx=b.x+b.w,by=b.y+b.h/2;
    const consecutive=adjacent(edge);
    let x=ax,ty=ay,points:number[][],labelX:number,labelY:number,labelWidth:number;
    if(consecutive){
      const distance=b.y-a.y-a.h;
      x=a.x+a.w/2;ty=a.y+a.h;points=[[0,0],[0,distance]];
      labelX=a.x+6;labelWidth=a.w-12;
      const captionHeight=captionHeights.get(edge)||0;
      const laneHeight=Math.max(0,(laneHeights.get(edge.from_id)||0)-6);
      labelY=ty+(distance-laneHeight)/2+(captionOffsets.get(edge.from_id)||0);
      if(edge.label)captionOffsets.set(edge.from_id,(captionOffsets.get(edge.from_id)||0)+captionHeight+6);
    } else {
      const route=width-gutter+4+(index%2)*6;
      points=edge.from_id===edge.to_id?[[0,0],[route-ax,0],[route-ax,24],[0,24]]:
        [[0,0],[route-ax,0],[route-ax,by-ay],[bx-ax,by-ay]];
      labelX=route+5;labelWidth=Math.max(36,width-labelX-4);
      const captionHeight=wrapGraphText(edge.label||"",labelWidth,captionFont).length*lineHeight;
      labelY=Math.max(0,(ay+by)/2-captionHeight/2);
      if(edge.label){
        // Captions in the outside lane avoid one another. A leader preserves
        // the association with the original edge after displacement.
        const originalY=labelY;
        let conflict=occupied.find(box=>intersects({x:labelX-2,y:labelY-1,width:labelWidth+4,height:captionHeight+2},box));
        while(conflict){
          labelY=conflict.y+conflict.height+7;
          conflict=occupied.find(box=>intersects({x:labelX-2,y:labelY-1,width:labelWidth+4,height:captionHeight+2},box));
        }
        if(labelY!==originalY)elements.push({type:"line",id:`${block.id}_edge_leader_${index}`,
          x:route,y:originalY+captionHeight/2,width:Math.max(1,labelX-route),height:labelY-originalY,
          points:[[0,0],[labelX-route,labelY-originalY]],strokeColor:"#31588b",strokeWidth:1,
          backgroundColor:"transparent",roughness:0});
      }
    }
    const minX=Math.min(...points.map(point=>point[0])),maxX=Math.max(...points.map(point=>point[0]));
    const minY=Math.min(...points.map(point=>point[1])),maxY=Math.max(...points.map(point=>point[1]));
    elements.push({type:"arrow",id:`${block.id}_edge_${index}`,x,y:ty,
      width:Math.max(1,maxX-minX),height:Math.max(1,maxY-minY),points,endArrowhead:"arrow",
      strokeColor:"#31588b",strokeWidth:1,backgroundColor:"transparent",roughness:0,
      customData:{blockId:block.id,edgeFrom:edge.from_id,edgeTo:edge.to_id}});
    if(edge.label){
      const lines=wrapGraphText(edge.label,labelWidth,captionFont),lh=lines.length*lineHeight;
      height=Math.max(height,labelY+lh+4);
      occupied.push({x:labelX-2,y:labelY-1,width:labelWidth+4,height:lh+2});
      edgeLabels.push({type:"rectangle",id:`${block.id}_edge_label_bg_${index}`,x:labelX-2,y:labelY-1,
        width:labelWidth+4,height:lh+2,strokeColor:"transparent",backgroundColor:"#fcfcfb",fillStyle:"solid",roughness:0},
        {type:"text",id:`${block.id}_edge_label_${index}`,x:labelX,y:labelY,width:labelWidth,height:lh,
          text:lines.join("\n"),fontSize:captionFont,fontFamily:3,lineHeight:1.5,strokeColor:"#31588b",
          customData:{blockId:block.id,edgeFrom:edge.from_id,edgeTo:edge.to_id}});
    }
  });
  nodes.forEach((node:any)=>{
    const pos=positions[node.id],active=selected===node.id||highlights.includes(node.id);
    elements.push({type:"rectangle",id:`${block.id}_node_${node.id}`,x:pos.x,y:pos.y,width:pos.w,height:pos.h,
      strokeColor:active?"#31588b":"#899d90",backgroundColor:active?"#dce9f8":"#eef3ef",fillStyle:"solid",roughness:0},
      {type:"text",id:`${block.id}_node_text_${node.id}`,x:pos.x+8,y:pos.y+8,width:pos.w-16,height:pos.h-16,
        text:wrapGraphText(nodeText(node),pos.w-16,font).join("\n"),fontSize:font,fontFamily:3,lineHeight:1.5,strokeColor:"#252927"});
  });
  elements.push(...edgeLabels);
  if(allEdges.length>12){
    const text=selected?"显示所选节点的相关连接":"连接较多：选择节点查看相关连接";
    elements.push({type:"text",id:`${block.id}_detail_hint`,x:0,y:height+4,width,height:36,
      text,fontSize:12,fontFamily:3,lineHeight:1.5,strokeColor:"#31588b"});height+=44;
  }
  return {elements,height,font};
}
