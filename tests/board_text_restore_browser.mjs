import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir} from "node:fs/promises";

const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright-core");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const output=".backup/board-text-restore";
await mkdir(output,{recursive:true});
const checks=[];
try{
 for(const width of [1440,390]){
  const context=await browser.newContext({viewport:{width,height:900}}),page=await context.newPage(),errors=[];
  page.on("pageerror",error=>errors.push(error.message));
  await page.route("**/src/main.tsx*",route=>route.fulfill({contentType:"text/javascript",body:`
    import React from '/node_modules/.vite/deps/react.js';
    import ReactDOM from '/node_modules/.vite/deps/react-dom_client.js';
    import {restore} from '/node_modules/.vite/deps/@excalidraw_excalidraw.js';
    import TeachingBoard from '/src/ui/TeachingBoard.tsx';
    import {layoutBoard} from '/src/lib/boardLayout.ts';
    import {presentBoard,stableElements} from '/src/lib/boardPresentation.ts';
    import '/src/tokens.css';
    import '/src/classroom.css';
    const block={id:'synthetic-table',type:'table',title:'配置与正文',
      columns:['部分','给谁看','职责'],rows:[['head','浏览器','描述文档配置'],['body','读者','呈现网页正文']]};
    const page=layoutBoard([block],Math.max(240,innerWidth-64),new Set([block.id]),[],{},'文档结构',{},
      {responsive:true,theme:'ink',pageHeight:500}).pages[0],cache=new Map();
    window.seedLegacyCache=()=>stableElements(page.elements.filter(e=>e.customData?.tableCellOrder!==undefined)
      .map(e=>({...e,text:''})),cache,elements=>restore({elements},null,null,
        {repairBindings:false,refreshDimensions:true}).elements).filter(e=>e.isDeleted).length;
    window.checkRestore=progress=>{
      const raw=presentBoard(page,[{...block,reveal_progress:progress}]);
      const restored=stableElements(raw,cache,elements=>restore({elements},null,null,
        {repairBindings:false,refreshDimensions:true}).elements);
      return {cells:restored.filter(e=>e.customData?.tableCellOrder!==undefined).map(e=>
        ({id:e.id,text:e.text,originalText:e.originalText,isDeleted:e.isDeleted})),
        expected:raw.filter(e=>e.customData?.tableCellOrder!==undefined).map(e=>({id:e.id,text:e.text})),
        invisibleFrames:restored.filter(e=>e.customData?.elementRole==='table-frame'&&(e.opacity===0||e.isDeleted)).length};
    };
    const root=ReactDOM.createRoot(document.getElementById('root')),h=React.createElement;
    window.showTable=progress=>root.render(h('div',{className:'ink-classroom',style:{display:'block',height:'100dvh'}},
      h('div',{className:'bd-col',style:{height:'100%'}},h(TeachingBoard,{theme:'ink',sceneKey:'table',
        scene:{presentation:{title:'文档结构',blocks:[{...block,reveal_progress:progress}],all_blocks:[block],
          writing:{block_id:block.id,progress}}},presenting:true}))));
    window.showTable(.161);
  `}));
  await page.goto(process.env.TEST_FRONTEND||"http://127.0.0.1:5173");
  await page.waitForFunction(()=>typeof window.checkRestore==="function");
  assert.equal(await page.evaluate(()=>window.seedLegacyCache()),9,"legacy blank cells are tombstoned by the real library");
  const samples=await page.evaluate(()=>[.161,.2,.35,.6,.9,1].map(progress=>({progress,...window.checkRestore(progress)})));
  for(const sample of samples){
   for(const expected of sample.expected){
    const actual=sample.cells.find(cell=>cell.id===expected.id);
    assert.equal(actual?.text,expected.text,`restored text at ${sample.progress} for ${expected.id}`);
    assert(!actual.isDeleted||!expected.text,`text cannot retain deletion state at ${sample.progress}`);
   }
  }
  const final=samples.at(-1);
  assert.equal(final.cells.length,9);
  assert(final.cells.every(cell=>cell.text&&!cell.isDeleted));
  assert.equal(final.invisibleFrames,0);
  checks.push(`actual-excalidraw-restores-all-cells-after-empty-and-partial-${width}`);
  await page.evaluate(()=>window.showTable(.65));
  await page.waitForFunction(()=>document.querySelector('.teaching-board-canvas')?.dataset.visibleBlocks==="synthetic-table");
  await page.locator(".excalidraw canvas").first().waitFor({state:"visible"});
  const height=await page.locator(".teaching-board-canvas").evaluate(n=>n.scrollHeight);
  await page.evaluate(()=>window.showTable(1));
  await page.waitForTimeout(350);
  assert.equal(await page.locator(".teaching-board-canvas").evaluate(n=>n.scrollHeight),height);
  await page.screenshot({path:output+`/table-${width}.png`});
  assert.deepEqual(errors,[]);
  checks.push(`table-completes-with-fixed-geometry-${width}`);
  await context.close();
 }
 console.log(JSON.stringify({passed:checks.length,checks,synthetic:true}));
}finally{await browser.close();}
