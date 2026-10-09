import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdirSync} from "node:fs";
const require=createRequire(import.meta.url),{chromium}=require(process.env.PW_ROOT+"/playwright");
const base=process.env.TEST_FRONTEND||"http://127.0.0.1:5173";
const explanation=("中文说明 index <= size；保留比较运算符与变量名称。Synthetic width measurement.\n").repeat(50);
const code=Array.from({length:30},(_,i)=>`  if (index_${i} >= size) return false; // ${'synthetic_long_identifier_'.repeat(5)}`).join('\n');
const blocks=[{id:'explain',type:'text',title:'Synthetic boundary explanation',text:explanation},
  {id:'code',type:'code',title:'Synthetic code',language:'cpp',code}];
const browser=await chromium.launch({headless:true,...(process.env.TEST_BROWSER?{executablePath:process.env.TEST_BROWSER}:{})});
try{
 for(const width of [1600,390]){
  const context=await browser.newContext({viewport:{width,height:1000}}),page=await context.newPage(),errors=[];
  page.on('pageerror',error=>{errors.push(error.message);console.error('PAGE_ERROR',error.message);});
  page.on('console',message=>{if(/Maximum update depth/.test(message.text()))errors.push(message.text());});
  await page.route('**/src/main.tsx*',route=>route.fulfill({contentType:'text/javascript',body:`
    import React from '/node_modules/.vite/deps/react.js';
    import ReactDOM from '/node_modules/.vite/deps/react-dom_client.js';
    import TeachingBoard from '/src/ui/TeachingBoard.tsx';
    import LectureSplit from '/src/ui/LectureSplit.tsx';
    import {TeachingBuffer} from '/src/lib/teachingBuffer.ts';
    import {layoutBoard} from '/src/lib/boardLayout.ts';
    import {measureBoardText} from '/src/lib/boardText.ts';
    import '/src/tokens.css';
    const blocks=${JSON.stringify(blocks)},buffer=new TeachingBuffer();
    buffer.receive('step',{text:'a'.repeat(1000),active:true,final:true,
      beats:[{id:'beat',text:'a'.repeat(1000),block_ids:blocks.map(block=>block.id)}],board:{presentation:{title:'Synthetic lesson',blocks}}});
    const root=ReactDOM.createRoot(document.getElementById('root'));
    window.unmountBoard=()=>root.unmount();
    const h=React.createElement;
    window.renderScene=(scene,playing=true,key='fixture')=>root.render(h(React.StrictMode,null,h('div',{className:'lw-simple'},
      h(LectureSplit,{pane:'board',children:[h('div',{className:'bd-col',key:'board'},h(TeachingBoard,{scene,sceneKey:key,presenting:playing})),
        h('div',{className:'tx-col',key:'text'},h('p',null,'Synthetic narration'))]}))));
    window.setShown=(shown,playing=true)=>{buffer.entries.get('step').shown=shown;window.renderScene(buffer.view('step').board,playing);};
    window.checkWidths=()=>{
      const width=document.querySelector('.teaching-board-canvas').clientWidth;
      const geometry=layoutBoard(blocks,width,new Set(blocks.map(b=>b.id)),[],{},'Synthetic lesson',{},{responsive:true});
      return geometry.pages.flatMap(p=>p.elements).filter(e=>e.customData?.bodyText).every(e=>
        e.customData.wrappedLines.every(line=>measureBoardText(line,e.fontSize)<=e.width+1));
    };
    window.setShown(1);
  `}));
  await page.goto(base);
  const board=page.locator('[data-layout]'),arrow=page.getByRole('button',{name:'回到讲解位置',exact:true});
  await page.locator('.excalidraw canvas').first().waitFor({state:'visible'});
  await page.evaluate(()=>document.fonts.ready);await page.waitForTimeout(350);
  assert.equal(await page.locator('[data-code-id]').count(),0,'code cannot precede the explanation');
  assert.equal(await page.getByRole('button',{name:'回到最新板书',exact:true}).count(),0);
  await page.evaluate(()=>window.setShown(300));
  await page.waitForFunction(()=>{
    const n=document.querySelector('[data-writing-y]');return n.dataset.writingId==='explain'&&Math.abs(Number(n.dataset.writingY)-n.scrollTop-n.clientHeight*.55)<n.clientHeight*.2;
  });
  assert.equal(await page.locator('[data-code-id]').count(),0);
  await page.waitForFunction(()=>document.querySelector('[data-writing-y]').dataset.boardMoving!=='true');
  const trajectory=await page.evaluate(async()=>{
    const n=document.querySelector('[data-writing-y]'),samples=[];
    const revisions=document.querySelector('[data-layout-revisions]').dataset.layoutRevisions;
    window.setShown(420);
    for(let i=0;i<100;i++){
      const time=await new Promise(resolve=>requestAnimationFrame(resolve));
      if(i===10)window.setShown(470);
      samples.push({time,top:n.scrollTop,height:n.scrollHeight});
    }
    return {samples,revisions,after:document.querySelector('[data-layout-revisions]').dataset.layoutRevisions};
  });
  assert.equal(new Set(trajectory.samples.map(s=>s.height)).size,1,'reveal cannot resize the canvas');
  assert.equal(trajectory.revisions,trajectory.after,'scroll frames cannot relayout the scene');
  let movingFrames=0;
  for(let i=1;i<trajectory.samples.length;i++){
    const a=trajectory.samples[i-1],b=trajectory.samples[i],delta=b.top-a.top;
    assert(delta>=-.5,'continuous reveal cannot reverse the scroll');
    assert(delta<=Math.min(32,b.time-a.time)*.95+3,'scroll frame remains speed bounded');
    if(delta>.1)movingFrames++;
  }
  assert(movingFrames>8,'new writing moves over multiple consecutive frames');
  await page.waitForFunction(()=>!document.querySelector('.board-return'));
  await page.evaluate(()=>window.setShown(550));
  await page.waitForFunction(()=>{const n=document.querySelector('[data-writing-y]');return Math.abs(Number(n.dataset.writingY)-n.scrollTop-n.clientHeight*.55)<n.clientHeight*.2;});
  await board.hover();await page.mouse.wheel(0,-500);
  await page.waitForFunction(()=>document.querySelector('[data-board-following]').dataset.boardFollowing==='false');
  await arrow.waitFor({state:'visible'});
  assert.equal(await arrow.getAttribute('title'),'回到讲解位置');
  const frozen=await board.evaluate(n=>n.scrollTop);
  await page.evaluate(()=>window.setShown(580));await page.waitForTimeout(450);
  assert(Math.abs(await board.evaluate(n=>n.scrollTop)-frozen)<3,'manual review must not be pulled forward');
  if(process.env.TEST_OUTPUT){mkdirSync(process.env.TEST_OUTPUT,{recursive:true});await page.screenshot({path:`${process.env.TEST_OUTPUT}/review-${width}.png`});}
  await arrow.click();
  await page.waitForFunction(()=>{const n=document.querySelector('[data-writing-y]');return n.dataset.boardFollowing==='true'&&n.dataset.boardMoving==='false'&&Math.abs(Number(n.dataset.writingY)-n.scrollTop-n.clientHeight*.55)<3;});
  await page.waitForFunction(()=>!document.querySelector('.board-return'));
  if(width===1600){
    const separator=page.getByRole('separator',{name:'调整讲解栏宽度'}),bounds=await separator.boundingBox();
    const previous=await board.evaluate(n=>n.clientWidth);
    await page.mouse.move(bounds.x+4,bounds.y+100);await page.mouse.down();await page.mouse.move(bounds.x-160,bounds.y+100,{steps:12});await page.mouse.up();
    await page.waitForFunction(previous=>document.querySelector('[data-layout]').clientWidth<previous-100,previous);
    await page.waitForFunction(()=>{const n=document.querySelector('[data-writing-y]');return Math.abs(Number(n.dataset.writingY)-n.scrollTop-n.clientHeight*.55)<n.clientHeight*.2;});
  }
  assert(await page.evaluate(()=>window.checkWidths()),'actual font metrics fit all rendered lines after resize');
  const layout=JSON.parse(await board.getAttribute('data-layout')),client=await board.evaluate(n=>n.clientWidth);
  assert(layout.every(p=>p.x+p.width<=client));
  const explanationPlacement=layout.find(p=>p.id==='explain');assert(explanationPlacement.width>client*.85,'long explanation uses available width');
  await page.evaluate(()=>window.setShown(950));
  await page.locator('[data-code-id=code]').waitFor();
  const partial=await page.locator('.teaching-code-content').textContent();
  assert(partial.split('\n').length<30&&partial.split('\n').length>1);
  assert(partial.startsWith('  if'),'indentation is preserved');
  await page.waitForFunction(()=>{const n=document.querySelector('[data-writing-y]');return n.dataset.writingId==='code'&&Math.abs(Number(n.dataset.writingY)-n.scrollTop-n.clientHeight*.55)<n.clientHeight*.2;});
  const codeDimensions=await page.locator('.teaching-code-content').evaluate(n=>({scroll:n.scrollWidth,width:n.clientWidth}));
  assert(codeDimensions.scroll>codeDimensions.width,'long code line can be scrolled without clipping');
  await page.locator('.teaching-code-content').evaluate(n=>n.scrollLeft=n.scrollWidth);
  assert(await page.locator('.teaching-code-content').evaluate(n=>n.scrollLeft>0));
  await page.locator('.teaching-code-content').evaluate(n=>n.scrollLeft=0);
  await page.waitForTimeout(450);const paused=await board.evaluate(n=>n.scrollTop);
  await page.evaluate(()=>window.setShown(980,false));await page.waitForTimeout(450);
  assert(Math.abs(await board.evaluate(n=>n.scrollTop)-paused)<3,'paused presentation does not move');
  await page.evaluate(()=>window.setShown(1000));
  await page.waitForFunction(()=>{const n=document.querySelector('[data-writing-y]');return Math.abs(Number(n.dataset.writingY)-n.scrollTop-n.clientHeight*.55)<n.clientHeight*.2;});
  assert.equal(await page.locator('.teaching-code-content').textContent(),code);
  await page.evaluate(()=>window.setShown(520));
  await page.waitForFunction(()=>document.querySelector('[data-writing-y]').dataset.boardMoving==='true');
  const hidden=await page.evaluate(()=>{
    Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});
    document.dispatchEvent(new Event('visibilitychange'));
    const n=document.querySelector('[data-writing-y]');return {top:n.scrollTop,moving:n.dataset.boardMoving};
  });
  assert.equal(hidden.moving,'false');await page.waitForTimeout(200);
  assert.equal(await board.evaluate(n=>n.scrollTop),hidden.top,'hidden document has no active scroll');
  await page.evaluate(()=>{delete document.hidden;document.dispatchEvent(new Event('visibilitychange'));});
  await page.waitForFunction(()=>document.querySelector('[data-writing-y]').dataset.boardMoving==='true');
  await board.hover();await page.mouse.wheel(0,120);
  await page.waitForFunction(()=>{const n=document.querySelector('[data-writing-y]');return n.dataset.boardFollowing==='false'&&n.dataset.boardMoving==='false';});
  const takeover=await board.evaluate(n=>n.scrollTop);
  await page.evaluate(()=>window.setShown(540));await page.waitForTimeout(250);
  assert.equal(await board.evaluate(n=>n.scrollTop),takeover,'downward wheel during animation owns the viewport');
  await page.evaluate(()=>{const n=document.querySelector('[data-layout]');n.scrollTop=n.scrollHeight;});
  await arrow.waitFor({state:'visible'});await arrow.click();
  await page.waitForFunction(()=>document.querySelector('[data-writing-y]').dataset.boardMoving==='false');
  await page.emulateMedia({reducedMotion:'reduce'});
  await page.evaluate(()=>window.setShown(1000));
  await page.waitForFunction(()=>{const n=document.querySelector('[data-writing-y]');return n.dataset.writingId==='code'&&n.dataset.boardMoving!=='true'&&Math.abs(Number(n.dataset.writingY)-n.scrollTop-n.clientHeight*.55)<3;});
  await page.emulateMedia({reducedMotion:'no-preference'});
  if(process.env.TEST_OUTPUT)await page.screenshot({path:`${process.env.TEST_OUTPUT}/code-${width}.png`});
  const staged=[{id:'table',type:'table',columns:['Role','Meaning'],rows:Array.from({length:25},(_,i)=>['Role '+i,'Synthetic meaning '+i])},
    {id:'sequence',type:'graph',diagram:'sequence',nodes:[{id:'a',label:'Client'},{id:'b',label:'Server'}],edges:Array.from({length:12},(_,i)=>({from_id:i%2?'b':'a',to_id:i%2?'a':'b',label:'Synthetic message '+i}))}];
  await page.evaluate(blocks=>{
    window.staged=blocks;
    window.setStage=(id,progress)=>window.renderScene({presentation:{title:'Staged geometry',all_blocks:blocks,
      blocks:blocks.filter(b=>b.id===id).map(b=>({...b,reveal_progress:progress})),writing:{block_id:id,progress}}},true,'staged');
    window.setStage('table',.04);
  },staged);
  await page.waitForFunction(()=>document.querySelector('[data-writing-y]').dataset.writingId==='table');
  const stagedHeight=await board.evaluate(n=>n.scrollHeight);
  await page.evaluate(()=>window.setStage('table',.8));
  await page.waitForFunction(()=>{const n=document.querySelector('[data-writing-y]');return n.dataset.boardMoving==='false'&&Number(n.dataset.writingY)-n.scrollTop<=n.clientHeight*.7;});
  assert.equal(await board.evaluate(n=>n.scrollHeight),stagedHeight);
  if(process.env.TEST_OUTPUT)await page.screenshot({path:`${process.env.TEST_OUTPUT}/table-${width}.png`});
  await page.evaluate(()=>window.setStage('sequence',.9));
  await page.waitForFunction(()=>{const n=document.querySelector('[data-writing-y]');return n.dataset.writingId==='sequence'&&n.dataset.writingPage===n.dataset.boardPage&&n.dataset.boardMoving==='false'&&Math.abs(Number(n.dataset.writingY)-n.scrollTop-n.clientHeight*.55)<n.clientHeight*.2;});
  if(process.env.TEST_OUTPUT)await page.screenshot({path:`${process.env.TEST_OUTPUT}/sequence-${width}.png`});
  const pages=Array.from({length:6},(_,i)=>({id:'p'+i,type:'text',title:'Topic '+i,span:3,text:'Synthetic pagination line. '.repeat(65)}));
  await page.evaluate(blocks=>window.renderScene({presentation:{title:'Pages',blocks:blocks.slice(0,1),all_blocks:blocks,writing:{block_id:'p0',progress:.2}}},true,'pages'),pages);
  await page.evaluate(blocks=>window.renderScene({presentation:{title:'Pages',blocks,all_blocks:blocks,writing:{block_id:'p5',progress:1}}},true,'pages'),pages);
  await page.waitForFunction(()=>{const n=document.querySelector('[data-writing-page]');return n.dataset.writingPage===n.dataset.boardPage&&Number(n.dataset.boardPage)>0;});
  await page.getByRole('button',{name:'上一页板书',exact:true}).click();await arrow.waitFor({state:'visible'});
  await arrow.click();await page.waitForFunction(()=>{const n=document.querySelector('[data-writing-page]');return n.dataset.writingPage===n.dataset.boardPage;});
  await page.evaluate(()=>window.setStage('sequence',.9));
  await page.waitForFunction(()=>document.querySelector('[data-writing-y]').dataset.boardMoving==='true');
  const detached=await page.evaluate(()=>{window.detachedBoard=document.querySelector('[data-writing-y]');window.unmountBoard();return window.detachedBoard.scrollTop;});
  await page.waitForTimeout(200);
  assert.equal(await page.evaluate(()=>window.detachedBoard.scrollTop),detached,'unmount cancels pending animation');
  await page.reload();await page.locator('.excalidraw canvas').first().waitFor({state:'visible'});
  await page.waitForTimeout(350);assert.deepEqual(errors,[]);
  console.log(`Writing board ${width}px: continuous bounded RAF, retarget, fixed height/layout, manual takeover/return, reduced motion, pause, visibility/unmount, resize, table/sequence, pagination and reload passed; pageErrors=[]`);
  await context.close();
 }
}finally{await browser.close();}
