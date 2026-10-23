import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir} from "node:fs/promises";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";

const {chromium} = createRequire(import.meta.url)(process.env.PW_ROOT + "/playwright-core");
const browser = await chromium.launch({headless:true, executablePath:process.env.TEST_BROWSER});
const output = ".backup/study-sources-browser";
await mkdir(output, {recursive:true});
const checks = [];
try {
  for (const width of [1440,390]) {
    const page = await browser.newPage({viewport:{width,height:960},serviceWorkers:"block"});
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const served = await mockBuiltFrontend(page);
    const sources = [
      {kind:"original",page_no:18,end_page:19,chapter:"简单线性回归的模型与假设",
       answer_basis:"material",transcription_verified:false},
      {kind:"adapted",page_no:34,end_page:35,chapter:"案例：考试成绩与测验成绩",answer_basis:"ai"},
      {kind:"generated",reason:"资料中没有适合本次已学目标的完整原题。",answer_basis:"ai"},
      {kind:"original",unit:"document",page_no:2,line_start:3,line_end:5,
       chapter:"合成文本章节",transcription_verified:true,answer_basis:"material"},
      {kind:"original",unit:"slide",page_no:6,end_page:7,
       chapter:"合成课件章节",transcription_verified:false,answer_basis:"ai"},
    ];
    const questions = sources.map((source,index) => ({id:`q${index}`,kind:"short",
      prompt:`合成统计题 ${index + 1}：请说明 $\\beta_0$ 与 $\\beta_1$ 的含义和适用范围。`,goal:"理解线性回归",
      options:[],points:20,source}));
    await page.route("**/api/**", async route => {
      const path = new URL(route.request().url()).pathname;
      let body = {data:[]};
      if (path === "/api/auth/me") body = {user_id:"synthetic-owner",status:"approved"};
      else if (path.endsWith("/overview")) body = {
        title:"合成资料课程",outline_confirmed:true,teaching_unlocked:true,scenario:"school",contexts:[],
        progress:{lesson_total:1,lesson_completed:1},
        units:[{unit_id:"synthetic-unit",title:"回归基础",lesson_count:1,lessons:[
          {lesson_id:"synthetic-lesson",title:"模型解释",order_index:0,status_preparation:"ready",
           learning_state:"user_completed",startable:false}]}]};
      else if (path.endsWith("/assessment")) body = {
        assessment_id:"synthetic-assessment",status:"succeeded",eligible:true,title:"合成资料测验",questions};
      else if (path.endsWith("/cover")) body = {status:"not_generated"};
      await route.fulfill({status:200,contentType:"application/json",body:JSON.stringify(body)});
    });
    await page.goto(served.origin + "/#/course/synthetic-course");
    await page.getByRole("button",{name:"查看单元测验",exact:true}).click();
    await page.getByRole("button",{name:"资料原题 · 第 18 页",exact:true}).click();
    await page.getByText(/识别文本，请核对原页/).waitFor();
    await page.getByText(/参考资料答案/).waitFor();
    await page.getByRole("button",{name:"返回作答",exact:true}).click();
    assert.equal(await page.locator(".question-title .question-math").count(),2);
    assert.equal(await page.locator(".question-math .katex").count(),2);
    assert.equal(await page.getByText("参考答案",{exact:true}).count(),0);
    assert.equal(await page.getByRole("textbox",{name:/第\d题答案/}).count(),1);
    await page.getByRole("textbox",{name:"第1题答案",exact:true}).fill("合成回答：检查条件。");
    await page.getByRole("button",{name:"下一题",exact:true}).click();
    await page.getByRole("button",{name:"资料改编 · 第 34 页",exact:true}).waitFor();
    await page.getByRole("button",{name:"下一题",exact:true}).click();
    await page.getByRole("button",{name:"AI 补充",exact:true}).click();
    await page.getByText(/评分依据由 AI 拟定/).waitFor();
    await page.getByRole("button",{name:"返回作答",exact:true}).click();
    await page.getByRole("button",{name:"下一题",exact:true}).click();
    await page.getByRole("button",{name:"资料原题 · 第 3 行",exact:true}).click();
    await page.getByText(/第 3–5 行/).waitFor();
    assert.equal(await page.getByText(/识别文本，请核对原页/).count(),0);
    await page.getByRole("button",{name:"返回作答",exact:true}).click();
    await page.getByRole("button",{name:"下一题",exact:true}).click();
    await page.getByRole("button",{name:"资料原题 · 第 6 张幻灯片",exact:true}).click();
    await page.getByText(/第 6–7 张幻灯片/).waitFor();
    await page.getByText(/识别文本，请核对原页/).waitFor();
    await page.getByRole("button",{name:"返回作答",exact:true}).click();
    assert(await page.locator(".assessment-screen").evaluate(el => el.scrollWidth <= el.clientWidth + 1));
    const overflowing = await page.locator(".source-row").evaluateAll(elements =>
      elements.some(el => el.scrollWidth > el.clientWidth + 1));
    assert.equal(overflowing,false);
    await page.screenshot({path:`${output}/assessment-${width}.png`,fullPage:true});
    assert.deepEqual(errors,[]);
    checks.push(`assessment_sources_answers_hidden_layout_${width}`);
    await page.close();
  }
  console.log(JSON.stringify({checks,synthetic:true}));
} finally {
  await browser.close();
}
