import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {spawnSync} from "node:child_process";
import {mkdirSync,writeFileSync} from "node:fs";
const require=createRequire(import.meta.url),{chromium}=require(process.env.PW_ROOT+'/playwright');
const backend=process.env.TEST_BACKEND,base='http://127.0.0.1:5173',output=process.env.TEST_OUTPUT;
assert.equal(new URL(backend).hostname,'127.0.0.1');assert.notEqual(new URL(backend).port,'8000');mkdirSync(output,{recursive:true});
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const context=await browser.newContext({viewport:{width:1600,height:1000}}),page=await context.newPage(),checks=[],errors=[];
let browserRuns=0;page.on('pageerror',error=>errors.push(error.message));
page.on('request',request=>{if(request.url().endsWith('/run'))browserRuns++;});
await context.addInitScript(({backend})=>{
 const original=window.fetch.bind(window);window.fetch=(input,init)=>original(typeof input==='string'&&input.startsWith('/api/')?backend+input:input,init);
 const NativeAudio=window.Audio;window.__firstMedia=[];window.Audio=function(...args){const media=new NativeAudio(...args);window.__firstMedia.push(media);return media;};
},{backend});
await page.route('**/api/teaching/audio/*/content',async route=>route.fulfill({response:await route.fetch({url:backend+new URL(route.request().url()).pathname})}));
try{
 await page.goto(base+'/#/capabilities');await page.getByRole('heading',{name:'个人语音（MiniMax）',exact:true}).waitFor();
 const owner=await page.evaluate(async()=>(await (await fetch('/api/auth/guest',{credentials:'include'})).json()).user_id);
 const saved=await page.evaluate(async()=>{
  const result=await fetch('/api/capabilities/configs',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({kind:'tts',protocol:'minimax',endpoint:'https://voice.example.test',model_name:'speech-2.8-hd',credentials:{api_key:'synthetic-key',voice_id:'synthetic-voice'},owner_scope:'user'})});return result.status;
 });assert.equal(saved,201);
 const fixture=spawnSync(process.env.TEST_PYTHON,[process.env.TEST_FIXTURE,owner],{encoding:'utf8',env:process.env});
 assert.equal(fixture.status,0,fixture.stderr);const course=JSON.parse(fixture.stdout);
 await page.goto(base+'/#/courses');
 await page.waitForFunction(async ({backend,course})=>{
  const status=await (await fetch(backend+'/api/courses/'+course+'/first-preparation',{credentials:'include'})).json();
  return status.status==='ready'&&status.audio_ready;
 },{backend,course:course.course_id},{timeout:30000});
 const prepared=await page.evaluate(async ({backend,course})=>(await (await fetch(backend+'/api/courses/'+course+'/first-preparation',{credentials:'include'})).json()),{backend,course:course.course_id});
 await page.waitForFunction(async ({backend,lesson})=>{
  const result=await (await fetch(backend+'/synthetic-prewarm-counts')).json();return result.by_lesson[lesson]===1&&result.speech_calls>0;
 },{backend,lesson:prepared.lesson_id});
 checks.push('library_prepares_only_one_first_segment_with_audio');
 await page.goto(base+'/#/course/'+course.course_id);await page.getByRole('button',{name:'开始学习',exact:true}).waitFor();assert.equal(await page.getByText('首段已准备',{exact:true}).count(),0);assert.equal(await page.getByRole('button',{name:/预生成|重试首段预加载/}).count(),0);
 const starting=page.waitForResponse(response=>response.url().endsWith('/start'));
 const began=Date.now();await page.getByRole('button',{name:'开始学习',exact:true}).click();const started=await (await starting).json();
 assert(started.cache_hit);await page.waitForFunction(()=>document.querySelector('.lecture-segment p')?.textContent.length>1);
 const entryMs=Date.now()-began;assert(entryMs<3000);checks.push('cached_entry_without_first_model_request');
 await page.getByRole('button',{name:'暂停讲解',exact:true}).click();
 const text=await page.locator('.stream').textContent();await page.waitForTimeout(500);assert.equal(await page.locator('.stream').textContent(),text);
 assert.equal(browserRuns,0);checks.push('server_executes_tasks_and_pause_freezes_cached_presentation');
 const after=await page.evaluate(async backend=>(await (await fetch(backend+'/synthetic-prewarm-counts')).json()),backend);assert.equal(after.by_lesson[prepared.lesson_id],1);
 const workspace=await page.evaluate(async conv=>(await (await fetch('/api/teaching/'+conv,{credentials:'include'})).json()),started.conversation_id);
 assert(workspace.server_managed&&workspace.steps[0].audio_ready);assert(!workspace.lesson_finished);checks.push('cache_audio_bound_and_learning_not_auto_completed');
 await page.getByRole('button',{name:'继续讲解',exact:true}).click();
 const enable=page.getByRole('button',{name:'点击开启声音',exact:true});
 if(await enable.count())await enable.click();
 await page.waitForFunction(()=>window.__firstMedia.some(media=>!media.paused&&media.currentTime>.1),null,{timeout:10000});
 checks.push('prepared_audio_plays_without_resynthesis');
 await page.waitForFunction(async conv=>{
  const view=await (await fetch('/api/teaching/'+conv,{credentials:'include'})).json();return view.steps.some(step=>step.step_no===2);
 },started.conversation_id,{timeout:20000});
 assert.equal(await page.locator('.lw-simple').getAttribute('data-current-segment'),'1');
 assert.equal(browserRuns,0);checks.push('server_generates_next_segment_before_current_presentation_finishes');
 await page.getByRole('button',{name:'暂停讲解',exact:true}).click();
 await page.reload();await page.getByRole('button',{name:'继续讲解',exact:true}).waitFor();
 assert.equal((await page.evaluate(async backend=>(await (await fetch(backend+'/synthetic-prewarm-counts')).json()),backend)).by_lesson[prepared.lesson_id],1);
 checks.push('refresh_reuses_existing_first_segment');assert.deepEqual(errors,[]);
 writeFileSync(output+'/first-prewarm-browser.json',JSON.stringify({checks,entryMs,browserRuns,pageErrors:errors,synthetic:true},null,2));
 console.log(JSON.stringify({checks,entryMs,browserRuns,pageErrors:errors}));
}finally{await browser.close();}
