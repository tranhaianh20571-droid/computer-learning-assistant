import {createRequire} from "node:module";
import {spawnSync} from "node:child_process";
import {mkdirSync,writeFileSync} from "node:fs";
import assert from "node:assert/strict";
const require=createRequire(import.meta.url),{chromium}=require(process.env.PW_ROOT+"/playwright");
const backend=process.env.TEST_BACKEND,base="http://127.0.0.1:5173",output=process.env.TEST_OUTPUT;
assert.equal(new URL(backend).hostname,"127.0.0.1");assert.notEqual(new URL(backend).port,"8000");mkdirSync(output,{recursive:true});
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const checks=[],errors=[];
async function coursePage(audio=false){
 const context=await browser.newContext({viewport:{width:1800,height:1100}});
 const guest=await context.request.get(backend+"/api/auth/guest");
 const fixture=spawnSync(process.env.TEST_PYTHON,[process.env.TEST_FIXTURE,(await guest.json()).user_id],{encoding:"utf8",env:process.env});
 assert.equal(fixture.status,0,fixture.stderr);const course=JSON.parse(fixture.stdout);
 await context.addInitScript(({backend,audio})=>{
  const original=window.fetch.bind(window);window.fetch=(input,init)=>original(typeof input==="string"&&input.startsWith('/api/')?backend+input:input,init);
  const NativeAudio=window.Audio;window.__lectureMedia=[];
  window.Audio=function(...args){const media=new NativeAudio(...args);window.__lectureMedia.push(media);return media;};
  if(audio){const play=HTMLMediaElement.prototype.play;window.__allowSpeech=false;
   HTMLMediaElement.prototype.play=function(){if(!window.__allowSpeech)return Promise.reject(new DOMException('synthetic autoplay block','NotAllowedError'));return play.call(this);};}
 },{backend,audio});
 const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
 await page.route('**/api/teaching/audio/*/content',async route=>{
  const path=new URL(route.request().url()).pathname;
  await route.fulfill({response:await route.fetch({url:backend+path})});
 });
 await page.goto(base+'/#/course/'+course.course_id);await page.getByRole('button',{name:'开始学习',exact:true}).waitFor();
 if(audio){const saved=await page.evaluate(async()=>{
  const response=await fetch('/api/capabilities/configs',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',
   body:JSON.stringify({kind:'tts',protocol:'minimax',endpoint:'https://voice.example.test',model_name:'speech-2.8-hd',credentials:{api_key:'synthetic-key',voice_id:'synthetic-voice'},owner_scope:'user'})});
  return response.status;
 });assert.equal(saved,201);}
 await page.getByRole('button',{name:'开始学习',exact:true}).click();return {page,context};
}
try{
 const {page,context}=await coursePage();
 await page.waitForFunction(()=>document.querySelector('.lecture-segment p')?.textContent.length>2,null,{timeout:30000});
 const start=await page.locator('.lecture-segment p').first().textContent();await page.waitForTimeout(1800);
 const later=await page.locator('.lecture-segment p').first().textContent();
 assert(later.length>start.length&&later.length-start.length<=24);
 assert.equal(await page.locator('.lecture-topic,.lecture-phase').count(),0);
 const style=await page.locator('.lecture-segment').first().evaluate(node=>{const css=getComputedStyle(node);return {border:css.borderTopWidth,shadow:css.boxShadow,padding:css.padding};});
 assert.equal(style.border,'0px');assert.equal(style.shadow,'none');checks.push('plain_paragraphs_and_slow_buffer');
 await page.getByRole('button',{name:'暂停讲解',exact:true}).click();
 const frozen=await page.locator('.stream').textContent();const board=await page.locator('[data-visible-blocks]').getAttribute('data-visible-blocks');
 await page.waitForTimeout(600);assert.equal(await page.locator('.stream').textContent(),frozen);assert.equal(await page.locator('[data-visible-blocks]').getAttribute('data-visible-blocks'),board);
 checks.push('pause_freezes_text_and_board');
 await page.reload();await page.getByRole('button',{name:'继续讲解',exact:true}).waitFor();
 assert.equal((await page.locator('.stream').textContent()).trim(),frozen.trim());checks.push('refresh_restores_paused_cursor');
 await page.getByRole('button',{name:'继续讲解',exact:true}).click();
 await page.locator('.lecture-segment:not([data-teaching-preview])').first().waitFor({timeout:30000});
 const revisions=await page.locator('[data-layout-revisions]').getAttribute('data-layout-revisions');
 const layout=await page.locator('[data-layout]').getAttribute('data-layout');
 await page.waitForTimeout(1800);
 assert.equal(await page.locator('[data-layout-revisions]').getAttribute('data-layout-revisions'),revisions);
 assert.equal(await page.locator('[data-layout]').getAttribute('data-layout'),layout);checks.push('typed_characters_do_not_relayout_board');
 await page.getByRole('button',{name:'放大板书',exact:true}).click();
 const scale=await page.locator('[data-board-scale]').getAttribute('data-board-scale');await page.waitForTimeout(700);
 assert.equal(await page.locator('[data-board-scale]').getAttribute('data-board-scale'),scale);checks.push('incremental_updates_preserve_manual_zoom');
 await page.screenshot({path:output+'/plain-stable-board.png',fullPage:true});await context.close();
 const second=await coursePage(true),voicePage=second.page;
  await voicePage.getByRole('button',{name:'点击开启声音',exact:true}).waitFor({timeout:30000});checks.push('autoplay_block_is_visible');
  await voicePage.evaluate(()=>window.__allowSpeech=true);
 await voicePage.getByRole('button',{name:'点击开启声音',exact:true}).click();
 await voicePage.waitForFunction(()=>window.__lectureMedia.some(media=>!media.paused&&media.currentTime>.2),null,{timeout:10000});checks.push('saved_voice_generates_and_media_plays');
 await voicePage.getByRole('button',{name:'暂停讲解',exact:true}).click();
 const position=await voicePage.evaluate(()=>window.__lectureMedia.at(-1).currentTime);await voicePage.waitForTimeout(400);
 assert(await voicePage.evaluate(()=>window.__lectureMedia.at(-1).paused));
  assert(Math.abs((await voicePage.evaluate(()=>window.__lectureMedia.at(-1).currentTime))-position)<.1);checks.push('pause_stops_audio_immediately');
  const checkpoint=await voicePage.evaluate(async()=>{
    const id=location.hash.split('/').at(-1);
    return (await (await fetch('/api/teaching/'+id,{credentials:'include'})).json()).last_checkpoint;
  });
  assert(checkpoint.presentation.paused&&checkpoint.presentation.audio_id);
  assert(checkpoint.position_ms>=150);
  await voicePage.reload();await voicePage.getByRole('button',{name:'继续讲解',exact:true}).waitFor();
  await voicePage.waitForFunction(position=>Math.abs((window.__lectureMedia.at(-1)?.currentTime||0)-position/1000)<.15,
    checkpoint.position_ms,{timeout:10000});
  assert(await voicePage.evaluate(()=>window.__lectureMedia.at(-1).paused));checks.push('refresh_restores_audio_position_while_paused');
  await voicePage.evaluate(()=>window.__allowSpeech=true);
 await voicePage.getByRole('button',{name:'继续讲解',exact:true}).click();
 await voicePage.waitForFunction(()=>window.__lectureMedia.at(-1).currentTime>1,null,{timeout:10000});
 await voicePage.waitForFunction(()=>document.querySelector('.lw-simple')?.dataset.currentSegment==='2',null,{timeout:25000});
 checks.push('audio_end_and_text_end_gate_automatic_next_segment');
  await voicePage.getByRole('button',{name:'暂停讲解',exact:true}).click();
  await second.context.close();
  const third=await coursePage(true),skipPage=third.page;
  await skipPage.getByRole('button',{name:'继续文字讲解',exact:true}).waitFor({timeout:30000});
  await skipPage.getByRole('button',{name:'继续文字讲解',exact:true}).click();
  await skipPage.waitForFunction(()=>window.__lectureMedia.at(-1)?.src.includes('/audio/'),null,{timeout:30000});
  await skipPage.waitForTimeout(400);assert(await skipPage.evaluate(()=>window.__lectureMedia.at(-1).paused));
  checks.push('text_only_choice_does_not_restart_late_audio');
  await third.context.close();assert.deepEqual(errors,[]);
 writeFileSync(output+'/lecture-audio-browser.json',JSON.stringify({checks,pageErrors:errors,synthetic:true},null,2));console.log(JSON.stringify({checks,pageErrors:errors}));
}catch(error){
 for(const context of browser.contexts())for(const page of context.pages()){
  console.log(JSON.stringify(await page.evaluate(()=>({state:document.querySelector('.lw-simple')?.dataset.audioState,
   media:window.__lectureMedia?.map(media=>({position:media.currentTime,duration:media.duration,ready:media.readyState,error:media.error?.code,paused:media.paused})),
   current:document.querySelector('.lw-simple')?.dataset.currentSegment}))));
 }
 throw error;
}finally{await browser.close();}
