import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir,readFile,writeFile} from "node:fs/promises";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";

const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright-core");
const image=await readFile(process.env.TEST_CROP),pdf=await readFile(process.env.TEST_PDF);
const classroomImage=process.env.TEST_CLASSROOM_CROP?await readFile(process.env.TEST_CLASSROOM_CROP):image;
const output=".backup/paddle-crops-20261023/browser";
await mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const checks=[];
try{
  for(const width of [1440,768,390,320]){
    const page=await browser.newPage({viewport:{width,height:1000},serviceWorkers:"block"});
    const errors=[],requests=[];
    page.on("pageerror",error=>errors.push(error.message));
    const served=await mockBuiltFrontend(page);
    let failCrop=false;
    const focus={material_id:"synthetic-material",sha256:"b".repeat(64),target_id:"p1_b1",
      page_no:1,page_count:4,text:"资料题设原图",bbox:null,precision:"provider_crop",
      crop_sha256:"a".repeat(64),block_no:1,crop_width:500,crop_height:300};
    const text="根据资料原图观察各输入和输出，再解释它们的关系。";
    const workspace={conversation_id:"synthetic-crop",kind:"teaching",course_id:"synthetic-course",
      lesson_id:"synthetic-lesson",lesson_title:"合成原图课堂",generation:1,session_state:"active",
      server_managed:true,playback_version:0,audio_mode:"text",tts_authorized:false,
      steps:[{step_id:"s1",step_no:1,title:"原图例题",text,text_version_no:1,status:"ready",
        board_scope:"synthetic-stage",board:{pdf_focus:focus,presentation:{version:2,title:"原图",blocks:[]}},
        audio_ready:false,audio_id:null}],followups:[],queued_tasks:[],
      last_checkpoint:{client_seq:1,step_no:1,position_ms:0,presentation:{visible_chars:text.length,paused:true}}};
    const question={id:"q1",kind:"short",prompt:"解释题图中输入与输出的关系。",options:[],points:2,
      lesson_ids:["synthetic-lesson"],primary_lesson_id:"synthetic-lesson",
      figure:{kind:"material_figure",caption:"资料题图",source:{material_id:focus.material_id,
        page_no:1,block_no:1,crop_sha256:focus.crop_sha256}}};
    const json=(route,value,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(value)});
    await page.route("**/api/**",async route=>{
      const url=new URL(route.request().url()),path=url.pathname;
      if(path.includes("/figures/")||path.includes("/images/")){
        requests.push(url.pathname+url.search);
        return failCrop?route.fulfill({status:503}):route.fulfill({contentType:"image/png",
          body:path.includes("/figures/")?classroomImage:image});
      }
      if(path.endsWith("/original")||path.endsWith("/original/download"))
        return route.fulfill({contentType:"application/pdf",body:pdf});
      if(path==="/api/auth/me")return json(route,{user_id:"synthetic-owner",status:"approved"});
      if(path==="/api/teaching/synthetic-crop")return json(route,workspace);
      if(path.endsWith("/interactive"))return json(route,{revision:0,playback_version:0,cells:[]});
      if(path.endsWith("/overview"))return json(route,{title:"合成课程",outline_confirmed:true,
        units:[{unit_id:"unit",title:"合成单元",lessons:[{lesson_id:"synthetic-lesson",title:"合成讲次",
          learning_state:"user_completed"}]}],progress:{}});
      if(path.endsWith("/assessment"))return json(route,{assessment_id:"quiz",status:"succeeded",
        eligible:true,assessment_version:2,questions:[question]});
      return json(route,{data:[],status:"succeeded",saved:true});
    });
    await page.goto(served.origin+"/#/lecture/synthetic-crop");
    await page.waitForFunction(()=>document.querySelector("[data-source-crop] img")?.naturalWidth>0);
    const crop=page.locator("[data-source-crop] img");
    const initial=await crop.evaluate(node=>node.getBoundingClientRect().width);
    await page.getByRole("button",{name:"放大题图",exact:true}).click();
    assert((await crop.evaluate(node=>node.getBoundingClientRect().width))>initial*1.2);
    await page.getByRole("button",{name:"重置题图缩放",exact:true}).click();
    await page.screenshot({path:`${output}/classroom-${width}.png`,fullPage:true});
    await page.getByRole("button",{name:"查看原 PDF 页面",exact:true}).click();
    await page.waitForFunction(()=>document.querySelector(".pdf-page canvas")?.width>0);
    await page.getByRole("button",{name:"查看题图裁片",exact:true}).click();
    await page.waitForFunction(()=>document.querySelector("[data-source-crop] img")?.naturalWidth>0);
    failCrop=true;
    await page.reload();
    await page.getByRole("button",{name:"重试题图",exact:true}).waitFor();
    failCrop=false;
    await page.getByRole("button",{name:"重试题图",exact:true}).click();
    await page.waitForFunction(()=>document.querySelector("[data-source-crop] img")?.naturalWidth>0);
    await page.goto(served.origin+"/#/course/synthetic-course");
    failCrop=true;
    await page.getByRole("button",{name:"查看单元测验",exact:true}).click();
    await page.getByRole("button",{name:"重试题图",exact:true}).waitFor();
    failCrop=false;
    await page.getByRole("button",{name:"重试题图",exact:true}).click();
    await page.waitForFunction(()=>document.querySelector(".assessment-figure img")?.naturalWidth>0);
    assert(requests.some(value=>value.includes("?crop_sha256="+"a".repeat(64))));
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1);
    assert(!overflow,`Unexpected overflow at ${width}`);
    await page.screenshot({path:`${output}/assessment-${width}.png`,fullPage:true});
    assert.deepEqual(errors,[]);
    checks.push({width,crop:true,zoom:true,originalPage:true,retry:true,assessment:true,assessmentRetry:true,noOverflow:true});
    await page.close();
  }
  await writeFile(`${output}/checks.json`,JSON.stringify(checks,null,2));
  console.log(JSON.stringify({checks}));
}finally{await browser.close();}
