import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {readFile,mkdir} from "node:fs/promises";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";
const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright-core");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const checks=[],out=".backup/classroom-revision-browser";
await mkdir(out,{recursive:true});
const profile={name:"合成导师",identity:"计算机导师",personality:"温柔亲切",background:"合成背景",appearance:"清晰立绘",
 speaking_style:"短句引导",tags:[],voice_id:"voice-a",voice_config_id:"tts-a",media_ids:[],base_asset_id:""};
const card={card_id:"mentor-a",revision:1,editable:true,collected:false,profile,assets:[]};
const text="合成讲解用于验证暂停与恢复。".repeat(14);
const board={presentation:{version:2,title:"合成板书",blocks:[{id:"text-a",type:"text",text:"先明确工具职责，再检查保存和地址。".repeat(8)}]}};
try{
 for(const width of [1440,390,320]){
  const context=await browser.newContext({viewport:{width,height:900},serviceWorkers:"block"}),page=await context.newPage(),errors=[];
  page.on("pageerror",e=>errors.push(e.message));const served=await mockBuiltFrontend(page);
  await page.route("https://esm.sh/**/CascadiaCode-Regular.woff2",async r=>r.fulfill({contentType:"font/woff2",body:await readFile("frontend/node_modules/@excalidraw/excalidraw/dist/prod/fonts/Cascadia/CascadiaCode-Regular.woff2")}));
  let title="编辑器配置与网页运行原理",preference={version:0,card_id:null,revision:null},coverageCalls=0;
  const knowledge=()=>({lesson_id:"lesson-a",lesson_title:title,plan_hash:"a".repeat(64),total:2,next_after:null,
   data:[{record_id:"r1",title:"编辑器负责写与保存",summary:"修改后先保存，再检查页面地址。",assessment:"passed",
    completed_at:"2026-10-16T02:00:00Z",conversation_id:"conv-a",after_step_no:2,sources:[{step_no:1},{step_no:2}]},
   {record_id:"r2",title:"浏览器负责解析与呈现",summary:"浏览器读取网页文件并绘制页面。",assessment:"pending",
    completed_at:"2026-10-16T02:05:00Z",conversation_id:"conv-a",after_step_no:4,sources:[{step_no:3}]}]});
  let ws={conversation_id:"conv-a",kind:"teaching",course_id:"course-a",lesson_id:"lesson-a",generation:1,
   session_state:"active",server_managed:true,audio_mode:"text",tts_authorized:false,queued_tasks:[],
   lesson_title:title,last_checkpoint:{client_seq:1,step_no:1,position_ms:0,presentation:{paused:true,visible_chars:0}},
   knowledge:knowledge(),feynman:{checkpoints:[],active:null},followups:[],
   steps:[{step_id:"step-a",step_no:1,title:"合成讲解",text,status:"ready",text_version_no:1,board,audio_ready:false,audio_id:null}]};
  const json=(route,body,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(body)});
  await page.route("**/api/**",async route=>{
   const request=route.request(),url=new URL(request.url()),p=url.pathname,m=request.method();
   if(p==="/api/auth/me")return json(route,{user_id:"owner-a",status:"approved",is_admin:false});
   if(p==="/api/mentor-preference"&&m==="GET")return json(route,preference);
   if(p==="/api/mentor-preference"&&m==="PUT"){
    const body=request.postDataJSON();assert.equal(body.expected_version,preference.version);
    assert.equal(body.conversation_id,"conv-a");
    preference={version:preference.version+1,card_id:body.card_id,revision:body.revision,
     classroom:{card_id:body.card_id,revision:body.revision,voice_id:body.card_id?"voice-a":""}};
    return json(route,{...preference,effective_step_no:2});
   }
   if(p==="/api/cards"||p==="/api/card-market")return json(route,{data:[card]});
   if(p==="/api/cards/mentor-a")return json(route,card);
   if(p.endsWith("/media-jobs"))return json(route,{data:[]});
   if(p==="/api/capabilities/configs")return json(route,{data:[{kind:"tts",protocol:"minimax",config_id:"tts-a",config_version:1,is_active:true,model_name:"speech-2.8-hd"}]});
   if(p==="/api/character-voices")return json(route,{data:[{voice_id:"voice-a",name:"合成清朗声音"}]});
   if(p==="/api/character-expression")return json(route,{data:[{voice_id:"voice-a",name:"合成清朗声音"}],
    recommended_voice_id:"voice-a",voice_gender:"unknown",match_status:"matched",speaking_style:"耐心拆解，短句引导。"});
   if(p==="/api/teaching/conv-a/knowledge")return json(route,knowledge());
   if(p==="/api/teaching/conv-a/lesson-title"){
    const body=request.postDataJSON();assert.equal(body.expected_title,title);title=body.title;ws.lesson_title=title;return json(route,{lesson_title:title});
   }
   if(p==="/api/teaching/conv-a")return json(route,ws);
   if(p.endsWith("/checkpoint")){ws.last_checkpoint={...request.postDataJSON(),presentation:request.postDataJSON().board_state.presentation};return json(route,{saved:true});}
   if(p.endsWith("/coverage")){coverageCalls++;return json(route,{saved:true});}
   if(p.endsWith("/presentation")){ws.channel_checkpoint=request.postDataJSON();return json(route,{saved:true});}
   if(p.endsWith("/followup")){
    ws.session_state="awaiting_resume";ws.generation=2;ws.followups=[{generation:2,role:"user",content:"为什么要保存"},
     {generation:2,role:"assistant",content:"先保存文件，再检查浏览器地址。".repeat(12)}];
    return json(route,{task_id:"answer-a",deduped:false});
   }
   if(p.endsWith("/run"))return json(route,{status:"succeeded"});
   if(p.endsWith("/resume")){ws.session_state="active";return json(route,{resumed:true});}
   if(p.endsWith("/content-events"))return route.fulfill({contentType:"text/event-stream",body:'event: done\ndata: {"event":"done"}\n\n'});
   if(p==="/api/materials"||p==="/api/navigation")return json(route,{data:[]});
   return json(route,{});
  });
  await page.goto(served.origin+"/#/knowledge/conv-a");
  await page.getByRole("heading",{name:title,exact:true}).waitFor();
  assert.equal(await page.locator(".taught-list article").count(),2);
  assert((await page.locator(".taught-page").innerText()).includes("问答 · 待回答"));
  await page.getByRole("button",{name:"重命名本节课",exact:true}).click();
  await page.getByRole("textbox",{name:"本节课名称"}).fill("新的本节课名称");
  await page.getByRole("button",{name:"保存讲次名称"}).click();
  await page.getByRole("heading",{name:"新的本节课名称",exact:true}).waitFor();
  await page.reload();await page.getByRole("heading",{name:"新的本节课名称",exact:true}).waitFor();
  await page.getByRole("button",{name:"重命名本节课",exact:true}).click();
  await page.getByRole("textbox",{name:"本节课名称"}).fill("这个改名要取消");
  await page.getByRole("textbox",{name:"本节课名称"}).press("Escape");
  await page.getByRole("heading",{name:"新的本节课名称",exact:true}).waitFor();
  assert.equal(title,"新的本节课名称");
  assert.equal(await page.locator(".taught-list article").count(),2);
  checks.push(`knowledge-title-state-and-reload-${width}`);
  await page.screenshot({path:`${out}/knowledge-${width}.png`});
  await page.getByRole("button",{name:"回看编辑器负责写与保存",exact:true}).click();
  await page.locator(".classroom-nav h1").waitFor();
  assert.equal(await page.locator(".classroom-nav h1").innerText(),title);
  assert.equal(await page.locator(".classroom-nav select").count(),0);
  await page.waitForTimeout(2200);
  assert.equal(coverageCalls,0,"review must not write progress");
  assert.equal(await page.getByRole("button",{name:"停止讲解",exact:true}).count(),0);
  checks.push(`review-keeps-paused-without-progress-${width}`);
  await page.goto(served.origin+"/#/cards?conversation=conv-a");
  await page.getByRole("button",{name:"使用导师",exact:true}).click();
  await page.getByRole("button",{name:"正在使用",exact:true}).waitFor();
  assert.equal(preference.card_id,"mentor-a");
  await page.reload();await page.getByRole("button",{name:"正在使用",exact:true}).waitFor();
  await page.getByRole("button",{name:"使用课堂默认",exact:true}).click();
  await page.getByRole("button",{name:"使用导师",exact:true}).waitFor();
  assert.equal(preference.card_id,null);
  checks.push(`mentor-select-default-and-reload-${width}`);
  await page.goto(served.origin+"/#/card/mentor-a?conversation=conv-a");
  await page.waitForFunction(()=>document.querySelector('select[aria-label="音色"]')?.textContent.includes("合成清朗声音"));
  assert.equal(await page.locator('input[type="range"]').count(),0);
  assert.equal(await page.getByRole("heading",{name:"声音",exact:true}).count(),1);
  await page.getByLabel("说话风格",{exact:true}).waitFor();
  assert.equal(await page.locator('textarea[aria-label="说话风格"]').count(),0);
  checks.push(`automatic-voice-catalog-without-technical-controls-${width}`);
  await page.goto(served.origin+"/#/lecture/conv-a");
  await page.getByRole("button",{name:"继续讲解",exact:true}).click();
  const stream=page.getByRole("log",{name:"讲解文字流"});
  await page.waitForFunction(()=>document.querySelector(".stream").textContent.length>4);
  await page.getByRole("button",{name:"停止讲解",exact:true}).click();
  const frozen=await stream.innerText();await page.waitForTimeout(3100);
  assert.equal(await stream.innerText(),frozen);
  await page.getByLabel("课程追问",{exact:true}).fill("为什么要保存？");
  await page.getByRole("button",{name:"发送追问",exact:true}).click();
  await page.getByRole("button",{name:"继续讲解",exact:true}).waitFor();
  const beforeAnswer=await stream.innerText();await page.waitForTimeout(1600);assert.equal(await stream.innerText(),beforeAnswer);
  await page.getByRole("button",{name:"继续讲解",exact:true}).click();
  await page.waitForTimeout(700);
  const progressing=await stream.innerText();await page.waitForTimeout(1800);
  assert((await stream.innerText()).length>progressing.length,"workspace polling must not pause an explicitly playing answer");
  await page.getByRole("button",{name:"停止讲解",exact:true}).click();
  const frozenAnswer=await stream.innerText();await page.waitForTimeout(3100);
  assert.equal(await stream.innerText(),frozenAnswer);
  const box=await page.locator(".composer2").boundingBox();assert(box.y+box.height<=900);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1&&document.documentElement.scrollHeight<=innerHeight+1));
  assert.equal(errors.length,0,errors.join("\n"));
  checks.push(`lesson-and-followup-pause-layout-${width}`);
  await page.screenshot({path:`${out}/paused-${width}.png`});
  await context.close();
 }
 console.log(JSON.stringify({passed:checks.length,checks,synthetic:true}));
}finally{await browser.close();}
