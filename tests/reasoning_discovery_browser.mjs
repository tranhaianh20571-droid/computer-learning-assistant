import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir} from "node:fs/promises";
import {fileURLToPath} from "node:url";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PW_ROOT+"/playwright-core");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
const served=await mockBuiltFrontend(page);
const errors=[],requests=[],saved=[],unexpected=[],checks=[];
page.on("pageerror",error=>errors.push(error.message));
let rows=["planning","teaching","dialogue"].map(role=>({role,revision:1,config_id:"synthetic-"+role,
  configuration:{config_id:"synthetic-"+role,config_version:1,protocol:"openai_responses",
    endpoint:"https://relay.synthetic.test/v1",model_name:"initial-"+role,credential_mask:"****",
    capability_status:{},reasoning_settings:{mode:"default",show_summary:false}}}));
const json=(route,data,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(data)});
await page.route("**/api/**",async route=>{
  const request=route.request(),p=new URL(request.url()).pathname;
  if(p==="/api/auth/me")return json(route,{user_id:"synthetic-owner",email:"synthetic@example.test",is_admin:false,status:"approved"});
  if(p==="/api/capabilities/model-roles")return json(route,{data:rows});
  if(p==="/api/capabilities/configs")return json(route,{data:rows.map(item=>item.configuration)});
  if(p==="/api/connectors")return json(route,{data:[]});
  if(p==="/api/capabilities/reasoning/discover"){
    const body=request.postDataJSON();requests.push(body);
    if(body.model_name==="failure")return json(route,{detail:{error_code:"capability_test_failed"}},503);
    if(body.model_name==="retry-service"&&!body.refresh)return json(route,{
      state:"unknown",options:[],parameter_validation:"unknown",error_code:"MODEL_PROVIDER_ERROR",provider_status:502});
    if(body.model_name==="late-first")await new Promise(resolve=>setTimeout(resolve,1500));
    const effort=["final-model","retry-service"].includes(body.model_name)?"low":"ultra";
    try{return await json(route,{state:"completed",parameter_validation:"unconfirmed",
      options:[{value:"effort:"+effort,evidence:body.model_name==="declared"?"declared":"request_accepted",
        settings:{mode:"effort",effort,show_summary:false,discovered:true}}]});}catch{return;}
  }
  const role=p.match(/model-roles\/([^/]+)\/configuration$/);
  if(role){
    const body=request.postDataJSON();saved.push(body);
    rows=rows.map(row=>row.role===role[1]?{...row,revision:row.revision+1,
      configuration:{...row.configuration,...body,config_version:row.configuration.config_version+1}}:row);
    return json(route,{data:rows});
  }
  unexpected.push(p);return json(route,{detail:{error_code:"ERROR"}},404);
});
const form=page.getByRole("region",{name:"课程规划模型配置",exact:true});
const select=form.getByRole("combobox",{name:"思考强度",exact:true});
const model=form.getByLabel("模型",{exact:true});
async function detected(){
  await page.waitForFunction(()=>document.querySelector('[aria-label="课程规划模型配置"] .reasoning-select')?.dataset.discoveryState==="completed");
}
try{
  await page.goto(served.origin+"/#/capabilities");
  await form.waitFor();await detected();
  assert.equal(requests.length,1);
  assert.equal(requests[0].config_id,"synthetic-planning");
  assert.equal("api_key" in requests[0],false);
  assert.equal(requests.some(item=>item.model_name.includes("teaching")),false);
  assert.equal(await select.locator('option[value="effort:ultra"]').textContent(),"ultra");
  await select.selectOption("effort:ultra");
  await form.getByRole("button",{name:"保存",exact:true}).click();
  await form.getByText("已保存 · 新任务将使用此配置",{exact:true}).waitFor();
  assert.equal(saved.at(-1).reasoning_settings.effort,"ultra");
  assert.equal(saved.at(-1).reasoning_settings.discovered,true);
  checks.push("automatic saved-credential discovery, visible role only, nonpreset value saved");
  await detected();
  const before=requests.length;
  await model.fill("rapid-1");await model.fill("rapid-2");await model.fill("final-model");
  await page.waitForFunction(()=>document.querySelector('[aria-label="课程规划模型配置"] .reasoning-select option[value="effort:low"]'));
  assert.equal(requests.length,before+1);
  assert.equal(requests.at(-1).model_name,"final-model");
  checks.push("debounce only sends the final input");
  const lateStarted=page.waitForRequest(request=>request.url().endsWith("/reasoning/discover")&&request.postDataJSON().model_name==="late-first");
  await model.fill("late-first");await lateStarted;
  await model.fill("final-model");
  await page.waitForTimeout(1800);
  assert.equal(await select.locator('option[value="effort:ultra"]').count(),0);
  assert.equal(await select.inputValue(),"default");
  checks.push("late response cannot replace a newer target");
  await model.fill("declared");await detected();
  assert.equal(await select.locator('option[value="effort:ultra"]').textContent(),"ultra");
  checks.push("effort labels use English values without evidence suffixes");
  await model.fill("retry-service");
  await page.waitForFunction(()=>document.querySelector('[aria-label="课程规划模型配置"] .reasoning-select option[value="__retry__"]'));
  assert((await select.locator('option[value="__status__"]').textContent()).includes("模型服务请求失败"));
  assert((await select.locator('option[value="__status__"]').textContent()).includes("502"));
  assert(await select.locator('option[value="__status__"]').isDisabled());
  assert(await form.getByRole("button",{name:"保存",exact:true}).isEnabled());
  await select.selectOption("__retry__");
  await page.waitForFunction(()=>document.querySelector('[aria-label="课程规划模型配置"] .reasoning-select option[value="effort:low"]'));
  assert.equal(requests.at(-1).refresh,true);
  assert.equal(await select.locator('option[value="__status__"]').count(),0);
  assert.equal(await select.locator('option[value="effort:low"]').textContent(),"low");
  await select.selectOption("effort:low");
  assert.equal(await select.inputValue(),"effort:low");
  checks.push("HTTP 200 with failed discovery shows a controlled failure and in-place retry recovers selectable effort");
  await model.fill("final-model");await detected();
  await form.getByLabel("服务地址",{exact:true}).fill("https://new-target.synthetic.test");
  const beforeKey=requests.length;await page.waitForTimeout(1100);
  assert.equal(requests.length,beforeKey);
  assert((await select.getAttribute("title")).includes("API Key"));
  await form.getByLabel("API Key",{exact:true}).fill("synthetic-new-key");
  await detected();
  assert.equal(requests.at(-1).endpoint,"https://new-target.synthetic.test");
  assert.equal(requests.at(-1).api_key,"synthetic-new-key");
  checks.push("new destination never reuses a stored secret without a newly entered key");
  await model.fill("failure");
  await page.waitForFunction(()=>document.querySelector('[aria-label="课程规划模型配置"] .reasoning-select')?.dataset.discoveryState==="failed");
  assert(await form.getByRole("button",{name:"保存",exact:true}).isEnabled());
  assert(await select.isEnabled());
  await select.selectOption("default");
  await form.getByRole("button",{name:"保存",exact:true}).click();
  await form.getByText("已保存 · 新任务将使用此配置",{exact:true}).waitFor();
  assert.equal(saved.at(-1).reasoning_settings.mode,"default");
  checks.push("failed detection keeps defaults and saving available");
  const output=fileURLToPath(new URL("../.backup/reasoning-discovery",import.meta.url));
  await mkdir(output,{recursive:true});
  for(const width of [1440,390,320]){
    await page.setViewportSize({width,height:1000});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:output+`/discovery-${width}.png`,fullPage:true});
  }
  assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
  console.log(JSON.stringify({checks,pageErrors:errors,unexpected,requests:requests.length}));
}finally{await browser.close();}
