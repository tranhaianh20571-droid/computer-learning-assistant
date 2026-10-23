import assert from 'node:assert/strict';
import {layoutBoard} from '../frontend/src/lib/boardLayout.ts';
import {presentBoard} from '../frontend/src/lib/boardPresentation.ts';
import {boardWritingY,codeFocusLine} from '../frontend/src/lib/boardWriting.ts';
import {fitPointer} from '../frontend/src/lib/pointingRig.ts';
import {audioCharacters} from '../frontend/src/lib/audioTiming.ts';

const block={id:'synthetic_table',type:'table',columns:['Item','Value'],rows:[['Input','Ordered'],['Cost','log n']]};
const geometry=layoutBoard([block],800,new Set([block.id]),[],{},'',{},{responsive:true,theme:'ink'});
const page=geometry.pages[0],placement=page.placements[0];
const frame=presentBoard(page,[{...block,reveal_progress:.15}]);
assert(!frame.some(element=>element.type==='text'&&element.customData?.tableCellOrder!==undefined));
assert(frame.some(element=>element.customData?.elementRole==='table-frame'));
const early=presentBoard(page,[{...block,reveal_progress:.25}]);
const full=presentBoard(page,[{...block,reveal_progress:1}]);
assert(full.filter(element=>element.customData?.elementRole==='table-frame').length===7);
assert(full.filter(element=>element.customData?.tableCellOrder!==undefined).length===6);
assert(early.filter(element=>element.customData?.tableCellOrder!==undefined).map(e=>e.text).join('').length<full.filter(element=>element.customData?.tableCellOrder!==undefined).map(e=>e.text).join('').length);
assert(!early.some(element=>element.type==="text"&&!element.text.trim()),"unwritten cells are not restored as deleted text");
assert.equal(full.find(element=>element.id==='synthetic_table_cell_0_0').strokeColor,'#f1f3e9');
assert(boardWritingY(page,placement,block,.95)>boardWritingY(page,placement,block,.25));
assert.deepEqual(page.placements,geometry.pages[0].placements);
assert.equal(codeFocusLine({id:'code',type:'code',code:'first\nsecond\nthird',active_lines:[2]}),2);
assert.equal(codeFocusLine({id:'code',type:'code',code:'first\n\n',active_lines:[99]}),1);
assert.equal(codeFocusLine({id:'code',type:'code',code:''}),null);
for(const target of [{x:495,y:250},{x:3,y:3},{x:497,y:497}]){
  const pointer=fitPointer(target.x,target.y,500,500);
  assert(pointer.length>=100,'edge targets must keep a visible pointer');
  const end={x:target.x+pointer.length*Math.cos(pointer.angle*Math.PI/180),
    y:target.y+pointer.length*Math.sin(pointer.angle*Math.PI/180)};
  assert(end.x>=0&&end.x<=500&&end.y>=0&&end.y<=500);
}
assert.equal(audioCharacters('ABC DEF',[{text:'ABC',begin_ms:0,end_ms:100,display_start:0,display_end:3},{text:'DEF',begin_ms:200,end_ms:300,display_start:4,display_end:7}],150),3);
console.log('PASS: table frame before text, shared edges, staged cells, ink theme, stable geometry and writing cursor');
