import assert from "node:assert/strict";
import {boardGroups} from "../frontend/src/lib/boardGroups.ts";
import {layoutBoard} from "../frontend/src/lib/boardLayout.ts";
import {stageBoard} from "../frontend/src/lib/stageBoard.ts";
const definitions=Array.from({length:4},(_,i)=>({id:"definition"+i,type:"text",text:"合成概念说明".repeat(8)}));
const diagram={id:"diagram",type:"graph",title:"关系图",nodes:Array.from({length:4},(_,i)=>({id:"n"+i,label:"合成关系节点".repeat(3)})),
 edges:[{from_id:"n0",to_id:"n2",label:"读取指令"},{from_id:"n2",to_id:"n3",label:"输出"}]};
const blocks=[...definitions,diagram];
const groups=boardGroups(blocks);
assert.equal(groups.length,1);
assert.deepEqual(groups[0].block_ids,blocks.map(block=>block.id));
const layout=layoutBoard(blocks,860,new Set(blocks.map(block=>block.id)),[],{},"",{},{pageHeight:500});
assert(layout.pages[0].height>500,"oversized content extends the column vertically");
assert(layout.pages[0].placements.every(p=>p.column===0));
const graphPlace=layout.pages[0].placements.at(-1),textPlace=layout.pages[0].placements.at(-2);
assert(graphPlace.y>=textPlace.y+textPlace.height && graphPlace.x===textPlace.x);
assert(layout.pages[0].elements.filter(e=>e.type==="text"&&e.id.startsWith("diagram_node_text")).every(e=>e.fontSize===14));
const separate=[{id:"a",type:"text",title:"知识点甲",text:"说明甲".repeat(30)},
 {...diagram,id:"ga"}, {id:"b",type:"text",title:"知识点乙",text:"说明乙".repeat(30)},
 {...diagram,id:"gb"}];
assert.equal(boardGroups(separate).length,2);
const placed=layoutBoard(separate,1200,new Set(separate.map(b=>b.id)),[],{},"",{},{pageHeight:500});
assert(placed.pages[0].groups.length===2);
assert(placed.pages[0].groups[0].column===0 && placed.pages[0].groups[1].column===1);
const prefix=layoutBoard(definitions,860,new Set(definitions.map(b=>b.id)),[],{},"",{},{pageHeight:800});
const anchor=prefix.pages[0].groups[0];
const appended=layoutBoard(blocks,860,new Set(blocks.map(b=>b.id)),[],{},"",{},
 {pageHeight:800,anchors:{[anchor.id]:{page:anchor.page,column:anchor.column,span:anchor.span,y:anchor.y}}});
assert.deepEqual(appended.pages[0].placements.slice(0,4),prefix.pages[0].placements);
const semantic=[{id:"text",type:"text",title:"定义",text:"说明"}, {...diagram,id:"g"},
 {id:"other",type:"text",title:"新知识点",text:"其他说明"}];
assert.deepEqual(boardGroups(semantic,[{block_ids:["text","g"]}])[0].block_ids,["text","g"]);
const wide=[{id:"t",type:"text",text:"宽图的说明"},{...diagram,id:"w",span:3}];
assert(layoutBoard(wide,1200,new Set(wide.map(b=>b.id))).pages[0].placements.every(p=>p.span===3));
const scene={presentation:{blocks},beats:[]};
const restored=stageBoard([{step_no:1,board_scope:"stage",board:scene}],{presentation:{blocks:[{id:"later",type:"text",text:"下一组"}]}},2,"stage");
const tags=restored.presentation.blocks.filter(block=>block.id!=="later").map(block=>block.layout_group);
assert.equal(new Set(tags).size,1);
assert(restored.presentation.blocks.find(block=>block.id==="later").layout_group!==tags[0]);
const frozenA={id:"frozen-a",type:"text",text:"First topic",layout_group:"first"};
const frozenB={id:"frozen-b",type:"text",text:"Second topic",layout_group:"second"};
const appendedA={id:"new-a",type:"text",text:"Later addition",layout_group:"first"};
assert.deepEqual(boardGroups([frozenA,frozenB,appendedA]).flatMap(group=>group.block_ids),
  ["frozen-a","frozen-b","new-a"],"a later reference to an earlier group stays at the end");
const mergedStage=stageBoard([{step_no:1,board_scope:"stage",
  board:{presentation:{blocks:[{id:"a",type:"text",text:"A"},{id:"b",type:"text",text:"B"}]},
    beats:[{block_ids:["a"]},{block_ids:["b"]}]}}],
  {presentation:{blocks:[{id:"a",type:"text",text:"A"},{id:"c",type:"text",text:"C"}]},
    beats:[{block_ids:["a","c"]}]},2,"stage");
assert.deepEqual(mergedStage.presentation.all_blocks.map(block=>block.id),["a","b","c"]);
assert.deepEqual(boardGroups(mergedStage.presentation.all_blocks,mergedStage.beats).flatMap(group=>group.block_ids),["a","b","c"]);
const textBeats=[{block_ids:["a"]},{block_ids:["b"]}];
const streamPrefix=boardGroups([{id:"a",type:"text",text:"A"}],textBeats.slice(0,1));
const streamAppend=boardGroups([{id:"a",type:"text",text:"A"},{id:"b",type:"text",text:"B"}],textBeats);
assert.equal(streamPrefix[0].id,streamAppend[0].id,"compacting a new beat preserves the existing group's anchor");
const oldGroups=[{id:"one",block_ids:["one"]},{id:"two",block_ids:["two"]}];
const oldBlocks=[{id:"one",type:"text",text:"第一组说明"},{id:"two",type:"text",text:"第二组说明"}];
const original=layoutBoard(oldBlocks,1200,new Set(["one","two"]),[],{},"",{},{groups:oldGroups,pageHeight:900});
const fixed=Object.fromEntries(original.pages[0].groups.map(group=>[group.id,{page:group.page,column:group.column,span:group.span,y:group.y}]));
const grew=layoutBoard([...oldBlocks,diagram],1200,new Set(["one","two","diagram"]),[],{},"",{},
 {groups:[{id:"one",block_ids:["one","diagram"]},oldGroups[1]],pageHeight:900,anchors:fixed});
assert(grew.pages[0].groups[1].y>=grew.pages[0].groups[0].y+grew.pages[0].groups[0].height);
assert(grew.pages[0].groups[1].column===fixed.two.column,"later group stays in its original column without overlap");
console.log("Legacy/semantic grouping, oversized vertical extension, anchored append, full-width group and restore passed");
