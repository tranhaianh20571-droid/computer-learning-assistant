import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {writeFile} from "node:fs/promises";

const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright-core");
const base=process.env.TEST_FRONTEND, backend=process.env.TEST_BACKEND, output=process.env.TEST_OUTPUT;
assert.equal(new URL(backend).hostname,"127.0.0.1");
assert.notEqual(new URL(backend).port,"8000");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:"reduce"});
const page=await context.newPage(),errors=[],checks=[];
page.on("pageerror",error=>errors.push(error.message));
page.setDefaultTimeout(30000);
await context.addInitScript(({backend})=>{
  const original=window.fetch.bind(window);
  window.fetch=(input,init)=>original(typeof input==="string"&&input.startsWith("/api/")?backend+input:input,init);
},{backend});
await page.route("**/*",route=>["127.0.0.1","localhost"].includes(new URL(route.request().url()).hostname)?route.continue():route.abort());
const read=async path=>{
  const response=await context.request.get(backend+path);
  assert.equal(response.status(),200);
  return response.json();
};
try{
  await page.goto(base+"/#/login");
  await page.getByLabel("邮箱",{exact:true}).fill("synthetic-browser-member@example.com");
  await page.getByLabel("密码",{exact:true}).fill("synthetic-password-12345");
  await page.getByRole("button",{name:"登录",exact:true}).click();
  await page.waitForURL("**/#/home");
  for(const config of [
    {kind:"content_model",protocol:"openai",endpoint:"https://model.example.test/v1",model_name:"synthetic-browser-content"},
    {kind:"search",protocol:"anysearch",endpoint:"https://api.anysearch.com",model_name:""},
  ]){
    const response=await context.request.post(backend+"/api/capabilities/configs",
      {data:{...config,credentials:{api_key:"synthetic-browser-key"}}});
    assert.equal(response.status(),201,await response.text());
  }
  await page.getByLabel("学习目标",{exact:true}).fill("Synthetic complete web development learning route");
  await page.getByRole("button",{name:"生成规划草稿",exact:true}).click();
  await page.locator(".q-card").nth(4).waitFor();
  const initial=(await read("/api/planning/current")).planning;
  const cards=page.locator(".q-card");
  for(let index=0;index<await cards.count();index++){
    const card=cards.nth(index);
    if(await card.getByRole("group",{name:"学习周期"}).count()){
      await card.getByRole("button",{name:"8周",exact:true}).click();
      await card.getByRole("button",{name:"5小时",exact:true}).click();
    }else await card.locator(".opt-card").first().click();
  }
  await page.getByRole("button",{name:"提交并继续",exact:true}).click();
  await page.getByRole("button",{name:"没问题，确认草稿",exact:true}).waitFor({timeout:60000});
  const state=await read("/api/planning/tasks/"+initial.task_id);
  assert.equal(state.task_status,"succeeded");
  assert.equal(state.error_code,null);
  assert.equal(state.intake.done,true);
  assert.equal(state.intake.answers.length,5);
  const process=(await read("/api/planning/tasks/"+initial.task_id+"/process")).process;
  assert.equal(process.search.status,"succeeded");
  assert.equal(process.search.queries.length,9);
  assert.equal(process.search.queries.filter(row=>row.status==="succeeded").length,3);
  assert.equal(process.search.queries.filter(row=>row.reason==="search_budget_limit").length,6);
  checks.push("real_login_interview_coverage_nine_queries_three_search_slots_and_draft");
  await page.reload();
  await page.getByRole("button",{name:"没问题，确认草稿",exact:true}).waitFor();
  assert.equal((await read("/api/planning/current")).planning.task_id,initial.task_id);
  assert.deepEqual((await read("/api/planning/tasks/"+initial.task_id+"/process")).process.search.queries,process.search.queries);
  checks.push("refresh_preserves_task_answers_search_and_draft_without_replay");
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:1000});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.screenshot({path:`${output}/planning-search-${width}.png`,fullPage:true});
  }
  const acknowledgement=page.getByRole("checkbox",{name:"我已了解并接受以上缺口，仍然使用这份规划"});
  if(await acknowledgement.count())await acknowledgement.check();
  await page.getByRole("button",{name:"没问题，确认草稿",exact:true}).click();
  await page.waitForFunction(()=>document.body.textContent.includes("已确认为不可变版本")||document.body.textContent.includes("已确认"));
  const confirmed=await read("/api/planning/tasks/"+initial.task_id);
  assert.equal(confirmed.confirmed,true);
  checks.push("route_confirmation_persisted_with_search_publication_fences");
  assert.deepEqual(errors,[]);
  await writeFile(output+"/planning-search-result.json",JSON.stringify({checks,errors,synthetic:true},null,2));
  console.log(JSON.stringify({checks,errors}));
}finally{await browser.close();}
