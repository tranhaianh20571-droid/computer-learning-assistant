import assert from "node:assert/strict";
import {paintedFocus} from "../frontend/src/lib/boardFocus.ts";
import {characterScene} from "../frontend/src/lib/characterScene.ts";
const frame={type:"line",x:0,y:0,width:100,height:50,customData:{blockId:"table"}};
assert.equal(paintedFocus([frame],{id:"table",type:"table"}),null);
const node={id:"graph_node_text_b",type:"text",text:"已写节点",x:40,y:400,width:120,fontSize:16,customData:{blockId:"graph"}};
const edge={id:"graph_edge_label_0",type:"text",text:"远处关系",x:40,y:20,width:120,fontSize:16,customData:{blockId:"graph"}};
assert.deepEqual(paintedFocus([node,edge],{id:"graph",type:"graph"}),paintedFocus([node],{id:"graph",type:"graph"}));
assert(paintedFocus([node,edge],{id:"graph",type:"graph",diagram:"sequence"}).y<100);
const stagedNode={...node,customData:{blockId:"graph",graphStage:1,nodeId:"b"}};
const stagedEdge={...edge,customData:{blockId:"graph",graphStage:2,edgeIndex:1}};
assert(paintedFocus([stagedNode,stagedEdge],{id:"graph",type:"graph"}).y<100,
  "new complete-relationship stages follow the current relationship, including feedback without new nodes");
for(const [flags,expected] of [
  [{playing:true,waiting:false,understood:false,completed:false},"explaining"],
  [{playing:false,waiting:true,understood:false,completed:false},"thinking"],
  [{playing:false,waiting:false,understood:true,completed:false},"encouraging"],
  [{playing:false,waiting:false,understood:false,completed:true},"celebrating"],
  [{playing:false,waiting:false,understood:false,completed:false},"neutral"],
])assert.equal(characterScene(flags),expected);
console.log("Painted focus, stable graph edge focus and five event-driven character states passed");
