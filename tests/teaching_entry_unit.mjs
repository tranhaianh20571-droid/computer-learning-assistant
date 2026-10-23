import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {createRequire} from "node:module";
import vm from "node:vm";
import path from "node:path";
import {fileURLToPath} from "node:url";

const root=fileURLToPath(new URL("../",import.meta.url));
const require=createRequire(path.join(root,"frontend/package.json"));
const ts=require("typescript");
function load(file,dependencies={}){
  const source=readFileSync(path.join(root,file),"utf8").replace(/const API = .*?;/,"const API = \"\";");
  const output=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
  const exports={};
  vm.runInNewContext(output,{exports,require:name=>dependencies[name]||{},fetch:fakeFetch,FormData,crypto,AbortController,TextDecoder},{filename:file});
  return exports;
}
const calls=[];
async function fakeFetch(url,init={}){
  calls.push({url,method:init.method||"GET",body:init.body});
  return {ok:true,text:async()=>JSON.stringify(url.endsWith("/preflight")?{title:"Synthetic cover",configs:[]}:{conversation_id:"synthetic-conversation",status:"queued"})};
}
let consentCount=0;
const {api}=load("frontend/src/api.ts",{"./ui/ExternalConsent":{requestConsent:async()=>{consentCount++;return null;}},"./lib/consent":{}});
await api.startLessonV2("synthetic-course","synthetic-lesson");
await api.startLesson("synthetic-course","synthetic-lesson");
await api.prewarmLesson("synthetic-course","synthetic-lesson");
await api.recoverLesson("synthetic-course","synthetic-lesson","synthetic-conversation");
assert.deepEqual(calls.map(call=>call.url),[
  "/api/courses/synthetic-course/lessons/synthetic-lesson/start",
  "/api/courses/synthetic-course/lessons/synthetic-lesson/start",
  "/api/courses/synthetic-course/lessons/synthetic-lesson/prewarm",
  "/api/teaching/synthetic-conversation/recover"
]);
assert(calls.every(call=>call.method==="POST"&&call.body===undefined));
assert.equal(consentCount,0);
await api.authorizeCover("course","synthetic-course");
assert.equal(consentCount,1);
assert.equal(calls.at(-1).url,"/api/planning/course/synthetic-course/cover/preflight");

const {lessonAudioRequired}=load("frontend/src/hooks/useLessonAudio.ts");
const step={text:"合成讲解",step_no:1};
assert.equal(lessonAudioRequired({tts_config:{config_id:"saved"}},step,1),false);
assert.equal(lessonAudioRequired({tts_authorized:false,queued_tasks:[{kind:"tts",step_no:1,status:"queued"}]},step,1),false);
assert.equal(lessonAudioRequired({tts_authorized:false},{...step,audio_ready:true,audio_id:"synthetic-audio"},1),false);
assert.equal(lessonAudioRequired({tts_authorized:true},step,1),true);
assert.equal(lessonAudioRequired({audio_mode:"text",tts_authorized:true},step,1),false);
assert.equal(lessonAudioRequired({}, {...step,audio_ready:true,audio_id:"synthetic-audio"},1),true);
assert.equal(lessonAudioRequired({queued_tasks:[{kind:"tts",step_no:1,status:"failed"}]},step,1),false);
assert.equal(lessonAudioRequired({queued_tasks:[{kind:"tts",step_no:2,status:"queued"}]},step,1),false);
assert.equal(lessonAudioRequired({queued_tasks:[{kind:"tts",step_no:1,status:"running"}]},step,1),true);
assert.equal(lessonAudioRequired({tts_authorized:true},undefined,1),true);
assert.equal(lessonAudioRequired({tts_authorized:false},undefined,1),false);

console.log(JSON.stringify({checks:["direct_teaching_actions_without_consent","cover_consent_preserved","authorized_audio_only","failed_and_wrong_step_audio_do_not_gate","teaching_actions_remain_compatible"],synthetic:true}));
