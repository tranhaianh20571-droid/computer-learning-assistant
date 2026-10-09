import assert from 'node:assert/strict';
import {BoardMotion} from '../frontend/src/lib/boardMotion.ts';
import {boardWritingY} from '../frontend/src/lib/boardWriting.ts';
import {layoutBoard} from '../frontend/src/lib/boardLayout.ts';
import {presentBoard} from '../frontend/src/lib/boardPresentation.ts';

function fixture(){
  const queue=new Map();let id=0,time=0;
  const node={scrollTop:0,clientHeight:600,scrollHeight:4000};
  const motion=new BoardMotion(node,{request:cb=>{queue.set(++id,cb);return id;},cancel:id=>queue.delete(id)});
  const frame=(ms=16.667)=>{time+=ms;const pending=[...queue.values()];queue.clear();pending.forEach(cb=>cb(time));assert(queue.size<=1,'one RAF only');return node.scrollTop;};
  const settle=()=>{for(let i=0;i<500&&motion.active;i++)frame();assert(!motion.active,'finite settling');};
  return {node,motion,queue,frame,settle};
}
let f=fixture();f.motion.follow(300);assert(!f.motion.active,'comfortable zone does not move');
f.motion.follow(1300);assert.equal(f.queue.size,1);
let previous=0,positions=[];
for(let i=0;i<15;i++){const next=f.frame();assert(next>=previous&&next-previous<=15.1,'monotone and speed bounded');positions.push(next);previous=next;}
assert(positions.filter((x,i)=>i&&x>positions[i-1]).length>10,'continuous motion');
f.motion.follow(1600);assert.equal(f.queue.size,1,'retarget does not restart RAF');
const continued=f.frame();assert(continued-previous>8,'retarget keeps velocity');
f.settle();assert(Math.abs(f.node.scrollTop-1270)<1);
f.motion.follow(100,true);f.settle();assert.equal(f.node.scrollTop,0,'top clamp');
f.motion.follow(9000,true);f.settle();assert.equal(f.node.scrollTop,3400,'bottom clamp');
f.motion.follow(900,true);f.frame();f.frame();f.motion.stop();assert.equal(f.queue.size,0);
const frozen=f.node.scrollTop;f.frame();assert.equal(f.node.scrollTop,frozen,'manual/pause stop clears RAF');
f=fixture();f.motion.follow(1500);f.frame();f.frame();const before=f.node.scrollTop;f.frame(5000);
assert(f.node.scrollTop-before<=28.9,'background delay cannot jump');
f.motion.setReduced(true);assert.equal(f.node.scrollTop,1170);assert(!f.motion.active);
f.motion.follow(1000,true);assert.equal(f.node.scrollTop,670);assert.equal(f.queue.size,0);
f.motion.setReduced(false);f.motion.follow(1800);f.settle();
f.node.scrollHeight=700;f.motion.follow(2000,true);f.settle();assert.equal(f.node.scrollTop,100,'resized scroll range');
f.motion.follow(Number.NaN);assert(!f.motion.active);
f=fixture();let integerTop=0;
Object.defineProperty(f.node,'scrollTop',{get:()=>integerTop,set:value=>{integerTop=Math.round(value);}});
f.motion.follow(1349.5,true);f.settle();assert.equal(f.node.scrollTop,1020,'integer DOM cannot stall the trajectory');
const table={id:'table',type:'table',columns:['Role','Meaning'],rows:[['A','First'],['B','Second'],['C','Third']]};
const sequence={id:'sequence',type:'graph',diagram:'sequence',nodes:[{id:'a',label:'Client'},{id:'b',label:'Server'}],edges:[{from_id:'a',to_id:'b',label:'Request'},{from_id:'b',to_id:'a',label:'Response'}]};
for(const width of [390,1400]){
  const g=layoutBoard([table,sequence],width,new Set(['table','sequence']),[],{},'',{},{responsive:true});
  const pageFor=id=>g.pages.find(p=>p.placements.some(x=>x.id===id));
  const tp=pageFor('table'),tplace=tp.placements.find(p=>p.id==='table');
  let y=0;
  for(let row=1;row<=3;row++){
    const progress=row/3,partial={...table,reveal_progress:progress};
    const painted=presentBoard(tp,[partial]);assert(painted.every(e=>e.customData?.tableRow===undefined||e.customData.tableRow<=row));
    const cell=painted.find(e=>e.type==='rectangle'&&e.customData?.tableRow===row);
    const next=boardWritingY(tp,tplace,partial,progress);assert.equal(next,cell.y+cell.height/2);assert(next>y);y=next;
  }
  const sp=pageFor('sequence'),splace=sp.placements.find(p=>p.id==='sequence');
  for(let stage=0;stage<4;stage++){
    const progress=(stage+1)/4,painted=presentBoard(sp,[{...sequence,reveal_progress:progress}]);
    assert(painted.every(e=>e.customData?.graphStage===undefined||e.customData.graphStage<=stage));
    const element=painted.find(e=>e.customData?.graphStage===stage&&e.type===(stage<2?'rectangle':'arrow'));
    assert.equal(boardWritingY(sp,splace,sequence,progress),element.y+element.height/2);
  }
}
console.log('Board motion: comfort zone, single RAF, smooth retarget, clamps, stop, tab delay, reduced motion, resize; table/sequence geometric cursors at desktop/mobile passed');
