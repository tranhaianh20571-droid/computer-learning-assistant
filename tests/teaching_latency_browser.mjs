import {createRequire} from "node:module";
import {spawnSync} from "node:child_process";
import {mkdirSync,writeFileSync} from "node:fs";
import assert from "node:assert/strict";
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PW_ROOT+"/playwright");
const backend=process.env.TEST_BACKEND||"http://127.0.0.1:8013",base=process.env.TEST_FRONTEND||"http://127.0.0.1:5173";
assert.equal(new URL(backend).hostname,"127.0.0.1");assert.notEqual(new URL(backend).port,"8000");
const output=process.env.TEST_OUTPUT;assert(output);mkdirSync(output,{recursive:true});
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const context=await browser.newContext({viewport:{width:1440,height:1000}});
const guest=await context.request.get(backend+"/api/auth/guest");assert.equal(guest.status(),200);
const owner=(await guest.json()).user_id;
const fixture=spawnSync(process.env.TEST_PYTHON,[process.env.TEST_FIXTURE,owner],{encoding:"utf8",env:process.env});
assert.equal(fixture.status,0,fixture.stderr);const course=JSON.parse(fixture.stdout);
// Direct browser fetch preserves SSE incrementality; route.fetch/fulfill buffers it.
await context.addInitScript(({backend})=>{
 const original=window.fetch.bind(window);
 window.fetch=(input,init)=>{
  if(typeof input==="string" && input.startsWith("/api/"))input=backend+input;
  return original(input,init);
 };
},{backend});
const page=await context.newPage(),errors=[],checks=[];
page.on("pageerror",e=>errors.push(e.message));
let runCompleted=false,events=[];
page.on("response",response=>{if(response.url().endsWith("/run"))runCompleted=true;});
try{
 await page.goto(base+"/#/course/"+course.course_id);
 await page.getByRole("button",{name:"开始学习",exact:true}).click();
 await page.waitForFunction(()=>Array.from(document.querySelectorAll('[data-teaching-preview] p')).some(n=>n.textContent.length>5),null,{timeout:30000});
 assert.equal(runCompleted,false);checks.push("text_visible_before_final_result");
 await page.screenshot({path:output+"/first-text.png",fullPage:true});
 await page.locator('[data-board-elements]:not([data-board-elements="0"]) canvas').first().waitFor({state:"visible",timeout:30000});
 assert(await page.locator('[data-teaching-preview]').filter({has:page.locator('p')}).count()>0);
 assert.equal(runCompleted,false);checks.push("board_visible_during_generation");
 await page.screenshot({path:output+"/streaming-board.png",fullPage:true});
 // Reload keeps the running task and resumes its current private snapshot.
 await page.reload();
 await page.waitForFunction(()=>document.querySelector('[data-teaching-preview] p')?.textContent.length>5,null,{timeout:30000});
 checks.push("running_task_resumes_after_refresh");
 await page.locator('.lecture-segment[data-segment-no="1"]:not([data-teaching-preview])').waitFor({timeout:30000});
 assert.equal(await page.locator('[data-teaching-preview]').filter({has:page.locator('p')}).count(),0);
 checks.push("validated_content_replaces_draft");
 await page.getByLabel("课程追问",{exact:true}).fill("合成问题：换一个标签类比");
 await page.getByRole("button",{name:"追问",exact:true}).click();
 await page.waitForFunction(()=>Array.from(document.querySelectorAll('[data-teaching-preview]')).some(n=>n.dataset.segmentNo==='0'),null,{timeout:30000});
 checks.push("followup_text_and_board_stream");
 await page.getByRole("button",{name:"继续课程 · 不自动播放",exact:true}).waitFor({timeout:30000});
 await page.setViewportSize({width:390,height:844});await page.getByRole("tab",{name:"文字",exact:true}).click();
 assert(await page.locator(".tx-col").isVisible());assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
 checks.push("mobile_stream_workspace");
 assert.deepEqual(errors,[]);
 writeFileSync(output+"/browser-result.json",JSON.stringify({checks,pageErrors:errors,synthetic:true},null,2));
 console.log(JSON.stringify({checks,pageErrors:errors}));
}finally{await browser.close();}
