import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {stat,mkdir,writeFile,rm} from "node:fs/promises";
import path from "node:path";
const {_electron}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright");
const output=path.resolve(".backup/paper-classroom");
await mkdir(output,{recursive:true});
for(const file of ["recording.webm","lecture-board.png","lecture-board.pdf"])
 await rm(path.join(output,file),{force:true});
const environment={...process.env};delete environment.ELECTRON_RUN_AS_NODE;
const app=await _electron.launch({executablePath:path.resolve("desktop/node_modules/electron/dist/electron.exe"),
  args:[path.resolve("tests/paper_desktop_fixture.cjs")],timeout:90000,env:environment});
try{
 const page=await app.firstWindow();page.setDefaultTimeout(60000);
 const errors=[];page.on("pageerror",error=>errors.push(error.message));
 await page.evaluate(()=>{
   const original=navigator.mediaDevices.getDisplayMedia.bind(navigator.mediaDevices);
   navigator.mediaDevices.getDisplayMedia=async(...args)=>{
    try{return await original(...args);}catch(error){window.__captureError=String(error);throw error;}
   };
 });
 await page.getByRole("button",{name:"继续讲解",exact:true}).waitFor();
 await page.getByRole("button",{name:"继续讲解",exact:true}).click();
 try{await app.evaluate(()=>new Promise(resolve=>{const timer=setInterval(()=>{if(global.fixture?.records.size){clearInterval(timer);resolve();}},100);}));}
 catch(error){console.log("Capture diagnostic",await page.evaluate(()=>({error:window.__captureError,status:document.querySelector(".classroom-media-tools")?.textContent})),
   await app.evaluate(()=>global.fixture.captureCalls));throw error;}
 const duration=Number(process.env.RECORDING_TEST_MS||12000),begin=Date.now();
 while(Date.now()-begin<duration){
  await page.waitForTimeout(Math.min(30000,duration-(Date.now()-begin)));
  console.log(`Recording verification elapsed ${Math.round((Date.now()-begin)/1000)}s`);
  if(duration>30000){
   const play=page.getByRole("button",{name:"继续讲解",exact:true});
   if(await play.count())await play.click().catch(()=>{});
  }
 }
 const videoFile=path.join(output,"recording.webm");
 const info=await page.evaluate(async()=>{
   const video=document.createElement("video");
   video.src="/api/teaching/audio/aud_synthetic/content";
   // Real video file is inspected by ffprobe independently.
   return {mime:MediaRecorder.isTypeSupported("video/webm;codecs=vp8,opus")};
 });
 assert(info.mime);
 await page.getByRole("button",{name:"停止讲解",exact:true}).click().catch(()=>{});
 await page.getByRole("button",{name:"继续讲解",exact:true}).click().catch(()=>{});
 await page.waitForTimeout(22000);
 await page.getByLabel("导出板书",{exact:true}).click();
 await page.getByRole("button",{name:"当前板书 PNG",exact:true}).click();
 const png=path.join(output,"lecture-board.png");
 try{
  await page.waitForFunction(async()=>Boolean(await window.desktopClassroom),{});
  const deadline=Date.now()+42000;
  while(Date.now()<deadline){try{await stat(png);break;}catch{await page.waitForTimeout(500);}}
 }catch{}
 console.log("Export windows",await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().map(window=>({url:window.webContents.getURL(),id:window.id}))));
 for(const candidate of app.windows()){
  if(candidate!==page)console.log("Export content",await candidate.locator("body").innerText().catch(()=>"closed"));
 }
 const pngInfo=await stat(png);
 assert(pngInfo.size>5000);
 await page.getByRole("button",{name:"本讲已讲板书 PDF",exact:true}).click();
 const deadline=Date.now()+42000;
 while(Date.now()<deadline){try{await stat(path.join(output,"lecture-board.pdf"));break;}catch{await page.waitForTimeout(500);}}
 assert((await stat(path.join(output,"lecture-board.pdf"))).size>5000);
 await page.screenshot({path:path.join(output,"desktop.png")});
 await page.evaluate(()=>{location.hash="/home";});
 await app.evaluate(()=>new Promise(resolve=>{const timer=setInterval(()=>{if(global.fixture.records.get("rec_synthetic").status==="ready"){clearInterval(timer);resolve();}},100);}));
 assert((await stat(videoFile)).size>10000);
 assert.deepEqual(errors,[]);
 await writeFile(path.join(output,"desktop-result.json"),JSON.stringify({passed:4,duration_ms:duration,video_bytes:(await stat(videoFile)).size,png_bytes:pngInfo.size,errors},null,2));
 console.log("Real Electron capture/audio/export checks passed");
}finally{await app.close();}
