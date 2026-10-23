import assert from "node:assert/strict";
import {graphLayout} from "../frontend/src/lib/graphLayout.ts";
import {layoutBoard} from "../frontend/src/lib/boardLayout.ts";
import {presentBoard} from "../frontend/src/lib/boardPresentation.ts";
import {paintedFocus} from "../frontend/src/lib/boardFocus.ts";
import {graphFixtures} from "./graph_layout_fixtures.mjs";

const overlaps=(a,b)=>a.x<b.x+b.width-.5&&a.x+a.width>b.x+.5&&a.y<b.y+b.height-.5&&a.y+a.height>b.y+.5;
for(const width of [240,292,362,720,1200]){
  for(const block of graphFixtures){
    const result=graphLayout(block,width,2,14,32);
    const nodes=result.elements.filter(e=>e.customData?.nodeId&&e.type!=="text");
    const labels=result.elements.filter(e=>e.id.includes("_edge_label_bg_"));
    for(const element of result.elements){
      assert([element.x,element.y,element.width,element.height].every(Number.isFinite));
      if(element.points){
        for(const [x,y] of element.points){
          assert(element.x+x>=-.1&&element.x+x<=width+.1,`${block.id} edge x bounds`);
          assert(element.y+y>=-.1&&element.y+y<=result.height+.1,`${block.id} edge y bounds`);
        }
      }else{
        assert(element.x>=0&&element.x+element.width<=width+.1,`${block.id} text/node x bounds`);
        assert(element.y>=0&&element.y+element.height<=result.height+.1,`${block.id} text/node y bounds`);
      }
    }
    for(let i=0;i<labels.length;i++){
      assert(nodes.every(node=>!overlaps(labels[i],node)),`${block.id} label/node collision`);
      assert(labels.slice(i+1).every(label=>!overlaps(labels[i],label)),`${block.id} label collision`);
    }
    for(const arrow of result.elements.filter(e=>e.type==="arrow")){
      const end=arrow.points.at(-1),x=arrow.x+end[0],y=arrow.y+end[1];
      assert(labels.every(label=>!(x>label.x&&x<label.x+label.width&&y>label.y&&y<label.y+label.height)),
        "arrowheads cannot be covered by caption backgrounds");
      const target=nodes.find(n=>n.customData.nodeId===arrow.customData.edgeTo);
      const dx=Math.abs((x-target.x-target.width/2)/(target.width/2));
      const dy=Math.abs((y-target.y-target.height/2)/(target.height/2));
      const boundary=target.type==="ellipse"?Math.hypot(dx,dy):target.type==="diamond"?dx+dy:Math.max(dx,dy);
      assert(Math.abs(boundary-1)<.001,"arrow meets the actual target shape boundary");
    }
    assert.equal(result.elements.filter(e=>e.customData?.edgeIndex!==undefined&&
      (e.type==="arrow"||result.mode==="relations")).length,block.edges.length);
    if(result.mode==="relations"){
      const rows=result.elements.filter(element=>element.type==="text");
      for(let i=1;i<rows.length;i++){
        assert(rows[i].customData.graphStage>=rows[i-1].customData.graphStage,
          "relationship rows appear top to bottom without backfilling earlier node rows");
      }
    }
    const stages=Math.max(...result.elements.map(element=>element.customData?.graphStageCount||1));
    let previousY=-Infinity;
    for(let stage=0;stage<stages;stage++){
      const visible=presentBoard({elements:result.elements},[{...block,reveal_progress:(stage+.5)/stages}]);
      const point=paintedFocus(visible,block);
      if(point){
        assert(point.y>=previousY-.1,`${block.id} ${width}px: graph writing cannot jump upward to an earlier row`);
        previousY=point.y;
      }
    }
    const focused=graphLayout(block,width,2,14,32,block.nodes[0].id);
    assert.deepEqual(focused.elements.map(e=>[e.id,e.x,e.y,e.width,e.height]),
      result.elements.map(e=>[e.id,e.x,e.y,e.width,e.height]),"focus cannot hide edges or move the graph");
    const page={elements:result.elements,placements:[],groups:[],height:result.height};
    for(const progress of [0,.01,.2,.5,.75,1]){
      const visible=presentBoard(page,[{...block,reveal_progress:progress}]);
      const ids=new Set(visible.filter(e=>e.customData?.nodeId).map(e=>e.customData.nodeId));
      for(const edge of visible.filter(e=>e.customData?.edgeIndex!==undefined)){
        assert(ids.has(edge.customData.edgeFrom)&&ids.has(edge.customData.edgeTo),"visible relation has both endpoints");
        if(result.mode==="graph"){
          const index=edge.customData.edgeIndex;
          assert(visible.some(e=>e.id===`${block.id}_edge_${index}`),"caption and arrow share a reveal stage");
        }
      }
    }
  }
}
const chain=graphLayout(graphFixtures[0],720,2,14,32);
const node=id=>chain.elements.find(e=>e.id==="chain_node_"+id);
assert(node("a").y<node("b").y&&node("b").y<node("c").y,"connections override input array order");
for(const width of [320,390,768,1440]){
  const layout=layoutBoard(graphFixtures,width,new Set(graphFixtures.map(b=>b.id)),[],{},"关系验证",{},{responsive:true});
  for(const page of layout.pages)for(const p of page.placements)assert(p.x+p.width<=layout.width+.1);
}
console.log("PASS: 6 graph families x 5 widths, bounds, caption collisions, all edges, focus stability and atomic reveal; board integration at 4 widths");
