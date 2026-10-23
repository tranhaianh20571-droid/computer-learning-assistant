import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
import {mockBuiltFrontend} from './frontend_dist_mock.mjs';

const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+'/playwright-core');
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const checks=[],output='.backup/lecture-sync-follow';
await mkdir(output,{recursive:true});
const text=Array.from({length:24},(_,i)=>`合成段落${i+1}。${'这里用于验证语音同步和文字滚动，音频暂停时文字必须保持原位。'.repeat(3)}`).join('\n\n');
const block={id:'synthetic-text',type:'text',text:'关键内容随语音逐步显示。'.repeat(100)};
const board={beats:[{id:'b',text,block_ids:[block.id]}],presentation:{version:2,title:'合成同步课堂',blocks:[block]}};
const step={step_no:1,step_id:'synthetic-step',text,board,status:'ready',text_version_no:1,
  audio_id:'synthetic-audio',audio_ready:true,audio_subtitles:[{text,begin_ms:0,end_ms:60000}],lesson_finished:false};
for(const width of [1440,390]){
 const context=await browser.newContext({viewport:{width,height:850},serviceWorkers:'block'});
 const page=await context.newPage(),errors=[];
 page.on('pageerror',error=>errors.push(error.message));
 await context.addInitScript(()=>{
  window.__media=[];
  window.Audio=class{
   paused=true;currentTime=0;duration=60;src='';preload='';readyState=4;seeking=false;
   constructor(){window.__media.push(this);}
   play(){this.paused=false;return Promise.resolve();}
   pause(){this.paused=true;}
   load(){this.readyState=4;this.currentTime=0;queueMicrotask(()=>this.onloadedmetadata?.());}
   removeAttribute(name){if(name==='src')this.src='';}
  };
 });
 const served=await mockBuiltFrontend(page);
 let audioRequests=0,audioRuns=0;
 let ws={conversation_id:'synthetic-sync',course_id:'synthetic-course',lesson_id:'synthetic-lesson',
  generation:1,kind:'teaching',session_state:'active',server_managed:true,tts_authorized:true,audio_mode:'tts',
  steps:[step],followups:[],queued_tasks:[],last_checkpoint:null};
 const json=(route,body)=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
 await page.route('**/api/**',route=>{
  const path=new URL(route.request().url()).pathname;
  if(path==='/api/auth/me')return json(route,{user_id:'synthetic-owner',status:'approved'});
  if(path==='/api/cards'||path==='/api/materials')return json(route,{data:[]});
  if(path==='/api/teaching/synthetic-sync')return json(route,ws);
  if(path==='/api/teaching/synthetic-sync/audio'){
   audioRequests++;return json(route,{task_id:'synthetic-audio-task',status:'queued'});
  }
  if(path==='/api/teaching/tasks/synthetic-audio-task/run'){
   audioRuns++;ws={...ws,steps:[{...step,audio_id:'synthetic-repaired-audio'}]};
   return json(route,{status:'queued'});
  }
  if(path.endsWith('/checkpoint'))return json(route,{saved:true});
  if(path==='/api/planning/library')return json(route,{routes:[],courses:[],drafts:[]});
  return json(route,{});
 });
 try{
  await page.goto(served.origin+'/#/lecture/synthetic-sync');
  const stream=page.getByRole('log',{name:'讲解文字流'});
  const readTail=()=>stream.evaluate(node=>{
   const paragraph=[...node.querySelectorAll(".lecture-segment p")].at(-1);
   const range=document.createRange();range.selectNodeContents(paragraph);
   const line=[...range.getClientRects()].at(-1);
   return {gap:node.scrollHeight-node.scrollTop-node.clientHeight,
    lineBottom:line.bottom,viewportBottom:node.getBoundingClientRect().top+node.clientTop+node.clientHeight};
  });
  await page.waitForFunction(()=>document.querySelector('.lw-simple')?.dataset.audioState==='playing');
  await page.waitForTimeout(200);
  assert.equal((await stream.innerText()).trim(),'','audio time zero must not reveal text');
  await page.evaluate(()=>{window.__media.at(-1).currentTime=24;});
  await page.waitForFunction(()=>document.querySelector('.stream')?.textContent.length>200);
  await page.waitForFunction(()=>{const s=document.querySelector('.stream');return s.scrollHeight>s.clientHeight+100&&s.scrollHeight-s.scrollTop-s.clientHeight<3;});
  assert.equal(await page.getByRole('button',{name:'回到最新',exact:true}).count(),0);
  checks.push(`audio_clock_and_bottom_follow_${width}`);
  for(const position of [24.2,24.4,24.6,24.8,25,25.2,25.4,25.6]){
   await page.evaluate(position=>{window.__media.at(-1).currentTime=position;},position);
   await page.waitForTimeout(650);
   const tail=await readTail();
   assert(tail.gap<3,`small line growth must keep following: ${JSON.stringify(tail)}`);
   assert(tail.lineBottom<=tail.viewportBottom-8,"last displayed line must remain fully above the bottom edge");
  }
  checks.push(`small-increments-keep-last-line-visible-${width}`);
  const enlarged=await page.addStyleTag({content:".ink-classroom .lecture-segment p{font-size:26px;line-height:1.85}"});
  await page.waitForFunction(()=>{const s=document.querySelector(".stream");return s.scrollHeight-s.scrollTop-s.clientHeight<3;});
  for(const position of [25.8,26,26.2]){
   await page.evaluate(position=>{window.__media.at(-1).currentTime=position;},position);
   await page.waitForTimeout(850);
   const tail=await readTail();
   assert(tail.gap<3&&tail.lineBottom<=tail.viewportBottom-8,
    `enlarged text must keep its complete last line visible: ${JSON.stringify(tail)}`);
  }
  await page.screenshot({path:`${output}/large-text-tail-${width}.png`});
  await enlarged.evaluate(node=>node.remove());
  await page.waitForTimeout(800);
  checks.push(`enlarged-text-last-line-visible-${width}`);
  const navBefore=await stream.boundingBox();
  await page.getByRole('button',{name:'收起课堂侧栏',exact:true}).click();
  await page.getByRole('button',{name:'展开课堂侧栏',exact:true}).waitFor();
  assert.equal(await page.locator('.ink-classroom').getAttribute('data-nav-collapsed'),'true');
  assert.equal(await page.locator('.lw-simple').getAttribute('data-audio-state'),'playing');
  await page.getByRole('button',{name:'展开课堂侧栏',exact:true}).click();
  const navAfter=await stream.boundingBox();
  assert(Math.abs(navBefore.width-navAfter.width)<2);
  checks.push(`collapsible-nav-preserves-playing-and-restores-layout-${width}`);
  await stream.hover();await page.mouse.wheel(0,140);await page.waitForTimeout(150);
  assert.equal(await page.getByRole('button',{name:'回到最新',exact:true}).count(),0,
    'downward wheel at bottom must not disable automatic follow');
  checks.push(`downward-scroll-does-not-suspend-follow-${width}`);

  await page.evaluate(()=>{
   window.__scrollSamples=[];
   window.__sampleFrame=0;
   const sample=()=>{const s=document.querySelector('.stream');window.__scrollSamples.push({time:performance.now(),top:s.scrollTop});window.__sampleFrame=requestAnimationFrame(sample);};
   sample();
  });
  for(const position of [27,30,33,36]){
   await page.evaluate(position=>{window.__media.at(-1).currentTime=position;},position);
   await page.waitForTimeout(160);
  }
  await page.waitForFunction(()=>{const s=document.querySelector('.stream');return s.scrollHeight-s.scrollTop-s.clientHeight<3;});
  const samples=await page.evaluate(()=>{cancelAnimationFrame(window.__sampleFrame);return window.__scrollSamples;});
  assert(samples.some((value,i)=>i&&value.top>samples[i-1].top+1));
  for(let i=1;i<samples.length;i++){
   assert(samples[i].top>=samples[i-1].top-1,'automatic follow must not oscillate backwards');
   assert(samples[i].top-samples[i-1].top<=31,'automatic follow has no poll-sized jump');
  }
  checks.push(`smooth_follow_without_reverse_or_large_jump_${width}`);

  const before=await stream.evaluate(node=>node.clientHeight);
  await stream.hover();await page.mouse.wheel(0,-260);
  await page.getByRole('button',{name:'回到最新',exact:true}).waitFor();
  await page.locator('.teaching-pointer').waitFor({state:'visible'});
  checks.push(`text-history-does-not-hide-board-pointer-${width}`);
  assert.equal(await stream.evaluate(node=>node.clientHeight),before,'history control must not resize the scroll viewport');
  const historyTop=await stream.evaluate(node=>node.scrollTop);
  await page.evaluate(()=>{window.__media.at(-1).currentTime=42;});
  await page.waitForTimeout(1700);
  assert(Math.abs((await stream.evaluate(node=>node.scrollTop))-historyTop)<2,'polling and new words cannot override manual reading');
  await page.getByRole('button',{name:'回到最新',exact:true}).click();
  await page.waitForFunction(()=>{const s=document.querySelector('.stream');return s.scrollHeight-s.scrollTop-s.clientHeight<3;});
  checks.push(`manual_reading_stays_put_and_explicit_resume_${width}`);
  await stream.hover();await page.mouse.wheel(0,-200);
  await page.getByRole('button',{name:'回到最新',exact:true}).waitFor();
  await page.mouse.wheel(0,10000);
  await page.getByRole('button',{name:'回到最新',exact:true}).waitFor({state:'hidden'});
  checks.push(`manual-return-to-bottom-restores-follow-${width}`);

  const buffered=await stream.innerText();
  await page.evaluate(()=>{const a=window.__media.at(-1);a.readyState=4;a.onstalled();a.currentTime=43;});
  await page.waitForFunction(length=>document.querySelector('.stream').textContent.length>length,buffered.length);
  checks.push(`network_stall_with_playable_buffer_keeps_sync_${width}`);

  await page.evaluate(()=>{const a=window.__media.at(-1);a.onwaiting();});
  await page.waitForFunction(()=>document.querySelector('.lw-simple')?.dataset.audioState==='waiting');
  const frozen=await stream.innerText();
  const frozenBoard=await page.locator('.teaching-board-canvas').getAttribute('data-visible-blocks');
  await page.evaluate(()=>{window.__media.at(-1).currentTime=46;});
  await page.waitForTimeout(1700);
  assert.equal(await stream.innerText(),frozen);
  assert.equal(await page.locator('.teaching-board-canvas').getAttribute('data-visible-blocks'),frozenBoard);
  await page.evaluate(()=>{window.__media.at(-1).onplaying();});
  await page.waitForFunction(length=>document.querySelector('.stream').textContent.length>length,frozen.length);
  checks.push(`buffering_freezes_text_and_board_then_resumes_${width}`);

  const seeking=await stream.innerText();
  await page.evaluate(()=>{const a=window.__media.at(-1);a.seeking=true;a.onseeking();a.currentTime=47;});
  await page.waitForTimeout(250);
  assert.equal(await stream.innerText(),seeking);
  await page.evaluate(()=>{const a=window.__media.at(-1);a.seeking=false;a.onseeked();});
  await page.waitForFunction(length=>document.querySelector('.stream').textContent.length>length,seeking.length);
  checks.push(`seek_freezes_then_restores_audio_clock_${width}`);

  await page.evaluate(()=>{const a=window.__media.at(-1);a.paused=true;a.onerror();});
  await page.getByRole('button',{name:'重试语音',exact:true}).waitFor();
  const failed=await stream.innerText();
  await page.waitForTimeout(1700);
  assert.equal(await stream.innerText(),failed,'media errors must preserve the displayed cursor');
  assert.equal(await page.getByRole('button',{name:'继续文字讲解',exact:true}).count(),0);
  await page.getByRole('button',{name:'重试语音',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.lw-simple')?.dataset.audioState==='playing');
  await page.evaluate(()=>{window.__media.at(-1).currentTime=50;});
  await page.waitForFunction(length=>document.querySelector('.stream').textContent.length>length,failed.length);
  checks.push(`media_error_freezes_and_retry_recovers_${width}`);

  ws={...ws,steps:[{...step,audio_id:null,audio_ready:false,audio_status:'invalid',audio_error:'subtitle_missing'}]};
  await page.getByRole('button',{name:'重试语音',exact:true}).waitFor();
  const missing=await stream.innerText();
  await page.waitForTimeout(1700);
  assert.equal(await stream.innerText(),missing);
  assert.equal(await page.locator('.lecture-segment.current').getAttribute('data-segment-no'),'1');
  await page.screenshot({path:`${output}/paused-on-missing-audio-${width}.png`,fullPage:true});
  checks.push(`invalid_subtitles_stop_automatic_next_step_${width}`);
  await page.getByRole('button',{name:'重试语音',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.lw-simple')?.dataset.audioState==='playing');
  assert.equal(audioRequests,1);assert.equal(audioRuns,1);
  await page.evaluate(()=>{window.__media.at(-1).currentTime=55;});
  await page.waitForFunction(length=>document.querySelector('.stream').textContent.length>length,missing.length);
  checks.push(`generation_retry_ensures_task_then_recovers_sync_${width}`);
  await page.getByRole('button',{name:'收起课堂侧栏',exact:true}).click();
  await page.reload();
  await page.getByRole('button',{name:'展开课堂侧栏',exact:true}).waitFor();
  await page.getByRole('button',{name:'展开课堂侧栏',exact:true}).click();
  await page.getByRole('button',{name:'收起课堂侧栏',exact:true}).waitFor();
  checks.push(`nav-preference-persists-reload-${width}`);
  assert.deepEqual(errors,[]);
 }finally{await context.close();}
}
await browser.close();
console.log(JSON.stringify({checks,synthetic:true,realProviderTested:false}));
