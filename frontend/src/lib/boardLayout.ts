/** Deterministic three-column pages. Typed semantics are never rewritten. */
export type Block = { id: string; type: string; title?: string; span?: number; formula?:string; format?:string; plain_text?:string; [key: string]: any };
export type Placement = { group: string; id: string; column: number; span: number; x: number; y: number; width: number; height: number;
  formulaBody?: {x:number;y:number;width:number;height:number};codeBody?:{x:number;y:number;width:number;height:number};
  figureBody?:{x:number;y:number;width:number;height:number} };
export type GroupAnchor = {page:number;column:number;span:number;y:number};
export type GroupPlacement = GroupAnchor & {id:string;height:number;block_ids:string[]};
export type BoardPage = { elements:any[];placements:Placement[];groups:GroupPlacement[];height:number };
export type LayoutOptions = {pageHeight?:number;groups?:{id:string;block_ids:string[]}[];anchors?:Record<string,GroupAnchor>;
  formulas?:Record<string,FormulaMeasurement>;responsive?:boolean;theme?:"paper"|"ink"};
const INK = "#252927", ACCENT = "#31588b", PAPER = "#fcfcfb";
import {wrapText,wrapGraphText,graphTextWidth,revealLines} from "./boardText.ts";
import {graphLayout,graphMinimumWidth} from "./graphLayout.ts";
import {boardGroups} from "./boardGroups.ts";
import type {FormulaMeasurement} from "./formula.ts";
import {semanticBlock} from "./semanticBoard.ts";
export {wrapText} from "./boardText.ts";

