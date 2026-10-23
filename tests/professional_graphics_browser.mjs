import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir,writeFile} from "node:fs/promises";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";

const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright-core");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const output=process.env.TEST_OUTPUT||".backup/professional-graphics/browser";
await mkdir(output,{recursive:true});
const checks=[],pageErrors=[],unexpected=[];
const bounds={x:[-2.3,2.3],y:[-4,4]};
const circuit={kind:"circuit",inputs:["A","B","C"],outputs:["Y"],gates:[
  {id:"n",type:"NOT",x:160,y:110},{id:"a",type:"AND",x:320,y:35},{id:"o",type:"OR",x:510,y:100}],
  wires:[{source:"B",target:"n",port:0},{source:"A",target:"a",port:0},{source:"n",target:"a",port:1},
    {source:"a",target:"o",port:0},{source:"C",target:"o",port:1},{source:"o",target:"Y",port:0}]};
const starter=structuredClone(circuit);starter.wires=starter.wires.filter(w=>w.source!=="C");
const plot={kind:"plot",bounds,curves:[{expression:"x^3-3*x"}],points:[{x:-1,y:2,label:"极大值点 (−1, 2)"},{x:1,y:-2,label:"极小值点 (1, −2)"}]};
const drawCircuit={kind:"circuit",inputs:circuit.inputs,outputs:circuit.outputs,allowed_gates:["NOT","AND","OR"],starter};
const drawPlot={kind:"plot",bounds,allow_expression:true,allow_sketch:true,allow_points:true,starter:null};
const qs=[
  {id:"q0",kind:"drawing",prompt:"画出逻辑函数 Y = A·B̅ + C 的电路。",drawing:drawCircuit,points:15,options:[]},
  {id:"q1",kind:"drawing",prompt:"画出 y = x³ − 3x，并标出两个极值点。",drawing:drawPlot,points:15,options:[]},
  {id:"q2",kind:"single",prompt:"根据图像，哪一项描述正确？",points:5,options:[{id:"A",text:"x = −1 处取得局部极大值"},{id:"B",text:"图像上没有驻点"}],
    figure:{kind:"generated_figure",graphic:{...plot,points:[]},caption:"函数图像"}},
  {id:"q3",kind:"short",prompt:"说明电路中非门的作用。",options:[],points:5,
    figure:{kind:"generated_figure",graphic:circuit,caption:"组合逻辑电路"}},
  {id:"q4",kind:"short",prompt:"解释局部极值的含义。",options:[],points:5},
].map(q=>({...q,lesson_ids:["l0"],primary_lesson_id:"l0"}));

