// Explicit installed-app acceptance; no real providers or learning writes.
import assert from "node:assert/strict";
import path from "node:path";
import {createRequire} from "node:module";
import {mkdir,writeFile} from "node:fs/promises";
const {_electron}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright");
assert(process.env.TEST_INSTALLED_EXE,"Select an installed executable explicitly");
const env={...process.env};delete env.ELECTRON_RUN_AS_NODE;
const application=await _electron.launch({executablePath:path.resolve(process.env.TEST_INSTALLED_EXE),args:[],env,timeout:90000});
const checks=[],errors=[],output=".backup/paper-classroom";
await mkdir(output,{recursive:true});
try{
 const page=await application.firstWindow();page.setDefaultTimeout(45000);
 page.on("pageerror",error=>errors.push(error.message));
 const deadline=Date.now()+600000;let previous="";
 while(!/http:\/\/127\.0\.0\.1:\d+\/#\/home/.test(page.url())){
  assert(Date.now()<deadline,"Installed startup timed out");
  const state=await page.evaluate(()=>window.desktopSetup?.status()).catch(()=>null);
  const label=state?.stage+":"+state?.component;
  if(label!==previous){console.log(label);previous=label;}
  assert.notEqual(state?.stage,"failed",`Installed startup failed: ${state?.code}`);
  await page.waitForTimeout(500);
 }
 const version=await application.evaluate(({app})=>app.getVersion());
 assert.equal(version,createRequire(import.meta.url)("../desktop/package.json").version);checks.push("installed-version");
 const origin=new URL(page.url()).origin;
 assert.equal((await fetch(origin+"/api/auth/me")).status,403);checks.push("gateway-isolation");
 const profile=await page.evaluate(()=>fetch("/api/auth/me").then(response=>({status:response.status})));
 assert.equal(profile.status,200);checks.push("existing-account-restored");
 assert(await page.evaluate(()=>Boolean(window.desktopClassroom?.allowCapture&&window.desktopClassroom?.exportBoard)));
 checks.push("packaged-capture-and-export-bridge");
 const conversation=process.env.TEST_CONVERSATION;
 if(conversation){
  const frequency=440,samples=48000*20,wav=Buffer.alloc(44+samples*2);
  wav.write("RIFF");wav.writeUInt32LE(wav.length-8,4);wav.write("WAVEfmt ",8);
  wav.writeUInt32LE(16,16);wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(48000,24);
  wav.writeUInt32LE(96000,28);wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);wav.write("data",36);wav.writeUInt32LE(samples*2,40);
  for(let i=0;i<samples;i++)wav.writeInt16LE(Math.round(Math.sin(i*2*Math.PI*frequency/48000)*3000),44+i*2);
  const workspace=await page.evaluate(id=>fetch(`/api/teaching/${id}`).then(r=>r.json()),conversation);
  assert.equal(workspace.conversation_id,conversation);
  const prefix=`/api/teaching/${conversation}`,recordings=[];
  const clean=(process.env.TEST_CLEAN_RECORDINGS||"").split(",").filter(Boolean);
  assert(clean.every(id=>/^rec_[0-9a-f]{32}$/.test(id)));
  if(clean.length)await page.evaluate(async({prefix,clean})=>{
   for(const id of clean){
    const response=await fetch(prefix+"/recordings/"+id,{method:"DELETE"});
    if(!response.ok)throw Error("Explicit synthetic cleanup failed");
   }
  },{prefix,clean});
  page.on("response",async response=>{
   if(response.request().method()==="POST"&&new URL(response.url()).pathname===prefix+"/recordings"&&response.ok())
    recordings.push((await response.json()).recording_id);
  });
  await page.route("**/api/teaching/**",async route=>{
   const pathname=new URL(route.request().url()).pathname,method=route.request().method();
   if(pathname.startsWith(prefix+"/recordings"))return route.continue();
   if(pathname.includes("/audio/"))return route.fulfill({contentType:"audio/wav",body:wav});
   if(method!=="GET")return route.fulfill({json:{saved:true,client_seq:1}});
   if(pathname===prefix)return route.fulfill({json:{...workspace,
    generation:1,session_state:"active",server_managed:true,playback_version:0,
    needs_upgrade:false,authorization_required:false,audio_mode:"tts",tts_authorized:true,
    planned_steps:1,followups:[],queued_tasks:[],feynman:null,
    last_checkpoint:{client_seq:1,step_no:1,position_ms:0,presentation:{visible_chars:0,paused:true}},
    steps:[{step_id:"synthetic-installed",step_no:1,title:"Synthetic acceptance",text:"Synthetic installed classroom.".repeat(4),
     status:"ready",text_version_no:1,lesson_finished:true,audio_ready:true,audio_id:"synthetic-installed",
     audio_subtitles:[{text:"Synthetic installed classroom.".repeat(4),begin_ms:0,end_ms:20000}],
     board:{presentation:{version:2,title:"Synthetic acceptance",blocks:[{id:"note",type:"text",text:"Automatic recording acceptance"}]}}}]}});
   return route.fulfill({json:{data:[]}});
  });
  try{
   await page.goto(origin+"/#/lecture/"+conversation);
   await page.getByRole("button",{name:"继续讲解",exact:true}).click();
   await page.waitForFunction(()=>!document.querySelector(".classroom-media-error"));
   const deadline=Date.now()+20000;
   while(!recordings.length&&Date.now()<deadline)await page.waitForTimeout(100);
   assert.equal(recordings.length,1,"Automatic capture without a manual recording button");
   await page.waitForTimeout(7000);
   await page.evaluate(()=>{location.hash="/home";});
   await page.waitForURL(/#\/home$/);
   const saved=await page.evaluate(async({prefix,id})=>{
    for(let attempt=0;attempt<120;attempt++){
     const rows=await fetch(prefix+"/recordings").then(r=>r.json());
     const row=rows.data.find(item=>item.recording_id===id);
     if(row?.status==="ready")return row;
     await new Promise(resolve=>setTimeout(resolve,500));
    }
    return null;
   },{prefix,id:recordings[0]});
   assert(saved?.bytes>10000);checks.push("installed-automatic-capture-and-real-backend-remux");
   const playback=await page.evaluate(async({prefix,id})=>{
    const video=document.createElement("video");video.controls=true;document.body.append(video);
    video.src=prefix+"/recordings/"+id+"/video";
    await new Promise((resolve,reject)=>{video.onloadeddata=resolve;video.onerror=()=>reject(Error(`Video error ${video.error?.code}`));});
    const wait=()=>new Promise(resolve=>video.addEventListener("seeked",resolve,{once:true}));
    let done=wait();video.currentTime=3;await done;const forward=video.currentTime;
    done=wait();video.currentTime=1;await done;
    const result={duration:video.duration,forward,back:video.currentTime,hasAudio:video.webkitAudioDecodedByteCount};
    video.remove();return result;
   },{prefix,id:recordings[0]});
   assert(playback.duration>=6&&playback.forward>2.9&&playback.back>=.9);
   checks.push("installed-video-seek-forward-and-backward");
   await writeFile(output+"/installed-video-result.json",JSON.stringify({version,...playback},null,2));
  }finally{
   await page.evaluate(async({prefix,recordings})=>{
    for(const id of recordings){
     const result=await fetch(prefix+"/recordings/"+id,{method:"DELETE"});
     if(!result.ok)throw Error("Synthetic recording cleanup failed");
    }
   },{prefix,recordings}).catch(()=>{errors.push("Synthetic recording cleanup failed");});
   await page.unrouteAll({behavior:"wait"});
   await page.goto(origin+"/#/home");
  }
 }
 await page.screenshot({path:output+"/installed-home.png"});
 assert.deepEqual(errors,[]);
 await writeFile(output+"/installed-result.json",JSON.stringify({version,checks,errors},null,2));
 console.log(JSON.stringify({version,passed:checks.length,checks}));
 const closed=application.waitForEvent("close");
 await application.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].close());
 await closed;
}finally{await application.close().catch(()=>{});}
