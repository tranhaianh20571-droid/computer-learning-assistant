import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdirSync,readFileSync} from "node:fs";
import {graphFixtures} from "./graph_layout_fixtures.mjs";

const require=createRequire(import.meta.url),{chromium}=require(process.env.PW_ROOT+"/playwright");
const base=process.env.TEST_FRONTEND||"http://127.0.0.1:5173";
const hash=JSON.parse(readFileSync("frontend/node_modules/.vite/deps/_metadata.json","utf8")).browserHash;
const out=".backup/graph-layout-browser";
mkdirSync(out,{recursive:true});
const browser=await chromium.launch({headless:true,...(process.env.TEST_BROWSER?{executablePath:process.env.TEST_BROWSER}:{})});
try{
  for(const width of [1440,390]){
    const context=await browser.newContext({viewport:{width,height:1000}}),page=await context.newPage(),errors=[];
    page.on("pageerror",error=>errors.push(error.message));
    await page.route("**/src/main.tsx*",route=>route.fulfill({contentType:"text/javascript",body:`
      import React from '/node_modules/.vite/deps/react.js?v=${hash}';
      import ReactDOM from '/node_modules/.vite/deps/react-dom_client.js?v=${hash}';
      import TeachingBoard from '/src/ui/TeachingBoard.tsx';
      import {layoutBoard} from '/src/lib/boardLayout.ts';
      import {presentBoard} from '/src/lib/boardPresentation.ts';
      import {TeachingBuffer} from '/src/lib/teachingBuffer.ts';
      import '/src/tokens.css';
      import '/src/classroom.css';
      const fixtures=${JSON.stringify(graphFixtures)};
      const root=ReactDOM.createRoot(document.getElementById('root')),h=React.createElement;
      window.drawGraph=(index,progress=1)=>{
        const block=fixtures[index];
        window.currentGraph={...block,reveal_progress:progress};
        root.render(h('div',{className:'lw-simple ink-classroom',style:{height:'100dvh',display:'flex',flexDirection:'column'}},
          h(TeachingBoard,{theme:'ink',presenting:false,sceneKey:'graph-'+index,
            scene:{presentation:{title:block.title,all_blocks:[block],blocks:[window.currentGraph]}}})));
      };
      const orderedBlocks=[{id:'second',type:'text',title:'Second',text:'Synthetic second explanation. '.repeat(18)},
        {id:'first',type:'text',title:'First',text:'Synthetic first explanation. '.repeat(18)},
        {id:'third',type:'text',title:'Third',text:'Synthetic third explanation. '.repeat(18)}];
      const readingBuffer=new TeachingBuffer();
      readingBuffer.receive('ordered',{text:'x'.repeat(300),active:true,final:true,
        beats:[{id:'one',text:'x'.repeat(100),block_ids:['first']},
          {id:'two',text:'x'.repeat(100),block_ids:['second']},
          {id:'three',text:'x'.repeat(100),block_ids:['first','third']}],
        board:{presentation:{title:'Sequential board',blocks:orderedBlocks}}});
      window.drawSequential=shown=>{
        readingBuffer.entries.get('ordered').shown=shown;
        root.render(h('div',{className:'lw-simple ink-classroom',style:{height:'100dvh',display:'flex',flexDirection:'column'}},
          h(TeachingBoard,{theme:'ink',presenting:true,sceneKey:'ordered',
            scene:readingBuffer.view('ordered').board})));
      };
      window.checkGraph=()=>{
        const node=document.querySelector('.teaching-board-canvas'),block=window.currentGraph;
        const geometry=layoutBoard([block],node.clientWidth,new Set([block.id]),[],{},block.title,{},{responsive:true,theme:'ink'});
        const page=geometry.pages.find(p=>p.placements.some(p=>p.id===block.id));
        const painted=presentBoard(page,[block]);
        const ids=new Set(painted.filter(e=>e.customData?.nodeId).map(e=>e.customData.nodeId));
        return {horizontalOverflow:node.scrollWidth-node.clientWidth,
          nodes:ids.size,edges:new Set(painted.filter(e=>e.customData?.edgeIndex!==undefined).map(e=>e.customData.edgeIndex)).size,
          missingEndpoints:painted.some(e=>e.customData?.edgeIndex!==undefined&&(!ids.has(e.customData.edgeFrom)||!ids.has(e.customData.edgeTo))),
          positions:page.elements.filter(e=>e.customData?.nodeId).map(e=>[e.id,e.x,e.y,e.width,e.height])};
      };
      window.drawGraph(2);
    `}));
    await page.goto(base);
    await page.locator(".excalidraw canvas").first().waitFor({state:"visible"});
    await page.evaluate(()=>document.fonts.ready);
    await page.waitForTimeout(500);
    for(let i=0;i<graphFixtures.length;i++){
      await page.evaluate(index=>window.drawGraph(index),i);
      await page.waitForTimeout(180);
      const full=await page.evaluate(()=>window.checkGraph());
      assert.equal(await page.locator(".board-graph-detail, .board-details-layer").count(),0,
        "the graph controls from the reported screenshot are removed");
      assert(full.horizontalOverflow<=1,`${width} ${i}: graph must fit horizontal viewport`);
      assert.equal(full.edges,graphFixtures[i].edges.length);
      assert(!full.missingEndpoints);
      if(i===2||i===1||i===3){
        const viewport=page.locator(".teaching-board-canvas");
        await viewport.evaluate(n=>n.scrollTop=0);
        await page.screenshot({path:`${out}/${graphFixtures[i].id}-${width}.png`});
        const ink=await page.locator(".excalidraw canvas").first().evaluate(canvas=>{
          const ctx=canvas.getContext("2d"),data=ctx.getImageData(0,0,canvas.width,canvas.height).data;
          let count=0;
          for(let p=0;p<data.length;p+=16)if(data[p]>100&&data[p+1]>100&&data[p+2]>100&&data[p+3]>0)count++;
          return count;
        });
        assert(ink>100,"rendered canvas contains visible lettering and strokes");
      }
      for(const progress of [.15,.4,.7]){
        await page.evaluate(({i,progress})=>window.drawGraph(i,progress),{i,progress});
        await page.waitForTimeout(100);
        const partial=await page.evaluate(()=>window.checkGraph());
        assert.deepEqual(partial.positions,full.positions,"reveal cannot rearrange nodes");
        assert(!partial.missingEndpoints,"every displayed relationship has visible endpoints");
        if(i===2&&progress===.4)await page.screenshot({path:`${out}/replay-partial-${width}.png`});
      }
    }
    const order=[];
    for(const shown of [90,190,220,290]){
      await page.evaluate(value=>window.drawSequential(value),shown);
      const expected=shown===90?'first':shown<=220?'second':'third';
      await page.waitForFunction(id=>{
        const node=document.querySelector('[data-writing-id]');
        return node?.dataset.writingId===id&&node.dataset.boardPage===node.dataset.writingPage;
      },expected);
      const place=await page.locator('[data-layout]').evaluate(node=>{
        const layout=JSON.parse(node.dataset.layout),item=layout.find(item=>item.id===node.dataset.writingId);
        return {page:Number(node.dataset.writingPage),column:item.column,y:item.y,id:item.id};
      });
      order.push(place);
    }
    for(let i=1;i<order.length;i++){
      const before=order[i-1],after=order[i];
      assert(after.page>before.page||after.page===before.page&&
        (after.column>before.column||after.column===before.column&&after.y>=before.y),
        "new writing advances down the column, right to the next column, or onto the next page");
    }
    await page.waitForFunction(()=>{
      const node=document.querySelector('[data-writing-y]'),relative=Number(node.dataset.writingY)-node.scrollTop;
      return node.dataset.boardMoving==='false'&&relative>=node.clientHeight*.3&&relative<=node.clientHeight*.75;
    });
    await page.screenshot({path:`${out}/sequential-${width}.png`});
    assert.deepEqual(errors,[]);
    console.log(`PASS ${width}px: 6 graph families, complete relations, no horizontal clipping, 18 reveal states, stable geometry, screenshots and nonblank canvas; pageErrors=[]`);
    await context.close();
  }
}finally{
  await browser.close();
}
