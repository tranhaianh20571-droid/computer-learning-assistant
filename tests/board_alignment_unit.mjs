import assert from "node:assert/strict";
import {TeachingBuffer} from "../frontend/src/lib/teachingBuffer.ts";
import {alignedBoard} from "../frontend/src/lib/boardAlignment.ts";
import {layoutBoard} from "../frontend/src/lib/boardLayout.ts";
import {presentBoard} from "../frontend/src/lib/boardPresentation.ts";
import {audioCharacters,exactAudioAlignment} from "../frontend/src/lib/audioTiming.ts";

const originalBuffer=new TeachingBuffer();
const firstFocus={material_id:"synthetic",target_id:"p1_r1",page_no:1};
const secondFocus={material_id:"synthetic",target_id:"p2_r2",page_no:2};
originalBuffer.receive("source",{text:"firstsecond",active:true,final:true,
  beats:[{id:"first",text:"first",block_ids:[],pdf_focus:firstFocus},
    {id:"second",text:"second",block_ids:[],pdf_focus:secondFocus}],board:{presentation:{blocks:[]}}});
originalBuffer.restoreCursor("source",3);
assert.deepEqual(originalBuffer.view("source").board.pdf_focus,firstFocus);
originalBuffer.setPaused(true);
originalBuffer.entries.get("source").shown=9;
assert.deepEqual(originalBuffer.view("source").board.pdf_focus,firstFocus);
originalBuffer.setPaused(false);
assert.deepEqual(originalBuffer.view("source").board.pdf_focus,secondFocus);

const text="先校验输入，再调用工具，最后汇总结果。";
const blocks=[{id:"check",type:"text",text:"校验输入"},{id:"tool",type:"text",text:"调用工具"},
  {id:"result",type:"text",text:"汇总结果"}];
const cues=blocks.map(block=>({block_id:block.id,target:"text",start:Array.from(text).indexOf(Array.from(block.text)[0]),
  end:Array.from(text).indexOf(Array.from(block.text)[0])+Array.from(block.text).length}));
const beats=[{id:"one",text,block_ids:blocks.map(block=>block.id),cues}];
const store=new TeachingBuffer();
store.receive("step",{text,beats,active:true,final:true,board:{alignment_version:1,presentation:{blocks}}});
store.audioClock("step",2);store.advance(0);
assert.deepEqual(store.view("step").board.presentation.blocks.map(block=>block.text),["校验输入"],
  "a referenced phrase appears complete rather than as an unrelated character fraction");
store.audioClock("step",cues[1].start+1);store.advance(0);
let view=store.view("step");
assert.deepEqual(view.board.presentation.blocks.map(block=>block.id),["check","tool"]);
assert.deepEqual(view.board.presentation.blocks[1].active_targets,["text"]);
const layout=layoutBoard(blocks,800,new Set(blocks.map(block=>block.id)),[],{},"",{},{responsive:true});
const rendered=layout.pages.flatMap(page=>presentBoard(page,view.board.presentation.blocks));
assert(rendered.some(element=>element.text==="调用工具"&&element.customData.alignmentActive));
assert(!rendered.some(element=>element.text==="汇总结果"));
store.setPaused(true);
const frozen=JSON.stringify(store.view("step"));
store.audioClock("step",text.length);store.advance(100);
assert.equal(JSON.stringify(store.view("step")),frozen);
store.setPaused(false);store.advance(0);
assert.equal(store.view("step").board.presentation.blocks.length,3);
store.restoreCursor("step",2);
assert.deepEqual(store.view("step").board.presentation.blocks.map(block=>block.id),["check"]);

const table={id:"t",type:"table",columns:["输入","输出"],rows:[["23","24"]]};
const tableBeats=[{cues:[{block_id:"t",target:"cell:0:0",start:0,end:2},
  {block_id:"t",target:"cell:0:1",start:4,end:6}]}];
const tableView=alignedBoard([table],tableBeats,1);
const tableLayout=layoutBoard([table],800,new Set(["t"]));
const cells=tableLayout.pages.flatMap(page=>presentBoard(page,tableView.visible)).filter(element=>element.customData?.tableRow!==undefined);
assert.deepEqual(cells.map(cell=>cell.text),["输入"]);
assert(cells[0].customData.alignmentActive);

const code={id:"c",type:"code",code:"let x = 23;\nreturn x;"};
const codeView=alignedBoard([code],[{cues:[{block_id:"c",target:"line:1",start:0,end:4},
  {block_id:"c",target:"line:2",start:6,end:10}]}],1);
assert.equal(codeView.visible[0].code,"let x = 23;\n");
assert.deepEqual(codeView.visible[0].active_lines,[1]);
const subtitles=[{text:"先校验输入，",begin_ms:0,end_ms:1000,display_start:0,display_end:6},
  {text:"再调用工具，",begin_ms:1000,end_ms:2000,display_start:6,display_end:12},
  {text:"最后汇总结果。",begin_ms:2000,end_ms:3000,display_start:12,display_end:19}];
assert(exactAudioAlignment(text,subtitles));
assert.equal(audioCharacters(text,subtitles,0,true),0,"no board reveal before speech starts");
assert.equal(audioCharacters(text,subtitles,1,true),1,"short phrases activate as soon as speech starts");
assert(audioCharacters(text,subtitles,1500,true)>=cues[1].start);
assert(!exactAudioAlignment(text,[{text:"different",begin_ms:0,end_ms:3000}]));
assert.equal(audioCharacters(text,[{text:"different",begin_ms:0,end_ms:3000}],1500,true),0,
  "an unaligned subtitle cannot become a guessed whole-track clock");
assert(audioCharacters(text,[{text:"different",begin_ms:0,end_ms:3000}],1500)>0,"legacy playback remains compatible");
const emoji="😀校验";
assert(exactAudioAlignment(emoji,[{text:emoji,begin_ms:0,end_ms:100,display_start:0,display_end:3}]));
console.log("PASS: exact phrase reveal, active targets, table/code granularity, pause/seek, Unicode and strict audio without global guessing");
