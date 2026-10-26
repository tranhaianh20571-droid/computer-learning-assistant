import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir,stat,writeFile} from "node:fs/promises";
import path from "node:path";
const {_electron}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright");
const env={...process.env,RECORDING_TEST_OUTPUT:"recording-soak.webm",RECORDING_TEST_DIR:"soak-chunks"};
delete env.ELECTRON_RUN_AS_NODE;
const output=path.resolve(".backup/paper-classroom");await mkdir(output,{recursive:true});
const app=await _electron.launch({executablePath:path.resolve("desktop/node_modules/electron/dist/electron.exe"),
 args:[path.resolve("tests/paper_desktop_fixture.cjs")],timeout:90000,env});
const samples=[],errors=[];
try{
 const page=await app.firstWindow();page.setDefaultTimeout(45000);
 page.on("pageerror",error=>errors.push(error.message));
 await page.getByRole("button",{name:"继续讲解",exact:true}).waitFor();
 // Keep a real, locally generated speech fixture playing through the whole soak.
 await page.evaluate(()=>{
  const play=HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play=function(){this.loop=true;return play.call(this);};
 });
 await page.getByRole("button",{name:"继续讲解",exact:true}).click();
 await app.evaluate(()=>new Promise(resolve=>{const timer=setInterval(()=>{if(global.fixture?.records.size){clearInterval(timer);resolve();}},100);}));
 const duration=Number(process.env.RECORDING_SOAK_MS||3600000),start=Date.now();
 while(Date.now()-start<duration){
  await page.waitForTimeout(Math.min(30000,duration-(Date.now()-start)));
  const memory=await page.evaluate(()=>({heap:performance.memory?.usedJSHeapSize,errors:[...document.querySelectorAll(".classroom-media-error")].map(el=>el.textContent)}));
  const chunks=await app.evaluate(()=>global.fixture.records.get("rec_synthetic").chunks.length);
  const sample={elapsed_ms:Date.now()-start,...memory,chunks};samples.push(sample);
  await writeFile(path.join(output,"recording-soak-progress.json"),JSON.stringify({samples,errors},null,2));
  assert.deepEqual(memory.errors,[]);assert(chunks>0);
  console.log(`Recording soak ${Math.round(sample.elapsed_ms/1000)}s; acknowledged chunks ${chunks}; heap ${Math.round((memory.heap||0)/1024/1024)} MiB`);
 }
 await page.evaluate(()=>{location.hash="/home";});
 await app.evaluate(()=>new Promise(resolve=>{const timer=setInterval(()=>{if(global.fixture.records.get("rec_synthetic").status==="ready"){clearInterval(timer);resolve();}},100);}));
 const bytes=(await stat(path.join(output,"recording-soak.webm"))).size;
 assert(bytes>10000);assert.deepEqual(errors,[]);
 await writeFile(path.join(output,"recording-soak-result.json"),JSON.stringify({duration_ms:duration,bytes,samples,errors},null,2));
 console.log(`Real recorder soak completed: ${duration} ms`);
}finally{await app.close();}
