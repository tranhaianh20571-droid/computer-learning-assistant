import assert from "node:assert/strict";
import {layoutBoard} from "../frontend/src/lib/boardLayout.ts";
import {alignedBoard} from "../frontend/src/lib/boardAlignment.ts";
import {boardWritingY} from "../frontend/src/lib/boardWriting.ts";
import {expression,emptyDrawing,hasDrawing,circuitIssues} from "../frontend/src/lib/graphics.ts";

for(const source of ["x.constructor","x[0]","x=1","import('x')","2^100000","x^x","[1,2]","2x","1e309"]){
  assert.throws(()=>expression(source),source);
}
assert.equal(expression("x^(-2)")({x:2}),.25);
assert.equal(expression("x^3-3*x")({x:-1}),2);
assert(Number.isNaN(expression("1/x")({x:0})));
assert(Number.isNaN(expression("sqrt(x)")({x:-1})));
const plot={id:"plot",type:"figure",graphic:{kind:"plot",bounds:{x:[-2,2],y:[-4,4]},curves:[{expression:"x^3-3*x"}],points:[]}};
for(const width of [1440,768,390,320]){
  const page=layoutBoard([plot,{id:"after",type:"text",text:"Following content"}],width,new Set(["plot","after"]),[],{},"Graph",{}, {responsive:true,theme:"ink",pageHeight:600}).pages[0];
  const body=page.placements.find(p=>p.id==="plot").figureBody;
  assert(body&&body.width<=width);
  assert.equal(boardWritingY(page,page.placements[0],plot),body.y+body.height/2);
}
const aligned=alignedBoard([plot],[{cues:[{block_id:"plot",target:"figure",start:0,end:10}]}],5);
assert.deepEqual(aligned.visible[0].aligned_targets,["figure"]);
assert.equal(alignedBoard([plot],[{cues:[{block_id:"plot",target:"figure",start:0,end:10}]}],0).visible.length,0);
assert(!hasDrawing(emptyDrawing({kind:"plot",bounds:{x:[-2,2],y:[-4,4]}})));
assert.deepEqual(circuitIssues({kind:"circuit",inputs:["A"],outputs:["Y"],gates:[],wires:[]}),["1 个输入引脚未连接"]);
console.log("Professional graphics expression, domain gaps, four-width figure layout, cue reveal and empty drafts passed");
