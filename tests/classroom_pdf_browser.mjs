import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir} from "node:fs/promises";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";
const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const checks=[],out=".backup/classroom-interactive";
await mkdir(out,{recursive:true});

function syntheticPdf(){
  const content="BT /F1 20 Tf 30 740 Td (Synthetic original region) Tj ET";
  const objects=["<< /Type /Catalog /Pages 2 0 R >>","<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 6 0 R >> >> /Contents 5 0 R >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 6 0 R >> >> /Contents 5 0 R >>",
    `<< /Length ${content.length} >>\nstream\n${content}\nendstream`,
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"];
  let value="%PDF-1.4\n",offsets=[0];
  objects.forEach((object,index)=>{offsets.push(Buffer.byteLength(value));value+=`${index+1} 0 obj\n${object}\nendobj\n`;});
  const xref=Buffer.byteLength(value);
  value+=`xref\n0 ${offsets.length}\n0000000000 65535 f \n`+
    offsets.slice(1).map(offset=>`${String(offset).padStart(10,"0")} 00000 n \n`).join("")+
    `trailer\n<< /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(value);
}
try{
 for(const width of [1440,390]){
  const context=await browser.newContext({viewport:{width,height:1000}});
  const page=await context.newPage(),errors=[];
  page.on("pageerror",error=>errors.push(error.message));
  const served=await mockBuiltFrontend(page);
  let focus={material_id:"synthetic-pdf",sha256:"a".repeat(64),target_id:"p1_r0",
    page_no:1,page_count:2,bbox:[.05,.05,.6,.09],precision:"text",text:"Synthetic original region"};
  let unavailable=false;
  const json=(route,value)=>route.fulfill({contentType:"application/json",body:JSON.stringify(value)});
  await page.route("**/api/**",route=>{
    const path=new URL(route.request().url()).pathname;
    if(path==="/api/auth/me")return json(route,{user_id:"synthetic-owner",status:"approved"});
    if(path.endsWith("/original")||path.endsWith("/download"))return unavailable?route.fulfill({status:404,body:""}):
      route.fulfill({contentType:"application/pdf",body:syntheticPdf()});
    if(path==="/api/teaching/synthetic-pdf")return json(route,{
      conversation_id:"synthetic-pdf",kind:"teaching",course_id:"synthetic-course",lesson_id:"synthetic-lesson",
      lesson_title:"合成原文精读",generation:1,session_state:"active",server_managed:true,playback_version:0,
      audio_mode:"text",tts_authorized:false,followups:[],queued_tasks:[],
      steps:[{step_id:"step-1",step_no:1,title:"原文",text:"合成精读说明。",status:"ready",text_version_no:1,
        audio_ready:false,board:{pdf_focus:focus,presentation:{version:2,title:"原文",blocks:[]}}}],
      last_checkpoint:{client_seq:1,step_no:1,position_ms:0,presentation:{visible_chars:8,paused:true}}});
    return json(route,{data:[],saved:true});
  });
  await page.goto(served.origin+"/#/lecture/synthetic-pdf");
  await page.getByLabel("当前讲解原文区域",{exact:true}).waitFor();
  await page.locator(".teaching-pointer").waitFor({state:"visible"});
  assert.equal(await page.getByRole("tab",{name:"原文",exact:true}).getAttribute("aria-selected"),"true");
  const pixelCount=await page.locator(".pdf-page canvas").evaluate(canvas=>{
    const bytes=canvas.getContext("2d").getImageData(0,0,canvas.width,canvas.height).data;
    let ink=0;for(let i=0;i<bytes.length;i+=4)if(bytes[i]<100&&bytes[i+1]<100&&bytes[i+2]<100)ink++;
    return ink;
  });
  assert(pixelCount>50,"original PDF must have visible source text pixels");
  checks.push(`real-original-render-${width}`);
  const initial=await page.locator(".pdf-focus-box").boundingBox();
  await page.getByRole("button",{name:"放大原文",exact:true}).click();
  await page.waitForFunction(initial=>document.querySelector(".pdf-focus-box")?.getBoundingClientRect().width>initial*1.2,initial.width);
  const zoomed=await page.locator(".pdf-focus-box").boundingBox();
  assert(Math.abs(zoomed.width/initial.width-1.25)<.04);
  checks.push(`region-scales-with-page-${width}`);
  await page.getByRole("button",{name:"下一页原文",exact:true}).click();
  await page.waitForFunction(()=>document.querySelector(".pdf-lesson")?.dataset.pdfFollowing==="false");
  await page.getByLabel("当前讲解原文区域",{exact:true}).waitFor({state:"hidden"});
  await page.getByRole("button",{name:"回到原文讲解位置",exact:true}).click();
  await page.getByLabel("当前讲解原文区域",{exact:true}).waitFor();
  assert.equal(await page.locator(".pdf-lesson").getAttribute("data-pdf-target"),"p1_r0");
  checks.push(`manual-review-and-return-${width}`);
  await page.getByRole("tab",{name:"板书",exact:true}).click();
  await page.locator(".excalidraw canvas").first().waitFor();
  await page.getByRole("tab",{name:"原文",exact:true}).click();
  await page.getByLabel("当前讲解原文区域",{exact:true}).waitFor();
  const link=await page.getByRole("link",{name:"下载原 PDF",exact:true}).getAttribute("href");
  assert(link.endsWith(`/sources/synthetic-pdf/${"a".repeat(64)}/original/download`));
  checks.push(`board-return-and-versioned-download-${width}`);
  await page.screenshot({path:`${out}/pdf-${width}.png`});
  focus={...focus,target_id:"p2",page_no:2,bbox:null,precision:"page"};
  await page.waitForFunction(()=>document.querySelector(".pdf-precision")?.textContent?.includes("当前仅能定位到原页"),{},{timeout:15000});
  assert.equal(await page.locator(".pdf-focus-box").count(),0);
  checks.push(`low-confidence-page-fallback-${width}`);
  unavailable=true;await page.reload();
  await page.getByText("原 PDF 加载失败，可能已删除或版本失效。",{exact:false}).waitFor();
  checks.push(`source-removal-fails-visibly-${width}`);
  assert.equal(errors.length,0,errors.join("\n"));
  await context.close();
 }
 console.log(JSON.stringify({checks,passed:checks.length}));
}finally{await browser.close();}