export function layoutBoard(blocks: Block[], availableWidth: number, visibleIds: Set<string>,
    highlights: string[] = [], focus: Record<string,string> = {}, title = "", revealed: Record<string,Block> = {},options:LayoutOptions={}) {
  const width=Math.max(options.responsive?240:640, Math.round(availableWidth)), margin=14, gap=20;
  const columns=options.responsive?(width<640?1:width<1100?2:3):3;
  const columnWidth=(width-2*margin-(columns-1)*gap)/columns;
  const titleFont=options.theme==="ink"?24:18;
  const topStart=18+wrapText(title.replace(/\s+/g," "),width-2*margin,titleFont).length*titleFont*1.5;
  const pageHeight=Math.max(topStart+120,options.pageHeight||600);
  const bottom=pageHeight-24;
  const pages:BoardPage[]=[];
  let page:BoardPage,column=0,heights:number[]=[];
  const newPage=()=>{page={elements:[],placements:[],groups:[],height:pageHeight};pages.push(page);column=0;heights=Array(columns).fill(topStart);};
  const spanFor=(block:Block)=>{
    let span=Math.min(columns,Math.max(1,block.span||1));
    if(block.type==="table"&&block.columns.length>=3)span=Math.max(2,span);
    if(block.type==="graph"&&block.nodes.length>6)span=Math.max(2,span);
    if(block.type==="graph"&&block.diagram==="sequence")span=block.nodes.length>3?3:2;
    if(block.type==="graph"){
      const required=()=>block.diagram==="sequence"
        ?block.nodes.length*Math.max(96,...block.nodes.map((node:any)=>graphTextWidth(node.label,13)+20))
        :graphMinimumWidth(block,span===1?1:2);
      while(span<columns&&span*columnWidth+(span-1)*gap<required())span++;
    }
    if(block.type==="chart"&&block.labels.length>6)span=Math.max(2,span);
    if(block.type==="formula")while(span<columns&&span*columnWidth+(span-1)*gap<(options.formulas?.[block.id]?.naturalWidth||0))span++;
    if(options.responsive&&block.type==="text"&&wrapText(block.text,columnWidth,16).length>3)span=columns;
    if(options.responsive&&block.type==="code"){
      span=columns;
    }
    if(["array","stack","layers"].includes(block.type))span=columns;
    if(block.type==="figure")span=columns;
    return Math.min(columns,span);
  };
  newPage();
  const byId=new Map(blocks.map(block=>[block.id,block]));
  const groups=options.groups||boardGroups(blocks);
  for(const group of groups){
    const members=group.block_ids.map(id=>byId.get(id)).filter((block):block is Block=>Boolean(block));
    if(!members.length)continue;
    const savedAnchor=options.anchors?.[group.id];
    const currentPage=pages.indexOf(page!);
    const anchor=savedAnchor&&(savedAnchor.page>currentPage||
      savedAnchor.page===currentPage&&savedAnchor.column>=column)?savedAnchor:undefined;
    const naturalSpan=Math.max(...members.map(spanFor));
    // Appending a wide graph must not reflow already-presented paragraphs.
    const span=anchor&&anchor.column+anchor.span<=columns?anchor.span:naturalSpan;
    const bw=span*columnWidth+(span-1)*gap;
    const rendered:{block:Block;elements:any[];height:number;formulaBody?:{x:number;y:number;width:number;height:number}}[]=[];
    for(const block of members){
    const local:any[]=[];
    let seq=0, y=0;
    let formulaBody;
    const add=(type:string,data:any,id?:string)=>local.push({type,id:id||`${block.id}_${seq++}`,strokeColor:INK,
      backgroundColor:"transparent",strokeWidth:1,roughness:0,customData:{blockId:block.id},...data});
    const text=(value:string,x:number,ty:number,w:number,font=16,color=INK,id?:string)=>{
      const lines=wrapText(value,w,font),height=Math.max(1,lines.length)*font*1.5;
      const displayed=block.type==="text" && value===block.text && revealed[block.id]
        ? revealLines(value,lines,Array.from(revealed[block.id].text).length).split("\n") : lines;
      add("text",{x,y:ty,width:w,height,text:displayed.join("\n"),fontSize:font,fontFamily:3,
        customData:{blockId:block.id,bodyText:block.type==="text"&&value===block.text,
          codeBodyText:options.responsive&&block.type==="code"&&value===block.code,fullText:value,wrappedLines:lines},
        lineHeight:1.5,strokeColor:color},id);return height;
    };
    if (block.title) y+=text(block.title,0,y,bw,18)+8;
    if (block.type==="text") y+=text(block.text,0,y,bw);
    else if (block.type==="code") {
      if (block.language) y+=text(block.language,10,y,bw-20,12,ACCENT)+6;
      const h=options.responsive?(block.execution_height||String(block.code).replace(/\r\n/g,"\n").split("\n").length*21+18):text(block.code,10,y+8,bw-20,14);
      if(options.responsive)formulaBody={x:10,y:y+8,width:bw-20,height:h};
      local.unshift({type:"rectangle",id:`${block.id}_code_bg`,x:0,y,width:bw,height:h+16,
        customData:{blockId:block.id,elementRole:"code-frame"},
        strokeColor:"#d7ddd9",backgroundColor:"#f0f3f1",fillStyle:"solid",roughness:0});
      y+=h+16;
    } else if (block.type==="formula") {
      const height=options.formulas?.[block.id]?.heights[span-1]||48;
      formulaBody={x:0,y,width:options.formulas?.[block.id]?.widths?.[span-1]||bw,height};
      y+=height;
    }
    else if(block.type==="figure"){
      const height=block.graphic.kind==="surface"?400:block.graphic.kind==="circuit"?Math.max(300,Math.min(600,block.graphic.inputs.length*66+40)):340;
      formulaBody={x:0,y,width:bw,height};
      y+=height;
    }
    else if (block.type==="table") {
      const rows=[block.columns,...block.rows],cellWidth=bw/block.columns.length;
      let cellOrder=0;
      const gridTop=y;
      const rowEnds:number[]=[];
      rows.forEach((row:string[],ri:number)=>{
        const height=Math.max(...row.map(cell=>wrapText(cell,cellWidth-16,14).length))*21+16;
        row.forEach((cell:string,ci:number)=>{
          text(cell,ci*cellWidth+8,y+8,cellWidth-16,14,INK,`${block.id}_cell_${ri}_${ci}`);
          const element=local[local.length-1];
          element.customData={...element.customData,tableRow:ri,tableColumn:ci,tableCellOrder:cellOrder++,
            cellId:`${block.id}:r${ri}:c${ci}`,elementRole:"cell-text"};
        });
        y+=height;
        rowEnds.push(y);
      });
      const edges=[[[0,gridTop],[bw,gridTop]],[[bw,gridTop],[bw,y]],[[bw,y],[0,y]],[[0,y],[0,gridTop]],
        ...rowEnds.slice(0,-1).map(ey=>[[0,ey],[bw,ey]]),
        ...Array.from({length:block.columns.length-1},(_,i)=>[[(i+1)*cellWidth,gridTop],[(i+1)*cellWidth,y]])];
      edges.forEach(([start,end],index)=>add("line",{x:start[0],y:start[1],width:Math.abs(end[0]-start[0]),height:Math.abs(end[1]-start[1]),
        points:[[0,0],[end[0]-start[0],end[1]-start[1]]],strokeColor:"#8d9e91",
        customData:{blockId:block.id,elementRole:"table-frame",edgeOrder:index,edgeCount:edges.length,cellCount:cellOrder}},`${block.id}_grid_${index}`));
    } else if (block.type==="graph") {
      if(block.diagram==="sequence") {
        const nodes=block.nodes,edges=block.edges||[],laneW=bw/nodes.length;
        const headerH=Math.max(...nodes.map((n:any)=>wrapGraphText(n.label,laneW-20,13).length))*19.5+16;
        const labelGeometry=edges.map((edge:any)=>{
          const from=nodes.findIndex((node:any)=>node.id===edge.from_id),to=nodes.findIndex((node:any)=>node.id===edge.to_id);
          const labelWidth=Math.max(laneW-12,Math.abs(to-from)*laneW-12);
          const lines=edge.label?wrapGraphText(edge.label,labelWidth,12):[];
          return {labelWidth,lines,height:lines.length*18};
        });
        const messageHeights=labelGeometry.map((caption:any,i:number)=>Math.max(52,caption.height+18+(edges[i].from_id===edges[i].to_id?24:0)));
        const timelineHeight=messageHeights.reduce((sum:number,h:number)=>sum+h,0)+30;
        const positions:Record<string,number>={};
        nodes.forEach((node:any,i:number)=>{
          const start=local.length;
          const x=i*laneW+4;positions[node.id]=i*laneW+laneW/2;
          add("rectangle",{x,y,width:laneW-8,height:headerH,strokeColor:ACCENT,backgroundColor:"#eef3ef",fillStyle:"solid"});
          add("text",{x:x+6,y:y+8,width:laneW-20,height:headerH-16,
            text:wrapGraphText(node.label,laneW-20,13).join("\n"),fontSize:13,fontFamily:3,lineHeight:1.5,strokeColor:INK});
          add("line",{x:positions[node.id],y:y+headerH,width:1,height:timelineHeight,
            points:[[0,0],[0,timelineHeight]],strokeColor:"#acb9af",strokeStyle:"dashed"});
          for(const element of local.slice(start))element.customData={...element.customData,graphStage:i,nodeId:node.id};
        });
        y+=headerH+12;
        edges.forEach((edge:any,i:number)=>{
          const start=local.length;
          const caption=labelGeometry[i],ax=positions[edge.from_id],bx=positions[edge.to_id],my=y+caption.height+6;
          const points=ax===bx?[[0,0],[16,0],[16,24],[0,24]]:[[0,0],[bx-ax,0]];
          add("arrow",{x:ax,y:my,width:Math.max(1,Math.abs(bx-ax)),height:ax===bx?24:1,points,endArrowhead:"arrow",strokeColor:ACCENT});
          if(edge.label){
            const labelX=Math.min(bw-caption.labelWidth,Math.min(ax,bx)+6);
            add("rectangle",{x:labelX-2,y:y-1,width:caption.labelWidth+4,height:caption.height+2,strokeColor:"transparent",backgroundColor:PAPER,fillStyle:"solid"});
            add("text",{x:labelX,y,width:caption.labelWidth,height:caption.height,text:caption.lines.join("\n"),fontSize:12,fontFamily:3,lineHeight:1.5,strokeColor:ACCENT});
          }
          for(const element of local.slice(start))element.customData={...element.customData,graphStage:nodes.length+i,edgeIndex:i};
          y+=messageHeights[i];
        });
        y+=16;
      } else {
      const columns=span===1?1:2;
      const graph=graphLayout(block,bw,columns,14,32,focus[block.id]||"",highlights);
      local.push(...graph.elements.map(element=>({...element,y:element.y+y,
        customData:{...element.customData,blockId:block.id}})));
      y+=graph.height;
      }
    } else if (["array","stack","layers"].includes(block.type)) {
      const rendered=semanticBlock(block,bw,y);
      local.push(...rendered.elements);
      y+=rendered.height;
    } else if (block.type==="chart") {
      const labels=block.labels,series=block.values;
      y+=text(block.is_example?"教学示例数据":`数据依据：${block.source}`,0,y,bw,12,ACCENT)+8;
      if(block.chart==="heatmap") {
        const cellW=(bw-52)/labels.length,maximum=Math.max(1,...series.flat().map((v:number)=>Math.abs(v)));
        series.forEach((row:number[],ri:number)=>{
          if(block.series_labels?.[ri]) text(block.series_labels[ri],0,y+ri*42+12,48,11);
          row.forEach((v:number,ci:number)=>{
            const intensity=Math.round(245-130*Math.abs(v)/maximum);
            add("rectangle",{x:52+ci*cellW,y:y+ri*42,width:cellW,height:42,
              backgroundColor:options.theme==="ink"
                ?`rgb(${32+Math.round(32*Math.abs(v)/maximum)},${68+Math.round(30*Math.abs(v)/maximum)},${53+Math.round(24*Math.abs(v)/maximum)})`
                :`rgb(${intensity},${Math.min(255,intensity+10)},245)`,fillStyle:"solid",strokeColor:"#fff"});
            text(String(v),56+ci*cellW,y+ri*42+10,cellW-8,12);
          });
        });y+=series.length*42+8;
        labels.forEach((label:string,i:number)=>text(label,52+i*cellW,y,cellW-4,11));y+=52;
      } else {
        const left=40,plotH=160,plotW=bw-left-8;
        const values=series.flat(),min=Math.min(0,...values);
        const max=Math.max(0,...values) || (min===0?1:0);
        const scale=Math.max(Math.abs(min),Math.abs(max))||1,range=max/scale-min/scale;
        const zero=y+plotH-(0-min/scale)/range*plotH;
        add("line",{x:left,y:zero,width:plotW,height:1,points:[[0,0],[plotW,0]],strokeColor:"#9da9a0"});
        const cell=plotW/labels.length;
        series.forEach((row:number[],si:number)=>{
          const points=row.map((v:number,i:number)=>[left+cell*(i+.5),y+plotH-(v/scale-min/scale)/range*plotH]);
          const color=[ACCENT,"#8b663a","#56836b","#805c89"][si%4];
          if(block.chart==="line") {
            for(let i=1;i<points.length;i++) add("line",{x:points[i-1][0],y:points[i-1][1],
              width:Math.abs(points[i][0]-points[i-1][0]),height:Math.max(1,Math.abs(points[i][1]-points[i-1][1])),
              points:[[0,0],[points[i][0]-points[i-1][0],points[i][1]-points[i-1][1]]],strokeColor:color});
          } else points.forEach((p:number[],i:number)=>add("rectangle",{x:left+i*cell+si*cell/series.length,
            y:Math.min(zero,p[1]),width:Math.max(2,cell/series.length-4),height:Math.max(1,Math.abs(zero-p[1])),
            strokeColor:color,backgroundColor:color,fillStyle:"solid"}));
          points.forEach((p:number[],i:number)=>text(String(row[i]),p[0]-18,p[1]-20,40,10,color));
        });
        text(String(max),0,y,36,11);text(String(min),0,y+plotH-16,36,11);
        y+=plotH+12;labels.forEach((label:string,i:number)=>text(label,left+i*cell,y,cell-4,11));y+=56;
        if(block.series_labels?.length) y+=text(block.series_labels.map((v:string,i:number)=>`${i+1}: ${v}`).join(" · "),0,y,bw,12);
      }
    }
      rendered.push({block,elements:local,height:Math.max(28,y)+12,...(formulaBody?{formulaBody}:{})});
    }
    const groupHeight=rendered.reduce((total,item)=>total+item.height+12,0)-12;
    let top:number;
    if(anchor && anchor.column+span<=columns && anchor.span===span){
      while(pages.length<=anchor.page)newPage();
      page=pages[anchor.page];column=anchor.column;
      // Rebuild occupancy from groups already assigned to this page.
      heights=Array(columns).fill(topStart);
      for(const assigned of page.groups)for(let c=assigned.column;c<assigned.column+assigned.span;c++)
        heights[c]=Math.max(heights[c],assigned.y+assigned.height+12);
      top=Math.max(anchor.y,...heights.slice(column,column+span));
    }else{
      // A knowledge group is indivisible. Oversized groups extend a fresh column
      // vertically instead of moving its trailing diagram to a different column.
      while(true){
        if(column+span>columns){newPage();continue;}
        top=Math.max(...heights.slice(column,column+span));
        if(top+groupHeight<=bottom || heights.slice(column,column+span).every(h=>h===topStart))break;
        column++;
      }
    }
    const x=margin+column*(columnWidth+gap);
    page!.groups.push({id:group.id,page:pages.indexOf(page!),column,span,y:top!,height:groupHeight,block_ids:members.map(b=>b.id)});
    let positionY=top!;
    for(const item of rendered){
      page!.placements.push({group:group.id,id:item.block.id,column,span,x,y:positionY,width:bw,height:item.height,
        ...(item.formulaBody?{[item.block.type==="figure"?"figureBody":item.block.type==="code"?"codeBody":"formulaBody"]:{...item.formulaBody,x:x+item.formulaBody.x,y:positionY+item.formulaBody.y}}:{})});
      if(visibleIds.has(item.block.id)){
        page!.elements.push(...item.elements.map(element=>({...element,x:element.x+x,y:element.y+positionY})));
      }
      positionY+=item.height+12;
    }
    page!.height=Math.max(page!.height,top!+groupHeight+14);
    for(let c=column;c<column+span;c++)heights[c]=Math.max(heights[c],top!+groupHeight+12);
  }

  pages.forEach((item,index)=>{
    const decoration:any[]=[{type:"text",id:"page_title",x:margin,y:12,width:width-2*margin,height:54,
      text:wrapText(title.replace(/\s+/g," "),width-2*margin,titleFont).join("\n"),fontSize:titleFont,fontFamily:3,lineHeight:1.5,strokeColor:INK}];
    // Invisible corners keep auto-fit steady while only the first block is visible.
    decoration.push({type:"rectangle",id:`page_bounds_${index}`,x:0,y:0,width,height:item.height,
      strokeColor:PAPER,backgroundColor:"transparent",roughness:0,opacity:0});
    item.elements=[...decoration,...item.elements];
    if(options.theme==="ink")item.elements=item.elements.map(element=>{
      const colors:Record<string,string>={"#252927":"#f1f3e9","#31588b":"#c3dbc3","#fcfcfb":"#17382d",
        "#d7ddd9":"#537462","#f0f3f1":"#204537","#c5cec8":"#799582","#eaf0ed":"#294d3d","#8d9e91":"#a4b9a2",
        "#eef3ef":"#244b3c","#dce9f8":"#315d49","#899d90":"#7e9e89",
        "#acb9af":"#6e8e7b","#8b663a":"#e1c58b","#56836b":"#bce5ce","#805c89":"#c0dce8",
        "#9da9a0":"#91ad99"};
      return {...element,strokeColor:colors[element.strokeColor]||element.strokeColor,
        backgroundColor:colors[element.backgroundColor]||element.backgroundColor};
    });
  });
  return {pages,width};
}