async function setup(page){
  const served=await mockBuiltFrontend(page);unexpected.push(served.unexpected);
  let submitted=null,phase="curve";
  const text="观察函数 x^3-3*x 的曲线，驻点附近由上升转为下降，或者由下降转为上升。";
  await page.route("**/api/**",async route=>{
    const p=new URL(route.request().url()).pathname,m=route.request().method();let body={data:[]};
    if(p==="/api/auth/me")body={user_id:"graphics-owner",status:"approved"};
    else if(p.endsWith("/overview"))body={title:"专业图形示例",outline_confirmed:true,teaching_unlocked:true,contexts:[],progress:{lesson_total:1,lesson_completed:1},
      units:[{unit_id:"unit",title:"图形与逻辑",lessons:[{lesson_id:"l0",title:"图形与逻辑",learning_state:"user_completed"}]}]};
    else if(p.endsWith("/assessment"))body={assessment_id:"graphics-assessment",status:"succeeded",eligible:true,assessment_version:2,questions:qs};
    else if(p.endsWith("/attempts")&&m==="POST"){submitted=route.request().postDataJSON();body={attempt_id:"graphics-attempt",task_id:"graphics-grade"};}
    else if(p==="/api/assessment-attempts/graphics-attempt")body={attempt_id:"graphics-attempt",status:"succeeded",feedback:{
      earned:45,maximum:45,advice:"回看图形与逻辑关系。",
      grades:qs.map(q=>({question_id:q.id,earned:q.points,maximum:q.points,reason:"合成后端评分结果。",explanation:"合成解释。",
        correct_answer:["合成参考答案"],...(q.kind==="drawing"?{drawing_answer:submitted.answers[q.id],drawing_solution:q.id==="q0"?circuit:plot}:{})}))}};
    else if(p==="/api/teaching/graphics"){
      const graphic=phase==="surface"?{kind:"surface",expression:"x^2-y^2",bounds:{x:[-2,2],y:[-2,2]}}:phase==="circuit"?circuit:plot;
      const blocks=[{id:"intro",type:"text",span:3,title:phase==="surface"?"驻点与鞍点":phase==="circuit"?"组合逻辑电路":"函数的极大值与极小值",text:"观察图形与表达式的对应关系。"},
        {id:"professional",type:"figure",title:phase==="surface"?"z = x² − y²":phase==="circuit"?"Y = A·B̅ + C":"f(x) = x³ − 3x",span:3,graphic}];
      body={conversation_id:"graphics",kind:"teaching",course_id:"course",lesson_id:"l0",lesson_title:"图像与逻辑",generation:1,playback_version:1,
        session_state:"active",server_managed:true,audio_mode:"text",queued_tasks:[],followups:[],feynman:{checkpoints:[],active:null},
        last_checkpoint:{client_seq:1,step_no:1,position_ms:0,presentation:{paused:true,visible_chars:text.length,text_complete:true}},
        steps:[{step_no:1,step_id:"s1",title:"观察图形",text,status:"ready",text_version_no:1,audio_ready:false,audio_id:null,
          board:{format_version:3,presentation:{title:"图像与逻辑",blocks,all_blocks:blocks}}}]};
    }else if(p.endsWith("/interactive"))body={revision:0,playback_version:1,cells:[]};
    else if(p.endsWith("/checkpoint")||p.endsWith("/presentation"))body={saved:true};
    return route.fulfill({contentType:"application/json",body:JSON.stringify(body)});
  });
  page.on("pageerror",e=>pageErrors.push(e.message));
  return {served,setPhase:p=>phase=p,submission:()=>submitted};
}
async function openExam(page,origin){
  await page.goto(origin+"/#/course/course");
  await page.getByRole("button",{name:"查看单元测验",exact:true}).click();
  await page.locator(".drawing-workspace").waitFor();
}
try{
  for(const width of [1440,768,390]){
    const page=await browser.newPage({viewport:{width,height:1050},serviceWorkers:"block"}),state=await setup(page);
    await page.goto(state.served.origin+"/#/lecture/graphics?review=1");
    await page.locator("[data-figure-id=professional] .function-plot-host svg").waitFor();
    await page.locator(".excalidraw canvas").first().waitFor();
    await page.evaluate(()=>document.fonts.ready);
    await page.waitForFunction(()=>{
      const canvases=Array.from(document.querySelectorAll(".excalidraw canvas"));
      return canvases.some(canvas=>{
        const ctx=canvas.getContext("2d");if(!ctx||!canvas.width||!canvas.height)return false;
        const {data}=ctx.getImageData(0,0,canvas.width,canvas.height);let board=0;
        for(let i=0;i<data.length;i+=4)if(data[i+3]>180&&Math.abs(data[i]-23)<2&&Math.abs(data[i+1]-56)<2&&Math.abs(data[i+2]-45)<2)board++;
        return board>10000&&!document.querySelector(".excalidraw .LoadingMessage");
      });
    },null,{timeout:15000});
    assert.equal(await page.locator(".bd-col").evaluate(el=>getComputedStyle(el).backgroundColor),"rgb(23, 56, 45)");
    assert(await page.locator(".JXGtext").count()>0);
    await page.screenshot({path:`${output}/class-curve-${width}.png`,fullPage:true});
    state.setPhase("surface");await page.reload();
    const canvas=page.locator(".surface-plot-host canvas");await canvas.waitFor();
    const pixels=await canvas.evaluate(c=>{
      const out=document.createElement("canvas");out.width=c.width;out.height=c.height;const ctx=out.getContext("2d");ctx.drawImage(c,0,0);
      const data=ctx.getImageData(0,0,out.width,out.height).data;let ink=0;for(let i=3;i<data.length;i+=4)if(data[i]>30)ink++;return ink;
    });
    assert(pixels>1000,"surface must have visible pixels");
    await canvas.scrollIntoViewIfNeeded();
    const before=await canvas.screenshot(),rect=await canvas.boundingBox();
    await page.mouse.move(rect.x+rect.width/2,rect.y+rect.height/2);await page.mouse.down();await page.mouse.move(rect.x+rect.width/2+45,rect.y+rect.height/2+20,{steps:12});await page.mouse.up();
    assert(!before.equals(await canvas.screenshot()),"rotation must change actual canvas");
    await page.getByLabel("y = 0 截面",{exact:true}).uncheck();
    await page.getByRole("button",{name:"复位视角",exact:true}).click();
    await page.screenshot({path:`${output}/class-surface-${width}.png`,fullPage:true});
    state.setPhase("circuit");await page.reload();await page.locator("[data-figure-id=professional] .circuit-diagram").waitFor();
    await page.screenshot({path:`${output}/class-circuit-${width}.png`,fullPage:true});
    checks.push(`classroom_ink_curve_surface_pixels_rotate_section_circuit_${width}`);

    await openExam(page,state.served.origin);
    await page.getByRole("button",{name:"检查连接",exact:true}).click();
    assert((await page.locator(".drawing-footer").textContent()).includes("1 个"));
    await page.getByRole("button",{name:"C 输出",exact:true}).click();
    await page.getByRole("button",{name:"o 输入 2",exact:true}).click();
    await page.getByRole("button",{name:"检查连接",exact:true}).click();
    assert((await page.locator(".drawing-footer").textContent()).includes("完整"));
    await page.getByRole("button",{name:"撤销作图",exact:true}).click();
    await page.getByRole("button",{name:"检查连接",exact:true}).click();
    assert((await page.locator(".drawing-footer").textContent()).includes("1 个"));
    await page.getByRole("button",{name:"重做作图",exact:true}).click();
    const gate=page.getByRole("button",{name:"AND a",exact:true});
    const transform=await gate.getAttribute("transform");await gate.focus();await gate.press("ArrowRight");
    assert.notEqual(await gate.getAttribute("transform"),transform);
    await page.getByRole("button",{name:"撤销作图",exact:true}).click();
    await page.getByRole("button",{name:"连线 C 到 o 输入 2",exact:true}).focus();
    await page.getByRole("button",{name:"连线 C 到 o 输入 2",exact:true}).press("Delete");
    await page.getByRole("button",{name:"检查连接",exact:true}).click();
    assert((await page.locator(".drawing-footer").textContent()).includes("1 个"));
    await page.getByRole("button",{name:"撤销作图",exact:true}).click();
    const beforeGates=await page.locator("[data-gate]").count();
    await page.getByRole("button",{name:"添加逻辑门",exact:true}).click();
    assert.equal(await page.locator("[data-gate]").count(),beforeGates+1);
    await page.getByRole("button",{name:"撤销作图",exact:true}).click();
    await page.screenshot({path:`${output}/exam-drawing-${width}.png`,fullPage:true});
    await page.reload();await page.getByRole("button",{name:"查看单元测验",exact:true}).click();
    await page.getByRole("button",{name:"检查连接",exact:true}).click();
    assert((await page.locator(".drawing-footer").textContent()).includes("完整"),"saved wire must survive reload");
    assert.equal(await page.getByText("参考答案",{exact:true}).count(),0);
    await page.getByRole("button",{name:"下一题",exact:true}).click();
    await page.getByLabel("函数表达式",{exact:true}).fill("x^3-3*x");
    await page.getByRole("button",{name:"添加函数曲线",exact:true}).click();
    for(const [x,y]of [[-1,2],[1,-2]]){
      await page.getByLabel("标点横坐标",{exact:true}).fill(String(x));await page.getByLabel("标点纵坐标",{exact:true}).fill(String(y));
      await page.getByRole("button",{name:"添加标点",exact:true}).click();
    }
    const plotHost=page.locator(".drawing-plot .function-plot-host");await plotHost.scrollIntoViewIfNeeded();
    const sketchBox=await plotHost.boundingBox();
    await page.mouse.move(sketchBox.x+25,sketchBox.y+25);await page.mouse.down();
    await page.mouse.move(sketchBox.x+95,sketchBox.y+65,{steps:15});await page.mouse.up();
    await page.waitForFunction(()=>JSON.parse(localStorage.getItem("assessment.draft.graphics-owner.course.unit.graphics-assessment")).answers.q1.strokes.length===1);
    await page.getByRole("button",{name:"撤销作图",exact:true}).click();
    await page.waitForFunction(()=>JSON.parse(localStorage.getItem("assessment.draft.graphics-owner.course.unit.graphics-assessment")).answers.q1.strokes.length===0);
    await page.screenshot({path:`${output}/exam-plot-${width}.png`,fullPage:true});
    await page.getByLabel("函数表达式",{exact:true}).fill("x.constructor");
    await page.getByRole("button",{name:"添加函数曲线",exact:true}).click();assert(await page.locator(".drawing-error").count()>0);
    await page.getByRole("button",{name:"下一题",exact:true}).click();await page.locator(".assessment-figure .function-plot-host svg").waitFor();
    await page.locator('.options input[value="A"]').check();
    await page.getByRole("button",{name:"切换深浅色",exact:true}).click();
    assert.equal(await page.locator(".assessment-screen").getAttribute("data-theme"),"dark");
    await page.getByRole("button",{name:"切换深浅色",exact:true}).click();
    await page.screenshot({path:`${output}/exam-pictured-${width}.png`,fullPage:true});
    await page.getByRole("button",{name:"下一题",exact:true}).click();await page.locator(".assessment-figure .circuit-diagram").waitFor();
    await page.locator("textarea").fill("非门将输入取反。");
    await page.getByRole("button",{name:"下一题",exact:true}).click();await page.locator("textarea").fill("局部极值比较邻域。");
    await page.getByRole("button",{name:"检查并提交",exact:true}).click();
    await page.getByRole("dialog").getByRole("button",{name:/确认提交|提交测验|提交答案/}).click();
    await page.getByText("本次得分：45 / 45",{exact:true}).waitFor();
    assert.equal(state.submission().answers.q0.kind,"circuit");
    assert.equal(state.submission().answers.q1.kind,"plot");
    await page.getByRole("button",{name:"上一题",exact:true}).click();
    await page.getByRole("button",{name:"上一题",exact:true}).click();
    await page.getByRole("button",{name:"上一题",exact:true}).click();
    await page.getByRole("button",{name:"上一题",exact:true}).click();
    assert(await page.getByRole("button",{name:"添加逻辑门",exact:true}).isDisabled());
    await page.locator(".reference-content").waitFor({state:"visible"});
    assert.equal(await page.locator(".drawing-feedback .circuit-diagram").count(),1);
    const layout=await page.locator(".assessment-screen").evaluate(el=>({client:el.clientWidth,scroll:el.scrollWidth}));
    assert(layout.scroll<=layout.client+1,JSON.stringify(layout));
    checks.push(`drawing_wire_add_undo_redo_reload_plot_validation_pictured_submit_feedback_lock_${width}`);
    await page.close();
  }
  assert.deepEqual(pageErrors,[]);
  assert.deepEqual(unexpected.flat(),[]);
  await writeFile(`${output}/result.json`,JSON.stringify({checks,pageErrors,externalRequests:unexpected.flat(),syntheticAPI:true},null,2));
  console.log(JSON.stringify({checks,pageErrors},null,2));
}finally{await browser.close();}
