import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir} from "node:fs/promises";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";
const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const checks=[],out=".backup/classroom-interactive";
await mkdir(out,{recursive:true});
try{
 for(const [width,language] of [[1440,"python"],[390,"python"],[1440,"javascript"]]){
  const context=await browser.newContext({viewport:{width,height:1000}});
  const page=await context.newPage(),errors=[];
  page.on("pageerror",error=>errors.push(error.message));
  const served=await mockBuiltFrontend(page);
  const code=language==="python"?"value = 2\nprint(value * 3)":"var value = 2; console.log(value * 3)";
  const block={id:"example",type:"code",language,title:"合成数值变化",code,interaction:true,
    run_instruction:"运行代码，观察输出。",change_instruction:"将 value 改为 10，再次运行观察。",source_step:1};
  const narration="合成讲解，点击运行观察，准备好了点击右下角继续按钮。";
  let ws={conversation_id:"synthetic-code",kind:"teaching",course_id:"synthetic-course",lesson_id:"synthetic-lesson",
    lesson_title:"合成代码课堂",generation:1,session_state:"active",server_managed:true,playback_version:0,
    audio_mode:"text",tts_authorized:false,
    steps:[{step_id:"step-1",step_no:1,title:"合成示例",text:narration,text_version_no:1,status:"ready",
      board_scope:"synthetic-stage",board:{presentation:{version:2,title:"合成代码",blocks:[block]}},audio_ready:false,audio_id:null}],
    followups:[],queued_tasks:[],
    last_checkpoint:{client_seq:1,step_no:1,position_ms:0,presentation:{visible_chars:narration.length,paused:false}}};
  let state={revision:0,playback_version:0,cells:[]},feedback=[];
  const json=(route,body,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(body)});
  await page.route("**/api/**",async route=>{
    const path=new URL(route.request().url()).pathname,method=route.request().method();
    if(path==="/api/auth/me")return json(route,{user_id:"synthetic-owner",status:"approved"});
    if(path==="/api/teaching/synthetic-code")return json(route,ws);
    if(path.endsWith("/interactive")){
      if(method==="PUT"){
        const value=route.request().postDataJSON();
        if(value.revision!==state.revision)return json(route,{error_code:"CHECKPOINT_STALE"},409);
        state={...value,revision:state.revision+1};
      }
      return json(route,state);
    }
    if(path.endsWith("/interactive/feedback")){
      const value=route.request().postDataJSON();
      feedback.push({action:value.action,cell:structuredClone(state.cells[0])});
      ws={...ws,generation:ws.generation+1,session_state:"awaiting_resume",
        code_feedback:{generation:ws.generation+1,action:value.action,step_no:1,block_id:"example",instruction:""},
        followups:[...ws.followups,{role:"assistant",generation:ws.generation+1,content:value.action==="observe"?"观察输出，再修改数据。":"结果随数据变化。"}]};
      return json(route,{task_id:"synthetic-feedback",generation:ws.generation});
    }
    if(path.endsWith("/resume")){ws={...ws,session_state:"active"};return json(route,{});}
    if(path.endsWith("/checkpoint")){
      const value=route.request().postDataJSON();
      ws={...ws,last_checkpoint:{...value,presentation:value.board_state.presentation}};
      return json(route,{saved:true});
    }
    return json(route,{data:[],status:"succeeded"});
  });
  await page.goto(served.origin+"/#/lecture/synthetic-code");
  await page.getByText("观察好了，点击右下角的继续按钮。",{exact:true}).waitFor();
  assert.equal(await page.locator(".lecture-play-control").getAttribute("aria-label"),"继续讲解");
  checks.push(`teacher-pauses-at-code-${language}-${width}`);
  await page.getByRole("button",{name:"运行代码",exact:true}).click();
  await page.waitForFunction(()=>["success","error","stopped"].includes(document.querySelector(".code-cell-output")?.dataset.status),{},{timeout:120000});
  assert.equal(await page.locator(".code-cell-output").getAttribute("data-status"),"success",
    await page.locator(".code-cell-output").innerText());
  assert.match(await page.getByRole("status",{name:"代码运行结果"}).innerText(),/6/);
  assert.equal(await page.locator(".lecture-play-control").getAttribute("aria-label"),"继续讲解");
  checks.push(`real-${language}-result-and-no-auto-resume-${width}`);
  if(language==="javascript"){
    const editor=page.getByRole("textbox",{name:"合成数值变化，可编辑",exact:true});
    await editor.fill("console.log(value * 5)");
    await page.getByRole("button",{name:"运行代码",exact:true}).click();
    await page.waitForFunction(()=>document.querySelector(".code-cell-output")?.dataset.status==="success");
    assert.match(await page.getByRole("status",{name:"代码运行结果"}).innerText(),/10/);
    checks.push("real-javascript-and-shared-vars");
    await context.close();continue;
  }
  await page.getByRole("button",{name:"继续讲解",exact:true}).click();
  await page.waitForFunction(()=>document.querySelector(".stream")?.textContent?.includes("观察输出，再修改数据。"));
  await page.getByRole("button",{name:"继续讲解",exact:true}).click();
  await page.getByText("将 value 改为 10，再次运行观察。",{exact:true}).waitFor();
  const editor=page.getByRole("textbox",{name:"合成数值变化，可编辑",exact:true});
  await editor.fill("value = 10\nprint(value * 3)");
  await page.getByRole("button",{name:"运行代码",exact:true}).click();
  await page.waitForFunction(()=>document.querySelector(".code-cell-output")?.dataset.status==="success");
  assert.match(await page.getByRole("status",{name:"代码运行结果"}).innerText(),/30/);
  checks.push(`editable-code-changes-real-output-${width}`);
  await page.getByRole("button",{name:"继续讲解",exact:true}).click();
  await page.waitForFunction(()=>document.querySelector(".stream")?.textContent?.includes("结果随数据变化。"));
  assert.equal(feedback[1].cell.output.trim(),"30");
  assert.equal(feedback[1].cell.baseline_output.trim(),"6");
  checks.push(`feedback-bound-to-actual-comparison-${width}`);
  await page.reload();
  await editor.waitFor();
  assert.match(await editor.inputValue(),/value = 10/);
  assert.match(await page.locator(".code-environment").innerText(),/变量环境已重置/);
  checks.push(`refresh-restores-draft-without-replaying-code-${width}`);
  // Exercise shared variables, numerical libraries and genuine raster plots.
  await editor.fill("import numpy as np\nprint(np.array([value, 3]).sum())\nplt.plot([1, 2], [2, 4])");
  await page.getByRole("button",{name:"运行代码",exact:true}).click();
  await page.waitForFunction(()=>document.querySelector(".code-cell-output")?.dataset.status==="error",{},{timeout:120000});
  assert.match(await page.getByRole("status",{name:"代码运行结果"}).innerText(),/value/);
  await editor.fill("import numpy as np\nvalue = 10\nprint(np.array([value, 3]).sum())\nplt.plot([1, 2], [2, 4])");
  await page.getByRole("button",{name:"运行代码",exact:true}).click();
  await page.waitForFunction(()=>document.querySelector(".code-cell-output")?.dataset.status==="success");
  await page.getByAltText("运行结果图 1").waitFor();
  assert.match(await page.getByRole("status",{name:"代码运行结果"}).innerText(),/13/);
  checks.push(`numpy-plot-and-explicit-kernel-reset-${width}`);
  await editor.fill("print(value + 1)");
  await page.getByRole("button",{name:"运行代码",exact:true}).click();
  await page.waitForFunction(()=>document.querySelector(".code-cell-output")?.dataset.status==="success");
  assert.match(await page.getByRole("status",{name:"代码运行结果"}).innerText(),/11/);
  checks.push(`shared-python-variables-${width}`);
  await editor.fill("import js\nawait js.fetch('https://egress.synthetic.test/probe')");
  await page.getByRole("button",{name:"运行代码",exact:true}).click();
  await page.waitForFunction(()=>["success","error"].includes(document.querySelector(".code-cell-output")?.dataset.status));
  assert(!served.unexpected.some(value=>value.includes("egress.synthetic.test")));
  checks.push(`sandbox-blocks-network-${width}`);
  // CSP must prevent network access from the opaque sandbox, regardless of Python error shape.
  await editor.fill("raise ValueError('synthetic failure')");
  await page.getByRole("button",{name:"运行代码",exact:true}).click();
  await page.waitForFunction(()=>document.querySelector(".code-cell-output")?.dataset.status==="error");
  assert.match(await page.getByRole("status",{name:"代码运行结果"}).innerText(),/synthetic failure/);
  checks.push(`visible-runtime-error-${width}`);
  await editor.fill("while True:\n    pass");
  await page.getByRole("button",{name:"运行代码",exact:true}).click();
  await page.getByRole("button",{name:"停止运行",exact:true}).click();
  await page.waitForFunction(()=>document.querySelector(".code-cell-output")?.dataset.status==="stopped");
  checks.push(`explicit-stop-destroys-worker-${width}`);
  if(width===1440){
    await page.getByRole("button",{name:"运行代码",exact:true}).click();
    await page.waitForFunction(()=>document.querySelector(".code-cell-output")?.dataset.status==="running");
    await page.waitForFunction(()=>document.querySelector(".code-cell-output")?.dataset.status==="stopped",{},{timeout:120000});
    assert.match(await page.getByRole("status",{name:"代码运行结果"}).innerText(),/超时/);
    checks.push("execution-deadline-destroys-worker");
  }
  await page.screenshot({path:`${out}/code-${width}.png`});
  assert.equal(errors.length,0,errors.join("\n"));
  await context.close();
 }
 console.log(JSON.stringify({checks,passed:checks.length}));
}finally{await browser.close();}
