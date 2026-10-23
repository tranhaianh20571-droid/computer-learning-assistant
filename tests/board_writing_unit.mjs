import assert from "node:assert/strict";
import {TeachingBuffer} from "../frontend/src/lib/teachingBuffer.ts";
import {layoutBoard} from "../frontend/src/lib/boardLayout.ts";
import {presentBoard} from "../frontend/src/lib/boardPresentation.ts";
import {stageBoard} from "../frontend/src/lib/stageBoard.ts";
import {wrapText,revealLines} from "../frontend/src/lib/boardText.ts";
import {writingTarget} from "../frontend/src/lib/boardFollow.ts";
import {boardGroups} from "../frontend/src/lib/boardGroups.ts";
const blocks=[{id:"explain",type:"text",text:"Synthetic explanation ".repeat(8)},
  {id:"code",type:"code",code:"if (index >= size) return false;\nreturn true;\n}"},
  {id:"graph",type:"graph",nodes:[{id:"a",label:"Input"},{id:"b",label:"Result"}],edges:[{from_id:"a",to_id:"b",label:"next"}]}];
const buffer=new TeachingBuffer();
buffer.receive("step",{text:"a".repeat(300),active:true,final:true,beats:[{id:"one",text:"a".repeat(200),block_ids:["code","explain"]},
  {id:"two",text:"a".repeat(100),block_ids:["graph"]}],board:{presentation:{blocks}}});
buffer.entries.get("step").shown=40;
let view=buffer.view("step");assert.deepEqual(view.board.presentation.blocks.map(b=>b.id),["code"]);
assert.equal(view.board.presentation.writing.block_id,"code");
buffer.entries.get("step").shown=170;view=buffer.view("step");
assert.equal(view.board.presentation.blocks.find(b=>b.id==="code").code,blocks[1].code);
assert(view.board.presentation.blocks.find(b=>b.id==="explain").text.length<blocks[0].text.length);
assert.equal(view.board.presentation.writing.block_id,"explain");
buffer.entries.get("step").shown=215;view=buffer.view("step");
const geometry=layoutBoard(blocks,650,new Set(blocks.map(b=>b.id)),[],{},"",{},{responsive:true});
const graphPage=geometry.pages.find(p=>p.placements.some(p=>p.id==="graph"));
const drawing=presentBoard(graphPage,view.board.presentation.blocks);
assert(drawing.some(e=>e.id==="graph_node_a"));assert(!drawing.some(e=>e.id==="graph_node_b"));assert(!drawing.some(e=>e.type==="arrow"));
const codePlacement=geometry.pages.flatMap(p=>p.placements).find(p=>p.id==="code");
assert(codePlacement.codeBody);assert.equal(codePlacement.codeBody.width,650-48);
const orderedGeometry=layoutBoard(blocks,650,new Set(blocks.map(b=>b.id)),[],{},"",{},
  {responsive:true,groups:boardGroups(blocks,buffer.entries.get("step").beats)});
assert.deepEqual(orderedGeometry.pages.flatMap(page=>page.placements).map(place=>place.id),["code","explain","graph"],
  "layout follows the same first-use order as narration even when source blocks are reversed");
const revisited=new TeachingBuffer();
revisited.receive("revisit",{text:"x".repeat(300),active:true,final:true,
  beats:[{id:"a",text:"x".repeat(100),block_ids:["code"]},
    {id:"b",text:"x".repeat(100),block_ids:["explain"]},
    {id:"c",text:"x".repeat(100),block_ids:["code","graph"]}],board:{presentation:{blocks}}});
revisited.entries.get("revisit").shown=220;
assert.equal(revisited.view("revisit").board.presentation.writing.block_id,"explain",
  "revisiting a completed block cannot move the writing cursor backward");
revisited.entries.get("revisit").shown=280;
assert.equal(revisited.view("revisit").board.presentation.writing.block_id,"graph");
for(const page of geometry.pages){
  const pending=presentBoard(page,[blocks[0]]);
  assert(!pending.some(element=>element.id==="code_code_bg"),"unstarted code has no frame");
  assert(page.elements.filter(element=>element.id.startsWith("page_bounds_")).every(element=>element.opacity===0),"layout bounds are never painted");
}
assert(stageBoard([],view.board,1,"stage").presentation.writing.block_id==="graph");
const legacy=new TeachingBuffer();legacy.receive("old",{text:"a".repeat(300),active:true,final:true,board:{presentation:{blocks}}});
legacy.entries.get("old").shown=20;assert.deepEqual(legacy.view("old").board.presentation.blocks.map(b=>b.id),["explain"]);
buffer.setPaused(true);const frozen=JSON.stringify(buffer.view("step"));buffer.entries.get("step").shown=250;
assert.equal(JSON.stringify(buffer.view("step")),frozen);buffer.setPaused(false);
const source="中文 index <= size。\nSecond identifier.";
const lines=wrapText(source,110,16);
assert(lines.every(line=>!line.endsWith("<")));
for(let count=1;count<=Array.from(source).length;count++)assert.equal(revealLines(source,lines,count).replaceAll("\n",""),Array.from(source).slice(0,count).join("").replaceAll("\n",""));
assert.equal(writingTarget(800,600,0,1800),500);
assert.equal(writingTarget(800,600,500,1800),500);
assert.equal(writingTarget(200,600,700,1800,true),0);
buffer.entries.get("step").shown=300;assert.equal(buffer.view("step").board.presentation.blocks.length,3);
console.log("Sequential beat/legacy reveal, code lines, graph stages, pause, stage cursor, prewrapped text and central follow passed");
