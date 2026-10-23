import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir} from "node:fs/promises";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";

const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright-core");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const output=".backup/character-voice-policy-browser";
await mkdir(output,{recursive:true});
const checks=[];
const maleA={voice_id:"male-a",name:"合成沉稳男声"},maleB={voice_id:"male-b",name:"合成清朗男声"};
const female={voice_id:"female-a",name:"合成女声"};
const sample=Buffer.alloc(44+16000*2);
sample.write("RIFF",0);sample.writeUInt32LE(sample.length-8,4);sample.write("WAVEfmt ",8);
sample.writeUInt32LE(16,16);sample.writeUInt16LE(1,20);sample.writeUInt16LE(1,22);
sample.writeUInt32LE(16000,24);sample.writeUInt32LE(32000,28);sample.writeUInt16LE(2,32);
sample.writeUInt16LE(16,34);sample.write("data",36);sample.writeUInt32LE(sample.length-44,40);
for(let i=0;i<16000;i++)sample.writeInt16LE(Math.round(Math.sin(i*440*Math.PI/8000)*2000),44+i*2);
try{
 for(const width of [1440,390]){
  const context=await browser.newContext({viewport:{width,height:1000},serviceWorkers:"block"});
  const page=await context.newPage(),errors=[],held=[];
  const served=await mockBuiltFrontend(page);
  page.on("pageerror",error=>errors.push(error.message));
  let saved={card_id:"mentor-a",revision:1,editable:true,assets:[],profile:{name:"合成导师",identity:"男性数学导师",
   personality:"温柔耐心",background:"擅长数学与计算机",appearance:"成年男性",tags:[],
   voice_id:"female-a",voice_config_id:"tts-a",media_ids:[],base_asset_id:""}};
  let preference={version:1,card_id:"mentor-a",revision:1},noCompatible=false,catalogUnavailable=false;
  let previewCalls=0,useCalls=0,saveCalls=0,audioCalls=0,choiceCalls=0,previewFailure=false;
  let mediaJobs=[],generationCalls=0;
  const json=(route,data,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(data)});
  await page.route("**/api/**",async route=>{
   const request=route.request(),p=new URL(request.url()).pathname,m=request.method();
   if(p==="/api/auth/me")return json(route,{user_id:"synthetic-owner",status:"approved",is_admin:false});
   if(p==="/api/market/profile")return json(route,{display_name:"合成用户"});
   if(p==="/api/market/imports/drain")return json(route,{cards:[]});
   if(p==="/api/mentor-preference"){
    if(m==="GET")return json(route,preference);
    const body=request.postDataJSON();
    assert.equal(body.expected_version,preference.version);
    preference={...preference,card_id:body.card_id,revision:body.revision,version:preference.version+1};
    useCalls++;return json(route,preference);
   }
   if(p==="/api/cards")return json(route,{data:[saved]});
   if(p==="/api/cards/mentor-a/voice-choice"){
    const body=request.postDataJSON();
    assert.equal(body.expected_version,saved.voice_choice_version||0);
    saved={...saved,voice_choice_version:(saved.voice_choice_version||0)+1,
     profile:{...saved.profile,voice_id:body.voice_id,voice_config_id:body.config_id,voice_selection:"manual"}};
    choiceCalls++;return json(route,saved);
   }
   if(p==="/api/character-voice-preview"){
    audioCalls++;assert.equal(request.postDataJSON().profile.voice_id,saved.profile.voice_id);
    if(previewFailure)return json(route,{error_code:"capability_test_failed",message:"试听暂时失败"},502);
    return route.fulfill({contentType:"audio/wav",body:sample});
   }
   if(p==="/api/cards/mentor-a"){
    if(m==="PATCH"){const body=request.postDataJSON();assert.equal(body.expected_revision,saved.revision);
     saved={...saved,revision:saved.revision+1,profile:body.profile};saveCalls++;}
    return json(route,saved);
   }
   if(p.endsWith("/media-jobs")){
    if(m==="POST"){
     const body=request.postDataJSON();
     assert.equal(body.revision,saved.revision);assert.equal(body.kind,"image");
     generationCalls++;mediaJobs=[{task_id:"synthetic-image-job",kind:"image",status:"queued",assets:[]}];
     return json(route,{task_id:"synthetic-image-job",status:"queued"},202);
    }
    return json(route,{data:mediaJobs});
   }
   if(p==="/api/capabilities/configs")return json(route,{data:[{config_id:"tts-a",config_version:1,
    kind:"tts",protocol:"minimax",is_active:true,model_name:"speech-2.8-hd"},
    {config_id:"image-a",config_version:1,kind:"image_generation",protocol:"openai_image",
     is_active:true,model_name:"synthetic-image"}]});
   if(p==="/api/character-expression"){
    previewCalls++;const body=request.postDataJSON();
    assert.equal(body.config_id,"tts-a");
    const lively=body.profile.personality.includes("活泼"),isFemale=body.profile.identity.includes("女性");
    const result={data:[maleA,maleB,female],compatible_voice_ids:noCompatible?[]:isFemale?["female-a"]:["male-a","male-b"],
     recommended_voice_id:noCompatible?null:isFemale?"female-a":lively?"male-b":"male-a",
     voice_gender:isFemale?"female":"male",match_status:noCompatible?"no_compatible_voice":"matched",
     speaking_style:lively?"语气轻快，自动使用语气词和类比。":"表达沉稳，自动耐心拆解。"};
    if(catalogUnavailable){result.data=[];result.recommended_voice_id=null;result.match_status="catalog_unavailable";}
    if(body.profile.personality==="迟到请求"){
     await new Promise(resolve=>held.push(resolve));
     result.speaking_style="这是不该覆盖的迟到风格";
    }
    return json(route,result).catch(()=>{});
   }
   throw new Error(`Unexpected synthetic API ${m} ${p}`);
  });
  await page.goto(served.origin+"/#/card/mentor-a");
  const select=page.getByLabel("音色",{exact:true}),style=page.getByLabel("说话风格",{exact:true});
  await page.waitForFunction(()=>document.querySelector('select[aria-label="音色"]')?.value==="male-a");
  assert(!(await page.locator(".character-section").filter({has:select}).innerText()).includes("推荐："));
  assert.equal(await select.locator('option[value="female-a"]').count(),1);
  assert((await select.locator('option[value="male-a"]').innerText()).includes("推荐"));
  assert.equal(await page.locator('textarea[aria-label="说话风格"]').count(),0);
  assert((await style.innerText()).includes("自动耐心拆解"));
  assert.equal(saveCalls,0,"opening and matching must not save or synthesize");
  checks.push(`automatic-male-recommendation-and-readonly-style-${width}`);

  await select.selectOption("male-b");
  await page.waitForFunction(()=>!document.querySelector('select[aria-label="音色"]').disabled);
  await page.getByLabel("性格",{exact:true}).fill("严谨沉稳");
  await page.waitForTimeout(700);
  assert.equal(await select.inputValue(),"male-b","compatible manual choice survives rematching");
  await page.getByRole("button",{name:"保存人物",exact:true}).click();
  await page.waitForFunction(()=>document.querySelector(".character-header")?.textContent.includes("版本 2"));
  assert.equal(saved.profile.voice_id,"male-b");
  assert.equal(useCalls,2);assert.equal(choiceCalls,1);assert.equal(preference.revision,2);
  checks.push(`manual-compatible-selection-save-and-active-revision-${width}`);

  await page.locator("select").filter({has:page.locator('option[value="image-a"]')}).selectOption("image-a");
  const generation=page.getByRole("button",{name:"生成图片",exact:true});
  const priorGenerationSaves=saveCalls,priorGenerationRevision=saved.revision;
  await generation.click();
  await page.getByText("排队中…",{exact:true}).waitFor();
  assert.equal(generationCalls,1);assert.equal(saveCalls,priorGenerationSaves);
  assert.equal(saved.revision,priorGenerationRevision);assert(await generation.isDisabled());
  mediaJobs=[];
  await page.waitForFunction(()=>!Array.from(document.querySelectorAll("button")).find(b=>b.textContent.includes("生成图片"))?.disabled);
  checks.push(`image-generation-preserves-revision-and-blocks-duplicate-${width}`);

  await page.getByLabel("性格",{exact:true}).fill("迟到请求");
  await page.waitForFunction(()=>document.body.textContent.includes("正在加载声音目录"));
  for(let attempt=0;attempt<40&&held.length===0;attempt++)await page.waitForTimeout(30);
  assert(held.length,"the delayed request must have started");
  await page.getByLabel("性格",{exact:true}).fill("活泼亲切");
  await page.waitForFunction(()=>document.querySelector('[aria-label="说话风格"]')?.textContent.includes("自动使用语气词"));
  for(const release of held.splice(0))release();
  await page.waitForTimeout(300);
  assert((await style.innerText()).includes("自动使用语气词"));
  assert.equal(await select.inputValue(),"male-b");
  checks.push(`persona-edit-regenerates-style-and-ignores-late-result-${width}`);

  catalogUnavailable=true;
  await page.getByLabel("身份",{exact:true}).fill("男性科学导师");
  const catalogError=page.getByRole("alert").filter({hasText:"声音目录暂时不可用，已保留当前音色，请重试。"});
  await catalogError.waitFor();
  assert.equal(await select.inputValue(),"male-b");
  assert((await style.innerText()).includes("自动使用语气词"));
  catalogUnavailable=false;
  await page.getByRole("button",{name:"重试声音目录",exact:true}).click();
  await catalogError.waitFor({state:"hidden"});
  checks.push(`catalog-failure-preserves-choice-and-style-and-retries-${width}`);

  noCompatible=true;
  saved={...saved,profile:{...saved.profile,voice_id:"",voice_selection:"auto"}};
  await page.reload();
  await page.getByText("未找到符合人物设定的推荐音色，可手动选择。",{exact:true}).waitFor();
  assert.equal(await select.inputValue(),"");
  assert.equal(await select.locator("option").count(),4);
  assert(await page.getByRole("button",{name:"试听",exact:true}).isDisabled());
  checks.push(`no-male-match-never-falls-back-to-female-${width}`);

  noCompatible=false;
  await page.getByLabel("身份",{exact:true}).fill("女性工程导师");
  await page.waitForFunction(()=>document.querySelector('select[aria-label="音色"]')?.value==="female-a");
  assert.equal(await select.locator('option[value="male-b"]').count(),1);
  checks.push(`explicit-gender-change-rematches-${width}`);
  await page.screenshot({path:`${output}/card-${width}.png`,fullPage:true});
  saved={...saved,editable:false,profile:{...saved.profile,identity:"男性导师",voice_id:"",voice_config_id:"",voice_selection:"auto"}};
  await page.reload();
  await page.waitForFunction(()=>document.querySelector('select[aria-label="音色"]')?.value==="male-a");
  assert(!(await select.isDisabled()));
  assert((await style.innerText()).includes("自动耐心拆解"));
  checks.push(`collected-readonly-card-still-recommends-and-derives-style-${width}`);
  const priorRevision=saved.revision,priorSaves=saveCalls;
  await select.selectOption("male-b");
  await page.waitForFunction(()=>!document.querySelector('select[aria-label="音色"]').disabled);
  assert.equal(saved.revision,priorRevision);assert.equal(saveCalls,priorSaves);
  await page.getByRole("button",{name:"试听",exact:true}).click();
  const audio=page.getByLabel("当前音色试听",{exact:true});await audio.waitFor();
  await audio.evaluate(n=>n.play());
  await page.waitForFunction(()=>document.querySelector('audio[aria-label="当前音色试听"]').currentTime>.1);
  await select.scrollIntoViewIfNeeded();
  await page.screenshot({path:`${output}/voice-preview-${width}.png`});
  assert.equal(audioCalls,1);assert.equal(saveCalls,priorSaves);
  await page.reload();
  await page.waitForFunction(()=>document.querySelector('select[aria-label="音色"]')?.value==="male-b");
  previewFailure=true;
  await page.getByRole("button",{name:"试听",exact:true}).click();
  await page.getByRole("alert").filter({hasText:"试听暂时失败"}).waitFor();
  assert.equal(await page.getByLabel("当前音色试听").count(),0);
  checks.push(`collected-card-personal-choice-preview-playback-and-controlled-error-${width}`);
  assert.deepEqual(errors,[]);
  assert.deepEqual(served.unexpected,[]);
  assert(previewCalls>=6);
  await context.close();
 }
 console.log(JSON.stringify({passed:checks.length,checks,synthetic:true}));
}finally{await browser.close();}
