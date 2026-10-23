// Production build with synthetic APIs: ordinary members, no provider requests.
import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir} from "node:fs/promises";
import path from "node:path";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PW_ROOT+"/playwright-core");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const output=process.env.TEST_OUTPUT||".backup/personal-memory-browser";
await mkdir(output,{recursive:true});
const context=await browser.newContext({viewport:{width:1440,height:1000}});
const page=await context.newPage();
page.setDefaultTimeout(10000);
const errors=[],bodies=[],catalogs=[],probes=[],discoveries=[];
page.on("pageerror",e=>errors.push(e.message));
const served=await mockBuiltFrontend(page);
let failSave=false,failCatalog=false,failDiscovery=true;
let config={
  revision:1,status:"active",dimensions:1024,rerank_protocol:"jev",
  content:{endpoint:"https://content.example.test/v1",model:"synthetic-content",credential_mask:"********"},
  embedding:{endpoint:"https://embedding.example.test/v1",model:"synthetic-embedding",credential_mask:"********"},
  rerank:{endpoint:"https://rerank.example.test",model:"synthetic-jev",credential_mask:"********"},
};
const json=(route,body,status=200)=>route.fulfill({status,json:body});
await page.route("**/api/**",async route=>{
  const request=route.request(),url=new URL(request.url()),p=url.pathname,method=request.method();
  if(p==="/api/auth/me")return json(route,{user_id:"synthetic-member",is_admin:false,status:"approved"});
  if(p==="/api/navigation")return json(route,{});
  if(["/api/capabilities/configs","/api/connectors"].includes(p))return json(route,{data:[]});
  if(p==="/api/capabilities/model-roles")return json(route,{data:["planning","teaching","dialogue"].map(role=>({
    role,revision:0,config_id:null,configuration:null,
  }))});
  if(p==="/api/memory/models"&&method==="GET")return json(route,config);
  if(p==="/api/memory/models/reasoning"){
    const body=request.postDataJSON();discoveries.push(body);
    assert.equal(body.endpoint,config.content.endpoint);
    assert.equal(body.model,config.content.model);
    assert.deepEqual(Object.keys(body).sort(),[
      ...(body.api_key?["api_key"]:[]),"endpoint","model",...(body.refresh?["refresh"]:[]),
    ]);
    if(body.api_key)assert.equal(body.api_key,"synthetic-content-key");
    if(failDiscovery)return json(route,{detail:{error_code:"capability_test_failed"}},502);
    return json(route,{state:"available",parameter_validation:"unconfirmed",options:[
      {value:"effort:low",settings:{mode:"effort",effort:"low",show_summary:false},evidence:"request_accepted"},
    ]});
  }
  if(p==="/api/memory/models"&&method==="PUT"){
    const body=request.postDataJSON();bodies.push(body);
    if(failSave)return json(route,{detail:{error_code:"state_invalid",message:"合成版本冲突，请重新加载。"}},409);
    config={...body,revision:body.expected_revision+1,status:"active"};
    for(const kind of ["content","embedding","rerank"])if(body[kind]){
      config[kind]={endpoint:body[kind].endpoint,model:body[kind].model,credential_mask:"********",
        ...(body[kind].reasoning_settings?{reasoning_settings:body[kind].reasoning_settings}:{})};
    }
    return json(route,config);
  }
  if(p==="/api/memory/models/catalog"){
    catalogs.push(request.postDataJSON());
    return failCatalog?json(route,{detail:{error_code:"capability_test_failed"}},502)
      :json(route,{models:["synthetic-catalog-choice"]});
  }
  if(p==="/api/memory/models/test"){probes.push(request.postDataJSON());return json(route,{state:"available"});}
  throw Error("Unexpected API "+method+" "+p);
});
try{
  await page.goto(served.origin+"/#/capabilities?section=memory");
  const region=page.getByRole("region",{name:"个人记忆模型配置",exact:true});
  await region.waitFor();
  const content=page.getByRole("region",{name:"记忆内容模型配置",exact:true});
  const reasoning=content.getByLabel("思考强度",{exact:true});
  await page.waitForFunction(()=>document.querySelector(".memory-provider .reasoning-select")?.dataset.discoveryState==="failed");
  failDiscovery=false;
  await reasoning.selectOption("__retry__");
  await reasoning.locator('option[value="effort:low"]').waitFor({state:"attached"});
  assert.equal(discoveries.at(-1).refresh,true);
  await reasoning.selectOption("effort:low");
  const masked=await region.locator("input.memory-saved-key").evaluateAll(nodes=>nodes.map(node=>({
    value:node.value,placeholder:node.placeholder,
    color:getComputedStyle(node,"::placeholder").color,ink:getComputedStyle(node).color,
  })));
  assert.equal(masked.length,3);
  assert(masked.every(row=>row.value===""&&row.placeholder==="••••••••"&&row.color===row.ink));
  assert.equal(await page.getByText("记忆引擎由管理员配置",{exact:false}).count(),0);
  const rerank=page.getByRole("region",{name:"重排模型配置",exact:true});
  await rerank.getByLabel("普通重排",{exact:true}).check();
  assert.equal(await rerank.getByLabel("服务地址").inputValue(),"");
  await rerank.getByLabel("服务地址").fill("https://rerank.example.test/v1");
  await rerank.getByLabel("API Key",{exact:true}).fill("synthetic-new-rerank-key");
  await rerank.getByRole("button",{name:"获取可用模型：模型"}).click();
  await rerank.getByRole("button",{name:"synthetic-catalog-choice",exact:true}).click();
  assert.equal(catalogs.at(-1).protocol,"rerank");
  await page.getByRole("navigation",{name:"设置分类"}).getByRole("button",{name:"内容模型",exact:true}).click();
  await page.getByRole("navigation",{name:"设置分类"}).getByRole("button",{name:"记忆模型",exact:true}).click();
  assert.equal(await rerank.getByLabel("模型",{exact:true}).inputValue(),"synthetic-catalog-choice");
  failSave=true;
  await region.getByRole("button",{name:"保存",exact:true}).click();
  await page.getByRole("alert").filter({hasText:"合成版本冲突"}).waitFor();
  assert.equal(await rerank.getByLabel("API Key",{exact:true}).inputValue(),"synthetic-new-rerank-key");
  failSave=false;
  await region.getByRole("button",{name:"保存",exact:true}).click();
  await page.getByRole("status").filter({hasText:"已保存 · 仅用于你的资料与记忆"}).waitFor();
  assert.equal(bodies.at(-1).expected_revision,1);
  assert.equal(bodies.at(-1).content.api_key,undefined);
  assert.equal(bodies.at(-1).content.reasoning_settings.effort,"low");
  assert.equal(bodies.at(-1).rerank.credential_mask,undefined);
  assert.equal(await rerank.getByLabel("API Key",{exact:true}).inputValue(),"");
  await rerank.getByRole("button",{name:"测试重排模型",exact:true}).click();
  await page.getByRole("status").filter({hasText:"连接测试通过"}).first().waitFor();
  assert.deepEqual(probes.at(-1),{kind:"rerank",revision:2});
  const contentKey=content.getByLabel("API Key",{exact:true});
  await contentKey.fill("synthetic-content-key");
  await page.waitForFunction(()=>document.querySelector(".memory-provider .reasoning-select")?.dataset.discoveryState==="available");
  assert.equal(discoveries.at(-1).api_key,"synthetic-content-key");
  await content.getByRole("button",{name:"显示输入的密钥",exact:true}).click();
  assert.equal(await contentKey.getAttribute("type"),"text");
  await content.getByRole("button",{name:"隐藏密钥",exact:true}).click();
  assert.equal(await contentKey.getAttribute("type"),"password");
  await region.getByRole("button",{name:"撤销更改",exact:true}).click();
  assert.equal(await contentKey.inputValue(),"");
  assert.equal(await contentKey.getAttribute("placeholder"),"••••••••");
  await reasoning.locator('option[value="effort:low"]').waitFor({state:"attached"});
  failCatalog=true;
  await rerank.getByRole("button",{name:"获取可用模型：模型"}).click();
  await rerank.getByText("获取失败，请检查地址和 API Key；也可手动填写。").waitFor();
  await page.keyboard.press("Escape");
  for(const width of [1440,768,390,320]){
    await page.setViewportSize({width,height:1000});
    await page.locator(".settings-scroll").evaluate(el=>{el.scrollTop=0;});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    const controls=await region.locator('input:not([type="radio"]),button[type="submit"]').evaluateAll(nodes=>
      nodes.map(el=>{const r=el.getBoundingClientRect();return {left:r.left,right:r.right,width:r.width};}));
    assert(controls.every(r=>r.left>=0&&r.right<=width+1&&r.width>0));
    await page.screenshot({path:path.join(output,`memory-models-${width}.png`),fullPage:true});
  }
  assert.deepEqual(errors,[]);
  assert.deepEqual(served.unexpected,[]);
  console.log("PASS ordinary member, reasoning payload/retry/selection/save, typed-key discovery/reveal/undo, masked credentials, catalog success/failure, save conflict/retry, protocol selection, test action, tab draft persistence, four widths.");
}finally{await context.close();await browser.close();}
