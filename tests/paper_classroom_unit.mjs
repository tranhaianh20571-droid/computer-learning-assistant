import assert from "node:assert/strict";
import {timelinePosition,timelineValue} from "../frontend/src/lib/lectureTimeline.ts";
import {TeachingBuffer} from "../frontend/src/lib/teachingBuffer.ts";
import {boardExportScenes} from "../frontend/src/lib/boardExport.ts";

const steps=[{step_no:1,text:"abcdefghij",audio_subtitles:[{text:"abcdefghij",begin_ms:0,end_ms:1000}]},
  {step_no:2,text:"klmnopqrst"}];
const ceiling={step_no:1,visible_chars:5,position_ms:500};
assert.equal(timelineValue(ceiling,steps),.5);
assert.equal(timelinePosition(.7,steps,ceiling),null);
assert.deepEqual(timelinePosition(.2,steps,ceiling),{step_no:1,visible_chars:2,position_ms:200});
assert.equal(timelinePosition(1.1,steps,ceiling),null);
const buffer=new TeachingBuffer();
buffer.receive("step:1",{text:"abcdefghij",final:true,active:true,board:{
  source_reading:{},pdf_focus:{page_no:1},source_cues:[{start:3,end:8,pdf_focus:{page_no:4}}],
  presentation:{blocks:[{id:"a",type:"text",text:"note"}]}},
  beats:[{id:"b",text:"abcdefghij",block_ids:["a"]}]});
buffer.restoreCursor("step:1",5);
assert.equal(buffer.view("step:1").board.pdf_focus.page_no,4);
buffer.setPaused(true);buffer.restoreCursor("step:1",2);
assert.equal(buffer.view("step:1").text,"ab");
assert.equal(buffer.view("step:1").board.pdf_focus.page_no,1);
const scene={presentation:{blocks:[{id:"a",type:"text",text:"shown"}],
  all_blocks:[{id:"a",type:"text",text:"shown"},{id:"b",type:"text",text:"future"}]}};
const exported=boardExportScenes({steps:[]},buffer,1,scene,false);
assert.equal(exported[0].presentation.all_blocks.length,1);
assert.equal(exported[0].presentation.blocks[0].text,"shown");
const partial={presentation:{blocks:[{id:"code",source_step:1,type:"code",code:"print(1)\n",reveal_progress:.5}]}};
const snapshot=boardExportScenes({steps:[]},buffer,1,partial,false,
  [{step_no:1,block_id:"code",code:"print(1)\nSECRET_FUTURE",output:"FUTURE_OUTPUT"}]);
assert.equal(snapshot[0].presentation.blocks[0].code,"print(1)\n");
assert.equal(snapshot[0].presentation.blocks[0].export_output,undefined);
console.log("paper timeline, rewind focus and export visibility: 11 checks passed");
