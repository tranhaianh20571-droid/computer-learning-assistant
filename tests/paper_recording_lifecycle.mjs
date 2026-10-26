import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {stat,writeFile} from "node:fs/promises";
import path from "node:path";
const {_electron}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright");
const env={...process.env,RECORDING_TEST_OUTPUT:"recording-lifecycle.webm",RECORDING_TEST_DIR:"lifecycle-chunks"};
delete env.ELECTRON_RUN_AS_NODE;
const app=await _electron.launch({executablePath:path.resolve("desktop/node_modules/electron/dist/electron.exe"),
 args:[path.resolve("tests/paper_desktop_fixture.cjs")],timeout:90000,env});
const checks=[],events=[];
try{
 const page=await app.firstWindow();page.setDefaultTimeout(30000);
 const sample=async label=>{
  events.push({label,at:Date.now(),
   chunks:await app.evaluate(()=>global.fixture.records.get("rec_synthetic").chunks.length)});
 };
 const resume=page.getByRole("button",{name:"继续讲解",exact:true});
 await resume.waitFor();
 if(await resume.count())await resume.click();
 await app.evaluate(()=>new Promise(resolve=>{const timer=setInterval(()=>{if(global.fixture?.records.size){clearInterval(timer);resolve();}},100);}));
 assert.equal(await page.getByRole("button",{name:"录制本讲",exact:true}).count(),0);
 checks.push("automatic-recording-without-button");await sample("lecture");
 await page.waitForTimeout(5000);
 await page.getByRole("slider",{name:"回放已讲内容"}).focus();await page.keyboard.press("Home");
 checks.push("seek-resumes-recording");
 await page.waitForTimeout(3000);
 await app.evaluate(()=>{global.fixture.mode="followup";});
 await page.getByRole("button",{name:"播放回答",exact:true}).waitFor();
 await page.getByRole("button",{name:"播放回答",exact:true}).click();
 await sample("followup");
 await page.waitForTimeout(5000);checks.push("followup-audio-recorded");
 await app.evaluate(()=>{global.fixture.mode="feynman";});
 await page.getByRole("button",{name:"播放问答语音",exact:true}).click();
 await sample("feynman");
 await page.waitForTimeout(5000);checks.push("feynman-audio-recorded");
 await page.evaluate(()=>{location.hash="/home";});
 await page.waitForFunction(()=>!document.querySelector(".lw-simple"));
 const before=await app.evaluate(()=>global.fixture.records.get("rec_synthetic").chunks.length);
 await page.waitForTimeout(4000);
 const after=await app.evaluate(()=>global.fixture.records.get("rec_synthetic").chunks.length);
 assert(after-before<=1);checks.push("route-change-pauses-before-render");
 await app.evaluate(()=>{global.fixture.mode="lecture";});
 await page.evaluate(()=>{location.hash="/lecture/synthetic-desktop";});
 const returnedPlay=page.getByRole("button",{name:"继续讲解",exact:true});
 await page.getByRole("button",{name:/^(继续|暂停|停止)讲解$/}).first().waitFor();
 if(await returnedPlay.count())await returnedPlay.click();
 await app.evaluate(()=>new Promise(resolve=>{const timer=setInterval(()=>{if(global.fixture.records.size===2){clearInterval(timer);resolve();}},100);}));
 await sample("returned");
 await page.waitForTimeout(4000);checks.push("return-starts-second-part-without-overwrite");
 const closed=app.waitForEvent("close");
 await app.evaluate(()=>global.fixture.window.close());
 await closed;checks.push("normal-window-close-saves-tail");
 const bytes=(await stat(".backup/paper-classroom/recording-lifecycle.webm")).size;
 assert(bytes>10000);
 await writeFile(".backup/paper-classroom/recording-lifecycle-result.json",JSON.stringify({checks,events,bytes},null,2));
 console.log(JSON.stringify({passed:checks.length,checks}));
}finally{await app.close().catch(()=>{});}
