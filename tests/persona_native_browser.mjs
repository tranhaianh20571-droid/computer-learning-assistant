// Browser -> production assembler -> isolated PostgreSQL -> genuine SDK FunctionModels.
import assert from "node:assert/strict";
import {createRequire} from "node:module";
import path from "node:path";
const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright-core");
const backend=process.env.TEST_BACKEND,base=process.env.TEST_FRONTEND;
assert.equal(new URL(backend).hostname,"127.0.0.1");
assert.notEqual(new URL(backend).port,"8000");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const context=await browser.newContext({viewport:{width:1440,height:1000}});
await context.addInitScript(({backend})=>{
  const original=window.fetch.bind(window);
  window.fetch=(input,init)=>original(typeof input==="string"&&input.startsWith("/api/")?backend+input:input,init);
},{backend});
const page=await context.newPage(),errors=[],checks=[];
page.setDefaultTimeout(30000);
page.on("pageerror",error=>errors.push(error.message));
await page.route("**/*",route=>["127.0.0.1","localhost"].includes(new URL(route.request().url()).hostname)?
  route.continue():route.abort());
async function request(endpoint,method="GET",data){
  const response=await context.request.fetch(backend+endpoint,{method,data});
  assert(response.ok(),`${method} ${endpoint}: ${response.status()}`);
  return response.json();
}
async function createForm(mode,topic){
  await page.goto(base+"/#/cards");
  await page.getByRole("button",{name:"新人物",exact:true}).click();
  await page.getByRole("button",{name:mode,exact:true}).click();
  await page.getByLabel(mode==="AI 创作"?"人物／主题":"人物或材料名称",{exact:true}).fill(topic);
}
async function accept(){
  await page.getByRole("button",{name:"保存为人物卡",exact:true}).waitFor({timeout:180000});
  await page.getByRole("button",{name:"保存为人物卡",exact:true}).click();
  await page.getByLabel("完整人物 Skill",{exact:true}).waitFor();
  assert((await page.getByLabel("完整人物 Skill",{exact:true}).inputValue()).includes("心智模型"));
}
try{
  await page.goto(base+"/#/login");
  await page.getByLabel("邮箱",{exact:true}).fill("synthetic-browser-admin@example.com");
  await page.getByLabel("密码",{exact:true}).fill("synthetic-password-12345");
  await page.getByRole("button",{name:"登录",exact:true}).click();
  await page.waitForURL("**/#/home");
  const configs={};
  for(const role of ["planning","teaching","research"]){
    configs[role]=await request("/api/capabilities/configs","POST",{kind:"content_model",protocol:"openai",
      endpoint:`https://${role}.synthetic.test/v1`,model_name:"synthetic-"+role,
      credentials:{api_key:"synthetic-browser-model-key"}});
  }
  await request("/api/capabilities/configs","POST",{kind:"search",protocol:"anysearch",
    endpoint:"https://api.anysearch.com",model_name:"",credentials:{api_key:"synthetic-browser-search-key"}});
  await page.goto(base+"/#/capabilities");
  for(const [role,label] of [["planning","课程规划"],["teaching","课程讲解"],["research","人物蒸馏"]]){
    if(role==="research")await page.goto(base+"/#/capabilities?section=research");
    else await page.getByRole("button",{name:label,exact:true}).click();
    const settings=page.getByRole("region",{name:label+"模型配置",exact:true});
    await settings.getByLabel("服务地址",{exact:true}).fill(configs[role].endpoint);
    await settings.getByLabel("模型",{exact:true}).fill(configs[role].model_name);
    await settings.getByLabel("API Key",{exact:true}).fill("synthetic-browser-model-key");
    await settings.getByRole("button",{name:"保存",exact:true}).click();
    await settings.getByText("已保存 · 新任务将使用此配置",{exact:true}).waitFor();
  }
  await page.goto(base+"/#/capabilities?section=research");
  await page.reload();
  assert.equal(await page.getByRole("region",{name:"人物蒸馏模型配置",exact:true}).getByLabel("模型",{exact:true}).inputValue(),configs.research.model_name);
  checks.push("real-api-independent-model-settings-persist");
  await createForm("AI 创作","Synthetic researched tutor");
  await page.getByText("目标时长 10 分钟",{exact:true}).waitFor();
  await page.getByRole("checkbox").check();
  await page.getByRole("button",{name:"开始蒸馏",exact:true}).click();
  await accept();
  assert.equal(await page.getByText("研究原稿",{exact:true}).count(),1);
  const initial=await request("/api/character-builds");
  const ai=initial.data.find(item=>item.request.mode==="ai");
  assert.equal(ai.status,"succeeded");
  assert.equal(ai.evidence.effective_documents,0);
  assert.equal(ai.stages.filter(item=>/^research_(writings|conversations|expression|external|decisions|timeline)(?:_bounded\d+)?$/.test(item.stage)).length,6);
  assert(ai.stages.some(item=>item.stage==="fast_verify"));
  assert(!ai.stages.some(item=>item.stage==="refine_structure"));
  assert(!ai.stages.some(item=>item.stage==="refine_role"));
  assert.equal(ai.result.persona.quality.passed,false);
  assert(await page.getByRole("button",{name:"发布到市集",exact:true}).isDisabled());
  const rejected=await context.request.post(backend+`/api/cards/${ai.card_id}/publish?revision=2`);
  assert.equal(rejected.status(),409);
  checks.push("real-ai-search-three-model-stages-six-reports-verification-accept");
  await page.getByRole("button",{name:"添加能力 Skill",exact:true}).click();
  const mounted=page.locator(".persona-mounted");
  await mounted.getByLabel("名称",{exact:true}).fill("Synthetic causal comparison");
  await mounted.getByLabel("能力指令",{exact:true}).fill("Compare two synthetic examples before drawing a conclusion.");
  await page.getByRole("button",{name:"保存人物 Skill",exact:true}).click();
  await page.getByText("完整人物 Skill 已保存",{exact:true}).waitFor();
  await page.reload();
  assert.equal(await page.locator(".persona-mounted").getByLabel("名称",{exact:true}).inputValue(),"Synthetic causal comparison");
  await page.screenshot({path:path.join(process.env.TEST_OUTPUT,"persona-native-desktop.png"),fullPage:true});
  checks.push("real-persona-capability-revision-refresh");
  await page.setViewportSize({width:390,height:844});
  await createForm("文字蒸馏","Synthetic text corpus");
  await page.locator('input[type=file]').setInputFiles({name:"synthetic.txt",mimeType:"text/plain",
    buffer:Buffer.from("Synthetic A and Synthetic B. Synthetic evidence of checking conditions.")});
  await page.getByRole("checkbox").check();
  await page.getByRole("button",{name:"开始蒸馏",exact:true}).click();
  await page.getByLabel("选择人物",{exact:true}).selectOption("Synthetic A",{timeout:180000});
  await page.getByRole("button",{name:"继续蒸馏所选人物",exact:true}).click();
  await accept();
  const builds=await request("/api/character-builds");
  assert(builds.data.some(item=>item.request.selected_character==="Synthetic A"&&item.status==="succeeded"));
  assert.equal(await page.locator(".persona-editor details").filter({hasText:"01-writings.md"}).count(),1);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await page.screenshot({path:path.join(process.env.TEST_OUTPUT,"persona-native-mobile.png"),fullPage:true});
  checks.push("real-txt-upload-candidate-selection-six-agents-mobile");
  for(const kind of ["fictional","original"]){
    const topic="Synthetic "+kind+" tutor";
    await createForm("AI 创作",topic);
    await page.getByLabel("人物类型",{exact:true}).selectOption(kind);
    if(kind==="fictional"){
      await page.getByLabel("所属作品",{exact:true}).fill("Synthetic novel");
      await page.getByLabel("作品版本",{exact:true}).fill("Book edition");
      await page.getByLabel("剧情截至点",{exact:true}).fill("Chapter 3");
    }else{
      assert.equal(await page.getByLabel("研究搜索",{exact:true}).count(),0);
    }
    await page.getByRole("checkbox",{name:/允许向/}).check();
    await page.getByRole("button",{name:"开始蒸馏",exact:true}).click();
    await accept();
    const result=(await request("/api/character-builds")).data.find(item=>item.request.topic===topic);
    assert.equal(result.request.subject.kind,kind);
    assert.equal(result.request.pipeline_version,7);
    assert.equal(result.result.persona.references[0].name,"01-dialogue.md");
    if(kind==="original"){
      assert.equal(result.evidence.search_calls,0);
      assert.equal(result.result.persona.quality.grade,"authored");
      assert.equal(result.result.persona.quality.passed,true);
      const published=await context.request.post(backend+`/api/cards/${result.card_id}/publish?revision=2`);
      assert.equal(published.status(),200);
    }else{
      assert.equal(result.request.subject.version,"Book edition");
      assert.equal(result.request.subject.story_point,"Chapter 3");
      assert.equal(result.result.persona.quality.passed,false);
    }
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    checks.push(`real-${kind}-typed-scope-and-publication`);
  }
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({checks,synthetic:true,realBackend:true,realProviderTested:false}));
}finally{await context.close();await browser.close();}
