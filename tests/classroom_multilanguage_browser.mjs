import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir} from "node:fs/promises";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";
const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const checks=[],out=".backup/classroom-multilanguage";
await mkdir(out,{recursive:true});
const examples=[
 ["java","public class Main {public static void main(String[] args){int value = 2;System.out.println(value*3);}}"],
 ["c",'#include <stdio.h>\nint main(){int value = 2;printf("%d",value*3);}'],
 ["c++","#include <iostream>\nint main(){int value = 2;std::cout<<value*3;}"],
 ["python","import torch\nvalue = 2\nprint(torch.tensor(value)*3)"],
 ["pytorch","import torch\nvalue = 2\nprint(torch.tensor(value)*3)"],
];
try{
 for(const width of [1440,390]){
  for(const [language,code] of examples){
   const context=await browser.newContext({viewport:{width,height:1000}});
   const page=await context.newPage(),errors=[],runs=[],stops=[];
   page.on("pageerror",error=>errors.push(error.message));
   const served=await mockBuiltFrontend(page);
   const block={id:"example",type:"code",language,title:"合成代码",code,interaction:true,source_step:1,
    run_instruction:"点击运行观察。",change_instruction:"修改 value 后再次运行。"};
   const narration="合成讲解，请运行代码，观察好了点击继续按钮。";
   const ws={conversation_id:"synthetic-code",kind:"teaching",course_id:"synthetic-course",lesson_id:"synthetic-lesson",
    lesson_title:"合成多语言课堂",generation:1,session_state:"active",server_managed:true,playback_version:0,
    audio_mode:"text",tts_authorized:false,steps:[{step_id:"step-1",step_no:1,title:"合成代码",text:narration,
      text_version_no:1,status:"ready",board_scope:"synthetic-stage",board:{presentation:{version:2,title:"合成代码",blocks:[block]}},
      audio_ready:false,audio_id:null}],followups:[],queued_tasks:[],last_checkpoint:{client_seq:1,step_no:1,
      position_ms:0,presentation:{visible_chars:narration.length,paused:false}}};
   let state={revision:0,playback_version:0,cells:[]},mode="success";
   const json=(route,body,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(body)});
   await page.route("**/api/**",async route=>{
    const path=new URL(route.request().url()).pathname,method=route.request().method();
    if(path==="/api/auth/me")return json(route,{user_id:"synthetic-owner",status:"approved"});
    if(path==="/api/teaching/synthetic-code")return json(route,ws);
    if(path.endsWith("/interactive")){
     if(method==="PUT")state={...route.request().postDataJSON(),revision:state.revision+1};
     return json(route,state);
    }
    if(path.includes("/interactive/runs/")){
     if(method==="DELETE"){stops.push(path);return json(route,{stopped:true});}
     const body=route.request().postDataJSON();runs.push(body);
     if(mode==="busy")return json(route,{error_code:"classroom_busy"},429);
     if(mode==="unavailable")return json(route,{error_code:"classroom_unavailable"},503);
     if(mode==="slow"){await new Promise(resolve=>setTimeout(resolve,1500));}
     return json(route,{status:"success",output:body.code.includes("value = 10")?"30":"6",images:[]}).catch(()=>{});
    }
    return json(route,{data:[],status:"succeeded"});
   });
   await page.goto(served.origin+"/#/lecture/synthetic-code");
   await page.getByText("观察好了，点击右下角的继续按钮。",{exact:true}).waitFor();
   assert.equal(runs.length,0);
   await page.getByRole("button",{name:"运行代码",exact:true}).click();
   await page.waitForFunction(()=>document.querySelector(".code-cell-output")?.dataset.status==="success");
   assert.equal(runs[0].original_code,code);
   assert.equal(runs[0].step_no,1);
   assert.match(await page.locator(".code-cell-output").innerText(),/6/);
   assert.equal(await page.locator(".lecture-play-control").getAttribute("aria-label"),"继续讲解");
   checks.push(`${language}-${width}-run-bound-and-paused`);
   const editor=page.getByRole("textbox",{name:"合成代码，可编辑",exact:true});
   await editor.fill(code.replace("value = 2","value = 10"));
   await page.getByRole("button",{name:"运行代码",exact:true}).click();
   await page.waitForFunction(()=>document.querySelector(".code-cell-output")?.dataset.status==="success");
   assert.match(await page.locator(".code-cell-output").innerText(),/30/);
   assert.equal(await page.locator(".lecture-play-control").getAttribute("aria-label"),"继续讲解");
   checks.push(`${language}-${width}-editable-results`);
   if(language==="java"){
    mode="busy";
    await page.getByRole("button",{name:"运行代码",exact:true}).click();
    await page.waitForFunction(()=>document.querySelector(".code-cell-output")?.dataset.status==="error");
    assert.match(await page.locator(".code-cell-output").innerText(),/忙/);
    mode="unavailable";
    await page.getByRole("button",{name:"运行代码",exact:true}).click();
    await page.waitForFunction(()=>document.querySelector(".code-cell-output")?.textContent?.includes("不可用"));
    checks.push(`controlled-errors-${width}`);
    mode="slow";
    await page.getByRole("button",{name:"运行代码",exact:true}).click();
    await page.getByRole("button",{name:"停止运行",exact:true}).click();
    await page.waitForFunction(()=>document.querySelector(".code-cell-output")?.dataset.status==="stopped");
    await page.waitForTimeout(1800);
    assert.equal(await page.locator(".code-cell-output").getAttribute("data-status"),"stopped");
    assert(stops.length>0);
    const editorState=await editor.evaluate(node=>({left:node.scrollLeft,top:node.scrollTop,
      color:getComputedStyle(node).color,indent:getComputedStyle(node).textIndent,
      length:node.value.length,width:node.clientWidth,height:node.clientHeight,
      topEdge:node.getBoundingClientRect().top,
      canvasTop:node.closest(".teaching-board-canvas").getBoundingClientRect().top}));
    await editor.screenshot({path:`${out}/editor-${width}.png`});
    assert.equal(editorState.left,0,JSON.stringify(editorState));
    assert.equal(editorState.top,0,JSON.stringify(editorState));
    assert(editorState.topEdge>=editorState.canvasTop,JSON.stringify(editorState));
    checks.push(`stop-fences-late-result-${width}`);
    await page.screenshot({path:`${out}/java-${width}.png`});
   }
   assert.equal(errors.length,0,errors.join("\n"));
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   await context.close();
  }
 }
 console.log(JSON.stringify({passed:checks.length,checks}));
}finally{await browser.close();}
