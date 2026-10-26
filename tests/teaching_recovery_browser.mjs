import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir} from "node:fs/promises";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";

const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright-core");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const out=".backup/teaching-recovery-browser",checks=[];
await mkdir(out,{recursive:true});
try{
 for(const width of [1440,390]){
  const context=await browser.newContext({viewport:{width,height:900},serviceWorkers:"block"});
  await context.addInitScript(()=>{
   window.__media=[];
   window.Audio=class{
    paused=true;currentTime=0;duration=90;src="";preload="";readyState=0;seeking=false;loads=0;
    constructor(){window.__media.push(this);}
    play(){this.paused=false;return Promise.resolve();}
    pause(){this.paused=true;}
    load(){this.loads++;queueMicrotask(()=>this.onloadedmetadata?.());}
    removeAttribute(name){if(name==="src")this.src="";}
   };
  });
  const page=await context.newPage(),errors=[];
  page.on("pageerror",error=>errors.push(error.message));
  const served=await mockBuiltFrontend(page);
  const text="合成恢复讲解。".repeat(80);
  const step=number=>({step_id:`step-${number}`,step_no:number,text,status:"ready",text_version_no:1,
   board:{presentation:{version:2,title:"合成恢复",blocks:[]}},audio_ready:false,audio_id:null});
  const task=(number,kind="lesson_step_generate")=>({task_id:`failed-${number}-${kind}`,step_no:number,kind,
   status:"failed",error_code:"INTERNAL_ERROR",required_action:"retry"});
  let ws={conversation_id:"synthetic-recovery",kind:"teaching",course_id:"course-a",lesson_id:"lesson-a",
   lesson_title:"合成恢复课堂",generation:1,session_state:"active",server_managed:true,
   audio_mode:"text",tts_authorized:false,steps:[step(1)],followups:[],
   queued_tasks:[task(1),task(2),task(0,"lesson_plan_refine")],
   last_checkpoint:{client_seq:1,step_no:1,position_ms:0,presentation:{paused:true}}};
  let disconnected=false,streams=0;
  const json=(route,body)=>route.fulfill({contentType:"application/json",body:JSON.stringify(body)});
  await page.route("**/api/**",route=>{
   const path=new URL(route.request().url()).pathname;
   if(disconnected)return route.abort("internetdisconnected");
   if(path==="/api/auth/me")return json(route,{user_id:"synthetic-owner",status:"approved"});
   if(path==="/api/teaching/synthetic-recovery")return disconnected?route.abort("internetdisconnected"):json(route,ws);
   if(path.endsWith("/content-events")){streams++;return route.abort("connectionfailed");}
   if(path.endsWith("/checkpoint"))return json(route,{saved:true});
   if(path==="/api/cards"||path==="/api/navigation"||path==="/api/materials")return json(route,{data:[]});
   return json(route,{});
  });
  await page.goto(served.origin+"/#/lecture/synthetic-recovery");
  await page.getByText("下一段暂未准备好",{exact:false}).waitFor();
  assert.equal(await page.getByRole("button",{name:"重试生成",exact:true}).count(),0);
  assert(!/操作失败|INTERNAL_ERROR|截止/.test(await page.locator(".lw-simple").innerText()));
  checks.push(`current-content-isolated-from-stale-and-next-failures-${width}`);
   ws={...ws,queued_tasks:[{...task(2),error_code:"OUTPUT_INVALID",attempt:1,generation:1}]};
   await page.waitForTimeout(2200);
   assert.equal(await page.getByRole("button",{name:"重试准备下一段",exact:true}).count(),0);
   checks.push(`invalid-next-output-recovers-quietly-within-budget-${width}`);
   ws={...ws,queued_tasks:[{...task(2),error_code:"OUTPUT_INVALID",attempt:3,generation:3}]};
  await page.getByText("模型返回的内容不符合要求，可重试生成。",{exact:false}).waitFor();
  assert(!/自动重试/.test(await page.locator(".lw-simple").innerText()));
  await page.getByRole("button",{name:"重试准备下一段",exact:true}).waitFor();
  checks.push(`invalid-next-output-displays-reason-and-manual-recovery-${width}`);
  ws={...ws,steps:[step(1),step(2)]};
  await page.getByText("下一段暂未准备好",{exact:false}).waitFor({state:"hidden"});
  checks.push(`published-artifact-clears-old-error-${width}`);
  await page.getByRole("button",{name:"继续讲解",exact:true}).click();
  disconnected=true;
  await page.getByText("连接暂不可用",{exact:false}).waitFor();
  await page.waitForTimeout(5500);
  assert.equal(await page.getByRole("alertdialog").count(),0);
  assert.equal(await page.getByRole("dialog",{name:"出错了"}).count(),0);
  assert.equal(await page.locator('[data-segment-no="1"]').count(),1);
  disconnected=false;
  await page.evaluate(()=>window.dispatchEvent(new Event("online")));
  await page.getByText("连接暂不可用",{exact:false}).waitFor({state:"hidden"});
  checks.push(`offline-retains-classroom-and-online-clears-status-${width}`);
  ws={...ws,queued_tasks:[{task_id:"stream-next",step_no:2,kind:"lesson_step_generate",status:"running"}]};
  await page.waitForFunction(()=>document.querySelector(".lw-simple"));
  await page.waitForTimeout(5200);
  assert(streams>=2&&streams<=5,`stream reconnect must be bounded: ${streams}`);
  checks.push(`broken-stream-reconnects-without-storm-${width}`);
  const voiced=number=>({...step(number),audio_ready:true,audio_id:`audio-${number}`,
   audio_subtitles:[{text,begin_ms:0,end_ms:90000}]});
  ws={...ws,steps:[voiced(1),voiced(2),voiced(3)],queued_tasks:[],audio_mode:"tts",tts_authorized:true,
   last_checkpoint:{client_seq:2,step_no:2,position_ms:1200,presentation:{paused:false,audio_id:"audio-2"}}};
  await page.reload();
  await page.waitForFunction(()=>document.querySelector(".lw-simple")?.dataset.currentSegment==="2");
  await page.waitForFunction(()=>window.__media.filter(a=>a.src).length===3);
  const media=await page.evaluate(()=>window.__media.filter(a=>a.src).map(a=>({src:a.src,loads:a.loads})));
  assert.equal(new Set(media.map(a=>a.src)).size,3);
  await page.waitForTimeout(12500);
  assert.equal(await page.locator(".lw-simple").getAttribute("data-audio-state"),"waiting");
  assert.equal(await page.getByText("语音重试失败。",{exact:true}).count(),0);
  checks.push(`both-neighbours-preloaded-and-slow-audio-remains-waiting-${width}`);
  const before=await page.evaluate(()=>window.__media.filter(a=>a.src).map(a=>a.loads));
  await page.evaluate(()=>window.dispatchEvent(new Event("online")));
  await page.waitForFunction(before=>window.__media.filter(a=>a.src).every((a,i)=>a.loads>before[i]),before);
  checks.push(`online-reloads-current-and-neighbour-audio-${width}`);
  assert.equal(errors.length,0,errors.join("\n"));
  await page.screenshot({path:`${out}/recovered-${width}.png`});
  await context.close();
 }
 console.log(JSON.stringify({passed:checks.length,checks,synthetic:true}));
}finally{await browser.close();}
