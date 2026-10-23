import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir,writeFile} from "node:fs/promises";
import {fileURLToPath} from "node:url";
import path from "node:path";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";

const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PW_ROOT+"/playwright-core");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const context=await browser.newContext({viewport:{width:1440,height:1000}});
const page=await context.newPage();
const served=await mockBuiltFrontend(page);
const checks=[],errors=[],unexpected=[];
const output=fileURLToPath(new URL("../.backup/settings-purpose",import.meta.url));
await mkdir(output,{recursive:true});
page.on("pageerror",e=>errors.push(e.message));
const roles=["planning","teaching","dialogue","research"];
const config=role=>({config_id:"synthetic-"+role,config_version:1,protocol:"openai",
  endpoint:`https://${role}.example.test/v1`,model_name:role+"-model",owner_scope:"user",
  kind:"content_model",credential_mask:"****",is_active:true,capability_status:{text:{state:"available"}},
  reasoning_settings:{mode:"default",show_summary:false}});
let rows=roles.map(role=>({role,revision:0,config_id:"synthetic-"+role,configuration:config(role)}));
let failSave=false,failRoles=false,isAdmin=false;
let saveBodies=[],revealCalls=0,testCalls=0;
const json=(route,body,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(body)});
await page.route("**/api/**",async route=>{
  const request=route.request(),url=new URL(request.url()),method=request.method();
  if(url.pathname==="/api/auth/me")return json(route,{user_id:"synthetic-owner",email:"synthetic@example.test",is_admin:isAdmin,status:"approved"});
  if(url.pathname==="/api/capabilities/configs")return json(route,{data:rows.map(row=>row.configuration)});
  if(url.pathname==="/api/capabilities/model-roles")return json(route,failRoles?{detail:{error_code:"ERROR"}}:{data:rows},failRoles?503:200);
  if(url.pathname==="/api/connectors"||url.pathname==="/api/materials"||url.pathname==="/api/auth/admin/pending")return json(route,{data:[]});
  if(url.pathname==="/api/memory/settings")return json(route,{enabled:true,revision:1});
  if(url.pathname==="/api/memory/profile"||url.pathname==="/api/cards")return json(route,{data:[]});
  if(url.pathname==="/api/navigation")return json(route,{});
  if(url.pathname==="/api/planning/current")return json(route,{planning:null});
  if(url.pathname==="/api/capabilities/reasoning/discover"){
    const known=request.postDataJSON().model_name==="recognized-reasoning-model";
    return json(route,{state:"completed",parameter_validation:"rejected_invalid",
      options:known?["low","high"].map(effort=>({value:"effort:"+effort,evidence:"request_accepted",
        settings:{mode:"effort",effort,show_summary:false,discovered:true}})):[]});
  }
  if(url.pathname==="/api/capabilities/models")return json(route,{models:["catalog-model","second-model"]});
  if(url.pathname.endsWith("/credential/reveal")){revealCalls++;return json(route,{api_key:"synthetic-display-only"});}
  if(url.pathname.endsWith("/test")){testCalls++;return json(route,{capability_status:{text:{state:"available"}}});}
  const match=url.pathname.match(/model-roles\/([^/]+)\/configuration$/);
  if(match&&method==="PUT"){
    const body=request.postDataJSON();saveBodies.push({role:match[1],body});
    if(failSave)return json(route,{detail:{error_code:"ERROR",message:"synthetic-private-error"}},500);
    rows=rows.map(row=>row.role===match[1]?{...row,revision:row.revision+1,config_id:"synthetic-"+row.role+"-"+(row.revision+1),
      configuration:{...row.configuration,config_id:"synthetic-"+row.role+"-"+(row.revision+1),protocol:body.protocol,endpoint:body.endpoint,model_name:body.model_name,reasoning_settings:body.reasoning_settings}}:row);
    return json(route,{data:rows});
  }
  unexpected.push(method+" "+url.pathname);
  return json(route,{detail:{error_code:"ERROR"}},404);
});
const section=name=>page.getByRole("region",{name:name+"模型配置",exact:true});
try{
 await page.goto(served.origin+"/#/capabilities");
 await section("课程规划").waitFor();
 for(const width of [1440,1150,900,850,768,390,320]){
   await page.setViewportSize({width,height:1000});
   await page.evaluate(()=>document.querySelector(".settings-scroll").scrollTop=0);
   const geometry=await section("课程规划").evaluate(element=>{
     const model=element.querySelector(".model-picker-input").getBoundingClientRect();
     const thinking=element.querySelector(".reasoning-select").getBoundingClientRect();
     const protocol=element.querySelector("select").getBoundingClientRect();
     return {adjacent:thinking.x>=model.right&&Math.abs(thinking.y-model.y)<=2,
       aligned:Math.abs(model.x-protocol.x)<=2,height:thinking.height,overflow:document.documentElement.scrollWidth>innerWidth};
   });
   assert(geometry.adjacent&&geometry.aligned&&geometry.height>=38&&!geometry.overflow,JSON.stringify({width,geometry}));
 }
 checks.push("approved horizontal layout, aligned fields and adjacent reasoning select at seven widths");
 assert.equal(await page.getByRole("heading",{name:"当前配置",exact:true}).count(),0);
 assert.equal(await page.getByText("任务分工",{exact:true}).count(),0);
 checks.push("purpose navigation without separate content summary");
 await section("课程规划").getByLabel("协议",{exact:true}).selectOption("openai_responses");
 await section("课程规划").getByLabel("API Key",{exact:true}).fill("synthetic-responses-key");
 await section("课程规划").getByLabel("模型",{exact:true}).fill("recognized-reasoning-model");
 await section("课程规划").getByRole("combobox",{name:"思考强度",exact:true}).selectOption("effort:high");
 await section("课程规划").getByRole("button",{name:"保存",exact:true}).click();
 await section("课程规划").getByText("已保存 · 新任务将使用此配置",{exact:true}).waitFor();
 assert.equal(saveBodies.at(-1).body.protocol,"openai_responses");
 assert.equal(saveBodies.at(-1).body.reasoning_settings.effort,"high");
 assert.equal(saveBodies.at(-1).body.reasoning_settings.discovered,true);
 assert.equal(testCalls,0);
 checks.push("Responses and automatically explored effort save without a mandatory connection test");
 for(const width of [1440,768,390,320]){
   await page.setViewportSize({width,height:1000});
   for(const name of ["课程规划","课程讲解","即时对话"]){
     await page.getByRole("button",{name,exact:true}).click();
     assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),name+" "+width);
   }
   await page.getByRole("button",{name:"课程规划",exact:true}).click();
   await page.screenshot({path:path.join(output,`settings-${width}.png`),fullPage:true});
   checks.push("responsive "+width);
 }
 await page.setViewportSize({width:1440,height:1000});
 await page.getByRole("button",{name:"课程讲解",exact:true}).click();
 let lecture=section("课程讲解");
 await lecture.getByLabel("服务地址",{exact:true}).fill("https://new-lecture.example.test");
 await lecture.getByLabel("模型",{exact:true}).fill("lecture-custom");
 await lecture.getByLabel("API Key",{exact:true}).fill("synthetic-new-credential");
 await page.getByRole("button",{name:"即时对话",exact:true}).click();
 assert.equal(await section("即时对话").getByLabel("模型",{exact:true}).inputValue(),"dialogue-model");
 await page.getByRole("button",{name:"课程讲解",exact:true}).click();
 assert.equal(await lecture.getByLabel("模型",{exact:true}).inputValue(),"lecture-custom");
 await lecture.getByRole("button",{name:"保存",exact:true}).click();
 await lecture.getByText("已保存 · 新任务将使用此配置",{exact:true}).waitFor();
 assert.equal(saveBodies.at(-1).role,"teaching");
 assert.equal(saveBodies.at(-1).body.endpoint,"https://new-lecture.example.test");
 assert.equal(rows.find(row=>row.role==="dialogue").configuration.model_name,"dialogue-model");
 checks.push("complete independent save and draft preservation");
 await lecture.getByRole("button",{name:"显示密钥",exact:true}).click();
 await page.waitForFunction(()=>document.querySelector("#purpose-teaching-key")?.value==="synthetic-display-only");
 await page.getByRole("button",{name:"即时对话",exact:true}).click();
 await page.getByRole("button",{name:"课程讲解",exact:true}).click();
 assert(!(await lecture.getByLabel("API Key",{exact:true}).inputValue()).includes("synthetic-display-only"));
 assert.equal(await lecture.getByLabel("API Key",{exact:true}).getAttribute("type"),"password");
 assert.equal(revealCalls,1);
 checks.push("stored secret reveal and clear on switch");
 await lecture.getByRole("button",{name:"获取可用模型：模型",exact:true}).click();
 await lecture.getByRole("button",{name:"catalog-model",exact:true}).click();
 assert.equal(await lecture.getByLabel("模型",{exact:true}).inputValue(),"catalog-model");
 await lecture.getByRole("button",{name:"撤销更改",exact:true}).click();
 assert.equal(await lecture.getByLabel("模型",{exact:true}).inputValue(),"lecture-custom");
 checks.push("real catalog API selection and undo");
 await lecture.getByRole("button",{name:"测试课程讲解模型",exact:true}).click();
 await lecture.getByText("连接测试通过",{exact:true}).waitFor();
 assert.equal(testCalls,1);
 checks.push("inline test uses current configuration");
 failSave=true;
 await lecture.getByLabel("模型",{exact:true}).fill("failed-save-kept");
 await lecture.getByRole("button",{name:"保存",exact:true}).click();
 await lecture.getByRole("alert").waitFor();
 assert.equal(await lecture.getByLabel("模型",{exact:true}).inputValue(),"failed-save-kept");
 assert(!(await page.locator("body").innerText()).includes("synthetic-private-error"));
 checks.push("failed save retains draft and controlled error");
 failSave=false;
 await lecture.getByRole("button",{name:"保存",exact:true}).click();
 await lecture.getByText("已保存 · 新任务将使用此配置",{exact:true}).waitFor();
 await page.reload();
 await page.getByRole("button",{name:"课程讲解",exact:true}).click();
 assert.equal(await section("课程讲解").getByLabel("模型",{exact:true}).inputValue(),"failed-save-kept");
 checks.push("saved values hydrate from API after reload");
 assert.equal(await page.locator(".side").evaluate(e=>getComputedStyle(e).backgroundColor),"rgb(17, 17, 17)");
 checks.push("black application navigation");
 failRoles=true;await page.reload();
 await page.getByText("加载模型配置失败，请重试。",{exact:false}).waitFor();
 failRoles=false;await page.getByRole("button",{name:"重新加载",exact:true}).click();
 await section("课程规划").waitFor();
 checks.push("role loading failure and recovery");
 isAdmin=true;await page.reload();await section("课程规划").waitFor();
 assert.equal(await page.getByRole("navigation",{name:"设置分类"}).getByRole("button").count(),6);
 assert.equal(await section("课程规划").getByLabel("思考强度",{exact:true}).count(),1);
 await page.getByRole("button",{name:"个人设置菜单",exact:true}).click();
 assert.equal(await page.getByRole("button",{name:"思考设置",exact:true}).count(),0);
 assert.equal(await page.getByRole("button",{name:"卡片市集",exact:true}).count(),0);
 await page.getByRole("button",{name:"连接与授权",exact:true}).click();
 assert((await page.getByRole("navigation",{name:"面包屑"}).innerText()).includes("连接与授权"));
 await page.getByRole("navigation",{name:"面包屑"}).getByRole("button",{name:"设置",exact:true}).click();
 await page.getByRole("button",{name:"个人设置菜单",exact:true}).click();
 await page.getByRole("button",{name:"个人记忆",exact:true}).click();
 await page.getByRole("heading",{name:"个人记忆",exact:true}).waitFor();
 await page.getByRole("navigation",{name:"面包屑"}).getByRole("button",{name:"设置",exact:true}).click();
 await page.getByRole("button",{name:"人物蒸馏模型",exact:true}).click();
 await section("人物蒸馏").waitFor();
 const previousPlanning=rows.find(row=>row.role==="planning").configuration.model_name;
 await section("人物蒸馏").getByLabel("模型",{exact:true}).fill("research-independent-updated");
 await section("人物蒸馏").getByRole("button",{name:"保存",exact:true}).click();
 await section("人物蒸馏").getByText("已保存 · 新任务将使用此配置",{exact:true}).waitFor();
 assert.equal(saveBodies.at(-1).role,"research");
 assert.equal(rows.find(row=>row.role==="planning").configuration.model_name,previousPlanning);
 await page.reload();
 assert.equal(await section("人物蒸馏").getByLabel("模型",{exact:true}).inputValue(),"research-independent-updated");
 checks.push("research dedicated settings save refresh leaves planning unchanged");
 await page.getByRole("button",{name:"个人设置菜单",exact:true}).click();
 await page.getByRole("button",{name:"管理员 OCR",exact:true}).click();
 await page.getByRole("heading",{name:"管理员云 OCR",exact:true}).waitFor();
 await page.getByRole("navigation",{name:"面包屑"}).getByRole("button",{name:"个人工作空间",exact:true}).click();
 assert(page.url().endsWith("#/home"));
 checks.push("secondary configuration access preserves approved primary navigation");
 assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);assert.deepEqual(served.unexpected,[]);
 await writeFile(path.join(output,"checks.json"),JSON.stringify({checks,errors,unexpected,scope:"production build with intercepted synthetic APIs"},null,2));
 console.log(JSON.stringify({passed:checks.length,errors,unexpected}));
}catch(error){
 await page.screenshot({path:path.join(output,"failure.png"),fullPage:true});
 console.log(JSON.stringify({visibleErrors:await page.getByRole("alert").allTextContents(),saveCount:saveBodies.length,errors,unexpected}));
 throw error;
}finally{await context.close();await browser.close();}
