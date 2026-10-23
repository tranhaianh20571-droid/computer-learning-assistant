import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir} from "node:fs/promises";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";

const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright-core");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const output=process.env.TEST_OUTPUT||".backup/unit-assessment-blue";
await mkdir(output,{recursive:true});
const checks=[];
const counts=[6,8,10,8],titles=["自动化与智能体","决策与执行","工具调用与反馈","边界与应用"];
const lessonIndex=i=>{let start=0;for(let n=0;n<counts.length;n++){start+=counts[n];if(i<start)return n}};
const questions=Array.from({length:32},(_,i)=>({
  id:`q${i}`,kind:i===9?"multiple":i===10?"short":i===11?"code_reading":"single",points:5,
  prompt:i===8?"什么决定了一个系统属于智能体，而不仅是自动化流程？\n\n一个系统从多个社区抓取帖子，调用大模型筛选内容，再发送邮件。它每次都按预设步骤执行，不能根据中间结果改变后续行动。"
    :i===10?"请解释目标与反馈的关系。":i===11?"阅读以下代码并说明结果。\n\nx = 4\ny = 2 * x + 3\nprint(y)":`合成练习 ${i+1}：请判断以下描述。`,
  options:i===10||i===11?[]:[
    {id:"A",text:"是否使用了大语言模型。"},
    {id:"B",text:"是否能根据目标与反馈，自主决定下一步行动。"},
    {id:"C",text:"是否同时接入了多个外部工具。"},
    {id:"D",text:"是否能够自动处理大量数据。"}],
  lesson_ids:[`lesson-${lessonIndex(i)}`],
  source:{kind:"adapted",page_no:18,chapter:"《智能体基础》· 2.1",answer_basis:"ai"}
}));
const key=owner=>`assessment.draft.${owner}.course.unit.assessment`;
const seed={current:8,answers:Object.fromEntries(Array.from({length:9},(_,i)=>[`q${i}`,["B"]])),flags:["q3","q6"]};
const submissions=[];
let owner="synthetic-owner",gradeState="failed",failSubmission=true,generationState="succeeded";
const feedback={earned:150,maximum:160,advice:"回看目标与反馈的关系。",
  grades:questions.map((q,i)=>({question_id:q.id,prompt:q.prompt,earned:i===0?0:i===1?3:i===2?2:5,maximum:5,
    reason:"合成评分结果。",explanation:"根据固定评分规则给出反馈。",correct_answer:["合成参考答案"],
    weaknesses:[],source:q.source}))};
