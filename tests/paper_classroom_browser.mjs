import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir} from "node:fs/promises";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";
const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER,args:["--no-proxy-server"]});
await mkdir(".backup/paper-classroom",{recursive:true});
const checks=[];
try{
 for(const width of [1440,768,390,320]){
  const context=await browser.newContext({viewport:{width,height:950}});
  const page=await context.newPage();page.setDefaultTimeout(30000);
  const errors=[];page.on("pageerror",error=>errors.push(error.message));
  const served=await mockBuiltFrontend(page);
  const text="本段说明研究问题、方法与证据之间的关系。".repeat(5);
  const steps=[1,2,3].map(number=>({step_id:`s${number}`,step_no:number,text,title:`段${number}`,status:"ready",text_version_no:1,
    audio_ready:false,board_scope:"scope",board:{presentation:{version:2,title:"研究过程",
      blocks:[{id:`b${number}`,type:"text",text:`当前知识 ${number}`}],all_blocks:[]}}}));
  let checkpoint={client_seq:1,step_no:2,position_ms:0,presentation:{visible_chars:50,paused:true}};
  const requests=[];
  const json=(route,value)=>route.fulfill({contentType:"application/json",body:JSON.stringify(value)});
  await page.route("**/api/**",route=>{
    const path=new URL(route.request().url()).pathname,method=route.request().method();
    if(path==="/api/auth/me")return json(route,{user_id:"synthetic-owner",status:"approved"});
    if(path==="/api/teaching/synthetic-paper")return json(route,{
      conversation_id:"synthetic-paper",course_id:"course",lesson_id:"lesson",lesson_title:"论文阅读",
      kind:"teaching",generation:1,session_state:"active",server_managed:true,playback_version:0,
      planned_steps:3,audio_mode:"text",tts_authorized:false,followups:[],queued_tasks:[],steps,last_checkpoint:checkpoint});
    if(path.endsWith("/checkpoint")||path.endsWith("/seek-backward")){
      const body=route.request().postDataJSON();requests.push({path,body});
      checkpoint={...body,presentation:body.board_state.presentation};
      return json(route,{saved:true,client_seq:body.client_seq});
    }
    if(path.endsWith("/recordings"))return json(route,{data:[]});
    return json(route,{data:[],saved:true});
  });
  await page.goto(served.origin+"/#/lecture/synthetic-paper",{waitUntil:"domcontentloaded",timeout:90000});
  const slider=page.getByRole("slider",{name:"回放已讲内容"});
  await slider.waitFor();
  await page.waitForFunction(()=>Number(document.querySelector('input[type=range]')?.value)>1);
  const before=Number(await slider.inputValue());
  await slider.focus();await page.keyboard.press("ArrowRight");await page.keyboard.press("End");
  assert.equal(Number(await slider.inputValue()),before);
  assert.equal(requests.filter(r=>r.path.endsWith("seek-backward")).length,0);
  checks.push(`forward-key-blocked-${width}`);
  await page.keyboard.press("Home");
  await page.waitForFunction(()=>document.querySelector(".lw-simple")?.dataset.currentSegment==="1");
  assert.equal(requests.filter(r=>r.path.endsWith("seek-backward")).length,1);
  assert.equal(requests.at(-1).body.board_state.presentation.visible_chars,0);
  checks.push(`backward-seek-restores-cursor-${width}`);
  const geometry=await page.evaluate(()=>{
    const footer=document.querySelector(".lecture-progress").getBoundingClientRect();
    const column=document.querySelector(".bd-col").getBoundingClientRect();
    const content=document.querySelector(".bd-col>.board").getBoundingClientRect();
    const track=document.querySelector(".lecture-progress-track").getBoundingClientRect();
    return {footerTop:footer.top,contentBottom:content.bottom,right:footer.right,width:innerWidth,
      columnRight:column.right,trackHeight:track.height};
  });
  assert(geometry.contentBottom<=geometry.footerTop+1,JSON.stringify(geometry));
  assert(geometry.right<=geometry.width+1,JSON.stringify(geometry));
  assert(geometry.right<=geometry.columnRight+1,JSON.stringify(geometry));
  assert(geometry.trackHeight<=4,JSON.stringify(geometry));
  checks.push(`footer-does-not-cover-content-${width}`);
  await page.screenshot({path:`.backup/paper-classroom/classroom-${width}.png`});
  assert.deepEqual(errors,[]);
  await context.close();
 }
 console.log(JSON.stringify({passed:checks.length,checks}));
}finally{await browser.close();}
