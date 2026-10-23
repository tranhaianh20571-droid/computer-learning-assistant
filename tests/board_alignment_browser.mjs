import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdirSync,readFileSync} from "node:fs";
const require=createRequire(import.meta.url),{chromium}=require(process.env.PW_ROOT+"/playwright");
const hash=JSON.parse(readFileSync("frontend/node_modules/.vite/deps/_metadata.json","utf8")).browserHash;
const out=".backup/board-alignment-browser";mkdirSync(out,{recursive:true});
const browser=await chromium.launch({headless:true,...(process.env.TEST_BROWSER?{executablePath:process.env.TEST_BROWSER}:{})});
try{
  for(const width of [1440,390]){
    const context=await browser.newContext({viewport:{width,height:1000}}),page=await context.newPage(),errors=[];
    page.on("pageerror",error=>errors.push(error.message));
    await page.route("**/src/main.tsx*",route=>route.fulfill({contentType:"text/javascript",body:`
      import React from '/node_modules/.vite/deps/react.js?v=${hash}';
      import ReactDOM from '/node_modules/.vite/deps/react-dom_client.js?v=${hash}';
      import TeachingBoard from '/src/ui/TeachingBoard.tsx';
      import {TeachingBuffer} from '/src/lib/teachingBuffer.ts';
      import {audioCharacters,exactAudioAlignment} from '/src/lib/audioTiming.ts';
      import {layoutBoard} from '/src/lib/boardLayout.ts';
      import {presentBoard} from '/src/lib/boardPresentation.ts';
      import {boardGroups} from '/src/lib/boardGroups.ts';
      import '/src/tokens.css';import '/src/classroom.css';
      const h=React.createElement,root=ReactDOM.createRoot(document.getElementById('root'));
      let store,blocks,beats,text,mode,current;
      window.startAlignment=(next='text')=>{
        mode=next;store=new TeachingBuffer();
        blocks=next==='text'?[{id:'a',type:'text',text:'校验输入'},{id:'b',type:'text',text:'调用工具'}]:
          next==='table'?[{id:'t',type:'table',columns:['输入','输出'],rows:[['23','24']]}]:
          [{id:'g',type:'graph',nodes:[{id:'a',label:'校验输入'},{id:'b',label:'调用工具'}],
            edges:[{from_id:'a',to_id:'b',label:'传递参数'}]}];
        const values=next==='text'?['校验输入','调用工具']:next==='table'?['输入','输出','23','24']:
          ['校验输入','调用工具','传递参数'];
        text=values.join('。')+'。';let offset=0;
        const targets=next==='text'?['text','text']:next==='table'?['cell:0:0','cell:0:1','cell:1:0','cell:1:1']:
          ['node:a','node:b','edge:0'];
        const cues=values.map((value,index)=>{const cue={block_id:next==='text'?blocks[index].id:blocks[0].id,
          target:targets[index],start:offset,end:offset+value.length};offset+=value.length+1;return cue;});
        beats=[{id:'one',text,block_ids:blocks.map(block=>block.id),cues}];
        window.voiceCues=values.map((value,index)=>({text:value+'。',begin_ms:index*1000,end_ms:(index+1)*1000,
          display_start:cues[index].start,display_end:cues[index].end+1}));
        store.receive('step',{text,beats,active:true,final:true,board:{alignment_version:1,presentation:{title:'合成对应验证',blocks}}});
        window.setVoice(0);
      };
      function render(){
        current=store.view('step').board;
        root.render(h('div',{className:'lw-simple ink-classroom',style:{height:'100dvh',display:'flex',flexDirection:'column'}},
          h(TeachingBoard,{theme:'ink',presenting:false,sceneKey:'aligned-'+mode,scene:current})));
      }
      window.setVoice=ms=>{store.audioClock('step',audioCharacters(text,window.voiceCues,ms,true));store.advance(0);render();};
      window.pauseVoice=()=>store.setPaused(true);
      window.resumeVoice=()=>{store.setPaused(false);store.advance(0);render();};
      window.inspectAlignment=()=>{
        const node=document.querySelector('.teaching-board-canvas');
        const geometry=layoutBoard(blocks,node.clientWidth,new Set(blocks.map(block=>block.id)),[],{},'合成对应验证',{},
          {responsive:true,theme:'ink',groups:boardGroups(blocks,beats)});
        const drawn=geometry.pages.flatMap(page=>presentBoard(page,current.presentation.blocks));
        return {text:drawn.filter(item=>item.type==='text').map(item=>item.text),
          active:drawn.filter(item=>item.customData?.alignmentActive).map(item=>item.text||item.id),
          ids:current.presentation.blocks.map(block=>block.id),exact:exactAudioAlignment(text,window.voiceCues)};
      };
      window.startAlignment();
    `}));
    await page.goto(process.env.TEST_FRONTEND||"http://127.0.0.1:5173");
    await page.locator(".excalidraw canvas").first().waitFor({state:"visible"});
    await page.evaluate(()=>document.fonts.ready);
    await page.evaluate(()=>window.setVoice(300));
    await page.waitForFunction(()=>document.querySelector('[data-visible-blocks]')?.dataset.visibleBlocks==='a');
    let state=await page.evaluate(()=>window.inspectAlignment());
    assert(state.text.includes("校验输入")&&!state.text.includes("调用工具"));
    await page.evaluate(()=>window.setVoice(1300));
    await page.waitForFunction(()=>document.querySelector('[data-visible-blocks]')?.dataset.visibleBlocks==='a,b');
    state=await page.evaluate(()=>window.inspectAlignment());
    assert(state.active.includes("调用工具")&&!state.active.includes("校验输入")&&state.exact);
    await page.waitForTimeout(200);
    await page.screenshot({path:out+"/phrase-"+width+".png"});
    const colored=await page.locator(".excalidraw canvas").first().evaluate(canvas=>{
      const data=canvas.getContext("2d").getImageData(0,0,canvas.width,canvas.height).data;
      let count=0;for(let i=0;i<data.length;i+=4)if(data[i]>190&&data[i+1]>145&&data[i+2]<200&&data[i+3]>0)count++;
      return count;
    });
    assert(colored>10,"the actual canvas emphasizes the phrase currently spoken");
    await page.evaluate(()=>{window.pauseVoice();window.setVoice(1900);});
    assert.deepEqual(await page.evaluate(()=>window.inspectAlignment()),state,"pause preserves the visible phrase and emphasis");
    await page.evaluate(()=>window.resumeVoice());
    for(const mode of ["table","graph"]){
      await page.evaluate(mode=>{window.startAlignment(mode);window.setVoice(1300);},mode);
      await page.waitForTimeout(200);
      const view=await page.evaluate(()=>window.inspectAlignment());
      assert(view.active.includes(mode==="table"?"输出":"调用工具"));
      if(mode==="table")assert(!view.text.includes("23")&&!view.text.includes("24"),"unspoken values stay hidden");
      await page.screenshot({path:out+"/"+mode+"-"+width+".png"});
    }
    assert.deepEqual(errors,[]);
    console.log("PASS "+width+"px: full phrase, speech-timed emphasis, canvas color, table cells, graph targets, pause, exact subtitle map; pageErrors=[]");
    await context.close();
  }
}finally{await browser.close();}