try{
  const page=await browser.newPage({viewport:{width:1440,height:1000},serviceWorkers:"block"});
  const errors=[];page.on("pageerror",e=>errors.push(e.message));
  const served=await mockBuiltFrontend(page);
  await page.addInitScript(({key,seed})=>{
    if(!localStorage.getItem("assessment.browser.seeded")){
      localStorage.setItem(key,JSON.stringify(seed));localStorage.setItem("assessment.browser.seeded","1");
    }
  },{key:key(owner),seed});
  await page.route("**/api/**",async route=>{
    const path=new URL(route.request().url()).pathname;
    let body={data:[]},status=200;
    if(path==="/api/auth/me")body={user_id:owner,email:"",status:"approved"};
    else if(path.endsWith("/overview"))body={
      title:"AI Agent 入门",outline_confirmed:true,teaching_unlocked:true,scenario:"school",contexts:[],
      progress:{lesson_total:4,lesson_completed:4},
      units:[{unit_id:"unit",title:"从自动化到智能体",lesson_count:4,lessons:titles.map((title,n)=>({
        lesson_id:`lesson-${n}`,title,order_index:n,status_preparation:"ready",learning_state:"user_completed",startable:false
      }))}]
    };
    else if(path.endsWith("/assessment")&&route.request().method()==="GET")body={
      assessment_id:"assessment",task_id:"generate-task",eligible:true,status:generationState,
      questions:generationState==="succeeded"?questions:[]};
    else if(path==="/api/assessments/assessment/attempts"){
      submissions.push(route.request().postDataJSON());
      if(failSubmission){status=503;body={error_code:"MODEL_CAPABILITY_UNAVAILABLE"};failSubmission=false}
      else body={attempt_id:"attempt",task_id:"grade-task"};
    }
    else if(path==="/api/assessment-attempts/attempt")body={
      attempt_id:"attempt",task_id:"grade-task",status:gradeState,
      ...(gradeState==="succeeded"?{feedback}:{})};
    else if(path.includes("/retry")){gradeState="succeeded";generationState="succeeded";body={task_id:"grade-task"}}
    else if(path.endsWith("/cover"))body={status:"not_generated"};
    await route.fulfill({status,contentType:"application/json",body:JSON.stringify(body)});
  });
  const open=async()=>{await page.goto(served.origin+"/#/course/course");await page.getByRole("button",{name:"查看单元测验",exact:true}).click()};
  await open();
  await page.getByRole("heading",{name:/什么决定了/}).waitFor();
  assert.equal(await page.locator(".options input").count(),4);
  assert.equal(await page.locator(".question-title").count(),1);
  assert.equal(await page.locator(".number").count(),32);
  assert.equal(await page.locator(".lesson-group").count(),4);
  assert.equal(await page.locator(".assessment-screen").evaluate(el=>getComputedStyle(el).getPropertyValue("--accent").trim()),"#2959b6");
  assert.equal(await page.getByText("参考答案",{exact:true}).count(),0);
  assert.equal(await page.locator("input:checked").inputValue(),"B");
  const reference=await browser.newPage({viewport:{width:1440,height:1000},serviceWorkers:"block"});
  const prototype=await mockBuiltFrontend(reference,"文档/ui/单元测验");
  await reference.goto(prototype.origin+"/?palette=blue");
  await reference.getByRole("heading",{name:/什么决定了/}).waitFor();
  await reference.evaluate(()=>document.fonts.ready);
  const selectors=["header",".course-head",".question-top",".question-title",".scenario",
    ".option",".source-row",".footer-inner","aside",".numbers",".btn.primary"];
  const layout=async p=>p.evaluate(selectors=>Object.fromEntries(selectors.map(selector=>{
    const root=document.querySelector(".assessment-screen")||document;
    const el=root.querySelector(selector),r=el.getBoundingClientRect(),s=getComputedStyle(el);
    return [selector,{x:r.x,y:r.y,width:r.width,height:r.height,font:s.fontSize,color:s.color,background:s.backgroundColor}];
  })),selectors);
  const expected=await layout(reference),actual=await layout(page);
  for(const selector of selectors){
    for(const prop of ["x","y","width","height"])assert(Math.abs(expected[selector][prop]-actual[selector][prop])<=2,
      `${selector} ${prop}: ${actual[selector][prop]} != ${expected[selector][prop]}`);
    assert.equal(actual[selector].font,expected[selector].font,selector+" font");
    assert.equal(actual[selector].background,expected[selector].background,selector+" background");
  }
  await reference.close();
  checks.push("approved_B_prototype_11_elements_geometry_font_background_parity");
  const measurements=[];
  for(const width of [1440,768,390,320]){
    await page.setViewportSize({width,height:1000});
    const measured=await page.locator(".assessment-screen").evaluate(el=>({
      width:el.clientWidth,scroll:el.scrollWidth,
      title:getComputedStyle(el.querySelector(".question-title")).fontSize,
      button:getComputedStyle(el.querySelector(".btn.primary")).backgroundColor
    }));
    assert(measured.scroll<=measured.width,JSON.stringify(measured));
    measurements.push({width,...measured});
    await page.screenshot({path:`${output}/blue-${width}.png`,fullPage:true,animations:"disabled"});
    if(width===390){
      await page.getByRole("button",{name:"答题卡",exact:true}).click();
      await page.screenshot({path:`${output}/mobile-navigation.png`,fullPage:true,animations:"disabled"});
      await page.getByRole("button",{name:"第10题，未答",exact:true}).click();
      assert.equal(await page.locator(".question-index strong").innerText(),"10");
      assert.equal(await page.locator(".assessment-screen aside").isVisible(),false);
      await page.getByRole("button",{name:"上一题",exact:true}).click();
    }
  }
  checks.push("B_palette_single_page_grouped_navigation_four_widths");
  await page.setViewportSize({width:1440,height:1000});
  await page.getByRole("button",{name:"切换深浅色",exact:true}).click();
  await page.screenshot({path:`${output}/blue-dark.png`,fullPage:true,animations:"disabled"});
  await page.getByRole("button",{name:"切换深浅色",exact:true}).click();
  await page.getByRole("button",{name:"标记",exact:true}).click();
  await page.getByRole("button",{name:"下一题",exact:true}).click();
  await page.locator('input[value="A"]').check();await page.locator('input[value="C"]').check();
  await page.getByRole("button",{name:"下一题",exact:true}).click();
  await page.getByRole("textbox",{name:"第11题答案",exact:true}).fill("根据反馈调整后续行动。");
  await page.getByRole("button",{name:"上一题",exact:true}).click();
  assert.equal(await page.locator("input:checked").count(),2);
  await page.getByRole("button",{name:"上一题",exact:true}).click();
  assert.equal(await page.locator("input:checked").inputValue(),"B");
  assert.equal(await page.getByRole("button",{name:"已标记",exact:true}).getAttribute("aria-pressed"),"true");
  await page.getByRole("button",{name:"资料改编 · 第 18 页",exact:true}).click();
  const source=page.getByRole("dialog",{name:"题目来源"});
  await source.getByText("评分依据由 AI 拟定",{exact:false}).waitFor();
  assert.equal(await source.getByText("参考答案",{exact:true}).count(),0);
  await source.getByRole("button",{name:"返回作答",exact:true}).click();
  await page.getByRole("button",{name:"退出测验",exact:true}).click();
  await page.getByRole("button",{name:"保存并退出",exact:true}).click();
  assert.equal(await page.locator(".assessment-screen").count(),0);
  assert.equal(await page.locator("#root").getAttribute("inert"),null);
  await open();
  await page.getByRole("button",{name:"下一题",exact:true}).click();
  await page.getByRole("button",{name:"下一题",exact:true}).click();
  assert.equal(await page.getByRole("textbox",{name:"第11题答案",exact:true}).inputValue(),"根据反馈调整后续行动。");
  await page.reload();await page.getByRole("button",{name:"查看单元测验",exact:true}).click();
  assert.equal(await page.getByRole("textbox",{name:"第11题答案",exact:true}).inputValue(),"根据反馈调整后续行动。");
  checks.push("radio_checkbox_text_flag_exit_reload_recovery");
  await page.getByRole("button",{name:"提交测验",exact:true}).click();
  await page.getByRole("button",{name:"前往第一道未答题",exact:true}).click();
  assert.equal(await page.locator(".question-index strong").innerText(),"12");
  for(let i=11;i<32;i++){
    if(i===11)await page.getByRole("textbox",{name:"第12题答案",exact:true}).fill("11。");
    else await page.locator('input[value="B"]').check();
    if(i<31)await page.getByRole("button",{name:"下一题",exact:true}).click();
  }
  const submit=async()=>{await page.getByRole("button",{name:"检查并提交",exact:true}).click();
    await page.getByRole("dialog",{name:"确认提交测验"}).getByRole("button",{name:"提交测验",exact:true}).click()};
  await submit();
  await page.getByRole("alert").waitFor();
  await submit();
  await page.getByRole("button",{name:"重试评价",exact:true}).waitFor();
  assert.equal(submissions.length,2);
  assert.equal(submissions[0].idempotency_key,submissions[1].idempotency_key);
  assert.deepEqual(submissions[0].answers,submissions[1].answers);
  assert.equal(Object.keys(submissions[1].answers).length,32);
  await page.getByRole("button",{name:"重试评价",exact:true}).click();
  await page.getByRole("heading",{name:"本次得分：150 / 160",exact:true}).waitFor();
  await page.getByText("参考答案",{exact:true}).click();
  await page.getByText("合成参考答案",{exact:true}).waitFor();
  await page.screenshot({path:`${output}/feedback.png`,fullPage:true,animations:"disabled"});
  checks.push("missing_question_submit_unknown_result_idempotency_retry_feedback");
  assert.equal(await page.locator(".assessment-report").count(),1);
  assert.equal(await page.locator(".number.result-wrong").count(),1);
  assert.equal(await page.locator(".number.result-partial").count(),2);
  assert.equal(await page.locator(".number.result-correct").count(),29);
  await page.getByRole("button",{name:"第1题，需要订正",exact:true}).click();
  assert((await page.getByRole("region",{name:"你的作答",exact:true}).innerText()).includes("B · 是否能根据目标"));
  await page.locator(".result-filter select").selectOption("partial");
  assert.equal(await page.locator(".number").count(),2);
  await page.locator(".result-filter select").selectOption("all");
  await page.locator(".reference-toggle").click();
  assert.equal(await page.locator(".reference-content").count(),0);
  await page.locator(".reference-toggle").click();
  await page.getByRole("checkbox",{name:"强调错误"}).uncheck();
  assert.equal(await page.locator(".highlight-errors").count(),0);
  await page.getByRole("checkbox",{name:"强调错误"}).check();
  await page.getByRole("button",{name:"写订正",exact:true}).click();
  await page.getByRole("button",{name:"保存订正",exact:true}).click();
  await page.getByRole("status").filter({hasText:"请先填写订正内容"}).waitFor();
  await page.getByLabel("重新写出思路与答案",{exact:true}).fill("合成订正 <script>throw 1</script>");
  await page.getByRole("button",{name:"下一题",exact:true}).click();
  assert.equal(await page.locator(".question-index strong").innerText(),"01");
  await page.getByRole("button",{name:"保存订正",exact:true}).click();
  await page.getByRole("button",{name:"下一题",exact:true}).click();
  await page.getByRole("button",{name:"上一题",exact:true}).click();
  assert.equal(await page.locator(".correction-copy").innerText(),"合成订正 <script>throw 1</script>");
  assert.equal(await page.locator(".correction-copy script").count(),0);
  await page.reload();await page.getByRole("button",{name:"查看单元测验",exact:true}).click();
  await page.locator(".correction-copy").waitFor();
  const downloadPromise=page.waitForEvent("download");
  await page.getByRole("button",{name:"导出批改报告",exact:true}).click();
  const download=await downloadPromise;
  assert.equal(download.suggestedFilename(),"测验批改报告.txt");
  await page.getByRole("button",{name:"第11题，回答正确",exact:true}).click();
  assert((await page.getByRole("region",{name:"你的作答",exact:true}).innerText()).includes("根据反馈调整后续行动。"));
  await page.evaluate(key=>{
    const draft=JSON.parse(localStorage.getItem(key));
    draft.answers.q10="提交后被改写的本机草稿，不应作为原作答";
    localStorage.setItem(key,JSON.stringify(draft));
  },key(owner));
  await page.reload();await page.getByRole("button",{name:"查看单元测验",exact:true}).click();
  await page.locator(".assessment-report").waitFor();
  assert((await page.getByRole("region",{name:"你的作答",exact:true}).innerText()).includes("根据反馈调整后续行动。"));
  for(const width of [1440,768,390,320]){
    await page.setViewportSize({width,height:1000});
    assert(await page.locator(".assessment-screen").evaluate(el=>el.scrollWidth<=el.clientWidth));
    assert.equal(await page.locator(".result-comparison").evaluate(el=>getComputedStyle(el).gridTemplateColumns.split(" ").length),width===1440?2:1);
    await page.screenshot({path:`${output}/result-${width}.png`,fullPage:true,animations:"disabled"});
    if(width===390){
      await page.getByRole("button",{name:"答题卡",exact:true}).click();
      await page.getByRole("button",{name:"第2题，部分得分",exact:true}).click();
      assert.equal(await page.locator(".assessment-screen aside").isVisible(),false);
    }
  }
  await page.setViewportSize({width:1440,height:1000});
  await page.getByRole("button",{name:"切换深浅色",exact:true}).click();
  await page.screenshot({path:`${output}/result-dark.png`,fullPage:true,animations:"disabled"});
  checks.push("graded_comparison_filters_reference_corrections_download_four_widths_dark");
  await page.getByRole("button",{name:"再测一次",exact:true}).click();
  assert.equal(await page.locator(".question-index strong").innerText(),"01");
  assert.equal(await page.locator("input:checked").count(),0);
  assert.equal(await page.locator(".result-score").count(),0);
  owner="other-synthetic-owner";
  await page.reload();await page.getByRole("button",{name:"查看单元测验",exact:true}).click();
  assert.equal(await page.locator("input:checked").count(),0);
  assert.equal(await page.locator(".question-index strong").innerText(),"01");
  checks.push("repeat_and_owner_scoped_draft");
  await page.close();

  const failure=await browser.newPage({viewport:{width:390,height:960},serviceWorkers:"block"});
  await mockBuiltFrontend(failure);
  generationState="failed";
  await failure.route("**/api/**",async route=>{
    const path=new URL(route.request().url()).pathname;
    let body={data:[]};
    if(path==="/api/auth/me")body={user_id:"synthetic-failure",email:"",status:"approved"};
    else if(path.endsWith("/overview"))body={title:"合成课程",outline_confirmed:true,units:[{unit_id:"unit",title:"合成单元",lessons:[]}],progress:{}};
    else if(path.endsWith("/assessment"))body={assessment_id:"assessment",task_id:"task",eligible:true,status:generationState,
      questions:generationState==="succeeded"?questions.slice(0,3):[]};
    else if(path.includes("/retry"))generationState="succeeded";
    await route.fulfill({contentType:"application/json",body:JSON.stringify(body)});
  });
  await failure.addInitScript(()=>{Storage.prototype.setItem=()=>{throw new Error("Storage unavailable")}});
  await failure.goto(served.origin+"/#/course/course");
  await failure.getByRole("button",{name:"查看单元测验",exact:true}).click();
  await failure.getByRole("button",{name:"重试生成测验",exact:true}).click();
  await failure.locator(".question-title").waitFor();
  await failure.locator('input[value="A"]').check();
  await failure.getByRole("button",{name:"下一题",exact:true}).click();
  await failure.getByRole("button",{name:"上一题",exact:true}).click();
  assert.equal(await failure.locator("input:checked").inputValue(),"A");
  assert(await failure.getByText("草稿暂存于当前页面",{exact:true}).count()>0);
  assert.equal(await failure.locator(".number").count(),3);
  assert.equal(await failure.locator(".lesson-group").count(),1);
  await failure.close();
  checks.push("generation_failure_retry_legacy_group_storage_unavailable");
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({checks,measurements,synthetic:true,pageErrors:errors}));
}finally{await browser.close()}
