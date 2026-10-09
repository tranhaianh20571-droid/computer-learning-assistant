import assert from "node:assert/strict";
import {TeachingBuffer} from "../frontend/src/lib/teachingBuffer.ts";
import {layoutBoard,wrapText} from "../frontend/src/lib/boardLayout.ts";

const buffer=new TeachingBuffer();
buffer.receive("step:1",{text:"中文😀第一句。第二句。",active:true,epoch:"1:1"});
assert.equal(buffer.view("step:1").text,"中");
buffer.advance(100);assert.equal(buffer.view("step:1").text,"中文");
const shown=buffer.view("step:1").shown;
buffer.receive("step:1",{text:"中文😀第一句。第二句。",final:true});
assert.equal(buffer.view("step:1").shown,shown); // Final publication does not jump.
buffer.flush("step:1");assert.equal(buffer.view("step:1").remaining,0);
buffer.receive("step:1",{text:"重试内容",active:true,epoch:"2:1"});assert.equal(buffer.view("step:1").text,"重");
buffer.receive("step:2",{text:"后台预生成",active:false});buffer.advance(100);assert.equal(buffer.view("step:2").text,"");
const blocks=[{id:"first",type:"text",text:"第一块"},{id:"next",type:"text",text:"第二块"}];
buffer.receive("sync",{text:"第一句第二句",active:true,beats:[{id:"b1",text:"第一句",block_ids:["first"]},
 {id:"b2",text:"第二句",block_ids:["next"]}],board:{presentation:{blocks}}});
assert.deepEqual(buffer.view("sync").board.presentation.blocks.map(b=>b.id),["first"]);
for(let i=0;i<3;i++)buffer.advance(100);
assert.deepEqual(buffer.view("sync").board.presentation.blocks.map(b=>b.id),["first","next"]);
const boardBlocks=Array.from({length:12},(_,i)=>({id:"block"+i,type:"text",text:"合成长文本需要换行。".repeat(20)}));
const layout=layoutBoard(boardBlocks,1200,new Set(boardBlocks.map(b=>b.id)),[],{},"",{},
 {groups:boardBlocks.map(block=>({id:block.id,block_ids:[block.id]}))});
assert(layout.pages.length>1);
for(const page of layout.pages){
 for(let i=0;i<page.placements.length;i++){
  const a=page.placements[i];assert(a.x>=0 && a.x+a.width<=layout.width && a.y+a.height<=page.height);
  for(const b of page.placements.slice(i+1)){
   assert(a.x+a.width<=b.x || b.x+b.width<=a.x || a.y+a.height<=b.y || b.y+b.height<=a.y);
  }
 }
}
assert.deepEqual(layout.pages[0].placements.map(p=>p.column),[0,0,1,1,2,2]);
const before=layoutBoard(boardBlocks.slice(0,2),1200,new Set(["block0"]));
const after=layoutBoard(boardBlocks.slice(0,3),1200,new Set(["block0","block1"]));
assert.deepEqual(before.pages[0].placements,after.pages[0].placements.slice(0,2));
const longCode={id:"code",type:"code",code:"print('完整保留')\n".repeat(60)};
const tall=layoutBoard([longCode],1200,new Set(["code"]));assert.equal(tall.pages.length,1);
assert(tall.pages[0].elements.some(e=>e.text?.split("\n").length>=60));
assert(wrapText("长文本".repeat(15),100).length>1);
const graph={id:"g",type:"graph",diagram:"relationship",nodes:[{id:"a",label:"输入"},{id:"b",label:"输出"}],
 edges:Array.from({length:15},(_,i)=>({from_id:i%2?"a":"b",to_id:i%2?"b":"a",label:String(i)}))};
const summary=layoutBoard([graph],1200,new Set(["g"]));
assert(!summary.pages[0].elements.some(e=>e.type==="arrow"));
const detail=layoutBoard([graph],1200,new Set(["g"]),[],{g:"a"});
assert(detail.pages[0].elements.some(e=>e.type==="arrow"));
const sequence=layoutBoard([{...graph,id:"seq",diagram:"sequence",edges:[{from_id:"a",to_id:"b",label:"请求"}]}],1200,new Set(["seq"]));
assert(sequence.pages[0].elements.some(e=>e.strokeStyle==="dashed"));
const reveal=layoutBoard([{id:"typed",type:"text",text:"完整文字应保留完整尺寸"}],1200,new Set(["typed"]),[],{},"",
 {typed:{id:"typed",type:"text",text:"完"}});
assert(reveal.pages[0].elements.some(e=>e.text==="完"));
const extreme=layoutBoard([{id:"numbers",type:"chart",chart:"line",labels:["a","b"],values:[[-1e308,1e308]],is_example:true}],1200,new Set(["numbers"]));
assert(extreme.pages[0].elements.every(e=>[e.x,e.y,e.width,e.height].every(Number.isFinite)));
console.log("Buffer Unicode/final/epoch/prefetch/sync and three-column/pagination/graph checks passed");
