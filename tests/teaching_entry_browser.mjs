import assert from "node:assert/strict";
import {createRequire} from "node:module";
import path from "node:path";
const require=createRequire(path.resolve(process.env.PW_ROOT||"frontend/node_modules","package.json"));
const {chromium}=require("playwright-core");
const base=process.env.TEST_FRONTEND||"http://127.0.0.1:5173";
assert.equal(new URL(base).hostname,"127.0.0.1");
const browser=await chromium.launch({executablePath:process.env.TEST_BROWSER,headless:true});
const context=await browser.newContext({viewport:{width:1280,height:900}});
const page=await context.newPage(),checks=[],errors=[],requests=[];
const lesson={step_no:1,step_id:"synthetic-step",text:"合成讲解文字用于验证文字模式直接展示。".repeat(12),title:"合成首段",status:"ready",text_version_no:1,board:null,audio_id:null,audio_ready:false,lesson_finished:true};
let workspace={conversation_id:"synthetic-conversation",course_id:"synthetic-course",lesson_id:"synthetic-lesson",kind:"teaching",generation:1,session_state:"active",server_managed:true,tts_config:{config_id:"synthetic-tts",config_version:1},tts_authorized:false,needs_upgrade:false,steps:[lesson],followups:[],queued_tasks:[],last_checkpoint:null};
let preparation={status:"failed",lesson_id:"synthetic-lesson",error_code:"MODEL_TIMEOUT"};
let preparationReads=0;
let coverEligible=false;
let startFailure=null;
page.on("pageerror",error=>errors.push(error.message));
await context.addInitScript(()=>{
  window.__audioPlays=0;
   const nativeTimeout=window.setTimeout.bind(window);
   window.setTimeout=(callback,delay,...args)=>nativeTimeout(callback,delay>=11000&&delay<=12000?800:delay,...args);
  window.Audio=class{paused=true;currentTime=0;duration=30;src="";preload="";play(){window.__audioPlays++;return Promise.reject(new DOMException("synthetic autoplay block","NotAllowedError"));}pause(){this.paused=true;}removeAttribute(name){if(name==="src")this.src="";}load(){}};
});
await page.route("**/*",async route=>{
  const request=route.request(),url=new URL(request.url());
  if(url.origin!==new URL(base).origin){await route.abort();return;}
  if(!url.pathname.startsWith("/api/")){await route.continue();return;}
  requests.push({path:url.pathname,method:request.method(),body:request.postData()});
  let body={};
  if(url.pathname==="/api/auth/me"||url.pathname==="/api/auth/guest")body={user_id:"synthetic-user",status:"approved",role:"user",display_name:"Synthetic"};
  else if(url.pathname==="/api/materials")body={data:[]};
  else if(url.pathname==="/api/cards")body={data:[]};
  else if(url.pathname==="/api/planning/library")body={routes:[],courses:[],drafts:[]};
  else if(url.pathname==="/api/teaching/synthetic-conversation")body=workspace;
  else if(url.pathname.endsWith("/recover")){workspace={...workspace,steps:[lesson],artifact_recovery:null,required_action:null,queued_tasks:[]};body={conversation_id:"synthetic-conversation",task_id:"synthetic-repair",recovered:true};}
  else if(url.pathname.endsWith("/prewarm")){preparation={status:"queued",lesson_id:"synthetic-lesson"};preparationReads=0;body=preparation;}
  else if(url.pathname.endsWith("/first-preparation")){preparationReads++;if(preparation.status==="queued"&&preparationReads>1)preparation={status:"ready",lesson_id:"synthetic-lesson"};body=preparation;}
  else if(url.pathname.endsWith("/start")){
    if(startFailure)return route.fulfill({status:400,contentType:"application/json",body:JSON.stringify({error_code:startFailure,message:startFailure})});
    body={conversation_id:"synthetic-conversation",task_id:"synthetic-task",cache_hit:true};
  }
  else if(url.pathname.endsWith("/overview"))body={cover_eligible:coverEligible,title:"合成课程",outline_confirmed:true,teaching_unlocked:true,units:[{unit_id:"synthetic-unit",title:"合成单元",lesson_count:1,lessons:[{lesson_id:"synthetic-lesson",title:"合成首讲",order_index:0,startable:true,status_preparation:"not_generated",learning_state:"not_started"}]}]};
  else if(url.pathname.endsWith("/cover"))body={status:"unavailable"};
  else if(url.pathname.endsWith("/assessment"))body={status:"not_generated"};
  await route.fulfill({status:200,contentType:"application/json",body:JSON.stringify(body)});
});
async function mount(component){
 await page.evaluate(async component=>{
  const React=await import("/node_modules/.vite/deps/react.js"),ReactDOM=await import("/node_modules/.vite/deps/react-dom_client.js");
  window.__testRoot?.unmount();document.getElementById("teaching-entry-regression")?.remove();
  const host=document.createElement("div");host.id="teaching-entry-regression";document.body.append(host);
  window.__testRoot=(ReactDOM.createRoot||ReactDOM.default?.createRoot)(host);
  const module=await import(component==="lecture"?"/src/LectureView.tsx":"/src/Overview.tsx");
  const props={conversationId:"synthetic-conversation",courseId:"synthetic-course",onErr:message=>window.__testError=message,onOk:()=>{},go:route=>window.__testRoute=route};
  window.__testRoot.render((React.createElement||React.default.createElement)(component==="lecture"?module.default:module.CourseOverview,props));
 },component);
}
try{
 await page.goto(base+"/#/home");
 await mount("lecture");
 const host=page.locator("#teaching-entry-regression");
 await page.waitForFunction(()=>document.querySelector("#teaching-entry-regression .lecture-segment p")?.textContent.length>2);
 assert.equal(await host.getByText("正在准备语音…",{exact:true}).count(),0);
 assert.equal(await page.evaluate(()=>window.__audioPlays),0);
 assert.equal(requests.filter(request=>request.path.includes("/audio")&&request.method==="POST").length,0);
 checks.push("saved_tts_without_lesson_authorization_shows_text");
 workspace={...workspace,tts_authorized:true};await mount("lecture");
 await host.getByText("正在准备语音…",{exact:true}).waitFor();
 assert.equal(await host.locator(".lecture-segment p").count(),0);
 workspace={...workspace,tts_authorized:false};
 await page.waitForFunction(()=>document.querySelector("#teaching-entry-regression .lecture-segment p")?.textContent.length>2);
 checks.push("mode_change_unlocks_text_without_manual_skip");
 workspace={...workspace,steps:[{...lesson,audio_id:"synthetic-audio",audio_ready:true}]};await mount("lecture");
 await page.waitForFunction(()=>document.querySelector("#teaching-entry-regression .lecture-segment p")?.textContent.length>2);
 assert.equal(await page.evaluate(()=>window.__audioPlays),0);checks.push("unauthorized_late_audio_does_not_play");
 workspace={...workspace,steps:[],artifact_recovery:{available:true,error_code:"OUTPUT_INVALID",missing_steps:[1]},required_action:"retry"};
 await mount("lecture");await host.getByRole("button",{name:"恢复讲解",exact:true}).waitFor();
 assert.equal(await host.getByText("正在准备首段讲解…",{exact:true}).count(),0);
 await host.getByRole("button",{name:"恢复讲解",exact:true}).click();
 await page.waitForFunction(()=>document.querySelector("#teaching-entry-regression .lecture-segment p")?.textContent.length>2);
 assert(requests.some(request=>request.path==="/api/teaching/synthetic-conversation/recover"&&request.method==="POST"));
 assert.equal(await page.getByRole("dialog").count(),0);checks.push("missing_first_segment_recovers_without_consent");
 workspace={...workspace,steps:[],artifact_recovery:{status:"exhausted",available:false,attempts:2,max_attempts:2},required_action:null,
   queued_tasks:[{task_id:"synthetic-exhausted-repair",kind:"lesson_step_generate",status:"failed",error_code:"OUTPUT_INVALID"}]};
 await mount("lecture");await host.getByText("讲解补生成已达到重试上限，请检查模型设置。",{exact:true}).first().waitFor();
 assert.equal(await host.getByRole("button",{name:/恢复讲解|重试生成/}).count(),0);
 assert.equal(await host.getByText("正在准备首段讲解…",{exact:true}).count(),0);
 checks.push("exhausted_repairs_show_limit_and_stop_retry");
  workspace={...workspace,steps:[lesson],tts_authorized:true,queued_tasks:[],artifact_recovery:null};
  await mount("lecture");await host.getByText("正在准备语音…",{exact:true}).waitFor();
  await page.waitForTimeout(1200);
  assert.equal(await host.getByRole("button",{name:"重试语音",exact:true}).count(),0);
  assert.equal(await host.locator(".lecture-segment p").count(),0);
  assert.equal(await host.getByRole("button",{name:"继续文字讲解",exact:true}).count(),0);
  const played=await page.evaluate(()=>window.__audioPlays);
  workspace={...workspace,steps:[{...lesson,audio_id:"late-audio",audio_ready:true,
    audio_subtitles:[{text:lesson.text,begin_ms:0,end_ms:30000}]}]};
  await host.getByRole("button",{name:"点击开启声音",exact:true}).waitFor();
  assert.equal(await host.locator(".lecture-segment p").count(),0);
  assert((await page.evaluate(()=>window.__audioPlays))>played);
  checks.push("audio_timeout_keeps_text_gated_and_late_audio_requires_playback");
   workspace={...workspace,steps:[{...lesson,audio_id:"autoplay-blocked-audio",audio_ready:true,
     audio_subtitles:[{text:lesson.text,begin_ms:0,end_ms:30000}]}]};
   await mount("lecture");await host.getByRole("button",{name:"点击开启声音",exact:true}).waitFor();
   await page.waitForTimeout(1200);
   assert.equal(await host.locator(".lecture-segment p").count(),0);
   checks.push("autoplay_block_keeps_text_gated");
  await mount("overview");await host.getByRole("button",{name:"开始学习",exact:true}).waitFor();
  assert.equal(await host.getByRole("button",{name:/预生成|预加载/}).count(),0);
  assert.equal(await host.getByText(/首段已|首段预加载|首段准备/).count(),0);
  assert.equal(requests.filter(request=>request.path.endsWith("/first-preparation")||request.path.endsWith("/prewarm")).length,0);
  assert.equal(await host.locator(".ink-cover-block").count(),0);
  assert.equal(requests.filter(request=>request.path==="/api/planning/course/synthetic-course/cover").length,0);
  checks.push("background_prewarm_hidden_and_grouped_course_has_no_cover");
  coverEligible=true;await mount("overview");await host.locator(".ink-cover-block").waitFor();
  checks.push("independent_course_keeps_cover");
 startFailure="MODEL_CREDENTIAL_INVALID";
 await host.getByRole("button",{name:"开始学习",exact:true}).click();
 await host.getByRole("status").filter({hasText:"学习服务连接需要更新，课程已保留。"}).waitFor();
 assert.equal(await host.getByRole("link",{name:"前往设置",exact:true}).count(),1);
 assert.equal(await host.locator(".st-prep").count(),0);
 assert.equal(await page.evaluate(()=>window.__testError),undefined);
 checks.push("entry_configuration_error_is_one_local_action_without_internal_task_states");
 startFailure=null;
 await host.getByRole("button",{name:"开始学习",exact:true}).click();
 await page.waitForFunction(()=>window.__testRoute==="/lecture/synthetic-conversation");
 assert.equal(await page.getByRole("dialog").count(),0);
 assert.equal(requests.filter(request=>request.path.includes("authorization-")).length,0);
 checks.push("lesson_start_direct_post_without_consent");
 assert.deepEqual(errors,[]);assert.equal(await page.evaluate(()=>window.__testError),undefined);
 console.log(JSON.stringify({checks,pageErrors:errors,synthetic:true,allApiRequestsIntercepted:true}));
}catch(error){
 console.error(JSON.stringify({pageErrors:errors,testError:await page.evaluate(()=>window.__testError),
  visible:await page.locator("#teaching-entry-regression").innerText()}));
 throw error;
}finally{await context.close();await browser.close();}
