import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {readFile,mkdir} from "node:fs/promises";
import {spawnSync} from "node:child_process";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";
const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright");
const output=".backup/paper-classroom";
await mkdir(output,{recursive:true});
const ffmpeg=process.env.TEST_FFMPEG||"server/.venv/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe";
const videoFile=output+"/review-fixture.webm";
const generated=spawnSync(ffmpeg,["-y","-v","error","-f","lavfi","-i","testsrc2=size=640x360:rate=12",
 "-f","lavfi","-i","sine=frequency=440:sample_rate=48000","-t","4","-c:v","libvpx","-c:a","libopus",videoFile]);
assert.equal(generated.status,0,generated.stderr?.toString());
const bytes=await readFile(videoFile),checks=[];
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
try{
 for(const width of [1440,390]){
  const context=await browser.newContext({viewport:{width,height:950}});
  const page=await context.newPage();page.setDefaultTimeout(15000);
  const errors=[],requests=[];page.on("pageerror",e=>errors.push(e.message));
  const served=await mockBuiltFrontend(page);
  let rows=[{recording_id:"recording",conversation_id:"conversation",status:"ready",
   created_at:"2026-10-26T09:00:00Z",duration_ms:4000,chunks:2}];
  const json=(route,data)=>route.fulfill({contentType:"application/json",body:JSON.stringify(data)});
  await page.route("**/api/**",route=>{
   const path=new URL(route.request().url()).pathname,method=route.request().method();
   requests.push({path,method});
   if(path==="/api/auth/me")return json(route,{user_id:"synthetic-owner",status:"approved"});
   if(path==="/api/courses/course/overview")return json(route,{
    course_id:"course",title:"可靠学习",outline_confirmed:true,teaching_unlocked:true,
    progress:{lesson_total:1,lesson_completed:1},units:[{unit_id:"unit",title:"研究基础",lesson_count:1,
     lessons:[{lesson_id:"lesson",title:"研究问题与方法",order_index:0,startable:true,learning_state:"user_completed"}]}]});
   if(path==="/api/courses/course/lessons/lesson/recordings")
    return json(route,{title:"研究问题与方法",conversation_id:"conversation",completed:true,data:rows});
   if(path.endsWith("/video")){
    const range=route.request().headers().range?.match(/^bytes=(\d+)-(\d*)$/);
    const start=range?Number(range[1]):0,end=range&&range[2]?Number(range[2]):bytes.length-1;
    return route.fulfill({status:range?206:200,contentType:"video/webm",body:bytes.subarray(start,end+1),
      headers:{"Accept-Ranges":"bytes","Content-Length":String(end-start+1),
       ...(range?{"Content-Range":`bytes ${start}-${end}/${bytes.length}`}:{})}});
   }
   if(path.endsWith("/finish")){rows=[{...rows[0],status:"interrupted"}];return json(route,rows[0]);}
   return json(route,{data:[],saved:true});
  });
  await page.goto(served.origin+"/#/course/course");
  await page.locator(".lec").getByRole("button",{name:"复习",exact:true}).click();
  try{await page.getByLabel("课堂录像播放器").waitFor();}
  catch(error){console.log({url:page.url(),body:await page.locator("body").innerText(),requests,errors});throw error;}
  await page.waitForFunction(()=>document.querySelector("video")?.readyState>=2);
  const seek=await page.evaluate(async()=>{
   const video=document.querySelector("video"),wait=()=>new Promise(resolve=>video.addEventListener("seeked",resolve,{once:true}));
   let done=wait();video.currentTime=3;await done;const forward=video.currentTime;
   done=wait();video.currentTime=1;await done;const back=video.currentTime;
   await video.play();await new Promise(resolve=>setTimeout(resolve,200));video.pause();
   return {forward,back,duration:video.duration,controls:video.controls,width:video.getBoundingClientRect().right};
  });
  assert(seek.duration>=4&&seek.duration<4.2,JSON.stringify(seek));assert(seek.forward>2.9&&seek.back<1.5,JSON.stringify(seek));
  assert(seek.controls&&seek.width<=width+1);checks.push(`completed-review-video-bidirectional-${width}`);
  assert.equal(await page.getByRole("button",{name:/录制|开始学习/}).count(),0);
  assert(!requests.some(r=>r.method==="POST"));checks.push(`review-does-not-generate-or-mark-learning-${width}`);
  await page.screenshot({path:`${output}/review-${width}.png`});
  rows=[];await page.reload();
  await page.getByText("这节课暂无课堂录像。",{exact:true}).waitFor();
  await page.getByRole("button",{name:"查看已讲知识点",exact:true}).waitFor();
  checks.push(`old-lesson-empty-recording-${width}`);
  rows=[{recording_id:"unfinished",conversation_id:"conversation",status:"recording",chunks:2,created_at:"2026-10-26T09:00:00Z"}];
  await page.reload();await page.getByRole("button",{name:"恢复已录内容",exact:true}).click();
  await page.getByLabel("课堂录像播放器").waitFor();
  await page.getByText("这是中断后恢复的部分录像。",{exact:true}).waitFor();
  checks.push(`interrupted-recovery-${width}`);
  assert(!requests.some(r=>/\/start$|\/checkpoint$|\/complete$|\/seek-backward$/.test(r.path)&&r.method==="POST"));
  assert.deepEqual(errors,[]);await context.close();
 }
 console.log(JSON.stringify({passed:checks.length,checks}));
}finally{await browser.close();}
