import assert from "node:assert/strict";
import {createRequire} from "node:module";
import {mkdir} from "node:fs/promises";
import {createServer} from "node:http";
import {mockBuiltFrontend} from "./frontend_dist_mock.mjs";
const {chromium}=createRequire(import.meta.url)(process.env.PW_ROOT+"/playwright-core");
const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER});
const output=".backup/persona-workflows-browser";await mkdir(output,{recursive:true});
const checks=[];
const profile={name:"合成导师",identity:"检查条件再推导",personality:"清晰直接",background:"合成材料",
  appearance:"",tags:[],voice_id:"",voice_config_id:"",media_ids:[],base_asset_id:"",speaking_style:"先说明条件"};
let streamBuilds=[],streamConnections=new Map(),streamRequests=0;
const frame=(response,event)=>response.write(`data: ${JSON.stringify(event)}\n\n`);
const streamServer=createServer((request,response)=>{
  const id=request.url.split("/").at(-2),build=streamBuilds.find(row=>row.build_id===id);
  response.writeHead(build?200:404,{"Content-Type":"text/event-stream","Cache-Control":"no-store"});
  if(!build)return response.end();
  streamRequests++;
  const clients=streamConnections.get(id)||new Set();streamConnections.set(id,clients);clients.add(response);
  response.on("close",()=>clients.delete(response));
  frame(response,{event:"preview",build_id:id,reply:build.preview||"",question:""});
});
await new Promise(resolve=>streamServer.listen(0,"127.0.0.1",resolve));
const streamOrigin=`http://127.0.0.1:${streamServer.address().port}`;
function publish(build,event){
  if(event.event==="preview")build.preview=event.reply;
  for(const client of streamConnections.get(build.build_id)||[])frame(client,{build_id:build.build_id,...event});
}
try{
  for(const width of [1440,390,320]){
    const context=await browser.newContext({viewport:{width,height:1000},serviceWorkers:"block"});
    const page=await context.newPage(),errors=[];
    page.on("pageerror",error=>errors.push(error.message));page.setDefaultTimeout(12000);
    const served=await mockBuiltFrontend(page,undefined,streamOrigin);
    const configs=["planning","teaching","dialogue","research","research_search"].map((role,index)=>({
      config_id:"cfg-"+role,kind:"content_model",protocol:"openai",owner_scope:"user",
      endpoint:`https://${role}.synthetic.test/v1`,model_name:"synthetic-"+role,credential_mask:"****",
      config_version:1,is_active:true,capability_status:{},reasoning_settings:{mode:"default",show_summary:false}}));
    let roles=configs.map((configuration,index)=>({role:["planning","teaching","dialogue","research","research_search"][index],
      config_id:configuration.config_id,revision:1,configuration}));
    let card={card_id:"synthetic-persona",revision:1,editable:true,collected:true,published_revision:null,profile,assets:[],persona:null};
    let preference={version:0,card_id:null,revision:null};
    const builds=[];let dialogueRequests=0,materialUploads=0,holdNext=false,cancelRequests=0,cancelFails=true;
    streamBuilds=builds;streamConnections=new Map();streamRequests=0;
    const json=(route,value,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(value)});
    await page.route("**/api/**",async route=>{
      const request=route.request(),url=new URL(request.url()),path=url.pathname,method=request.method();
      if(path==="/api/auth/me")return json(route,{user_id:"synthetic-owner",status:"approved",is_admin:false});
      if(path==="/api/capabilities/configs")return json(route,{data:configs});
      if(path==="/api/capabilities/model-roles"&&method==="GET")return json(route,{data:roles});
      if(path==="/api/capabilities/reasoning/describe")return json(route,{schema_version:1,rules_version:"synthetic",source:"unknown",supported_modes:["default"],effort_values:[],budget:null,summary_supported:false,control_kind:"unknown"});
      if(path.startsWith("/api/capabilities/model-roles/")){
        const role=path.split("/").at(-2),body=request.postDataJSON();
        roles=roles.map(row=>row.role===role?{...row,config_id:body.config_id,
          configuration:{...row.configuration,endpoint:body.endpoint,model_name:body.model_name,reasoning_settings:body.reasoning_settings},revision:row.revision+1}:row);
        return json(route,{data:roles});
      }
      if(/\/configs\/[^/]+\/test$/.test(path))return json(route,{capability_status:{text:{state:"available"}}});
      if(path==="/api/connectors"||path==="/api/navigation"||path==="/api/materials")return json(route,{data:[]});
      if(path==="/api/mentor-preference"){
        if(method==="PUT"){
          const body=request.postDataJSON();
          assert.equal(body.card_id,card.card_id);
          preference={version:preference.version+1,card_id:card.card_id,revision:card.revision,using_system_default:false};
        }
        return json(route,preference);
      }
      if(path==="/api/cards"&&method==="POST"){card={...card,profile:request.postDataJSON()};return json(route,card,201);}
      if(path==="/api/cards"||path==="/api/card-market")return json(route,{data:[card]});
      if(path==="/api/cards/synthetic-persona/persona"){
        card={...card,persona:request.postDataJSON().persona,revision:card.revision+1};return json(route,card);
      }
      if(path==="/api/cards/synthetic-persona")return json(route,card);
      if(path.endsWith("/media-jobs"))return json(route,{data:[]});
      if(path==="/api/character-builds"&&method==="GET")return json(route,{data:builds});
      if(path.endsWith("/cancel")&&method==="POST"){
        cancelRequests++;
        if(cancelFails){cancelFails=false;return json(route,{detail:{message:"合成停止失败，请重试。"}},503);}
        const build=builds.find(item=>path.includes(item.build_id));
        build.status="cancelled";return json(route,build);
      }
      if(path.startsWith("/api/character-builds/")&&method==="GET"){
        const build=builds.find(item=>path.endsWith(item.build_id));
        return build?json(route,build):json(route,{error_code:"not_found"},404);
      }
      if(path==="/api/character-builds/material"&&method==="POST"){
        materialUploads++;
        return json(route,{text:"合成人物语料：先检查前提条件，再给出一个小提示。",characters:24});
      }
      if(path==="/api/character-builds"&&method==="POST"){
        const body=request.postDataJSON();
        if(body.dialogue){
          dialogueRequests++;assert.equal(body.mode,"ai");assert.equal(body.confirmed,true);
          assert.equal(body.complete_card,true);
          const parent=builds.find(item=>item.build_id===body.parent_build_id);
          const pending=parent?.result?.dialogue?.awaiting_confirmation;
          const complete=pending&&body.message==="确认创作此人物。";
          const ready=body.message.includes("直接生成全部")||pending;
          const introduction="合成导师是与你共同学习的原作学妹，沿用原作外观，性格温柔，表达清楚，遇到不确定的事实会说明边界。";
          const dialogue={reply:ready?introduction:"已将你的补充沉淀进人物思维。",
            question:ready&&!complete?"是否确认创作此人物？确认后，我会开始人物蒸馏、声音和图片生成。":
              body.source_text?"最希望保留哪种说话习惯？":"",
            question_count:body.source_text?1:0,topic:"合成导师",subject:{kind:"real"},
            profile,skill:"# 合成导师\n先检查条件，再解释推理边界。\n".repeat(12),
            ready:Boolean(ready),awaiting_confirmation:Boolean(ready&&!complete),creation_confirmed:Boolean(complete),
            thinking_mechanism:"先检查条件。",decision_method:"先问理由，再给一步提示。",
            expression_style:"简短平实。",knowledge_boundary:"未核验的信息保持未知。",
            ...(body.message.includes("搜索")?{search_note:"本轮完成 2 次搜索"}:{})};
          const report={skill:dialogue.skill,source_kind:"ai",references:[{name:"01-writings.md",body:"合成来源与机制证据"}],
            quality:{passed:true,schema_version:2,grade:"limited",warnings:[]},skills:[],provenance:{}};
          const history=parent?[...(parent.request.dialogue_history||[]),
            {role:"user",content:parent.request.message},
            {role:"assistant",content:[parent.result.dialogue?.reply,parent.result.dialogue?.question].filter(Boolean).join("\n\n")}]:[];
          const build={build_id:"build-"+builds.length,card_id:card.card_id,revision:1,request:{...body,dialogue_history:history},
            status:holdNext?"running":"succeeded",source_count:0,accepted_revision:complete?card.revision+1:null,
            result:holdNext?{}:{dialogue,...(complete?{profile,persona:report,
              completion:{status:"complete",completed_images:5,total_images:5,voice_ready:true}}:{})}};
          if(complete)card={...card,persona:report,revision:card.revision+1,
            profile:{...profile,identity:"原作女性学妹",voice_id:"Chinese_female",
              voice_config_id:"synthetic-voice",media_ids:["voice","neutral","explaining","thinking","encouraging","celebrating"],
              base_asset_id:"neutral"}};
          holdNext=false;
          builds.push(build);return json(route,build,202);
        }
        if(body.mode==="ai"){aiRequests++;assert.equal(body.source_text,"");}
        else{textRequests++;assert(body.source_text.includes("合成人物语料"));}
        const report={skill:"# 合成导师\n先检查条件，再逐步说明机制，遇到未知明确保留边界。\n".repeat(12),
          source_kind:body.mode,references:[{name:"01-writings.md",body:"合成来源与机制证据"}],
          quality:{passed:true,schema_version:2,grade:body.subject?.kind==="original"?"authored":
            body.subject?.kind==="fictional"?"grounded":"limited",
            warnings:body.subject?.kind==="real"?["合成资料范围有限，未覆盖的观点保持未知。"]:[]},skills:[],provenance:{}};
        const build={build_id:"build-"+builds.length,card_id:card.card_id,revision:1,request:body,
          status:"succeeded",source_count:body.mode==="ai"?10:0,
          evidence:{candidates:12,effective_documents:4,verified_primary:2,search_calls:3,read_attempts:20,provider_actions:17},
          stages:["writings","conversations","expression","external","decisions","timeline"].map(name=>({
            stage:"research_"+name,status:"succeeded",output:{body:"合成研究原稿 "+name}})),
          result:{profile,persona:report}};
        builds.push(build);return json(route,build,202);
      }
      if(path.endsWith("/accept")){
        const build=builds.find(item=>path.includes(item.build_id));card={...card,persona:build.result.persona,
          revision:card.revision+1};return json(route,card);
      }
      return json(route,{data:[]});
    });
    await page.route("**/api/character-builds/*/content-events",route=>route.continue());
    await page.goto(served.origin+"/#/capabilities");
    await page.getByRole("button",{name:"课程讲解",exact:true}).click();
    const teaching=page.getByRole("region",{name:"课程讲解模型配置",exact:true});
    await teaching.getByLabel("模型",{exact:true}).fill("synthetic-teaching-updated");
    await teaching.getByRole("button",{name:"保存",exact:true}).click();
    await teaching.getByText("已保存 · 新任务将使用此配置",{exact:true}).waitFor();
    await page.getByRole("button",{name:"个人设置菜单",exact:true}).click();
    await page.getByRole("button",{name:"人物蒸馏模型",exact:true}).click();
    await page.getByRole("button",{name:"测试人物蒸馏模型",exact:true}).click();
    await page.getByText("连接测试通过",{exact:true}).waitFor();
    await page.goto(served.origin+"/#/capabilities?section=research&purpose=research_search");
    await page.getByRole("region",{name:"人物蒸馏模型配置",exact:true}).waitFor();
    assert.equal(await page.getByRole("button",{name:"人物研究搜索",exact:true}).count(),0);
    assert.equal(await page.getByRole("region",{name:"人物研究搜索模型配置",exact:true}).count(),0);
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.screenshot({path:`${output}/research-search-${width}.png`});
    await page.reload();
    assert.equal(await page.getByRole("region",{name:"人物蒸馏模型配置",exact:true}).getByLabel("模型",{exact:true}).inputValue(),"synthetic-research");
    await page.getByRole("button",{name:"内容模型",exact:true}).click();
    assert.equal(await page.getByRole("region",{name:"课程规划模型配置",exact:true}).getByLabel("模型",{exact:true}).inputValue(),"synthetic-planning");
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.screenshot({path:`${output}/settings-${width}.png`});
    checks.push(`three-model-roles-save-refresh-${width}`);

    await page.goto(served.origin+"/#/cards");
    await page.getByRole("button",{name:"新人物",exact:true}).click();
    await page.getByRole("dialog",{name:"创建人物卡"}).getByRole("button",{name:"AI 创作",exact:true}).click();
    const composer=page.getByRole("textbox",{name:"继续与 AI 交流"});
    assert.equal(await page.locator(".persona-egress-consent").count(),0);
    assert.equal(await page.locator(".persona-composer .help").count(),0);
    const inputBounds=await composer.boundingBox();
    assert(inputBounds.height<=60,`compact input at ${width}px`);
    await composer.fill("我想创建一位数学导师，遇到困难先了解学生的思路。");
    await page.getByRole("button",{name:"发送消息",exact:true}).click();
    await page.getByText("已将你的补充沉淀进人物思维。",{exact:true}).waitFor();
    await composer.fill("请搜索启发式提问方法。");
    await page.getByRole("button",{name:"发送消息",exact:true}).click();
    await page.getByText("本轮完成 2 次搜索",{exact:true}).waitFor();
    assert.equal(dialogueRequests,2);
    assert.equal(builds[1].request.parent_build_id,builds[0].build_id);
    assert.equal(builds[1].card_id,builds[0].card_id);
    await page.screenshot({path:`${output}/ai-dialogue-${width}.png`});
    await page.reload();
    await page.getByRole("button",{name:"新人物",exact:true}).click();
    await page.getByRole("dialog",{name:"创建人物卡"}).getByRole("button",{name:"AI 创作",exact:true}).click();
    await page.getByText("本轮完成 2 次搜索",{exact:true}).waitFor();
    assert.equal(await page.getByRole("textbox",{name:"继续与 AI 交流"}).count(),1);
    checks.push(`dialogue-resume-after-refresh-${width}`);
    assert.equal(await page.getByRole("button",{name:"整理人物卡",exact:true}).count(),0);
    assert.equal(await page.getByRole("button",{name:"保存人物卡",exact:true}).count(),0);
    await page.getByRole("textbox",{name:"继续与 AI 交流"}).fill("就按原作学妹身份直接生成全部，不要改成职业老师。");
    await page.getByRole("button",{name:"发送消息",exact:true}).click();
    const confirmationText="是否确认创作此人物？确认后，我会开始人物蒸馏、声音和图片生成。";
    await page.getByText(new RegExp(confirmationText.replace(/[？。]/g,"."))).waitFor();
    assert.equal(card.persona,null);
    assert.equal(builds.at(-1).accepted_revision,null);
    assert.equal(await page.getByRole("button",{name:"确认创作此人物",exact:true}).count(),0);
    assert(await page.getByRole("textbox",{name:"继续与 AI 交流"}).isEnabled());
    await page.screenshot({path:`${output}/chat-confirmation-${width}.png`});
    await page.reload();
    await page.getByRole("button",{name:"新人物",exact:true}).click();
    await page.getByRole("dialog",{name:"创建人物卡"}).getByRole("button",{name:"AI 创作",exact:true}).click();
    await page.getByText(new RegExp(confirmationText.replace(/[？。]/g,"."))).waitFor();
    for(const message of ["嗯","确认，但把语气改得更温柔一点。"]){
      await page.getByRole("textbox",{name:"继续与 AI 交流"}).fill(message);
      const posted=page.waitForResponse(response=>response.url().endsWith("/api/character-builds")
        &&response.request().method()==="POST");
      await page.getByRole("button",{name:"发送消息",exact:true}).click();
      await posted;
      await page.waitForFunction(()=>!document.querySelector(".persona-composer textarea")?.disabled);
      assert.equal(builds.at(-1).result.dialogue.awaiting_confirmation,true);
      assert.equal(card.persona,null);
    }
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    checks.push(`chat-introduction-confirmation-refresh-ambiguity-revision-${width}`);
    await page.getByRole("textbox",{name:"继续与 AI 交流"}).fill("确认创作此人物。");
    await page.getByRole("button",{name:"发送消息",exact:true}).click();
    await page.getByLabel("完整人物 Skill",{exact:true}).waitFor();
    assert(card.persona.skill.includes("先检查条件"));
    assert.equal(card.profile.identity,"原作女性学妹");
    assert.equal(card.profile.media_ids.length,6);
    assert.equal(card.profile.voice_id,"Chinese_female");
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.screenshot({path:`${output}/persona-editor-${width}.png`});
    checks.push(`chat-confirmed-completion-without-save-prompt-${width}`);

    await page.goto(served.origin+"/#/cards");
    await page.getByRole("button",{name:"新人物",exact:true}).click();
    await page.getByRole("dialog",{name:"创建人物卡"}).getByRole("button",{name:"AI 创作",exact:true}).click();
    await page.locator('.persona-dialogue input[type="file"]').setInputFiles({name:"synthetic.docx",
      mimeType:"application/vnd.openxmlformats-officedocument.wordprocessingml.document",buffer:Buffer.from("synthetic-docx")});
    await page.getByText("synthetic.docx · 已读取",{exact:true}).waitFor();
    await page.getByRole("textbox",{name:"继续与 AI 交流"}).fill("请优先依据文档。");
    await page.getByRole("button",{name:"发送消息",exact:true}).click();
    await page.getByText("材料追问 1 / 8",{exact:true}).waitFor();
    assert.equal(materialUploads,1);
    assert(builds.at(-1).request.source_text.includes("合成人物语料"));
    checks.push(`docx-material-priority-and-question-limit-${width}`);

    await page.getByRole("button",{name:"返回人物列表",exact:true}).click();
    await page.getByRole("button",{name:"新人物",exact:true}).click();
    await page.getByRole("dialog",{name:"创建人物卡"}).getByRole("button",{name:"AI 创作",exact:true}).click();
    await page.locator('.persona-dialogue input[type="file"]').setInputFiles({name:"synthetic.txt",
      mimeType:"text/plain",buffer:Buffer.from("合成人物语料：文字资料优先提炼。")});
    await page.getByText("synthetic.txt · 已读取",{exact:true}).waitFor();
    await page.getByRole("textbox",{name:"继续与 AI 交流"}).fill("先依据这份文字提炼。");
    await page.getByRole("button",{name:"发送消息",exact:true}).click();
    await page.getByText("材料追问 1 / 8",{exact:true}).waitFor();
    assert.equal(materialUploads,2);
    checks.push(`txt-material-priority-and-question-limit-${width}`);

    holdNext=true;
    await page.getByRole("textbox",{name:"继续与 AI 交流"}).fill("继续整理人物");
    await page.getByRole("button",{name:"发送消息",exact:true}).click();
    const stop=page.getByRole("button",{name:"取消本轮",exact:true});
    await stop.waitFor();
    publish(builds.at(-1),{event:"preview",reply:"先检查人物的"});
    await page.getByText("先检查人物的",{exact:true}).waitFor();
    publish(builds.at(-1),{event:"preview",reply:"先检查人物的思维和表达。"});
    await page.getByText("先检查人物的思维和表达。",{exact:true}).waitFor();
    assert.equal(builds.at(-1).status,"running");
    assert.equal(await page.locator(".persona-streaming").count(),1);
    await page.reload();
    await page.getByRole("button",{name:"新人物",exact:true}).click();
    await page.getByRole("dialog",{name:"创建人物卡"}).getByRole("button",{name:"AI 创作",exact:true}).click();
    await page.getByText("先检查人物的思维和表达。",{exact:true}).waitFor();
    assert(await stop.isEnabled());
    assert.equal(await stop.locator("svg.lucide-square").count(),1);
    assert.equal(await page.locator(".persona-generation").count(),0);
    assert.equal(await page.getByText(/正在理解人物|正在整理人物/).count(),0);
    assert(await page.getByRole("textbox",{name:"继续与 AI 交流"}).isDisabled());
    await stop.scrollIntoViewIfNeeded();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.screenshot({path:`${output}/compact-stopping-${width}.png`});
    await stop.click();
    await page.getByRole("alert").waitFor();
    assert(await stop.isEnabled());
    await stop.click();
    await page.getByRole("button",{name:"发送消息",exact:true}).waitFor();
    assert(await page.getByRole("textbox",{name:"继续与 AI 交流"}).isEnabled());
    assert.equal(cancelRequests,2);
    checks.push(`compact-composer-stop-failure-retry-${width}`);
    checks.push(`actual-sse-incremental-display-refresh-and-stop-${width}`);

    await page.getByRole("button",{name:"返回人物列表",exact:true}).click();
    await page.getByRole("button",{name:"新人物",exact:true}).click();
    await page.getByRole("dialog",{name:"创建人物卡"}).getByRole("button",{name:"AI 创作",exact:true}).click();
    holdNext=true;
    await page.getByRole("textbox",{name:"继续与 AI 交流"}).fill("合成流式完成检查");
    await page.getByRole("button",{name:"发送消息",exact:true}).click();
    await page.getByRole("button",{name:"取消本轮",exact:true}).waitFor();
    const finishing=builds.at(-1),requestsBefore=streamRequests;
    publish(finishing,{event:"preview",reply:"临时回复"});
    await page.getByText("临时回复",{exact:true}).waitFor();
    publish(finishing,{event:"preview",reply:""});
    await page.getByText("临时回复",{exact:true}).waitFor({state:"hidden"});
    publish(finishing,{event:"preview",reply:"重新生成的回复"});
    await page.getByText("重新生成的回复",{exact:true}).waitFor();
    const reconnected=page.waitForResponse(response=>
      response.url()===`${streamOrigin}/api/character-builds/${finishing.build_id}/content-events`);
    for(const client of streamConnections.get(finishing.build_id)||[])client.end();
    await reconnected;
    assert(streamRequests>requestsBefore);
    await page.getByText("重新生成的回复",{exact:true}).waitFor();
    finishing.status="succeeded";
    finishing.result={dialogue:{reply:"校验后的完整回复。",question:"",question_count:0,profile}};
    publish(finishing,{event:"done",status:"succeeded"});
    await page.getByText("校验后的完整回复。",{exact:true}).waitFor();
    assert.equal(await page.getByText("校验后的完整回复。",{exact:true}).count(),1);
    assert.equal(await page.locator(".persona-streaming").count(),0);
    checks.push(`sse-reset-reconnect-final-replacement-${width}`);

    await page.getByRole("textbox",{name:"继续与 AI 交流"}).fill("这次按敲定的学妹形象直接生成全部。");
    await page.getByRole("button",{name:"发送消息",exact:true}).click();
    await page.getByText(new RegExp(confirmationText.replace(/[？。]/g,"."))).waitFor();
    holdNext=true;
    await page.getByRole("textbox",{name:"继续与 AI 交流"}).fill("确认创作此人物。");
    await page.getByRole("button",{name:"发送消息",exact:true}).click();
    await page.getByRole("button",{name:"取消本轮",exact:true}).waitFor();
    const delivering=builds.at(-1);
    delivering.result={dialogue:{reply:"已确认创作合成导师，开始完成人物。",question:"",question_count:0,
      ready:true,awaiting_confirmation:false,creation_confirmed:true,profile},
      persona:card.persona,completion:{status:"running",completed_images:2,total_images:5,voice_ready:true}};
    await page.getByText("正在完成声音与形象 · 2 / 5",{exact:true}).waitFor();
    assert.equal(await page.getByRole("button",{name:"保存人物卡",exact:true}).count(),0);
    assert.equal(await page.getByRole("button",{name:"整理人物卡",exact:true}).count(),0);
    await page.reload();
    await page.getByRole("button",{name:"新人物",exact:true}).click();
    await page.getByRole("dialog",{name:"创建人物卡"}).getByRole("button",{name:"AI 创作",exact:true}).click();
    await page.getByText("正在完成声音与形象 · 2 / 5",{exact:true}).waitFor();
    await page.screenshot({path:`${output}/automatic-progress-${width}.png`});
    card={...card,revision:card.revision+1};
    delivering.accepted_revision=card.revision;
    delivering.result.completion.status="complete";
    delivering.result.completion.completed_images=5;
    delivering.status="succeeded";
    publish(delivering,{event:"done",status:"succeeded"});
    await page.getByLabel("完整人物 Skill",{exact:true}).waitFor();
    assert.equal(await page.getByRole("heading",{name:"AI 创作",exact:true}).count(),0);
    checks.push(`automatic-delivery-resume-and-open-card-${width}`);

    await page.getByRole("button",{name:"返回人物列表",exact:true}).click();
    await page.getByRole("button",{name:"新人物",exact:true}).click();
    await page.getByRole("dialog",{name:"创建人物卡"}).getByRole("button",{name:"自定义",exact:true}).click();
    await page.getByLabel("名字",{exact:true}).fill("自定义导师");
    await page.getByLabel("完整人物 Skill",{exact:true}).fill("保留作者自己的完整设定。");
    await page.getByRole("button",{name:"添加能力 Skill",exact:true}).click();
    await page.getByLabel("能力指令",{exact:true}).fill("先依据条件逐步解释。");
    checks.push(`custom-retains-full-original-editor-${width}`);
    card={...card,editable:false,system_default:true,published_revision:card.revision};
    preference={version:1,card_id:card.card_id,revision:card.revision,using_system_default:true};
    await page.goto(served.origin+"/#/cards");
    await page.reload();
    await page.getByRole("button",{name:"正在使用",exact:true}).waitFor();
    assert.equal(await page.getByRole("button",{name:"系统默认导师",exact:true}).count(),0);
    assert.equal(await page.getByRole("button",{name:"使用课堂默认",exact:true}).count(),0);
    assert(await page.getByRole("button",{name:"正在使用",exact:true}).isDisabled());
    preference={version:2,card_id:"synthetic-private-choice",revision:1};
    await page.reload();
    await page.getByRole("button",{name:"使用导师",exact:true}).click();
    await page.getByRole("button",{name:"正在使用",exact:true}).waitFor();
    assert.equal(preference.card_id,card.card_id);
    await page.screenshot({path:`${output}/system-default-${width}.png`});
    await page.goto(served.origin+"/#/card-market");
    await page.getByRole("button",{name:"系统默认导师",exact:true}).waitFor();
    assert(await page.getByRole("button",{name:"系统默认导师",exact:true}).isDisabled());
    checks.push(`system-default-visible-explicit-selection-${width}`);
    assert.deepEqual(errors,[]);
    await context.close();
  }
}finally{
  await browser.close();
  streamServer.closeAllConnections();
  await new Promise(resolve=>streamServer.close(resolve));
}
console.log(JSON.stringify({checks,synthetic:true,realProviderTested:false}));
