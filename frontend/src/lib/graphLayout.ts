/** Topology owns placement; captions participate in layout before rendering. */
import dagre from "@dagrejs/dagre";
import {wrapText,measureBoardText} from "./boardText.ts";
import {graphStages} from "./graphStages.ts";

const ACCENT="#31588b",INK="#252927";
const ratio=(node:any)=>node.shape==="decision"?.58:node.shape==="terminal"?.7:1;
const nodeText=(node:any)=>(node.group?node.group+"\n":"")+node.label;

export function graphMinimumWidth(_block:any,_columns:number){
  return 280;
}

export function graphLayout(block:any,width:number,_columns:number,font:number,gap:number,
    selected="",highlights:string[]=[]){
  const nodes=block.nodes||[],edges=block.edges||[];
  const captionFont=12,lineHeight=18;
  const nodeWidth=Math.min(240,Math.max(96,(width-64)/2));
  const captionWidth=Math.min(192,Math.max(96,width*.4));
  const g=new dagre.graphlib.Graph({multigraph:true}).setGraph({
    rankdir:"TB",nodesep:28,edgesep:22,ranksep:Math.max(38,gap),marginx:12,marginy:12,
  }).setDefaultEdgeLabel(()=>({}));
  for(const node of nodes){
    const inner=ratio(node);
    const lines=wrapText(nodeText(node),nodeWidth*inner-20,font);
    g.setNode(node.id,{width:nodeWidth,height:Math.max(52,(lines.length*font*1.5+20)/inner)});
  }
  edges.forEach((edge:any,index:number)=>{
    if(!g.hasNode(edge.from_id)||!g.hasNode(edge.to_id))return;
    const lines=edge.label?wrapText(edge.label,captionWidth,captionFont):[];
    const labelWidth=lines.length?Math.max(...lines.map(line=>measureBoardText(line,captionFont)))+12:0;
    g.setEdge(edge.from_id,edge.to_id,{width:labelWidth,height:lines.length?lines.length*lineHeight+10:0,
      labelpos:"c",labeloffset:12},String(index));
  });
  dagre.layout(g);
  // Readable relationship rows are a lossless fallback for a graph which cannot
  // fit the viewport. Never scale lettering down or silently remove edges.
  if((g.graph().width||0)>width)return relationRows(block,width,font,selected,highlights);
  const stages=graphStages(block,Object.fromEntries(nodes.map((node:any)=>[node.id,g.node(node.id)])));
  const elements:any[]=[],labels:any[]=[];
  const offset=(width-(g.graph().width||0))/2;
  const meta=(stage:number,extra:any={})=>({blockId:block.id,graphStage:stage,
    graphStageCount:stages.total,...extra});
  edges.forEach((edge:any,index:number)=>{
    const laid=g.edge({v:edge.from_id,w:edge.to_id,name:String(index)});
    if(!laid?.points?.length)return;
    let points:number[][]=laid.points.map((p:{x:number;y:number})=>[p.x+offset,p.y]);
    if(edge.from_id===edge.to_id){
      const node=g.node(edge.from_id),right=node.x!+offset+node.width/2,cy=node.y!;
      const labelRight=(laid.x??node.x!)+offset+(laid.width||0)/2;
      const outside=Math.max(right+32,labelRight+12),near=right+10;
      const top=Math.min(cy-node.height/2,(laid.y??cy)-(laid.height||0)/2)-10;
      const bottom=Math.max(cy+node.height/2,(laid.y??cy)+(laid.height||0)/2)+10;
      points=[[right,cy-node.height/4],[near,cy-node.height/4],[near,top],[outside,top],
        [outside,bottom],[near,bottom],[near,cy+node.height/4],[right,cy+node.height/4]];
    }
    // Dagre intersects rectangles; ellipse and diamond ports need their own boundary.
    for(const [endpoint,neighbor,nodeId] of [[0,1,edge.from_id],
      [points.length-1,points.length-2,edge.to_id]] as [number,number,string][]){
      const node=nodes.find((n:any)=>n.id===nodeId),box=g.node(nodeId);
      if(!node||node.shape==="process"||!node.shape)continue;
      const dx=points[neighbor][0]-(box.x!+offset),dy=points[neighbor][1]-box.y!;
      const rx=box.width/2,ry=box.height/2;
      const divisor=node.shape==="decision"?Math.abs(dx/rx)+Math.abs(dy/ry):Math.hypot(dx/rx,dy/ry);
      if(divisor)points[endpoint]=[box.x!+offset+dx/divisor,box.y!+dy/divisor];
    }
    const origin=points[0],relative=points.map(p=>[p[0]-origin[0],p[1]-origin[1]]);
    const data=meta(stages.edgeStages[index],{edgeFrom:edge.from_id,edgeTo:edge.to_id,edgeIndex:index});
    const xs=points.map(p=>p[0]),ys=points.map(p=>p[1]);
    elements.push({type:"arrow",id:`${block.id}_edge_${index}`,x:origin[0],y:origin[1],
      width:Math.max(1,Math.max(...xs)-Math.min(...xs)),height:Math.max(1,Math.max(...ys)-Math.min(...ys)),
      points:relative,endArrowhead:"arrow",strokeColor:ACCENT,strokeWidth:selected&&
        (edge.from_id===selected||edge.to_id===selected)?2:1.5,backgroundColor:"transparent",
      roughness:0,customData:data});
    if(edge.label&&laid.x!==undefined&&laid.y!==undefined){
      const x=laid.x+offset-laid.width!/2,y=laid.y-laid.height!/2;
      const lines=wrapText(edge.label,captionWidth,captionFont);
      labels.push({type:"rectangle",id:`${block.id}_edge_label_bg_${index}`,x,y,
        width:laid.width,height:laid.height,strokeColor:"transparent",backgroundColor:"#fcfcfb",
        fillStyle:"solid",roughness:0,customData:data},
      {type:"text",id:`${block.id}_edge_label_${index}`,x:x+6,y:y+5,
        width:laid.width!-12,height:lines.length*lineHeight,text:lines.join("\n"),
        fontSize:captionFont,fontFamily:3,lineHeight:1.5,strokeColor:ACCENT,customData:data});
    }
  });
  for(const node of nodes){
    const laid=g.node(node.id),inner=ratio(node),active=selected===node.id||highlights.includes(node.id);
    const x=laid.x!+offset-laid.width/2,y=laid.y!-laid.height/2;
    const data=meta(stages.nodeStages[node.id],{nodeId:node.id});
    elements.push({type:node.shape==="decision"?"diamond":node.shape==="terminal"?"ellipse":"rectangle",
      id:`${block.id}_node_${node.id}`,x,y,width:laid.width,height:laid.height,
      strokeColor:active?ACCENT:"#899d90",backgroundColor:active?"#dce9f8":"#eef3ef",
      fillStyle:"solid",strokeWidth:1.5,roughness:0,customData:data},
    {type:"text",id:`${block.id}_node_text_${node.id}`,
      x:x+laid.width*(1-inner)/2+10,y:y+laid.height*(1-inner)/2+10,
      width:laid.width*inner-20,height:laid.height*inner-20,
      text:wrapText(nodeText(node),laid.width*inner-20,font).join("\n"),fontSize:font,
      fontFamily:3,lineHeight:1.5,strokeColor:INK,customData:data});
  }
  elements.push(...labels);
  const bounds=elements.flatMap(element=>element.points
    ?element.points.map(([x,y]:number[])=>[element.x+x,element.y+y])
    :[[element.x,element.y],[element.x+element.width,element.y+element.height]]);
  const minX=Math.min(0,...bounds.map(p=>p[0])),minY=Math.min(0,...bounds.map(p=>p[1]));
  const maxX=Math.max(0,...bounds.map(p=>p[0])),maxY=Math.max(0,...bounds.map(p=>p[1]));
  if(maxX-minX+12>width)return relationRows(block,width,font,selected,highlights);
  for(const element of elements){element.x-=minX;element.y+=8-minY;}
  return {elements,height:Math.max(40,maxY-minY+16),font,mode:"graph"};
}

