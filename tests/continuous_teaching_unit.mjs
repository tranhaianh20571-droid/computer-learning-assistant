import assert from "node:assert/strict";
import {layoutBoard} from "../frontend/src/lib/boardLayout.ts";
import {stageBoard,paragraphs} from "../frontend/src/lib/stageBoard.ts";
const board=blocks=>({presentation:{title:"同一阶段",blocks}});
const first={id:"first",type:"text",text:"第一段板书"},second={id:"second",type:"text",text:"第二段板书"};
const steps=[{step_no:1,board_scope:"stage:1",board:board([first])},
 {step_no:2,board_scope:"stage:1",board:board([first,second])},
 {step_no:3,board_scope:"stage:2",board:board([{id:"future",type:"text",text:"未来内容"}])}];
assert.deepEqual(stageBoard(steps,board([second]),2,"stage:1").presentation.blocks.map(b=>b.id),["first","second"]);
assert.deepEqual(stageBoard(steps,board([first]),1,"stage:1").presentation.blocks.map(b=>b.id),["first"]);
assert.deepEqual(stageBoard(steps,board([]),3,"stage:2").presentation.blocks,[]);
assert.deepEqual(paragraphs("第一句。第二句。",[{text:"第一句。"},{text:"第二句。"}]),["第一句。","第二句。"]);
assert.deepEqual(paragraphs("第一句。第",[{text:"第一句。"},{text:"第二句。"}]),["第一句。","第"]);
const blocks=Array.from({length:10},(_,i)=>({id:"t"+i,type:"text",text:"紧凑短句"}));
const layout=layoutBoard(blocks,1200,new Set(blocks.map(b=>b.id)));
assert.equal(layout.pages.length,1);assert(layout.pages[0].placements.every(p=>p.column<=2));
assert.equal(layout.pages[0].placements[1].y-layout.pages[0].placements[0].y,52);
const wide=layoutBoard([{id:"wide",type:"table",span:3,columns:["a","b","c"],rows:[["1","2","3"]]}],1200,new Set(["wide"]));
assert.equal(wide.pages[0].placements[0].span,3);
console.log("Three-column compact placement, backward span, stage accumulation and natural paragraphs passed");