function relationRows(block:any,width:number,font:number,selected:string,highlights:string[]){
  const nodes=block.nodes||[],edges=block.edges||[],stages=graphStages(block);
  const byId=new Map<string,any>(nodes.map((n:any)=>[n.id,n]));
  const elements:any[]=[];
  let y=8;
  const add=(id:string,text:string,stage:number,extra:any={})=>{
    const lines=wrapText(text,width-32,font),height=lines.length*font*1.5;
    elements.push({type:"text",id,x:16,y,width:width-32,height,text:lines.join("\n"),fontSize:font,
      fontFamily:3,lineHeight:1.5,strokeColor:extra.active?ACCENT:INK,
      customData:{blockId:block.id,graphStage:stage,graphStageCount:stages.total,...extra}});
    y+=height+20;
  };
  // Reserve rows in reveal order so later relationships never fill an earlier gap.
  for(let stage=0;stage<stages.total;stage++){
    for(const node of nodes)if(stages.nodeStages[node.id]===stage){
      add(`${block.id}_node_${node.id}`,nodeText(node),stage,
        {nodeId:node.id,active:node.id===selected||highlights.includes(node.id)});
    }
    edges.forEach((edge:any,i:number)=>{
      if(stages.edgeStages[i]!==stage)return;
      const from=byId.get(edge.from_id),to=byId.get(edge.to_id);
      if(!from||!to)return;
      add(`${block.id}_edge_${i}`,`${from.label} → ${edge.label?edge.label+" → ":""}${to.label}`,
        stage,{edgeFrom:edge.from_id,edgeTo:edge.to_id,edgeIndex:i,
          active:edge.from_id===selected||edge.to_id===selected});
    });
  }
  return {elements,height:y,font,mode:"relations"};
}
